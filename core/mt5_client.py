"""
core/mt5_client.py — MetaTrader 5 クライアント

VPS上のMT5は頻繁に切断されるため、堅牢な再接続ロジックを実装。
全MT5操作を asyncio.Lock で直列化し、並行処理の競合を防止する。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd

from config import CONFIG
from core.broker_time import BrokerTime
from core.models import OrderResult, PositionInfo, Direction
from core.spread_tracker import SpreadTracker

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
        self.spread_tracker: SpreadTracker = SpreadTracker()

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

        NOTE:
        - 本メソッドは「実現損益 + 含み損益」を返す。
        - 実現損益は以下の2段フォールバックで算出し、
          MT5時刻解釈差による 0 固定化を回避する。
          1) サーバー時間窓（XMT naivetime）で直接取得
          2) 広い期間取得 + deal.time(epoch) をXMT日付で手動フィルタ
        """
        async with _mt5_lock:
            if not await self.ensure_connection():
                return 0.0

            realized_pnl, floating_pnl, _ = self._calc_daily_pnl_components_locked()
            return realized_pnl + floating_pnl

    async def get_daily_pnl_breakdown(self) -> dict:
        """日次損益の内訳を返す（実現/含み/合計）。"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return {
                    "realized_pnl": 0.0,
                    "floating_pnl": 0.0,
                    "total_pnl": 0.0,
                    "matched_deals": 0,
                }

            realized_pnl, floating_pnl, matched_deals = self._calc_daily_pnl_components_locked()
            return {
                "realized_pnl": realized_pnl,
                "floating_pnl": floating_pnl,
                "total_pnl": realized_pnl + floating_pnl,
                "matched_deals": matched_deals,
            }

    def _calc_daily_pnl_components_locked(self) -> tuple[float, float, int]:
        """日次PnL内訳を算出（呼び出し元でMT5 lock保持前提）。"""

        # ─── 実現損益（今日XMT 00:00以降の決済分） ───
        now_xmt = BrokerTime.now()
        today_start = BrokerTime.today_start()

        out_by = getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)
        closing_entries = (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, out_by)
        trade_types = (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)

        # XMT当日のUnix境界（UTC/XMT変換を都度行わず直接比較）
        day_start_ts = int(today_start.timestamp())
        day_end_ts = int((now_xmt + timedelta(minutes=5)).timestamp())

        # 1) まずはXMT窓で直接取得（通常はこちらで正しい）
        realized_primary = 0.0
        primary_from = today_start.replace(tzinfo=None)
        primary_to = (now_xmt + timedelta(minutes=5)).replace(tzinfo=None)
        primary_deals = mt5.history_deals_get(primary_from, primary_to)
        if primary_deals:
            for deal in primary_deals:
                if deal.entry in closing_entries and deal.type in trade_types:
                    realized_primary += deal.profit + deal.swap + deal.commission

        # 2) 標準経路: 広窓取得 + epoch時刻でXMT当日フィルタ
        # deal.time(epoch)基準のため、MT5側datetime解釈差の影響を受けにくい。
        realized_fallback = 0.0
        # 取得窓は広めに確保（時刻解釈がズレても取りこぼしにくくする）
        from_utc = (now_xmt - timedelta(days=7)).astimezone(timezone.utc)
        to_utc = (now_xmt + timedelta(days=1)).astimezone(timezone.utc)
        fallback_deals = mt5.history_deals_get(
            from_utc.replace(tzinfo=None),
            to_utc.replace(tzinfo=None),
        )
        matched_deals = 0
        if fallback_deals:
            for deal in fallback_deals:
                if deal.entry not in closing_entries:
                    continue
                if deal.type not in trade_types:
                    continue
                if day_start_ts <= int(deal.time) <= day_end_ts:
                    realized_fallback += deal.profit + deal.swap + deal.commission
                    matched_deals += 1

        # 既定はepochフィルタ（fallback）を採用。
        # fallbackが取得不能(None)のときのみprimaryにフォールバックする。
        realized_pnl = realized_fallback if fallback_deals is not None else realized_primary

        # ─── 含み損益（保有中ポジション） ───
        positions = mt5.positions_get()
        floating_pnl = 0.0
        if positions:
            for pos in positions:
                floating_pnl += pos.profit + pos.swap

        return realized_pnl, floating_pnl, matched_deals

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

    async def get_mtf_summary(self, symbol: str) -> dict:
        """H4・日足のマルチタイムフレームサマリーを取得

        AIエントリー判断に必要な上位足コンテキストを提供する。
        取得失敗時は空dictを返す（呼び出し元でエラーにしない）。
        """
        result = {}
        try:
            # H4: 直近6本（24時間分）
            h4_df = await self.get_ohlcv(symbol, mt5.TIMEFRAME_H4, 6)
            if h4_df is not None and len(h4_df) >= 3:
                latest_h4 = h4_df.iloc[-1]
                prev_h4 = h4_df.iloc[-2]
                h4_highs = h4_df["high"].tolist()
                h4_lows = h4_df["low"].tolist()
                h4_closes = h4_df["close"].tolist()
                result["h4"] = {
                    "current": {
                        "open": round(float(latest_h4["open"]), 5),
                        "high": round(float(latest_h4["high"]), 5),
                        "low": round(float(latest_h4["low"]), 5),
                        "close": round(float(latest_h4["close"]), 5),
                    },
                    "prev_close": round(float(prev_h4["close"]), 5),
                    "trend": "BULLISH" if h4_closes[-1] > h4_closes[-3] else "BEARISH",
                    "range_high": round(float(max(h4_highs)), 5),
                    "range_low": round(float(min(h4_lows)), 5),
                }

            # D1: 直近5本（1週間分）
            d1_df = await self.get_ohlcv(symbol, mt5.TIMEFRAME_D1, 5)
            if d1_df is not None and len(d1_df) >= 3:
                latest_d1 = d1_df.iloc[-1]
                prev_d1 = d1_df.iloc[-2]
                d1_highs = d1_df["high"].tolist()
                d1_lows = d1_df["low"].tolist()
                d1_closes = d1_df["close"].tolist()
                result["d1"] = {
                    "current": {
                        "open": round(float(latest_d1["open"]), 5),
                        "high": round(float(latest_d1["high"]), 5),
                        "low": round(float(latest_d1["low"]), 5),
                        "close": round(float(latest_d1["close"]), 5),
                    },
                    "prev_close": round(float(prev_d1["close"]), 5),
                    "trend": "BULLISH" if d1_closes[-1] > d1_closes[-3] else "BEARISH",
                    "week_high": round(float(max(d1_highs)), 5),
                    "week_low": round(float(min(d1_lows)), 5),
                }

        except Exception as e:
            logger.warning(f"MTFサマリー取得失敗 ({symbol}): {e}")

        return result

    # ──────────── ユーティリティ ────────────

    def check_spread(self, symbol: str) -> tuple[bool, float]:
        """
        スプレッドが許容範囲内か判定。
        tick.ask - tick.bid を symbol_info.point で割って正規化。
        SpreadTrackerの適応型上限を使用（ウォームアップ中はconfig固定値）。
        """
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            logger.error(f"スプレッド取得失敗: {symbol}")
            return (False, 0.0)

        spread_price = tick.ask - tick.bid
        spread_points = spread_price / info.point if info.point > 0 else 9999.0

        limit = self.spread_tracker.get_limit(symbol)
        return (spread_points <= limit, spread_points)

    async def sample_spreads(self) -> None:
        """全銘柄のスプレッドを記録（60秒ごとにスケジューラから呼ばれる）"""
        async with _mt5_lock:
            if not await self.ensure_connection():
                return
            for symbol in CONFIG.SYMBOLS:
                tick = mt5.symbol_info_tick(symbol)
                info = mt5.symbol_info(symbol)
                if tick and info and info.point > 0:
                    spread_pts = (tick.ask - tick.bid) / info.point
                    self.spread_tracker.record(symbol, spread_pts)
