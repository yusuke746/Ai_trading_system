"""
config.py — 設定値一元管理

全モジュールはこのファイルから CONFIG をimportして使用すること。
デモ/本番の切り替えは IS_DEMO_MODE フラグ1つで完結する。
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # ─────────────────────────────────
    # デモ/本番切り替えフラグ（最重要）
    # True=デモ口座, False=本番口座
    # ─────────────────────────────────
    IS_DEMO_MODE: bool = True

    # MT5 口座情報（.envから取得）
    MT5_LOGIN_DEMO: int = int(os.getenv("MT5_LOGIN_DEMO", "0"))
    MT5_LOGIN_LIVE: int = int(os.getenv("MT5_LOGIN_LIVE", "0"))
    MT5_PASSWORD: str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER_DEMO: str = os.getenv("MT5_SERVER_DEMO", "XMTrading-Demo")
    MT5_SERVER_LIVE: str = os.getenv("MT5_SERVER_LIVE", "XMTrading-Real")

    @property
    def MT5_LOGIN(self) -> int:
        return self.MT5_LOGIN_DEMO if self.IS_DEMO_MODE else self.MT5_LOGIN_LIVE

    @property
    def MT5_SERVER(self) -> str:
        return self.MT5_SERVER_DEMO if self.IS_DEMO_MODE else self.MT5_SERVER_LIVE

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    MODEL_MAIN: str = "gpt-5.2"         # エントリー評価・H1監視用
    MODEL_FAST: str = "gpt-5-mini"      # 緊急判定・WAIT再チェック用

    # モデル料金テーブル (USD / 1M tokens)
    # モデル変更時はここだけ更新すればOK
    MODEL_PRICING: dict = field(default_factory=lambda: {
        "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
        "gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.0},
        "gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
        "gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.4},
    })

    # Discord
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # FastAPI
    FASTAPI_PORT: int = int(os.getenv("FASTAPI_PORT", "80"))

    # 対象銘柄
    SYMBOLS: tuple = ("USDJPY", "EURUSD", "GOLD")

    # リスク設定
    MAX_RISK_PER_TRADE_PCT: float = 2.0
    MAX_DAILY_DRAWDOWN_PCT: float = 3.0
    MAX_TOTAL_EXPOSURE_PCT: float = 3.0
    MAX_POSITIONS: int = 3
    MAX_USD_EXPOSURE: int = 2
    MAX_JPY_EXPOSURE: int = 1

    # AI設定
    AI_TIMEOUT_MAIN_SEC: int = 30
    AI_TIMEOUT_FAST_SEC: int = 20
    AI_MIN_CONFIDENCE: float = 0.6
    AI_MAX_TP_DEVIATION_PCT: float = 5.0
    AI_MAX_RETRIES: int = 3
    AI_RETRY_BACKOFF_SEC: tuple = (2, 5, 10)
    AI_REASONING_EFFORT_MAIN: str = "medium"  # gpt-5推論量: low/medium/high
    AI_REASONING_EFFORT_FAST: str = "low"     # 緊急判定は低推論でコスト抑制

    # 監視間隔
    H1_BATCH_CRON_MINUTE: int = 1
    PRICE_CHECK_INTERVAL_SEC: int = 60
    DD_CHECK_INTERVAL_SEC: int = 30

    # 指標前停止
    PRE_EVENT_STOP_MINUTES: int = 30

    # 市場クローズ・週末管理
    MARKET_HOURS: dict = field(default_factory=lambda: {
        "USDJPY": {
            "daily_close_start": "23:55",
            "daily_close_end": "00:05",
            "weekly_close": "23:55",
            "weekly_open": "23:05",
        },
        "EURUSD": {
            "daily_close_start": "23:55",
            "daily_close_end": "00:05",
            "weekly_close": "23:55",
            "weekly_open": "23:05",
        },
        "GOLD": {
            "daily_close_start": "22:58",
            "daily_close_end": "23:06",
            "weekly_close": "23:55",
            "weekly_open": "23:05",
        },
    })
    FRIDAY_ENTRY_CUTOFF_HOUR: int = 20
    FRIDAY_FORCE_CLOSE_HOUR: int = 22
    FRIDAY_FINAL_CHECK_HOUR: int = 22
    FRIDAY_FINAL_CHECK_MINUTE: int = 30
    WEEKLY_OPEN_WAIT_MINUTES: int = 15
    DEAD_ZONE_HARD_BLOCK: bool = True

    # スプレッド制限（points単位）
    SPREAD_LIMITS_POINTS: dict = field(default_factory=lambda: {
        "USDJPY": 30,
        "EURUSD": 25,
        "GOLD": 50,
    })

    # 物理SL上限（pips）
    MAX_SL_PIPS: dict = field(default_factory=lambda: {
        "USDJPY": 80,
        "EURUSD": 60,
        "GOLD": 300,
    })

    # Webhook認証
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    DEDUP_WINDOW_SEC: int = 300

    # ヘルスチェック
    STATUS_API_TOKEN: str = os.getenv("STATUS_API_TOKEN", "")

    # DB設定
    DB_PATH: str = "trading.db"
    PURGE_REVIEW_LOG_DAYS: int = 90
    PURGE_API_COST_DAYS: int = 30
    PURGE_AI_AUDIT_DAYS: int = 14
    PURGE_THESIS_DAYS: int = 180
    DB_SIZE_ALERT_MB: int = 50
    AI_AUDIT_RESPONSE_MAX_CHARS: int = 4000

    # WAITハンドリング
    WAIT_TTL_MINUTES: int = 15
    WAIT_MAX_RETRIES: int = 2
    WAIT_RECHECK_INTERVAL_SEC: int = 300

    # web_searchフォールバック
    WEB_SEARCH_TIMEOUT_SEC: int = 10

    # マルチインスタンス防止
    PID_FILE_PATH: str = "trading_system.pid"

    # MT5注文設定
    MT5_MAGIC_NUMBER: int = 20250101
    MT5_DEVIATION_POINTS: int = 10

    # ログ設定
    LOG_FILE: str = "logs/trading_system.log"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT: int = 5


CONFIG = TradingConfig()


def estimate_api_cost(
    model: str, tokens_in: int, tokens_out: int, cached_tokens: int = 0
) -> float:
    """モデル料金テーブルからAPIコストを概算する"""
    pricing = CONFIG.MODEL_PRICING.get(model)
    if not pricing:
        # 未知モデルはgpt-5料金で推定
        return (tokens_in * 1.25 + tokens_out * 10.0) / 1_000_000

    non_cached = max(0, tokens_in - cached_tokens)
    input_cost = (
        non_cached * pricing["input"] + cached_tokens * pricing["cached_input"]
    ) / 1_000_000
    output_cost = tokens_out * pricing["output"] / 1_000_000

    return input_cost + output_cost

def validate_config() -> None:
    """起動時に必須設定の存在と妥当性を確認"""
    errors = []
    if not CONFIG.MT5_LOGIN:
        errors.append("MT5_LOGIN未設定")
    if not CONFIG.MT5_PASSWORD:
        errors.append("MT5_PASSWORD未設定")
    if not CONFIG.OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY未設定")
    if not CONFIG.DISCORD_WEBHOOK_URL:
        errors.append("DISCORD_WEBHOOK_URL未設定")
    if not CONFIG.WEBHOOK_SECRET:
        errors.append("WEBHOOK_SECRET未設定")
    if errors:
        raise SystemExit(f"設定エラー: {', '.join(errors)}")
