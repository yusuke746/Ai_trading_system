"""
core/mt5_client.py — MetaTrader 5 クライアント

VPS上のMT5は頻繁に切断されるため、堅牢な再接続ロジックを実装。
全MT5操作を asyncio.Lock で直列化し、並行処理の競合を防止する。
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd

from config import CONFIG
from core.broker_time import BrokerTime
from core.models import OrderResult, PositionInfo, Direction

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS: int = 5
RECONNECT_BACKOFF_SEC: list[int] = [2, 5, 10, 30, 60]

_mt5_lock = asyncio.Lock()


class MT5Client:
    """MetaTrader5操作を安全にラップするクライアント"""

    def __init__(self):
        self._connected: bool = False
        self._notifier = None  # 後から注入
        self.magic_number: int = 20250101
        self.deviation: int = 10  # スリッページ上限

    def set_notifier(self, notifier):
        """notifierの循環import回避用"""
        self._notifier = notifier

    async def _notify(self, msg: str, level: str = "WARNING"):
        if self._notifier:
            await self._notifier.send(msg, level=level)

    # ──────────── 接続管理 ────────────

    def connect(self) -> bool:
        """MT5に接続（起動時・再接続共通）"""
        if not mt5.initialize(
            login=CONFIG.MT5_LOGIN,
            server=CONFIG.MT5_SERVER,
            password=CONFIG.MT5_PASSWORD,
        ):
            error = mt5.last_error()
            logger.error(f"MT5初期化失敗: {error}")
            return False

        account_info = mt5.account_info()
        if account_info is None:
            logger.error("MT5アカウント情報取得失敗")
            return False

        self._connected = True
        mode = "DEMO" if account_info.trade_mode == 0 else "LIVE"
        logger.info(
            f"MT5接続成功: {mode} | 口座#{account_info.login} | "
            f"残高: {account_info.balance:.0f} {account_info.currency}"
        )
        return True

    async def ensure_connection(self) -> bool:
        """全MT5操作の前に呼び出す。切断時は自動再接続。"""
        if mt5.terminal_info() is not None:
            return True

        for attempt, wait in enumerate(RECONNECT_BACKOFF_SEC):
            logger.warning(
                f"MT5切断検知。再接続試行 {attempt + 1}/{MAX_RECONNECT_ATTEMPTS}"
            )
            if self.connect():
                await self._notify("MT5再接続成功", level="INFO")
                return True
            await asyncio.sleep(wait)

        await self._notify("MT5再接続失敗（5回試行）", level="CRITICAL")
        self._connected = False
        return False

    def shutdown(self):
        """グレースフルシャットダウン"""
        mt5.shutdown()
        self._connected = False
        logger.info("MT5切断完了")

    # ──────────── 注文操作 ────────────

    async def open_position(
        self,
        symbol: str,
        direction: Direction,
        lot: float,
        sl_price: float,
        tp_price: float,
        comment: str = "",
    ) -> Optional[OrderResult]:
        """成行注文を送信する"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return OrderResult(
                    success=False, error="MT5接続不可", ticket=0
                )

            # スプレッドチェック
            spread_ok, spread_points = self.check_spread(symbol)
            if not spread_ok:
                msg = f"スプレッド異常拒否: {symbol} {spread_points:.1f}pts"
                logger.warning(msg)
                return OrderResult(success=False, error=msg, ticket=0)

            order_type = (
                mt5.ORDER_TYPE_BUY
                if direction == Direction.LONG
                else mt5.ORDER_TYPE_SELL
            )

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return OrderResult(
                    success=False, error=f"ティック取得失敗: {symbol}", ticket=0
                )

            requested_price = tick.ask if direction == Direction.LONG else tick.bid

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lot,
                "type": order_type,
                "price": requested_price,
                "sl": sl_price,
                "tp": tp_price,
                "deviation": self.deviation,
                "magic": self.magic_number,
                "comment": comment[:31],  # MT5コメント上限
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result is None:
                error = mt5.last_error()
                return OrderResult(
                    success=False, error=f"order_send失敗: {error}", ticket=0
                )

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                error_msg = f"注文拒否: retcode={result.retcode} comment={result.comment}"
                logger.error(error_msg)
                await self._notify(f"⚠️ 注文拒否: {symbol} {error_msg}", level="WARNING")
                return OrderResult(
                    success=False, error=error_msg, ticket=0
                )

            # スリッページ計算
            filled_price = result.price
            slippage = abs(filled_price - requested_price)

            logger.info(
                f"注文成功: {symbol} {direction.value} {lot}lot "
                f"ticket={result.order} price={filled_price} "
                f"slippage={slippage:.5f}"
            )

            return OrderResult(
                success=True,
                ticket=result.order,
                price=filled_price,
                lot=lot,
                slippage_points=round(
                    slippage / (mt5.symbol_info(symbol).point or 0.00001), 1
                ),
                spread_at_entry=spread_points,
            )

    async def modify_position(
        self, ticket: int, new_sl: Optional[float] = None, new_tp: Optional[float] = None
    ) -> bool:
        """ポジションのSL/TPを変更"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return False

            position = mt5.positions_get(ticket=ticket)
            if not position:
                logger.error(f"ポジション未発見: {ticket}")
                return False

            pos = position[0]
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": pos.symbol,
                "position": ticket,
                "sl": new_sl if new_sl is not None else pos.sl,
                "tp": new_tp if new_tp is not None else pos.tp,
            }

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = result.comment if result else mt5.last_error()
                logger.error(f"ポジション修正失敗: ticket={ticket} error={error}")
                return False

            logger.info(f"ポジション修正成功: ticket={ticket} SL={request['sl']} TP={request['tp']}")
            return True

    async def close_position(
        self, ticket: int, percentage: int = 100
    ) -> Optional[OrderResult]:
        """ポジションを決済（部分決済対応）"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return OrderResult(success=False, error="MT5接続不可", ticket=ticket)

            position = mt5.positions_get(ticket=ticket)
            if not position:
                logger.error(f"決済対象ポジション未発見: {ticket}")
                return OrderResult(
                    success=False, error=f"ポジション未発見: {ticket}", ticket=ticket
                )

            pos = position[0]
            close_volume = pos.volume

            if percentage < 100:
                close_volume = round(pos.volume * percentage / 100, 2)
                # volume_min チェック
                info = mt5.symbol_info(pos.symbol)
                if info and close_volume < info.volume_min:
                    logger.warning(
                        f"部分決済量 {close_volume} < volume_min {info.volume_min} → 全決済にフォールバック"
                    )
                    close_volume = pos.volume  # FULL_CLOSE

            order_type = (
                mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            )

            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                return OrderResult(
                    success=False, error=f"ティック取得失敗: {pos.symbol}", ticket=ticket
                )

            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": close_volume,
                "type": order_type,
                "position": ticket,
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic_number,
                "comment": f"close_{ticket}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = result.comment if result else str(mt5.last_error())
                logger.error(f"決済失敗: ticket={ticket} error={error}")
                return OrderResult(success=False, error=error, ticket=ticket)

            logger.info(f"決済成功: ticket={ticket} volume={close_volume} price={result.price}")
            return OrderResult(
                success=True, ticket=ticket, price=result.price, lot=close_volume
            )

    async def close_all_positions(self) -> list[OrderResult]:
        """全ポジションを成行決済（CB発動・週末前）"""
        results = []
        positions = await self.get_all_positions()

        for pos in positions:
            try:
                result = await self.close_position(pos.ticket)
                if result:
                    results.append(result)
            except Exception as e:
                logger.exception(f"全決済中にエラー: ticket={pos.ticket}")
                results.append(
                    OrderResult(success=False, error=str(e), ticket=pos.ticket)
                )

        return results

    # ──────────── 情報取得 ────────────

    async def get_all_positions(self) -> list[PositionInfo]:
        """全アクティブポジションを取得"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return []

            positions = mt5.positions_get()
            if positions is None:
                return []

            return [
                PositionInfo(
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    direction=Direction.LONG if pos.type == 0 else Direction.SHORT,
                    volume=pos.volume,
                    open_price=pos.price_open,
                    current_price=pos.price_current,
                    sl=pos.sl,
                    tp=pos.tp,
                    profit=pos.profit,
                    swap=pos.swap,
                    open_time=datetime.fromtimestamp(pos.time),
                    magic=pos.magic,
                    comment=pos.comment,
                )
                for pos in positions
            ]

    async def get_position_by_ticket(self, ticket: int) -> Optional[PositionInfo]:
        """個別ポジション取得"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return None

            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return None

            pos = positions[0]
            return PositionInfo(
                ticket=pos.ticket,
                symbol=pos.symbol,
                direction=Direction.LONG if pos.type == 0 else Direction.SHORT,
                volume=pos.volume,
                open_price=pos.price_open,
                current_price=pos.price_current,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
                swap=pos.swap,
                open_time=datetime.fromtimestamp(pos.time),
                magic=pos.magic,
                comment=pos.comment,
            )

    async def get_account_balance(self) -> float:
        """口座残高（JPY）"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return 0.0

            info = mt5.account_info()
            return info.balance if info else 0.0

    async def get_daily_pnl(self) -> float:
        """
        今日の日次損益合計（JPY）。
        daily_pnl = 実現損益（今日決済分） + 含み損益（保有中ポジション）
        """
        async with _mt5_lock:
            if not await self.ensure_connection():
                return 0.0

            # ─── 実現損益（今日XMT 00:00以降の決済分） ───
            today_start = BrokerTime.today_start()
            today_start_utc = BrokerTime.to_utc(today_start)

            # datetime to timestamp for MT5 API
            from_ts = today_start_utc.replace(tzinfo=None)
            to_ts = datetime.utcnow() + timedelta(hours=1)

            deals = mt5.history_deals_get(from_ts, to_ts)

            realized_pnl = 0.0
            if deals:
                for deal in deals:
                    # DEAL_ENTRY_OUT (1) or DEAL_ENTRY_INOUT (2) = 決済取引
                    if deal.entry in (1, 2):
                        realized_pnl += deal.profit + deal.swap + deal.commission

            # ─── 含み損益（保有中ポジション） ───
            positions = mt5.positions_get()
            floating_pnl = 0.0
            if positions:
                for pos in positions:
                    floating_pnl += pos.profit + pos.swap

            return realized_pnl + floating_pnl

    async def get_spread(self, symbol: str) -> float:
        """現在スプレッド（points単位）"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return 9999.0

            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            if tick is None or info is None:
                return 9999.0

            spread_price = tick.ask - tick.bid
            return spread_price / info.point if info.point > 0 else 9999.0

    async def get_ohlcv(
        self, symbol: str, timeframe: int, count: int = 100
    ) -> Optional[pd.DataFrame]:
        """ローソク足取得"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return None

            rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
            if rates is None or len(rates) == 0:
                return None

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df

    # ──────────── ユーティリティ ────────────

    def check_spread(self, symbol: str) -> tuple[bool, float]:
        """
        スプレッドが許容範囲内か判定。
        tick.ask - tick.bid を symbol_info.point で割って正規化。
        """
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            logger.error(f"スプレッド取得失敗: {symbol}")
            return (False, 0.0)

        spread_price = tick.ask - tick.bid
        spread_points = spread_price / info.point if info.point > 0 else 9999.0

        limit = CONFIG.SPREAD_LIMITS_POINTS.get(symbol, 50)
        return (spread_points <= limit, spread_points)
