"""
main.py — AI Trading System エントリーポイント

全コンポーネントの初期化・FastAPI起動・スケジューラー管理。
APScheduler: timezone=ZoneInfo("Europe/Athens") で全cronジョブをXMT時間で管理。
"""

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI

from config import CONFIG, validate_config

# ──────────── ログ設定 ────────────

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(LOG_DIR, "trading_system.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# ──────────── コンポーネント初期化 ────────────

# Core
from core.mt5_client import MT5Client
from core.thesis_db import ThesisDB
from core.risk_guardian import RiskGuardian

# Notifications
from notifications.discord_notifier import DiscordNotifier

# Ingestion
from ingestion.economic_calendar import EconomicCalendar
from ingestion import webhook_receiver

# AI
from ai.entry_evaluator import EntryEvaluator
from ai.position_monitor import PositionMonitor

# インスタンス生成
mt5_client = MT5Client()
thesis_db = ThesisDB(db_path=os.path.join(os.path.dirname(__file__), "data", "trading.db"))
guardian = RiskGuardian()
notifier = DiscordNotifier()
calendar = EconomicCalendar()
evaluator = EntryEvaluator()
monitor = PositionMonitor()

# XMTタイムゾーン
XMT_TIMEZONE = ZoneInfo("Europe/Athens")
scheduler = AsyncIOScheduler(timezone=XMT_TIMEZONE)


# ──────────── 依存性注入 ────────────

def wire_dependencies():
    """全コンポーネント間の依存関係を接続"""
    mt5_client.set_notifier(notifier)
    thesis_db.set_notifier(notifier)
    guardian.set_dependencies(mt5_client, thesis_db, notifier)
    notifier.set_dependencies(db=thesis_db, mt5_client=mt5_client)
    evaluator.set_dependencies(
        mt5_client=mt5_client,
        thesis_db=thesis_db,
        notifier=notifier,
        guardian=guardian,
        calendar=calendar,
        scheduler=scheduler,
        lot_calculator=None,  # モジュール直接importのため不要
    )
    monitor.set_dependencies(
        mt5_client=mt5_client,
        thesis_db=thesis_db,
        notifier=notifier,
        guardian=guardian,
        calendar=calendar,
    )
    webhook_receiver.set_dependencies(
        entry_evaluator=evaluator,
        notifier=notifier,
    )


# ──────────── スケジューラー設定 ────────────

def setup_scheduler():
    """APSchedulerのジョブを設定（全時刻はXMT時間）"""
    # H1バッチ査定（毎時01分）
    scheduler.add_job(
        monitor.run_h1_batch_review,
        "cron",
        minute=CONFIG.H1_BATCH_CRON_MINUTE,
        id="h1_batch",
    )

    # ガードチェック（30秒ごと）
    scheduler.add_job(
        guardian.check_all_guards,
        "interval",
        seconds=CONFIG.DD_CHECK_INTERVAL_SEC,
        id="guard_check",
    )

    # 価格近接チェック（60秒ごと）
    scheduler.add_job(
        monitor.check_price_proximity,
        "interval",
        seconds=CONFIG.PRICE_CHECK_INTERVAL_SEC,
        id="price_check",
    )

    # DBメンテナンス（XMT 00:05）
    scheduler.add_job(
        thesis_db.run_maintenance,
        "cron",
        hour=0,
        minute=5,
        id="db_maintenance",
    )

    # 日次レポート（XMT 07:00）
    scheduler.add_job(
        notifier.send_daily_report,
        "cron",
        hour=7,
        minute=0,
        id="daily_report",
    )

    # 金曜カットオフ（XMT金曜 20:00）
    scheduler.add_job(
        guardian.check_friday_cutoff,
        "cron",
        day_of_week="fri",
        hour=CONFIG.FRIDAY_ENTRY_CUTOFF_HOUR,
        minute=0,
        id="friday_cutoff",
    )

    # 週末強制決済（XMT金曜 22:00）
    scheduler.add_job(
        monitor.weekend_force_close,
        "cron",
        day_of_week="fri",
        hour=CONFIG.FRIDAY_FORCE_CLOSE_HOUR,
        minute=0,
        id="weekend_close",
    )

    # 週末最終チェック（XMT金曜 22:30）
    scheduler.add_job(
        monitor.weekend_final_check,
        "cron",
        day_of_week="fri",
        hour=CONFIG.FRIDAY_FINAL_CHECK_HOUR,
        minute=CONFIG.FRIDAY_FINAL_CHECK_MINUTE,
        id="weekend_final",
    )

    # 週明けオープン（XMT日曜 23:20）
    scheduler.add_job(
        guardian.check_weekly_open,
        "cron",
        day_of_week="sun",
        hour=23,
        minute=20,
        id="weekly_open",
    )

    logger.info("スケジューラー設定完了（全ジョブXMT時間基準）")


# ──────────── 起動チェック ────────────

async def startup_checks() -> bool:
    """起動時チェックリスト"""
    logger.info("=" * 50)
    logger.info("AI Trading System - 起動シーケンス開始")
    logger.info("=" * 50)

    # 1. 設定バリデーション
    logger.info("[1/10] 設定バリデーション...")
    errors = validate_config()
    if errors:
        for e in errors:
            logger.error(f"  設定エラー: {e}")
        return False
    logger.info("  ✅ 設定OK")

    # 2. MT5接続
    logger.info("[2/10] MT5接続...")
    if not mt5_client.connect():
        logger.critical("  ❌ MT5接続失敗 → 起動中止")
        return False
    logger.info("  ✅ MT5接続OK")

    # 3. 起動時リカバリー
    logger.info("[3/10] 起動時リカバリー...")
    positions = await mt5_client.get_all_positions()
    active_tickets = [p.ticket for p in positions]
    orphaned = await thesis_db.get_orphaned_theses(active_tickets)
    if orphaned:
        for o in orphaned:
            logger.warning(f"  ⚠️ 孤立Thesis検出: {o['trade_id'][:8]} ticket={o['ticket']}")
        await notifier.send(
            f"⚠️ 起動時リカバリー: {len(orphaned)}件の孤立Thesis検出\n"
            f"MT5にポジションなし → 手動確認推奨",
            level="WARNING",
        )
    # 未追跡ポジション
    tracked_tickets = set()
    theses = await thesis_db.get_active_theses()
    for t in theses:
        tracked_tickets.add(t["ticket"])
    untracked = [p for p in positions if p.ticket not in tracked_tickets]
    if untracked:
        for u in untracked:
            logger.warning(f"  ⚠️ 未追跡ポジション: ticket={u.ticket} {u.symbol}")
        await notifier.send(
            f"⚠️ 未追跡ポジション: {len(untracked)}件\n"
            f"Thesisなしで運用中 → 手動管理が必要",
            level="WARNING",
        )
    logger.info(f"  ✅ リカバリーチェック完了 (ポジション: {len(positions)}件)")

    # 4. OpenAI API疎通
    logger.info("[4/10] OpenAI API疎通確認...")
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=CONFIG.OPENAI_API_KEY)
        test_response = await client.responses.create(
            model=CONFIG.MODEL_FAST,
            input=[{"role": "user", "content": "ping"}],
        )
        logger.info("  ✅ OpenAI API OK")
    except Exception as e:
        logger.error(f"  ❌ OpenAI API疎通失敗: {e}")
        # 起動は止めない（AI障害時はルールベースフォールバック）

    # 5. Discord疎通
    logger.info("[5/10] Discord Webhook疎通確認...")
    discord_ok = await notifier.test_connection()
    if discord_ok:
        logger.info("  ✅ Discord OK")
    else:
        logger.warning("  ⚠️ Discord疎通失敗（通知なしで継続）")

    # 6. DB初期化
    logger.info("[6/10] DB初期化...")
    # start()内でスキーマ作成済み
    logger.info("  ✅ DB OK")

    # 7. 市場状態確認
    logger.info("[7/10] 市場状態確認...")
    from core.broker_time import BrokerTime

    if BrokerTime.is_weekend():
        guardian.status = guardian.status.__class__("WEEKEND_CLOSED")
        logger.info("  📅 市場クローズ中（WEEKEND_CLOSED）")
    elif BrokerTime.is_friday_cutoff():
        guardian.status = guardian.status.__class__("FRIDAY_CUTOFF")
        logger.info("  📅 金曜カットオフ中（FRIDAY_CUTOFF）")
    else:
        logger.info(f"  📅 市場オープン中 セッション: {BrokerTime.get_session()}")

    # 8. デモ/本番通知
    logger.info("[8/10] 起動通知...")
    import MetaTrader5 as mt5_module

    account_info = mt5_module.account_info()
    mode = "DEMO" if (account_info and account_info.trade_mode == 0) else "LIVE"
    balance = account_info.balance if account_info else 0

    await notifier.send_startup_notification(
        mode=mode,
        login=account_info.login if account_info else 0,
        balance=balance,
        position_count=len(positions),
    )

    # 9. スケジューラー設定
    logger.info("[9/10] スケジューラー設定...")
    setup_scheduler()

    # 10. シグナルハンドラー
    logger.info("[10/10] シグナルハンドラー登録...")
    # Windows互換: SIGTERM/SIGINTのみ
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(graceful_shutdown()))
    except NotImplementedError:
        # Windows: signal.signal で代替
        signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(graceful_shutdown()))
    logger.info("  ✅ シグナルハンドラーOK")

    logger.info("=" * 50)
    logger.info(f"起動完了 — {mode} | 残高: {balance:,.0f}円 | ポート: {CONFIG.FASTAPI_PORT}")
    logger.info("=" * 50)

    return True


async def graceful_shutdown():
    """グレースフルシャットダウン"""
    logger.info("シャットダウン開始...")

    scheduler.shutdown(wait=False)
    await notifier.send("🔴 システムシャットダウン", level="CRITICAL")
    await calendar.close()
    await notifier.close()
    await thesis_db.close()
    mt5_client.shutdown()

    logger.info("シャットダウン完了")


# ──────────── FastAPI Lifespan ────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI起動・終了時の処理"""
    # 起動
    wire_dependencies()
    await notifier.start()
    await calendar.start()
    await thesis_db.start()

    if not await startup_checks():
        logger.critical("起動チェック失敗 → システム停止")
        sys.exit(1)

    scheduler.start()
    logger.info("スケジューラー開始")

    yield

    # 終了
    await graceful_shutdown()


# ──────────── FastAPIアプリ ────────────

app = FastAPI(
    title="AI Trading System",
    description="TradingView Webhook → AI判断 → MT5自動売買",
    version="1.0.0",
    lifespan=lifespan,
)

# ルーター登録
app.include_router(webhook_receiver.router)


# ステータスAPI（guardianのフル情報付き）
@app.get("/api/status")
async def full_status():
    """システムフルステータス"""
    report = guardian.get_status_report()
    positions = await mt5_client.get_all_positions()
    balance = await mt5_client.get_account_balance()
    daily_pnl = await mt5_client.get_daily_pnl()
    db_stats = await thesis_db.get_db_stats()

    report.update({
        "positions": len(positions),
        "balance": balance,
        "daily_pnl": daily_pnl,
        "db_stats": db_stats,
    })
    return report


@app.post("/api/unlock")
async def unlock_system():
    """サーキットブレーカー手動解除"""
    result = await guardian.unlock()
    return {"result": result}


# ──────────── エントリーポイント ────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=CONFIG.FASTAPI_PORT,
        log_level="info",
        reload=False,
    )
