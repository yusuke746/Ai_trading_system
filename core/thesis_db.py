"""
core/thesis_db.py — SQLite DB (Dedicated Writerパターン)

全DB書き込みを単一のasyncioキューで直列化する。
WALモード + busy_timeout で安全に並行読み取りを許可。
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from core.broker_time import BrokerTime

logger = logging.getLogger(__name__)

# テーブル定義SQL
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS thesis (
    trade_id TEXT PRIMARY KEY,
    ticket INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    technical_ctx TEXT,
    fundamental_ctx TEXT,
    thesis_text TEXT,
    invalidation TEXT,
    entry_price REAL,
    initial_tp REAL,
    emergency_sl REAL,
    risk_multiplier REAL,
    market_regime TEXT,
    ai_confidence REAL,
    lot_size REAL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    broker_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    thesis_status TEXT,
    action TEXT,
    old_tp REAL,
    new_tp REAL,
    reasoning TEXT,
    ai_model_used TEXT,
    tokens_used INTEGER,
    broker_time TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_history (
    trade_id TEXT PRIMARY KEY,
    symbol TEXT,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    pnl_pips REAL,
    pnl_jpy REAL,
    lot_size REAL,
    thesis_text TEXT,
    ai_confidence REAL,
    spread_at_entry REAL,
    slippage_points REAL,
    thesis_broken_at TEXT,
    price_at_thesis_broken REAL,
    thesis_broken_correct INTEGER,
    broker_entry_time TEXT,
    broker_exit_time TEXT,
    hold_hours REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS circuit_breaker_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT,
    daily_pnl_jpy REAL,
    dd_pct REAL,
    broker_time TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    purpose TEXT,
    broker_time TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    purpose TEXT,
    model TEXT,
    prompt_summary TEXT,
    full_response TEXT,
    parsed_decision TEXT,
    validation_result TEXT,
    was_overridden INTEGER DEFAULT 0,
    override_reason TEXT,
    latency_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    trade_id TEXT,
    broker_time TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_thesis_status ON thesis(status);
CREATE INDEX IF NOT EXISTS idx_review_trade_id ON review_log(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_history_symbol ON trade_history(symbol);
CREATE INDEX IF NOT EXISTS idx_audit_request_id ON ai_audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_trade_id ON ai_audit_log(trade_id);
"""


class ThesisDB:
    """全DB書き込みを単一のasyncioキューで直列化する"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._conn: Optional[aiosqlite.Connection] = None
        self._notifier = None

    def set_notifier(self, notifier):
        self._notifier = notifier

    async def start(self):
        """起動時にライタータスクを開始し、スキーマを初期化"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path, timeout=30)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info(f"ThesisDB起動完了: {self.db_path}")

    async def _writer_loop(self):
        """単一ループで全書き込みを逐次処理"""
        while True:
            try:
                sql, params, future = await self._write_queue.get()
                try:
                    await self._conn.execute(sql, params)
                    await self._conn.commit()
                    if not future.done():
                        future.set_result(True)
                except Exception as e:
                    if not future.done():
                        future.set_result(False)
                    logger.error(f"DB書き込みエラー: {e}")
                    if self._notifier:
                        await self._notifier.send(f"⚠️ DB書き込み失敗: {e}", level="WARNING")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"ライターループ予期せぬエラー: {e}")

    async def write(self, sql: str, params: tuple = ()) -> bool:
        """書き込みリクエストをキューに投入し、完了を待つ"""
        future = asyncio.get_event_loop().create_future()
        await self._write_queue.put((sql, params, future))
        return await future

    async def read(self, sql: str, params: tuple = ()) -> list:
        """読み取りは永続接続を再利用（WALモードで並行OK）"""
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()

    async def read_one(self, sql: str, params: tuple = ()) -> Optional[tuple]:
        """単一行読み取り"""
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchone()

    async def close(self):
        """シャットダウン時に接続をクローズ"""
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            await self._conn.close()
        logger.info("ThesisDB切断完了")

    # ──────────── Thesis操作 ────────────

    async def save_thesis(
        self,
        trade_id: str,
        ticket: int,
        symbol: str,
        direction: str,
        technical_ctx: dict,
        fundamental_ctx: dict,
        thesis_text: str,
        invalidation: list[str],
        entry_price: float,
        initial_tp: float,
        emergency_sl: float,
        risk_multiplier: float,
        market_regime: str,
        ai_confidence: float,
        lot_size: float,
    ) -> bool:
        now = BrokerTime.now_str()
        return await self.write(
            """INSERT INTO thesis (
                trade_id, ticket, symbol, direction,
                technical_ctx, fundamental_ctx, thesis_text, invalidation,
                entry_price, initial_tp, emergency_sl, risk_multiplier,
                market_regime, ai_confidence, lot_size, status,
                broker_time, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)""",
            (
                trade_id, ticket, symbol, direction,
                json.dumps(technical_ctx, ensure_ascii=False),
                json.dumps(fundamental_ctx, ensure_ascii=False),
                thesis_text,
                json.dumps(invalidation, ensure_ascii=False),
                entry_price, initial_tp, emergency_sl, risk_multiplier,
                market_regime, ai_confidence, lot_size,
                now, now, now,
            ),
        )

    async def get_active_theses(self) -> list[dict]:
        """全ACTIVEなThesisを取得"""
        rows = await self.read(
            "SELECT * FROM thesis WHERE status = 'ACTIVE'"
        )
        if not rows:
            return []

        columns = [
            "trade_id", "ticket", "symbol", "direction",
            "technical_ctx", "fundamental_ctx", "thesis_text", "invalidation",
            "entry_price", "initial_tp", "emergency_sl", "risk_multiplier",
            "market_regime", "ai_confidence", "lot_size", "status",
            "broker_time", "created_at", "updated_at",
        ]
        results = []
        for row in rows:
            d = dict(zip(columns, row))
            # JSON文字列をパース
            for key in ("technical_ctx", "fundamental_ctx", "invalidation"):
                if d.get(key) and isinstance(d[key], str):
                    try:
                        d[key] = json.loads(d[key])
                    except json.JSONDecodeError:
                        pass
            results.append(d)
        return results

    async def get_thesis_by_id(self, trade_id: str) -> Optional[dict]:
        """trade_idでThesisを取得"""
        row = await self.read_one(
            "SELECT * FROM thesis WHERE trade_id = ?", (trade_id,)
        )
        if not row:
            return None

        columns = [
            "trade_id", "ticket", "symbol", "direction",
            "technical_ctx", "fundamental_ctx", "thesis_text", "invalidation",
            "entry_price", "initial_tp", "emergency_sl", "risk_multiplier",
            "market_regime", "ai_confidence", "lot_size", "status",
            "broker_time", "created_at", "updated_at",
        ]
        d = dict(zip(columns, row))
        for key in ("technical_ctx", "fundamental_ctx", "invalidation"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except json.JSONDecodeError:
                    pass
        return d

    async def close_thesis(self, trade_id: str) -> bool:
        """ThesisをCLOSEDに更新"""
        now = BrokerTime.now_str()
        return await self.write(
            "UPDATE thesis SET status = 'CLOSED', updated_at = ? WHERE trade_id = ?",
            (now, trade_id),
        )

    async def update_thesis_tp(self, trade_id: str, new_tp: float) -> bool:
        """ThesisのTPを更新"""
        now = BrokerTime.now_str()
        return await self.write(
            "UPDATE thesis SET initial_tp = ?, updated_at = ? WHERE trade_id = ?",
            (new_tp, now, trade_id),
        )

    # ──────────── Review Log ────────────

    async def save_review(
        self,
        trade_id: str,
        thesis_status: str,
        action: str,
        old_tp: Optional[float],
        new_tp: Optional[float],
        reasoning: str,
        ai_model_used: str,
        tokens_used: int,
    ) -> bool:
        now = BrokerTime.now_str()
        return await self.write(
            """INSERT INTO review_log (
                trade_id, thesis_status, action, old_tp, new_tp,
                reasoning, ai_model_used, tokens_used, broker_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, thesis_status, action, old_tp, new_tp,
             reasoning, ai_model_used, tokens_used, now, now),
        )

    # ──────────── Trade History ────────────

    async def save_trade_history(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        pnl_pips: float,
        pnl_jpy: float,
        lot_size: float,
        thesis_text: str,
        ai_confidence: float,
        spread_at_entry: float = 0.0,
        slippage_points: float = 0.0,
        thesis_broken_at: Optional[str] = None,
        price_at_thesis_broken: Optional[float] = None,
        broker_entry_time: Optional[str] = None,
        broker_exit_time: Optional[str] = None,
        hold_hours: float = 0.0,
    ) -> bool:
        now = BrokerTime.now_str()
        return await self.write(
            """INSERT OR REPLACE INTO trade_history (
                trade_id, symbol, direction, entry_price, exit_price,
                exit_reason, pnl_pips, pnl_jpy, lot_size, thesis_text,
                ai_confidence, spread_at_entry, slippage_points,
                thesis_broken_at, price_at_thesis_broken, thesis_broken_correct,
                broker_entry_time, broker_exit_time, hold_hours, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (
                trade_id, symbol, direction, entry_price, exit_price,
                exit_reason, pnl_pips, pnl_jpy, lot_size, thesis_text,
                ai_confidence, spread_at_entry, slippage_points,
                thesis_broken_at, price_at_thesis_broken,
                broker_entry_time, broker_exit_time, hold_hours, now,
            ),
        )

    # ──────────── Circuit Breaker Log ────────────

    async def save_cb_log(
        self, reason: str, daily_pnl_jpy: float, dd_pct: float
    ) -> bool:
        now = BrokerTime.now_str()
        return await self.write(
            """INSERT INTO circuit_breaker_log (
                reason, daily_pnl_jpy, dd_pct, broker_time, created_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (reason, daily_pnl_jpy, dd_pct, now, now),
        )

    # ──────────── API Cost Log ────────────

    async def save_api_cost(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        purpose: str,
    ) -> bool:
        now = BrokerTime.now_str()
        return await self.write(
            """INSERT INTO api_cost_log (
                model, tokens_in, tokens_out, cost_usd, purpose, broker_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (model, tokens_in, tokens_out, cost_usd, purpose, now, now),
        )

    # ──────────── AI Audit Log ────────────

    async def save_audit_log(
        self,
        request_id: str,
        purpose: str,
        model: str,
        prompt_summary: str,
        full_response: str,
        parsed_decision: str,
        validation_result: str,
        was_overridden: bool = False,
        override_reason: Optional[str] = None,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        trade_id: Optional[str] = None,
    ) -> bool:
        now = BrokerTime.now_str()
        # full_response最大4000文字（DBサイズ管理）
        truncated_response = full_response[:4000] if full_response else ""
        return await self.write(
            """INSERT INTO ai_audit_log (
                request_id, purpose, model, prompt_summary, full_response,
                parsed_decision, validation_result, was_overridden, override_reason,
                latency_ms, tokens_in, tokens_out, trade_id, broker_time, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, purpose, model, prompt_summary[:500], truncated_response,
                parsed_decision, validation_result,
                1 if was_overridden else 0, override_reason,
                latency_ms, tokens_in, tokens_out, trade_id, now, now,
            ),
        )

    # ──────────── メンテナンス ────────────

    async def run_maintenance(self):
        """自動パージ（毎日 XMT 00:05 に実行）"""
        logger.info("DBメンテナンス開始")

        now = datetime.utcnow()

        # review_log: 90日以上前
        cutoff_90 = (now - timedelta(days=90)).isoformat()
        await self.write("DELETE FROM review_log WHERE created_at < ?", (cutoff_90,))

        # api_cost_log: 30日以上前
        cutoff_30 = (now - timedelta(days=30)).isoformat()
        await self.write("DELETE FROM api_cost_log WHERE created_at < ?", (cutoff_30,))

        # ai_audit_log: 14日以上前（通常）
        # DB > 50MB の場合は7日に短縮
        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        if db_size > 50 * 1024 * 1024:
            audit_days = 7
            logger.warning(f"DB肥大化検知 ({db_size / 1024 / 1024:.1f}MB) → ai_audit_log保持を7日に短縮")
            if self._notifier:
                await self._notifier.send(
                    f"⚠️ DB肥大化: {db_size / 1024 / 1024:.1f}MB → audit_log 7日保持に短縮",
                    level="WARNING",
                )
        else:
            audit_days = 14
        cutoff_audit = (now - timedelta(days=audit_days)).isoformat()
        await self.write("DELETE FROM ai_audit_log WHERE created_at < ?", (cutoff_audit,))

        # thesis: CLOSED + 180日以上前（ACTIVEは絶対に削除しない）
        cutoff_180 = (now - timedelta(days=180)).isoformat()
        await self.write(
            "DELETE FROM thesis WHERE status = 'CLOSED' AND created_at < ?",
            (cutoff_180,),
        )

        # VACUUM
        await self._conn.execute("VACUUM")
        await self._conn.commit()

        logger.info("DBメンテナンス完了")

    async def get_db_stats(self) -> dict:
        """日次レポート用のDB統計"""
        thesis_active = await self.read_one(
            "SELECT COUNT(*) FROM thesis WHERE status = 'ACTIVE'"
        )
        review_count = await self.read_one("SELECT COUNT(*) FROM review_log")
        history_count = await self.read_one("SELECT COUNT(*) FROM trade_history")

        # 今月のAPI費用
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
        monthly_cost = await self.read_one(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_cost_log WHERE created_at >= ?",
            (month_start,),
        )

        return {
            "thesis_active": thesis_active[0] if thesis_active else 0,
            "review_log_count": review_count[0] if review_count else 0,
            "trade_history_count": history_count[0] if history_count else 0,
            "monthly_cost_usd": round(monthly_cost[0] if monthly_cost else 0, 2),
        }

    # ──────────── 起動時リカバリー ────────────

    async def get_orphaned_theses(self, active_tickets: list[int]) -> list[dict]:
        """MT5にポジションが存在しないACTIVEなThesisを検出"""
        active = await self.get_active_theses()
        orphaned = [t for t in active if t["ticket"] not in active_tickets]
        return orphaned
