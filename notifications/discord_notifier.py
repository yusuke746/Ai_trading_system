"""
notifications/discord_notifier.py — Discord Webhook通知

全システムイベントをDiscord Embedsで通知。
通知失敗はシステムを止めない。同一内容の連続通知を30秒で抑制。
同時刻の通知はバッファリングして一括送信（2秒待機で集約）。
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from config import CONFIG
from core.broker_time import BrokerTime

logger = logging.getLogger(__name__)

# 通知レベル定義
LEVEL_COLORS = {
    "INFO": 0x00FF00,      # 🟢 緑
    "WARNING": 0xFFFF00,   # 🟡 黄
    "CRITICAL": 0xFF0000,  # 🔴 赤
    "DAILY": 0xFFFFFF,     # ⚪ 白
}

LEVEL_EMOJI = {
    "INFO": "🟢",
    "WARNING": "🟡",
    "CRITICAL": "🔴",
    "DAILY": "⚪",
}

# 重複通知抑制
_recent_notifications: dict[str, datetime] = {}
DEDUP_WINDOW_SEC: int = 30

# バッチ送信設定
BATCH_WINDOW_SEC: float = 2.0  # 同時通知の集約待機時間


class DiscordNotifier:
    """Discord Webhook通知マネージャー"""

    def __init__(self):
        self.webhook_url: str = CONFIG.DISCORD_WEBHOOK_URL
        self._client: Optional[httpx.AsyncClient] = None
        self._db = None  # ThesisDB参照（日次レポート用）
        self._mt5_client = None
        # バッチ送信用バッファ
        self._batch_buffer: list[dict] = []  # {"message", "level", "title"}
        self._batch_task: Optional[asyncio.Task] = None
        self._batch_lock = asyncio.Lock()

    def set_dependencies(self, db=None, mt5_client=None):
        self._db = db
        self._mt5_client = mt5_client

    async def start(self):
        """httpxクライアント初期化"""
        self._client = httpx.AsyncClient(timeout=5.0)

    async def close(self):
        """クリーンアップ"""
        # バッファに残っている通知をフラッシュ
        async with self._batch_lock:
            if self._batch_buffer:
                await self._flush_batch()
        if self._client:
            await self._client.aclose()

    # ──────────── コア送信 ────────────

    async def send(self, message: str, level: str = "INFO", title: Optional[str] = None):
        """Discord Embed通知を送信（バッチ対応）"""
        if not self.webhook_url:
            logger.warning("Discord Webhook URLが未設定")
            return

        # 重複抑制
        msg_hash = hashlib.md5(message.encode()).hexdigest()
        now = datetime.utcnow()
        if msg_hash in _recent_notifications:
            elapsed = (now - _recent_notifications[msg_hash]).total_seconds()
            if elapsed < DEDUP_WINDOW_SEC:
                logger.debug(f"重複通知抑制: {message[:50]}...")
                return
        _recent_notifications[msg_hash] = now

        # 古い通知ハッシュをクリーンアップ
        expired = [k for k, v in _recent_notifications.items()
                    if (now - v).total_seconds() > DEDUP_WINDOW_SEC * 2]
        for k in expired:
            del _recent_notifications[k]

        # CRITICAL は即時送信（バッファしない）
        if level == "CRITICAL":
            await self._send_immediate(message, level, title)
            return

        # バッファに追加して遅延送信
        async with self._batch_lock:
            self._batch_buffer.append({
                "message": message,
                "level": level,
                "title": title,
            })
            # 最初の追加時にフラッシュタスクを起動
            if self._batch_task is None or self._batch_task.done():
                self._batch_task = asyncio.create_task(self._schedule_flush())

    async def _schedule_flush(self):
        """BATCH_WINDOW_SEC 待機後にバッファをフラッシュ"""
        await asyncio.sleep(BATCH_WINDOW_SEC)
        async with self._batch_lock:
            if self._batch_buffer:
                await self._flush_batch()

    async def _flush_batch(self):
        """バッファ内の通知をまとめて送信"""
        items = self._batch_buffer.copy()
        self._batch_buffer.clear()

        if not items:
            return

        # 1件のみ → そのまま送信
        if len(items) == 1:
            item = items[0]
            await self._send_immediate(item["message"], item["level"], item["title"])
            return

        # 複数件 → 同一レベル・同一理由のものをグループ化
        groups = self._group_notifications(items)

        for group in groups:
            if len(group) == 1:
                item = group[0]
                await self._send_immediate(item["message"], item["level"], item["title"])
            else:
                await self._send_grouped(group)

    def _group_notifications(self, items: list[dict]) -> list[list[dict]]:
        """同一レベルかつ同一タイトルの通知をグループ化"""
        from collections import OrderedDict
        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for item in items:
            key = f"{item['level']}:{item.get('title', '')}"
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return list(groups.values())

    async def _send_grouped(self, items: list[dict]):
        """同一グループの通知を1つのEmbedにまとめて送信"""
        level = items[0]["level"]
        title = items[0].get("title") or f"{LEVEL_EMOJI.get(level, 'ℹ️')} {level}"
        color = LEVEL_COLORS.get(level, 0xFFFFFF)

        # 各メッセージを改行区切りで結合
        combined = "\n\n".join(item["message"] for item in items)

        # Discord Embedの上限 (4096文字) を考慮
        if len(combined) > 4000:
            combined = combined[:3997] + "..."

        embed = {
            "title": f"{title} ({len(items)}件)",
            "description": combined,
            "color": color,
            "footer": {"text": f"⏰ {BrokerTime.now_str()}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        payload = {"embeds": [embed]}

        try:
            if not self._client:
                await self.start()
            response = await self._client.post(self.webhook_url, json=payload)
            if response.status_code not in (200, 204):
                logger.warning(f"Discord送信異常: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"Discord通知失敗: {e}")

    async def _send_immediate(self, message: str, level: str = "INFO", title: Optional[str] = None):
        """即時送信（バッファを経由しない）"""
        color = LEVEL_COLORS.get(level, 0xFFFFFF)
        emoji = LEVEL_EMOJI.get(level, "ℹ️")
        display_title = title or f"{emoji} {level}"

        embed = {
            "title": display_title,
            "description": message,
            "color": color,
            "footer": {"text": f"⏰ {BrokerTime.now_str()}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        payload = {"embeds": [embed]}

        try:
            if not self._client:
                await self.start()
            response = await self._client.post(self.webhook_url, json=payload)
            if response.status_code not in (200, 204):
                logger.warning(f"Discord送信異常: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"Discord通知失敗: {e}")

    # ──────────── 特化通知 ────────────

    async def send_entry_notification(
        self,
        trade_id: str,
        order_result,
        ai_response: dict,
        webhook_data: dict,
    ):
        """新規エントリー通知"""
        symbol = webhook_data.get("symbol", "N/A")
        direction = webhook_data.get("direction", "N/A")
        lot = order_result.lot if order_result else 0

        risk_pct = ai_response.get("risk_multiplier", 1.0)
        confidence = ai_response.get("confidence", 0)
        thesis = ai_response.get("thesis", "N/A")[:200]
        market_regime = ai_response.get("market_regime", "N/A")
        tp = ai_response.get("initial_tp", 0)
        sl = ai_response.get("emergency_sl", 0)

        invalidation = ai_response.get("invalidation_conditions", [])
        inv_text = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(invalidation))

        spread = order_result.spread_at_entry if order_result else 0
        slippage = order_result.slippage_points if order_result else 0

        msg = (
            f"📊 {symbol} {direction}  |  {lot}lot\n"
            f"🎯 TP: {tp}  |  🛡 SL: {sl}\n"
            f"🤖 GPT-4o  信頼度: {confidence * 100:.0f}%  |  {market_regime}\n"
            f"📝 Thesis: {thesis}\n"
            f"⚠️ 無効化条件:\n{inv_text}\n"
            f"📏 Spread: {spread:.1f}pts  |  Slippage: {slippage:.1f}pts\n"
            f"🆔 {trade_id[:8]}"
        )

        await self.send(msg, level="INFO", title="🟢 新規エントリー承認")

    async def send_h1_summary(self, results: list[dict]):
        """H1バッチ査定サマリー"""
        if not results:
            return

        lines = []
        for r in results:
            trade_id = r.get("trade_id", "N/A")[:8]
            symbol = r.get("symbol", "?")
            status = r.get("thesis_status", "?")
            action = r.get("action", "?")
            lines.append(f"  {trade_id} {symbol}: {status} → {action}")

        msg = "\n".join(lines)
        await self.send(msg, level="INFO", title="📋 H1査定サマリー")

    async def send_close_notification(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        exit_reason: str,
        pnl_pips: float,
        pnl_jpy: float,
        hold_hours: float,
    ):
        """ポジション決済通知"""
        emoji = "📈" if pnl_jpy >= 0 else "📉"
        msg = (
            f"{emoji} {symbol} {direction}  |  {exit_reason}\n"
            f"💰 損益: {pnl_jpy:+,.0f}円 ({pnl_pips:+.1f}pips)\n"
            f"⏱ 保有: {hold_hours:.1f}h\n"
            f"🆔 {trade_id[:8]}"
        )

        level = "INFO" if pnl_jpy >= 0 else "WARNING"
        await self.send(msg, level=level, title=f"{'📈' if pnl_jpy >= 0 else '📉'} ポジション決済")

    async def send_daily_report(self):
        """日次レポート（毎朝 XMT 07:00）"""
        try:
            # DB統計
            db_stats = {}
            if self._db:
                db_stats = await self._db.get_db_stats()

            # 口座状況
            balance = 0
            daily_pnl = 0
            if self._mt5_client:
                balance = await self._mt5_client.get_account_balance()
                daily_pnl = await self._mt5_client.get_daily_pnl()

            dd_pct = abs(min(0, daily_pnl)) / balance * 100 if balance > 0 else 0

            # 経済指標（後でeconomic_calendarから取得）
            events_text = "（取得中...）"

            msg = (
                f"📈 昨日の損益: {daily_pnl:+,.0f}円 ({dd_pct:.2f}%)\n"
                f"💰 口座残高: {balance:,.0f}円\n"
                f"💾 DB: thesis={db_stats.get('thesis_active', 0)}件 / "
                f"history={db_stats.get('trade_history_count', 0)}件\n"
                f"💵 API費用（今月累計）: ${db_stats.get('monthly_cost_usd', 0):.2f}\n"
                f"🔋 システム: ACTIVE\n"
                f"⏰ 今日の重要指標:\n{events_text}"
            )

            await self.send(msg, level="DAILY", title=f"⚪ 日次レポート - {BrokerTime.now().strftime('%Y-%m-%d')}")

        except Exception as e:
            logger.exception(f"日次レポート生成エラー: {e}")

    async def send_startup_notification(
        self,
        mode: str,
        login: int,
        balance: float,
        position_count: int,
    ):
        """起動時通知"""
        msg = (
            f"🔧 モード: {mode}\n"
            f"🏦 口座: #{login}\n"
            f"💰 残高: {balance:,.0f}円\n"
            f"📊 保有ポジション: {position_count}件\n"
            f"🌐 Webhook: ポート {CONFIG.FASTAPI_PORT}"
        )
        await self.send(msg, level="INFO", title="🚀 システム起動")

    async def test_connection(self) -> bool:
        """起動時のWebhook疎通テスト"""
        try:
            if not self.webhook_url:
                logger.warning("Discord Webhook URL未設定")
                return False

            if not self._client:
                await self.start()

            response = await self._client.post(
                self.webhook_url,
                json={"content": "🔧 AI Trading System - 疎通テスト OK"},
            )
            return response.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Discord疎通テスト失敗: {e}")
            return False
