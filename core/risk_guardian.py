"""
core/risk_guardian.py — リスク管理エンジン

システム全体のリスクを30秒ごとに監視し、破綻を防止する。
全ガードチェックをcan_enter_new_trade()に集約。
"""

import asyncio
import logging
from typing import Optional

from config import CONFIG
from core.broker_time import BrokerTime
from core.models import Direction, SystemStatus

logger = logging.getLogger(__name__)

# 相関グループ定義
CORRELATION_GROUPS = {
    "USD_LONG": ["USDJPY_LONG", "EURUSD_SHORT", "GOLD_SHORT"],
    "USD_SHORT": ["USDJPY_SHORT", "EURUSD_LONG", "GOLD_LONG"],
    "RISK_ON": ["USDJPY_LONG", "EURUSD_LONG"],
    "RISK_OFF": ["USDJPY_SHORT", "GOLD_LONG"],
}


class RiskGuardian:
    """システムリスク監視エンジン"""

    def __init__(self):
        self.status: SystemStatus = SystemStatus.ACTIVE
        self._mt5_client = None
        self._thesis_db = None
        self._notifier = None
        self._lock_reason: Optional[str] = None

    def set_dependencies(self, mt5_client, thesis_db, notifier):
        self._mt5_client = mt5_client
        self._thesis_db = thesis_db
        self._notifier = notifier

    async def _notify(self, msg: str, level: str = "WARNING"):
        if self._notifier:
            await self._notifier.send(msg, level=level)

    # ──────────── メインガードチェック ────────────

    async def check_all_guards(self):
        """30秒ごとに全ガードチェックを実行"""
        if self.status == SystemStatus.LOCKED:
            return  # LOCKED中は何もしない

        try:
            # 週末チェック
            if BrokerTime.is_weekend():
                if self.status != SystemStatus.WEEKEND_CLOSED:
                    self.status = SystemStatus.WEEKEND_CLOSED
                    logger.info("ステータス変更: WEEKEND_CLOSED")
                return

            # 金曜カットオフ
            if BrokerTime.is_friday_cutoff():
                if self.status != SystemStatus.FRIDAY_CUTOFF:
                    self.status = SystemStatus.FRIDAY_CUTOFF
                    await self._notify("🔸 金曜カットオフ: 新規エントリー停止", level="INFO")
                    logger.info("ステータス変更: FRIDAY_CUTOFF")
                return

            # 日次DDチェック
            await self._check_daily_drawdown()

            # ポジション数チェック
            await self._check_position_count()

            # 通常状態に戻す（上記でMONITOR_ONLYにされなかった場合）
            if self.status in (SystemStatus.WEEKEND_CLOSED, SystemStatus.FRIDAY_CUTOFF):
                self.status = SystemStatus.ACTIVE
                logger.info("ステータス変更: ACTIVE に復帰")

        except Exception as e:
            logger.exception(f"ガードチェックエラー: {e}")

    async def _check_daily_drawdown(self):
        """日次ドローダウンチェック"""
        daily_pnl = await self._mt5_client.get_daily_pnl()
        balance = await self._mt5_client.get_account_balance()

        if balance <= 0:
            return

        dd_pct = abs(min(0, daily_pnl)) / balance * 100

        if dd_pct >= CONFIG.DAILY_MAX_DD_PCT:
            await self._trigger_circuit_breaker(
                f"DD {dd_pct:.1f}% >= {CONFIG.DAILY_MAX_DD_PCT}%",
                daily_pnl,
                dd_pct,
            )

    async def _check_position_count(self):
        """ポジション数上限チェック"""
        positions = await self._mt5_client.get_all_positions()
        if len(positions) >= CONFIG.MAX_POSITIONS:
            if self.status == SystemStatus.ACTIVE:
                self.status = SystemStatus.MONITOR_ONLY
                await self._notify(
                    f"🔸 MONITOR_ONLY: ポジション数 {len(positions)} >= {CONFIG.MAX_POSITIONS}",
                    level="WARNING",
                )

    # ──────────── エントリー事前チェック ────────────

    async def can_enter_new_trade(
        self, symbol: str, direction: str
    ) -> tuple[bool, str]:
        """
        エントリー前の全ガードチェック。
        Falseの場合は理由文字列を含む。
        """
        # ステータスチェック
        if self.status == SystemStatus.LOCKED:
            return False, f"LOCKED: {self._lock_reason or 'CB発動'}"
        if self.status == SystemStatus.MONITOR_ONLY:
            return False, "MONITOR_ONLY: 新規エントリー停止中"
        if self.status == SystemStatus.FRIDAY_CUTOFF:
            return False, "FRIDAY_CUTOFF: 金曜エントリー停止"
        if self.status == SystemStatus.WEEKEND_CLOSED:
            return False, "WEEKEND_CLOSED: 市場クローズ中"

        # DEAD_ZONEブロック
        if BrokerTime.is_dead_zone():
            return False, "DEAD_ZONE: XMT 22:00-23:59 はエントリー禁止"

        # 祝日チェック
        if BrokerTime.is_holiday():
            return False, "HOLIDAY: 祝日のためエントリー禁止"

        # 市場オープンチェック
        if not BrokerTime.is_market_open(symbol):
            return False, f"MARKET_CLOSED: {symbol} は現在取引時間外"

        # 日次クローズ近接
        if BrokerTime.is_near_daily_close(symbol):
            return False, f"NEAR_CLOSE: {symbol} は日次クローズ直前"

        # ポジション数
        positions = await self._mt5_client.get_all_positions()
        if len(positions) >= CONFIG.MAX_POSITIONS:
            return False, f"MAX_POSITIONS: ポジション数 {len(positions)} >= {CONFIG.MAX_POSITIONS}"

        # 総エクスポージャーチェック
        total_risk_pct = await self._calculate_total_exposure(positions)
        if total_risk_pct > CONFIG.MAX_TOTAL_EXPOSURE_PCT:
            return False, f"EXPOSURE: 総リスク {total_risk_pct:.1f}% > {CONFIG.MAX_TOTAL_EXPOSURE_PCT}%"

        # 通貨集中チェック
        usd_count = sum(1 for p in positions if "USD" in p.symbol)
        jpy_count = sum(1 for p in positions if "JPY" in p.symbol)

        if "USD" in symbol and usd_count >= 3:
            return False, f"USD_CONCENTRATION: USD絡み {usd_count} >= 3"
        if "JPY" in symbol and jpy_count >= 2:
            return False, f"JPY_CONCENTRATION: JPY絡み {jpy_count} >= 2"

        # スプレッドチェック
        spread_ok, spread_points = self._mt5_client.check_spread(symbol)
        if not spread_ok:
            return False, f"SPREAD: {symbol} スプレッド {spread_points:.1f}pts が上限超過"

        return True, "OK"

    async def _calculate_total_exposure(self, positions: list) -> float:
        """総エクスポージャー（%）を算出"""
        if not positions:
            return 0.0

        balance = await self._mt5_client.get_account_balance()
        if balance <= 0:
            return 0.0

        # 簡易計算: 各ポジションのpotential lossの合計 / balance
        total_risk = 0.0
        for pos in positions:
            if pos.sl and pos.sl > 0:
                risk_price = abs(pos.open_price - pos.sl) * pos.volume
                # 大雑把なJPY換算（正確さより安全側に）
                if "JPY" in pos.symbol:
                    total_risk += risk_price * 100  # pips × lot × 100
                else:
                    total_risk += risk_price * 100000 * 150  # 仮のJPYレート

        return (total_risk / balance) * 100

    # ──────────── 相関アラート ────────────

    def check_correlation_alert(
        self,
        new_symbol: str,
        new_direction: str,
        active_positions: list[dict],
    ) -> dict:
        """新規エントリーが既存ポジションと同一方向リスクに集中していないか検知"""
        new_key = f"{new_symbol}_{new_direction}"
        overlap_count = 0
        overlapping_groups = []

        for group_name, members in CORRELATION_GROUPS.items():
            if new_key not in members:
                continue
            existing_in_group = sum(
                1
                for pos in active_positions
                if f"{pos['symbol']}_{pos['direction']}" in members
            )
            if existing_in_group > 0:
                overlap_count += existing_in_group
                overlapping_groups.append(group_name)

        if overlap_count == 0:
            return {
                "has_alert": False,
                "alert_level": "NONE",
                "overlapping_group": None,
                "recommended_risk_multiplier": 1.0,
                "message": "相関リスクなし",
            }
        elif overlap_count == 1:
            return {
                "has_alert": True,
                "alert_level": "WARNING",
                "overlapping_group": overlapping_groups[0],
                "recommended_risk_multiplier": 0.75,
                "message": f"相関警告: {overlapping_groups[0]}方向に集中(既存1ポジ)",
            }
        else:
            return {
                "has_alert": True,
                "alert_level": "HIGH",
                "overlapping_group": ",".join(overlapping_groups),
                "recommended_risk_multiplier": 0.5,
                "message": f"高相関警告: {overlap_count}ポジが同方向リスク",
            }

    # ──────────── サーキットブレーカー ────────────

    async def _trigger_circuit_breaker(
        self, reason: str, daily_pnl: float, dd_pct: float
    ):
        """CB発動: 全決済 + LOCKED"""
        logger.critical(f"🔴 サーキットブレーカー発動: {reason}")
        self.status = SystemStatus.LOCKED
        self._lock_reason = reason

        # 1. 全決済（失敗してもログして継続）
        try:
            results = await self._mt5_client.close_all_positions()
            success = sum(1 for r in results if r.success)
            fail = sum(1 for r in results if not r.success)
            logger.info(f"CB全決済: 成功={success} 失敗={fail}")
        except Exception as e:
            logger.exception(f"CB全決済中にエラー: {e}")

        # 2. DB記録
        try:
            await self._thesis_db.save_cb_log(reason, daily_pnl, dd_pct)
        except Exception as e:
            logger.error(f"CB記録失敗: {e}")

        # 3. Discord緊急通知
        await self._notify(
            f"🔴 サーキットブレーカー発動\n"
            f"理由: {reason}\n"
            f"日次損益: {daily_pnl:,.0f}円\n"
            f"DD: {dd_pct:.1f}%\n"
            f"全ポジション決済試行完了\n"
            f"⚠️ 手動解除が必要です",
            level="CRITICAL",
        )

    async def unlock(self) -> str:
        """手動解除（STATUS APIから呼ばれる）"""
        if self.status != SystemStatus.LOCKED:
            return f"現在のステータスは {self.status.value} です（LOCKEDではありません）"

        self.status = SystemStatus.ACTIVE
        self._lock_reason = None
        await self._notify("🟢 手動解除: LOCKED → ACTIVE", level="INFO")
        return "UNLOCKED"

    # ──────────── 週末・金曜管理 ────────────

    async def check_friday_cutoff(self):
        """金曜カットオフ（XMT金曜 20:00）"""
        self.status = SystemStatus.FRIDAY_CUTOFF
        await self._notify(
            "🔸 金曜カットオフ発動: 新規エントリー停止\n"
            "既存ポジション管理のみ継続",
            level="INFO",
        )

    async def check_weekly_open(self):
        """週明けオープン（XMT日曜 23:20）"""
        self.status = SystemStatus.ACTIVE
        balance = await self._mt5_client.get_account_balance()
        await self._notify(
            f"🟢 市場オープン: ACTIVE\n"
            f"口座残高: {balance:,.0f}円",
            level="INFO",
        )

    def get_status_report(self) -> dict:
        """ステータスAPIのレスポンス"""
        return {
            "status": self.status.value,
            "lock_reason": self._lock_reason,
            "broker_time": BrokerTime.now_str(),
            "session": BrokerTime.get_session(),
            "is_dst": BrokerTime.is_dst(),
            "is_holiday": BrokerTime.is_holiday(),
        }
