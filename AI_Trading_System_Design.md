# 自律型AIトレーディングシステム 設計仕様書

**バージョン**: 5.0
**作成日**: 2025年3月（v5.0: 2026年3月改訂）
**対象実装者**: VSCode Agent (Claude Opus)

---

## 目次

1. [システムコンセプト](#1-システムコンセプト)
2. [技術スタック](#2-技術スタック)
3. [運用パラメータ](#3-運用パラメータ)
4. [プロジェクト構造](#4-プロジェクト構造)
5. [監視アーキテクチャ（3層構造）](#5-監視アーキテクチャ3層構造)
6. [コアワークフロー](#6-コアワークフロー)
7. [モジュール設計仕様](#7-モジュール設計仕様)
   - [config.py](#71-configpy--設定値一元管理)
   - [core/models.py](#71b-データ型定義coremodels.py)
   - [core/broker_time.py](#72-corebroker_timepy--xmt時間管理)
   - [core/lot_calculator.py](#73-corelot_calculatorpy--jpy口座専用ロット計算)
   - [core/mt5_client.py](#74-coremt5_clientpy--mt5接続管理)
   - [core/thesis_db.py](#75-corethesis_dbpy--dbスキーマ自動パージ)
   - [core/risk_guardian.py](#76-corerisk_guardianpy--リスク管理)
   - [ingestion/economic_calendar.py](#77-ingestioneconomic_calendarpy--経済指標カレンダー)
   - [ingestion/webhook_receiver.py](#78-ingestionwebhook_receiverpy--tradingview受信)
   - [ai/prompt_builder.py](#79-aiprompt_builderpy--プロンプト構築)
   - [ai/entry_evaluator.py](#710-aentry_evaluatorpy--エントリー評価)
   - [ai/position_monitor.py](#711-aiposition_monitorpy--ポジション監視)
   - [notifications/discord_notifier.py](#712-notificationsdiscord_notifierpy--discord通知)
   - [main.py](#713-mainpy--エントリーポイント)
8. [TradingView Pine Script仕様](#8-tradingview-pine-script仕様)
9. [AIプロンプト設計](#9-aiプロンプト設計)
10. [DBスキーマ・パージポリシー](#10-dbスキーマパージポリシー)
11. [リスク管理・サーキットブレーカー設計](#11-リスク管理サーキットブレーカー設計)
12. [市場クローズ・週末ポジション管理](#12-市場クローズ週末ポジション管理)
13. [Discord通知仕様](#13-discord通知仕様)
14. [デモ・本番切り替え設計](#14-デモ本番切り替え設計)
15. [運用堅牢性設計](#15-運用堅牢性設計)
16. [バックテスト設計](#16-バックテスト設計)
17. [テスト戦略](#17-テスト戦略)
18. [実装ロードマップ](#18-実装ロードマップ)
19. [コスト試算](#19-コスト試算)

---

## 1. システムコンセプト

### 基本思想

テクニカル分析（TradingView）を**エントリートリガー**とし、ファンダメンタルズと相場文脈（OpenAI）を**フィルターおよび出口戦略**に用いるハイブリッド型自動売買システム。

**最大の特徴**:
固定SL/TPに依存せず、トレードの根拠（**Thesis**）の妥当性をAIが継続的に評価してポジションを動的に管理する。

### 設計原則

- **半自律運用**: AI判断 + 人間（Discord通知経由）のハイブリッド。完全自律より安全。
- **Thesis駆動**: 「なぜ持つか」「何が起きたら手放すか」を言語化してDBに保存。
- **フェイルセーフ優先**: AIの上位に物理SL・サーキットブレーカーを多層配置。
- **JPY建て統一**: 口座通貨JPYに合わせた計算を全モジュールで徹底。
- **XMTサーバー時間統一**: 全datetimeをXMT（GMT+2/+3）で統一管理。

---

## 2. 技術スタック

| 役割               | 技術                                        |
| ------------------ | ------------------------------------------- |
| シグナル生成       | TradingView (Pine Script v5)                |
| オーケストレーター | Python 3.11+ / FastAPI / asyncio            |
| AIエンジン         | OpenAI API (GPT-4o / GPT-4o-mini)           |
| 執行エンジン       | MetaTrader 5 (Python MetaTrader5ライブラリ) |
| DB                 | SQLite (WALモード)                          |
| スケジューラー     | APScheduler (AsyncIOScheduler)              |
| 通知               | Discord Webhook                             |
| 経済指標           | Forex Factory RSS (無料)                    |
| ニュース           | OpenAI web_search機能（tools）              |
| インフラ           | Windows Server VPS (24時間稼働)             |
| ブローカー         | XMTrading                                   |

---

## 3. 運用パラメータ

```
【口座設定】
口座通貨:         JPY（円建て）
ブローカー:       XMTrading
デモ口座ログイン: 環境変数 MT5_LOGIN_DEMO
本番口座ログイン: 環境変数 MT5_LOGIN_LIVE
サーバー時間:     XMT (冬:GMT+2 / 夏:GMT+3)

【XMTの対象銘柄】（3銘柄・相関分散）
USDJPY - JPYクォート・流動性最高
EURUSD - USDクォート・USDJPYと逆相関
GOLD   - コモディティ・FXと異なる値動き(XAUUSD)

【時間軸】
エントリートリガー: M15 (15分足)
監視・管理:        H1 (1時間足)
フィルター:        H1トレンド確認

【リスク設定】
1トレードリスク:     口座残高の 1.0%
最大同時保有数:      3ポジション（1銘柄1ポジ原則）
最大総エクスポージャー: 口座残高の 3.0%
日次最大ドローダウン: 口座残高の 3.0%（CB発動閾値）
USD絡みポジ上限:     2
JPY絡みポジ上限:     1（GOLDはJPY絡みにカウントしない）
```

---

## 4. プロジェクト構造

```
trading_system/
├── config.py                      # 設定値一元管理・デモ/本番フラグ
├── main.py                        # FastAPIエントリーポイント・スケジューラー
│
├── core/
│   ├── models.py                  # データ型定義（Pydantic / Enum）
│   ├── broker_time.py             # XMTサーバー時間管理
│   ├── lot_calculator.py          # JPY口座専用ロット計算
│   ├── mt5_client.py              # MT5接続・注文管理
│   ├── thesis_db.py               # SQLite DB管理・自動パージ
│   └── risk_guardian.py           # リスク管理・サーキットブレーカー
│
├── ingestion/
│   ├── economic_calendar.py       # Forex Factory RSS取得
│   └── webhook_receiver.py        # TradingView Webhook受信・バリデーション
│
├── ai/
│   ├── prompt_builder.py          # プロンプト構築（エントリー/監視）
│   ├── entry_evaluator.py         # エントリー判断AI (GPT-4o)
│   └── position_monitor.py        # H1バッチ監視AI + 緊急判定
│
├── notifications/
│   └── discord_notifier.py        # Discord通知（レベル別フォーマット）
│
├── tests/
│   ├── test_lot_calculator.py     # ロット計算テスト（必須）
│   ├── test_broker_time.py        # XMT時間変換テスト
│   ├── test_risk_guardian.py      # CB発動テスト
│   ├── test_market_close.py       # 週末クローズ・DEAD_ZONEテスト
│   ├── test_semantic_validation.py # セマンティックバリデーションテスト
│   ├── test_correlation_alert.py  # 相関アラートテスト
│   └── mock_ai.py                 # モックAIクライアント（統合テスト用）
│
├── backtest/
│   └── thesis_backtester.py       # デモ検証用バックテストパイプライン
│
├── trading.db                     # SQLiteデータファイル
├── requirements.txt
└── .env                           # 機密情報（Gitに含めない）
```

---

## 5. 監視アーキテクチャ（3層構造）

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Pythonルールベース（AIなし・常時稼働）      │
│                                                     │
│  ・60秒ごとに価格チェック                            │
│  ・TP/SLの80%地点への到達を検知                     │
│  ・1本足でATR×2超の急変動を検知                     │
│  ・重要指標30分前フラグを検知                        │
│  ・条件合致でLayer 2をトリガー                       │
└──────────────────────┬──────────────────────────────┘
                       │ 条件合致時のみ
┌──────────────────────▼──────────────────────────────┐
│  Layer 2: GPT-4o-mini（緊急判定・安価・高速）         │
│                                                     │
│  ・「今すぐDiscord通知を送るか否か」の二択判定        │
│  ・出力: ALERT_HUMAN / CONTINUE_MONITORING          │
│  ・ALERT_HUMANならDiscordへ即時通知                  │
│  ・人間が判断してMT5を手動操作                       │
└──────────────────────┬──────────────────────────────┘
                       │ 独立して並行稼働
┌──────────────────────▼──────────────────────────────┐
│  Layer 3: GPT-4o（H1定期バッチ・高精度）              │
│                                                     │
│  ・H1足確定の1分後（毎時01分）に全ポジを一括査定      │
│  ・Thesis健全性評価 / TP更新 / 分割決済判断          │
│  ・判断結果をMT5へ反映 + Discordへログ送信           │
└─────────────────────────────────────────────────────┘
```

### AI呼び出しフォールバック（Layer 3）

```
GPT-4o
  ↓ タイムアウト(20秒) or エラー
GPT-4o-mini
  ↓ タイムアウト(15秒) or エラー
ルールベース（全ポジHOLD・新規エントリー停止・Discord通知）
```

---

## 6. コアワークフロー

### A. エントリーフェーズ

```
1. TradingView M15シグナル合致
   └─ Webhookで FastAPI に JSON送信

2. Python: 事前ガードチェック（RiskGuardian）
   ├─ システムステータス確認（ACTIVE?）
   ├─ 同一銘柄ポジション重複チェック
   ├─ 通貨集中リスクチェック
   ├─ 経済指標30分前チェック（ForexFactory）
   └─ NG → 即時reject（Discordに理由通知）

3. AI（GPT-4o）: エントリー評価
   入力: テクニカルシグナル + 口座状況 + XMT時間 + web_search（最新ニュース）
   出力:
   ├─ decision: APPROVE / REJECT / WAIT
   ├─ confidence: 0.0〜1.0
   ├─ thesis: 根拠テキスト（200字程度）
   ├─ invalidation_conditions: 根拠崩壊条件リスト（3件）
   ├─ initial_tp: 初期利確目標
   ├─ emergency_sl: AIが提案するSL
   ├─ risk_multiplier: 0.5〜1.5
   └─ market_regime: TRENDING / RANGING / HIGH_VOLATILITY / PRE_EVENT

4. Python: AI出力バリデーション
   ├─ TP値が現在値から5%以上乖離 → 棄却・前回値維持
   ├─ confidence < 0.6 → REJECT扱い
   └─ emergency_sl が物理SL範囲外 → 物理SL優先

5. MT5: 注文執行
   ├─ JPY口座ロット計算（LotCalculator）
   ├─ 注文送信（成行 + 物理SL + 初期TP）
   └─ Thesis DBに保存

6. Discord通知（🟢 エントリー承認）
```

### B. モニタリング・出口フェーズ

```
【H1バッチ（毎時01分・Layer 3）】
1. MT5から全アクティブポジション取得
2. ThesisDBからThesis情報を結合
3. GPT-4oに一括送信（共通コンテキスト1回 + 個別差分）
4. 各ポジションへの指示を実行:
   ├─ HOLD     → 何もしない
   ├─ UPDATE_TP → OrderModifyでTP更新
   ├─ PARTIAL_CLOSE → 50%決済
   └─ FULL_CLOSE → 全決済 + ThesisをCLOSEDに更新
5. 判断ログをreview_logに保存
6. Discord通知（🟢 査定完了ログ）

【Layer 1 → Layer 2（常時・60秒ごと）】
- 急変動検知 → GPT-4o-miniで「人間を呼ぶか」判定
- ALERT_HUMAN → Discord通知（🔴 手動介入要請）

【サーキットブレーカー（30秒ごと）】
- 日次DD 3%超 → 全ポジ強制決済 → システムLOCK
- Discord通知（🔴 緊急・CB発動）
```

---

## 7. モジュール設計仕様

### 7.1 `config.py` — 設定値一元管理

```python
"""
全モジュールはこのファイルからCONFIGをimportして使用すること。
デモ/本番の切り替えはIS_DEMO_MODEフラグ1つで完結する。
"""

import os
from dataclasses import dataclass
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
    MT5_LOGIN_DEMO: int = int(os.getenv("MT5_LOGIN_DEMO", 0))
    MT5_LOGIN_LIVE: int = int(os.getenv("MT5_LOGIN_LIVE", 0))
    MT5_PASSWORD:   str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER_DEMO: str = os.getenv("MT5_SERVER_DEMO", "XMTrading-Demo")
    MT5_SERVER_LIVE: str = os.getenv("MT5_SERVER_LIVE", "XMTrading-Real")

    # 使用する口座情報（IS_DEMO_MODEで自動選択）
    @property
    def MT5_LOGIN(self) -> int:
        return self.MT5_LOGIN_DEMO if self.IS_DEMO_MODE else self.MT5_LOGIN_LIVE

    @property
    def MT5_SERVER(self) -> str:
        return self.MT5_SERVER_DEMO if self.IS_DEMO_MODE else self.MT5_SERVER_LIVE

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    MODEL_MAIN:  str = "gpt-4o"        # エントリー評価・H1バッチ
    MODEL_FAST:  str = "gpt-4o-mini"   # 緊急判定・フォールバック

    # Discord
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

    # 対象銘柄
    SYMBOLS: list = ("USDJPY", "EURUSD", "GOLD")

    # リスク設定
    MAX_RISK_PER_TRADE_PCT:    float = 1.0
    MAX_DAILY_DRAWDOWN_PCT:    float = 3.0
    MAX_TOTAL_EXPOSURE_PCT:    float = 3.0
    MAX_POSITIONS:             int   = 3
    MAX_USD_EXPOSURE:          int   = 2
    MAX_JPY_EXPOSURE:          int   = 1

    # AI設定
    AI_TIMEOUT_MAIN_SEC:   int   = 20
    AI_TIMEOUT_FAST_SEC:   int   = 15
    AI_MIN_CONFIDENCE:     float = 0.6
    AI_MAX_TP_DEVIATION_PCT: float = 5.0  # TP異常値判定の乖離率

    # 監視間隔
    H1_BATCH_CRON_MINUTE:     int = 1    # 毎時何分に実行するか
    PRICE_CHECK_INTERVAL_SEC: int = 60
    DD_CHECK_INTERVAL_SEC:    int = 30

    # 指標前停止
    PRE_EVENT_STOP_MINUTES: int = 30

    # 市場クローズ・週末管理
    FRIDAY_ENTRY_CUTOFF_HOUR: int = 20       # 金曜XMT何時以降エントリー停止
    FRIDAY_FORCE_CLOSE_HOUR:  int = 22       # 金曜XMT何時に全決済実行
    FRIDAY_FINAL_CHECK_HOUR:  int = 22       # 金曜残存確認時
    FRIDAY_FINAL_CHECK_MINUTE: int = 30
    WEEKLY_OPEN_WAIT_MINUTES: int = 15       # 週明けスプレッド安定待機
    DEAD_ZONE_HARD_BLOCK: bool = True        # DEAD_ZONEをハードブロック

    # スプレッド制限（points単位）
    SPREAD_LIMITS_POINTS: dict = None  # __post_init__で設定

    # Webhook認証
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    DEDUP_WINDOW_SEC: int = 300              # 重複排除ウィンドウ（秒）

    # ヘルスチェック
    STATUS_API_TOKEN: str = os.getenv("STATUS_API_TOKEN", "")

    # DB設定
    DB_PATH: str = "trading.db"
    PURGE_REVIEW_LOG_DAYS:  int = 90
    PURGE_API_COST_DAYS:    int = 30
    PURGE_AI_AUDIT_DAYS:    int = 14
    PURGE_THESIS_DAYS:      int = 180
    DB_SIZE_ALERT_MB:       int = 50     # DB肥大化アラート閾値
    AI_AUDIT_RESPONSE_MAX_CHARS: int = 4000  # full_response保存上限

    # WAITハンドリング
    WAIT_TTL_MINUTES:         int = 15   # WAIT有効期限（M15足1本分）
    WAIT_MAX_RETRIES:         int = 2    # WAIT再チェック回数
    WAIT_RECHECK_INTERVAL_SEC: int = 300 # 再チェック間隔（5分）

    # web_searchフォールバック
    WEB_SEARCH_TIMEOUT_SEC: int = 10     # web_search追加タイムアウト

    # マルチインスタンス防止
    PID_FILE_PATH: str = "trading_system.pid"

CONFIG = TradingConfig()
```

**`.env` ファイルテンプレート:**

```
MT5_LOGIN_DEMO=12345678
MT5_LOGIN_LIVE=87654321
MT5_PASSWORD=your_password
MT5_SERVER_DEMO=XMTrading-Demo
MT5_SERVER_LIVE=XMTrading-Real3
OPENAI_API_KEY=sk-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
WEBHOOK_SECRET=your_random_secret_token_here
STATUS_API_TOKEN=your_status_api_token_here
```

---

### 7.1b データ型定義（`core/models.py`）

**責務**: システム全体で使用するデータ構造を型安全に定義する。
モジュール間の受け渡しはすべてこれらの型を使用し、dictの暗黙的なキーアクセスを排除する。

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum
from datetime import datetime


# ─── Enum定義 ───
class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

class Decision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    WAIT = "WAIT"

class ThesisStatus(str, Enum):
    VALID = "VALID"
    WEAKENING = "WEAKENING"
    BROKEN = "BROKEN"

class PositionAction(str, Enum):
    HOLD = "HOLD"
    UPDATE_TP = "UPDATE_TP"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"

class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    PRE_EVENT = "PRE_EVENT"

class SystemStatus(str, Enum):
    ACTIVE = "ACTIVE"
    MONITOR_ONLY = "MONITOR_ONLY"
    FRIDAY_CUTOFF = "FRIDAY_CUTOFF"
    WEEKEND_CLOSED = "WEEKEND_CLOSED"
    LOCKED = "LOCKED"


# ─── Webhook受信データ ───
class WebhookPayload(BaseModel):
    """
    TradingViewから受信するWebhook JSON。
    カスタムインジケータは全フィールドを含む。
    外部インジケータ（LuxAlgo, Lorentzian等）は部分フィールドのみ。
    不足データはwebhook_receiver.pyがMT5から補完する。
    """
    secret: str
    symbol: str
    direction: Direction
    timeframe: str = "M15"
    h1_trend: str = ""               # BULLISH / BEARISH / ""（外部インジケータは空）
    price: float = Field(gt=0)
    pattern: str                     # EMA_CROSS, BREAKOUT, FVG_FILL, LORENTZIAN等

    # テクニカルコンテキスト（Optional: 外部インジケータではNone → サーバー側で補完）
    ema21: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    rsi: Optional[float] = Field(default=None, ge=0, le=100)
    atr: Optional[float] = Field(default=None, gt=0)
    atr_ratio: Optional[float] = None
    volume_ratio: float = 1.0

    # 追加コンテキスト（カスタムインジケータのみ）
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    dc_upper: Optional[float] = None
    dc_lower: Optional[float] = None

    # メタデータ
    broker_time: str = ""            # TradingViewからの時刻文字列（参考値）
    source: str = "custom_multistrat"  # custom_multistrat / luxalgo_fvg / luxalgo_sweep / lorentzian

    @field_validator("symbol")
    @classmethod
    def symbol_must_be_valid(cls, v):
        # XMTではGOLD（TradingViewではXAUUSDの場合もある）
        symbol_map = {"XAUUSD": "GOLD"}
        v = symbol_map.get(v, v)
        valid = ("USDJPY", "EURUSD", "GOLD")
        if v not in valid:
            raise ValueError(f"無効なsymbol: {v} (有効: {valid})")
        return v

    def needs_supplement(self) -> bool:
        """MT5からのデータ補完が必要か判定"""
        return self.rsi is None or self.atr is None or self.ema21 is None


# ─── MT5注文結果 ───
class OrderResult(BaseModel):
    """MT5の注文・決済結果"""
    success: bool
    ticket: Optional[int] = None       # MT5チケット番号
    symbol: str = ""
    direction: Optional[Direction] = None
    lot: float = 0.0
    price: float = 0.0                 # 約定価格
    requested_price: float = 0.0       # 要求価格
    slippage_points: float = 0.0       # スリッページ（points）
    retcode: int = 0                   # MT5リターンコード
    error: Optional[str] = None        # エラーメッセージ
    comment: str = ""


# ─── AIエントリー評価レスポンス ───
class AIEntryResponse(BaseModel):
    """GPT-4oのエントリー評価JSON出力"""
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    invalidation_conditions: list[str] = Field(min_length=1)
    initial_tp: float
    emergency_sl: float
    risk_multiplier: float = Field(ge=0.5, le=1.5, default=1.0)
    market_regime: MarketRegime
    reject_reason: Optional[str] = None

    # 内部フラグ（AI応答には含まれない・パイプライン内で付与）
    _web_search_failed: bool = False
    _fallback_model: bool = False

    @field_validator("invalidation_conditions")
    @classmethod
    def must_have_three_conditions(cls, v):
        if len(v) < 3:
            raise ValueError(f"invalidation_conditionsは3件必要（現在{len(v)}件）")
        return v


# ─── AIポジション監視レスポンス ───
class AIPositionInstruction(BaseModel):
    """H1バッチ監視の個別ポジション指示"""
    trade_id: str
    thesis_status: ThesisStatus
    action: PositionAction
    new_tp: Optional[float] = None
    close_percentage: int = Field(ge=0, le=100, default=0)
    reasoning: str
    urgency: str = "NORMAL"          # NORMAL / HIGH

class AIH1BatchResponse(BaseModel):
    """H1バッチ監視のGPT-4o出力"""
    positions: list[AIPositionInstruction]


# ─── AI緊急判定レスポンス ───
class AIEmergencyResponse(BaseModel):
    """GPT-4o-miniの緊急判定出力"""
    action: str                      # ALERT_HUMAN / CONTINUE_MONITORING
    reason: str


# ─── ポジション情報（MT5 + Thesis統合） ───
class PositionInfo(BaseModel):
    """MT5ポジションとThesis情報を統合した構造体"""
    trade_id: str
    ticket: int
    symbol: str
    direction: Direction
    lot_size: float
    entry_price: float
    current_price: float = 0.0
    pnl_pips: float = 0.0
    pnl_jpy: float = 0.0
    initial_tp: float
    emergency_sl: float
    thesis_text: str
    invalidation_conditions: list[str]
    risk_multiplier: float
    market_regime: str
    ai_confidence: float
    hold_hours: float = 0.0
    broker_entry_time: str
    spread_at_entry: float = 0.0


# ─── 相関アラート結果 ───
class CorrelationAlert(BaseModel):
    """相関チェックの結果"""
    has_alert: bool
    alert_level: str = "NONE"        # NONE / WARNING / HIGH
    overlapping_group: Optional[str] = None
    recommended_risk_multiplier: float = 1.0
    message: str = ""
```

> **使用ガイドライン**:
> - Webhook受信: `WebhookPayload(**payload_dict)` でバリデーション込みでパース
> - AI応答: `AIEntryResponse(**json.loads(response))` で形式バリデーション自動実施
> - MT5注文後: `OrderResult(success=True, ticket=result.order, ...)` で構造化
> - `field_validator` によりPydanticレベルで不正値を弾く（confidence範囲、SL/TP等）
> - セマンティックバリデーション（Section 9）はPydanticバリデーション**通過後**に実施

---

### 7.2 `core/broker_time.py` — XMT時間管理

**責務**: システム全体のdatetimeをXMTサーバー時間で統一する。

```
【XMTサーバー時間仕様】
冬時間: GMT+2（11月第1日曜 〜 3月第2日曜）
夏時間: GMT+3（3月第2日曜 〜 11月第1日曜）
切替: 欧州夏時間（CEST）に準拠、UTC 01:00に切替

【日本時間との差】
冬時間: JST(UTC+9) - 7時間 = XMT
夏時間: JST(UTC+9) - 6時間 = XMT

【重要】
- 日本時間でのサマータイム変換は禁止
- 全てのdatetime保存・比較はXMTベースで行うこと
- ログファイルへの出力も XMT表記を付与すること（例: 2024-01-15 14:30:00 XMT+2）
```

**実装すべきメソッド:**

- `BrokerTime.now() -> datetime` : 現在のXMT時刻を返す
- `BrokerTime.now_str() -> str` : ログ用文字列（タイムゾーン表記付き）
- `BrokerTime.is_dst() -> bool` : 現在夏時間かどうか
- `BrokerTime.get_session() -> str` : 現在の市場セッションを返す
- `BrokerTime.from_utc(utc_dt) -> datetime` : UTC → XMT変換
- `BrokerTime.to_utc(xmt_dt) -> datetime` : XMT → UTC変換（MT5 API用）
- `BrokerTime.today_start() -> datetime` : 今日のXMT 00:00:00 を返す（日次PnL計算用）

**セッション定義（XMT時間基準）:**

```
00:00〜06:59  SYDNEY_TOKYO
07:00〜08:59  TOKYO_LONDON_OVERLAP
09:00〜11:59  LONDON
12:00〜15:59  LONDON_NY_OVERLAP  ← 最重要・最高ボラ
16:00〜21:59  NEW_YORK
22:00〜23:59  DEAD_ZONE          ← 新規エントリー非推奨
```

---

### 7.3 `core/lot_calculator.py` — JPY口座専用ロット計算

**責務**: JPY口座でのロットサイズを正確に計算する。USD建て計算の混入を防ぐ。

**重要設計方針: `mt5.symbol_info()` による動的パラメータ取得**

> ハードコードされたpip値・コントラクトサイズは使用しない。
> ブローカー（XMTrading）の実値を `mt5.symbol_info()` で取得し、計算に使用する。
> これにより銘柄追加時のpip定義ミスを根本的に防止する。

```python
def get_symbol_params(symbol: str) -> dict:
    """MT5からシンボルの取引パラメータを動的に取得する"""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"symbol_info取得失敗: {symbol}")

    return {
        "trade_contract_size": info.trade_contract_size,  # 1lotあたりの単位数
        "point": info.point,                               # 最小価格変動幅
        "digits": info.digits,                             # 小数点桁数
        "volume_min": info.volume_min,                     # 最小ロット
        "volume_max": info.volume_max,                     # 最大ロット
        "volume_step": info.volume_step,                   # ロット刻み幅
        "currency_profit": info.currency_profit,           # 利益通貨（USD等）
    }

# 起動時にキャッシュし、毎時更新（ブローカー側の変更に追従）
_symbol_cache: dict[str, dict] = {}
_cache_updated_at: datetime = None
CACHE_TTL_SEC: int = 3600  # 1時間
```

```
【pip値計算（mt5.symbol_info()ベース・JPY口座向け）】

■ 共通計算ロジック:
  pip_size = point × 10  （1pip = 10points で統一）
  pip_value_in_profit_ccy = trade_contract_size × pip_size

  利益通貨がJPYの場合:
    pip_value_jpy = pip_value_in_profit_ccy（そのまま）

  利益通貨がUSDの場合:
    pip_value_jpy = pip_value_in_profit_ccy × USDJPY現在値

■ 参考値（検証用・ハードコードではなくアサーション用途）:
  USDJPY: contract=100,000 / point=0.001 / pip_size=0.01 → 1,000円/lot/pip
  EURUSD: contract=100,000 / point=0.00001 / pip_size=0.0001 → 約1,500円/lot/pip(USDJPY=150時)
  GOLD:   contract=100    / point=0.01   / pip_size=0.1  → 約1,500円/lot/pip(USDJPY=150時)

【ロット計算式】
risk_jpy = 口座残高(JPY) × (risk_pct / 100) × risk_multiplier
lot = risk_jpy / (sl_pips × pip_value_jpy)
最終lot = symbol_info の volume_min〜volume_max 範囲内、volume_step で丸め
```

```python
def calculate(symbol: str, sl_pips: float, risk_pct: float,
              balance_jpy: float, risk_multiplier: float = 1.0) -> float:
    params = get_symbol_params(symbol)
    pip_size = params["point"] * 10
    pip_value_raw = params["trade_contract_size"] * pip_size

    # 利益通貨 → JPY変換
    if params["currency_profit"] == "JPY":
        pip_value_jpy = pip_value_raw
    else:
        # USDなど外貨 → USDJPY等で円換算
        conversion_rate = _get_jpy_conversion_rate(params["currency_profit"])
        pip_value_jpy = pip_value_raw * conversion_rate

    risk_jpy = balance_jpy * (risk_pct / 100) * risk_multiplier
    lot = risk_jpy / (sl_pips * pip_value_jpy)

    # ブローカー制限に丸め
    lot = max(params["volume_min"], min(params["volume_max"], lot))
    lot = round(lot / params["volume_step"]) * params["volume_step"]
    return round(lot, 2)

def _get_jpy_conversion_rate(currency: str) -> float:
    """利益通貨をJPY変換するレートを取得"""
    if currency == "JPY":
        return 1.0
    pair = f"{currency}JPY"  # 例: USDJPY
    tick = mt5.symbol_info_tick(pair)
    if tick is None:
        raise ValueError(f"JPY変換レート取得失敗: {pair}")
    return (tick.bid + tick.ask) / 2  # mid price
```

**実装すべきメソッド:**

- `calculate(symbol, sl_pips, risk_pct, balance_jpy, risk_multiplier) -> float`
- `verify_calculation(symbol, lot, sl_pips) -> dict` : 注文前の最終サニティチェック
  - リスク率が3%を超えていたらエラーログ + Discord通知
  - 必ず注文執行前に呼び出すこと

**テスト必須ケース:**

```python
# USDJPY: 残高50万円, SL50pips, リスク1%
# 期待値: 0.1lot（±0.02以内）

# EURUSD: 残高50万円, SL40pips, リスク1%, USDJPY=150
# 期待値: 約0.08lot

# GOLD: 残高50万円, SL200pips($2), リスク1%, USDJPY=150
# 期待値: 約0.16lot
```

---

### 7.4 `core/mt5_client.py` — MT5接続管理

**責務**: MT5との全通信を担う。注文・決済・情報取得・自動再接続を提供する。

**初期化:**

- `connect()` : CONFIG.IS_DEMO_MODEに従いデモ/本番を自動選択
- 接続成功時にDiscordへ通知（口座番号・残高・デモ/本番の明示）
- 接続失敗時はシステム起動を中止

**自動再接続（ランタイム切断対応）:**

```python
# VPS上のMT5は頻繁に切断されるため、堅牢な再接続が必須
MAX_RECONNECT_ATTEMPTS: int = 5
RECONNECT_BACKOFF_SEC: list = [2, 5, 10, 30, 60]  # 指数バックオフ

async def ensure_connection(self) -> bool:
    """全MT5操作の前に呼び出す。切断時は自動再接続を試みる。"""
    if mt5.terminal_info() is not None:
        return True
    for attempt, wait in enumerate(RECONNECT_BACKOFF_SEC):
        logger.warning(f"MT5切断検知。再接続試行 {attempt+1}/{MAX_RECONNECT_ATTEMPTS}")
        if self.connect():
            await notifier.send("MT5再接続成功", level="INFO")
            return True
        await asyncio.sleep(wait)
    await notifier.send("MT5再接続失敗（5回試行）", level="CRITICAL")
    return False
```

**MT5操作のロック機構（並行処理の競合防止）:**

```python
import asyncio
_mt5_lock = asyncio.Lock()

async def open_position(self, ...):
    async with _mt5_lock:
        if not await self.ensure_connection():
            return None
        # 注文実行ロジック
```

> H1バッチ・Layer1価格チェック・CB全決済が同時に走る可能性がある。
> 全MT5操作を `_mt5_lock`で直列化し、操作の衝突を防止する。

**注文関連:**

- `open_position(symbol, direction, lot, sl_price, tp_price, comment) -> OrderResult`
  - commentにtrade_id（先頭8文字）を含めること（MT5での識別用）
  - deviation（スリッページ）は10 pointsに固定
  - magic numberは固定値（例: 20250101）
  - **注文前にスプレッドチェックを実施**（SPREAD_LIMITS_POINTS超過なら拒否）
  - **実行後にスリッページを記録**（requested_price vs filled_price）
- `modify_position(ticket, new_sl, new_tp) -> bool`
- `close_position(ticket, percentage=100) -> OrderResult`
  - 50%決済時はvolume_minを下回らないよう注意
  - **volume_min下回る場合はFULL_CLOSEにフォールバック**
- `close_all_positions() -> list[OrderResult]` : CB発動時 / 週末前全決済用

**情報取得:**

- `get_all_positions() -> list[PositionInfo]` : 全アクティブポジション
- `get_position_by_ticket(ticket) -> Optional[PositionInfo]` : 個別ポジション
- `get_account_balance() -> float` : 残高（JPY）
- `get_daily_pnl() -> float` : 今日の実現+含み損益（JPY）
- `get_ohlcv(symbol, timeframe, count) -> pd.DataFrame` : ローソク足取得
- `get_spread(symbol) -> float` : 現在スプレッド取得（points単位）

**`get_daily_pnl()` 実装設計:**

> MT5には「今日のPnL」を返す単一APIが存在しない。
> 「今日の実現損益」+「現在の含み損益」を合算して算出する。

```python
from datetime import datetime, timedelta
import MetaTrader5 as mt5

async def get_daily_pnl(self) -> float:
    """
    今日の日次損益合計（JPY）を返す。

    計算方法:
      daily_pnl = 実現損益（今日決済分） + 含み損益（保有中ポジション）

    実現損益: mt5.history_deals_get() で本日の決済済みDealを取得し、
                 deal.profit + deal.swap + deal.commission を合計。
    含み損益: mt5.positions_get() で保有中ポジションの position.profit を合計。
    """
    async with _mt5_lock:
        if not await self.ensure_connection():
            return 0.0

        # ─── 実現損益（今日XMT 00:00以降の決済分） ───
        today_start = BrokerTime.today_start()  # 今日XMT 00:00:00 のUTC変換値
        today_start_utc = BrokerTime.to_utc(today_start)

        deals = mt5.history_deals_get(
            today_start_utc,
            datetime.utcnow() + timedelta(hours=1)  # 将来側に余裕を持たせる
        )

        realized_pnl = 0.0
        if deals:
            for deal in deals:
                # DEAL_ENTRY_OUT (1) または DEAL_ENTRY_INOUT (2) = 決済取引
                if deal.entry in (1, 2):
                    realized_pnl += deal.profit + deal.swap + deal.commission

        # ─── 含み損益（保有中ポジション） ───
        positions = mt5.positions_get()
        floating_pnl = 0.0
        if positions:
            for pos in positions:
                floating_pnl += pos.profit + pos.swap

        return realized_pnl + floating_pnl
```

> **重要ノート**:
> - `mt5.history_deals_get()` はUTC時刻を受け取るため、`BrokerTime.to_utc()` で変換が必要
> - `deal.entry == 1` (OUT) でフィルタリングし、新規エントリー（entry==0）を除外
> - `deal.commission` と `deal.swap` も含めて実際のコストを反映
> - サーキットブレーカーのDD判定にも使用されるため、正確性が最重要
> - `broker_time.py` に `today_start()` と `to_utc()` メソッドの追加が必要

**エラーハンドリング:**

- MT5エラーコードをすべてログ出力
- retcode != TRADE_RETCODE_DONE の場合はDiscordへ警告通知
- 接続切断時は自動再接続を試行（上記参照）

---

### 7.5 `core/thesis_db.py` — DBスキーマ・自動パージ

**責務**: Thesisの永続化・査定ログ・コスト追跡・AI監査証跡・自動メンテナンス。

**SQLite並行アクセス設計（Dedicated Writerパターン）:**

> APSchedulerのH1バッチ・CBチェック・Webhook処理が同時にDBへ書き込む可能性がある。
> SQLiteはWALモードでも同時書き込みはエラーになるため、Dedicated Writerで直列化する。

```python
import asyncio
import aiosqlite

class ThesisDB:
    """全DB書き込みを単一のasyncioキューで直列化する"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._write_queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: asyncio.Task = None

    async def start(self):
        """起動時にライタータスクを開始"""
        self._conn = await aiosqlite.connect(
            self.db_path,
            timeout=30,
        )
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self):
        """単一ループで全書き込みを逐次処理（並行書き込み衝突を根絶）"""
        while True:
            sql, params, future = await self._write_queue.get()
            try:
                await self._conn.execute(sql, params)
                await self._conn.commit()
                future.set_result(True)
            except Exception as e:
                future.set_result(False)
                logger.error(f"DB書き込みエラー: {e}")
                await notifier.send(f"⚠️ DB書き込み失敗: {e}", level="WARNING")

    async def write(self, sql: str, params: tuple = ()) -> bool:
        """書き込みリクエストをキューに投入し、完了を待つ"""
        future = asyncio.get_event_loop().create_future()
        await self._write_queue.put((sql, params, future))
        return await future

    async def read(self, sql: str, params: tuple = ()) -> list:
        """読み取りはstart()で確立した永続接続を再利用（WALモードなら読み取りは並行OK）"""
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()

    async def close(self):
        """シャットダウン時に接続をクローズ"""
        if self._writer_task:
            self._writer_task.cancel()
        if self._conn:
            await self._conn.close()
```

> **設計根拠**: `read()` が毎回新規接続を作成すると、H1バッチ（毎時01分×全ポジション読み取り）で
> 接続オーバーヘッドが累積する。`start()` で確立した `self._conn` を再利用することで、
> 接続コストを零にする。WALモードでは読み取りは書き込みと並行実行可能なため、
> ライタータスクと同一接続でも問題ない。
>
> `close()` メソッドを追加し、グレースフルシャットダウン時に接続を正しく解放する。

**テーブル構成:**

```sql
-- thesis: アクティブなThesis（ACTIVE中は絶対に削除しない）
thesis (
    trade_id TEXT PK,    -- UUID
    ticket INTEGER,      -- MT5チケット番号
    symbol TEXT,
    direction TEXT,      -- LONG / SHORT
    technical_ctx TEXT,  -- JSON: RSI/ATR/EMA値等
    fundamental_ctx TEXT,-- JSON: ニュースヘッドライン
    thesis_text TEXT,    -- AIが生成した根拠テキスト
    invalidation TEXT,   -- JSON配列: 根拠崩壊条件3件
    entry_price REAL,
    initial_tp REAL,
    emergency_sl REAL,
    risk_multiplier REAL,
    market_regime TEXT,
    ai_confidence REAL,
    lot_size REAL,
    status TEXT,         -- ACTIVE / CLOSED
    broker_time TEXT,    -- XMT時間文字列
    created_at TEXT,
    updated_at TEXT
)

-- review_log: H1査定の記録（90日でパージ）
review_log (
    id INTEGER PK AUTOINCREMENT,
    trade_id TEXT FK,
    thesis_status TEXT,  -- VALID / WEAKENING / BROKEN
    action TEXT,         -- HOLD / UPDATE_TP / PARTIAL_CLOSE / FULL_CLOSE
    old_tp REAL,
    new_tp REAL,
    reasoning TEXT,
    ai_model_used TEXT,
    tokens_used INTEGER,
    broker_time TEXT,
    created_at TEXT
)

-- trade_history: 完結済みトレード（永続保存・分析資産）
trade_history (
    trade_id TEXT PK,
    symbol TEXT,
    direction TEXT,
    entry_price REAL,
    exit_price REAL,
    exit_reason TEXT,            -- TP_HIT / THESIS_BROKEN / SL_HIT / MANUAL / CB / WEEKEND_CLOSE / DAILY_CLOSE / HOLIDAY_CLOSE
    pnl_pips REAL,
    pnl_jpy REAL,                -- JPY建てで記録
    lot_size REAL,
    thesis_text TEXT,
    ai_confidence REAL,
    spread_at_entry REAL,        -- エントリー時のスプレッド（points）
    slippage_points REAL,        -- 実際のスリッページ（points）
    thesis_broken_at TEXT,       -- THESIS_BROKENの場合のXMT時間
    price_at_thesis_broken REAL,
    thesis_broken_correct INTEGER, -- 0/1（後から手動入力）
    broker_entry_time TEXT,
    broker_exit_time TEXT,
    hold_hours REAL,
    created_at TEXT
)

-- circuit_breaker_log: CB記録（永続保存）
circuit_breaker_log (
    id INTEGER PK AUTOINCREMENT,
    reason TEXT,
    daily_pnl_jpy REAL,
    dd_pct REAL,
    broker_time TEXT,
    created_at TEXT
)

-- api_cost_log: APIコスト追跡（30日でパージ）
api_cost_log (
    id INTEGER PK AUTOINCREMENT,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,      -- 概算（GPT-4o: $2.5/1Mtokens_in, $10/1Mtokens_out）
    purpose TEXT,       -- ENTRY_EVAL / H1_MONITOR / EMERGENCY / ENTRY_REJECTED
    broker_time TEXT,
    created_at TEXT
)

-- ai_audit_log: AI判断の完全監査証跡（14日でパージ・DBサイズ管理）
ai_audit_log (
    id INTEGER PK AUTOINCREMENT,
    request_id TEXT,         -- UUID: リクエスト識別子
    purpose TEXT,            -- ENTRY_EVAL / H1_MONITOR / EMERGENCY
    model TEXT,              -- gpt-4o / gpt-4o-mini
    prompt_summary TEXT,     -- プロンプトの要約（先頭500文字）
    full_response TEXT,      -- AIの生のJSON応答（全文）
    parsed_decision TEXT,    -- APPROVE / REJECT / WAIT / HOLD / etc.
    validation_result TEXT,  -- PASS / FAIL_CONFIDENCE / FAIL_TP / etc.
    was_overridden INTEGER,  -- 0/1: バリデーションで上書きされたか
    override_reason TEXT,    -- 上書き理由（confidence低すぎ、TP異常等）
    latency_ms INTEGER,      -- API応答時間（ミリ秒）
    tokens_in INTEGER,
    tokens_out INTEGER,
    trade_id TEXT,           -- 関連するtrade_id（あれば）
    broker_time TEXT,
    created_at TEXT
)
```

**自動パージ（毎日 XMT 00:05 に実行）:**

```
review_log:    90日以上前を削除
api_cost_log:  30日以上前を削除
ai_audit_log:  14日以上前を削除（full_responseが容量の主因のため短めに）
thesis:        status='CLOSED' かつ 180日以上前を削除
               ※ ACTIVEは絶対に削除しない
VACUUM:        パージ後に必ず実行してファイルサイズを縮小
```

**ai_audit_logサイズ管理:**

```
・14日保持で約2MB/月（H1バッチ720回×1KB + エントリー30回×2KB）
・full_responseの最大保存長: 4,000文字（超過分は切り捨て）
・緊急時のDB肥大化対策: DB全体が50MBを超えた場合、
  ai_audit_logを7日保持に自動短縮 + Discord WARNING通知
```

**`get_db_stats() -> dict`** : 毎朝Discordの日次レポートに含めること

```
{
  "thesis_active": N,
  "review_log_count": N,
  "trade_history_count": N,
  "monthly_cost_usd": X.XX
}
```

---

### 7.6 `core/risk_guardian.py` — リスク管理

**責務**: システム全体のリスクを監視し、破綻を防止する。

**システムステータス:**

```
ACTIVE          - 通常稼働（新規エントリー可）
MONITOR_ONLY    - 新規エントリー停止、既存ポジ管理のみ
FRIDAY_CUTOFF   - 金曜エントリー停止（既存ポジ管理+週末決済待ち）
WEEKEND_CLOSED  - 市場クローズ中（全ポジ決済済み・週明け待ち）
LOCKED          - 全機能停止（CB発動後・手動解除必須）
```

**ガードチェック一覧（30秒ごとに全実行）:**

| ガード             | 条件                 | アクション                          |
| ------------------ | -------------------- | ----------------------------------- |
| 日次DDチェック     | DD ≥ 3.0%           | CB発動（全決済+LOCKED）             |
| 総エクスポージャー | リスク合計 > 3.0%    | MONITOR_ONLY                        |
| ポジション数       | ≥ 3                 | MONITOR_ONLY                        |
| USD集中            | USD絡み ≥ 3         | 新規エントリー拒否                  |
| JPY集中            | JPY絡み ≥ 2         | 新規エントリー拒否                  |
| DEAD_ZONEブロック  | XMT 22:00-23:59      | 新規エントリー強制拒否              |
| 金曜カットオフ     | 金曜 XMT 20:00以降   | 新規エントリー全拒否(FRIDAY_CUTOFF) |
| 週末前強制決済     | 金曜 XMT 22:00       | 全ポジ成行決済                      |
| スプレッド異常     | SPREAD_LIMITS超過    | 新規エントリー拒否                  |
| 市場クローズ       | 各銘柄のクローズ時間 | 新規エントリー拒否                  |

**`can_enter_new_trade(symbol, direction) -> tuple[bool, str]`:**
エントリー前の事前チェック。Falseの場合は理由文字列を返す。

**相関アラートロジック（通貨集中リスクの動的検知）:**

> 同方向USD建てポジションの集中を検知し、AIのrisk_multiplier調整に使用する。
> 完全なピアソン相関計算はコストに見合わないため、通貨ペア構成ベースの簡易方式を採用。

```python
# 相関グループ定義（共通通貨による影響度マッピング）
CORRELATION_GROUPS = {
    "USD_LONG": ["USDJPY_LONG", "EURUSD_SHORT", "GOLD_SHORT"],
    "USD_SHORT": ["USDJPY_SHORT", "EURUSD_LONG", "GOLD_LONG"],
    "RISK_ON": ["USDJPY_LONG", "EURUSD_LONG"],    # リスクオン方向
    "RISK_OFF": ["USDJPY_SHORT", "GOLD_LONG"],   # リスクオフ方向
}

def check_correlation_alert(
    new_symbol: str,
    new_direction: str,
    active_positions: list[dict]
) -> dict:
    """
    新規エントリーが既存ポジションと同一方向リスクに集中していないか検知

    Returns:
        {
            "has_alert": bool,
            "alert_level": "NONE" | "WARNING" | "HIGH",
            "overlapping_group": str | None,
            "recommended_risk_multiplier": float,  # 0.5〜1.0
            "message": str
        }
    """
    new_key = f"{new_symbol}_{new_direction}"
    overlap_count = 0
    overlapping_groups = []

    for group_name, members in CORRELATION_GROUPS.items():
        if new_key not in members:
            continue
        # この相関グループに属する既存ポジションを数える
        existing_in_group = sum(
            1 for pos in active_positions
            if f"{pos['symbol']}_{pos['direction']}" in members
        )
        if existing_in_group > 0:
            overlap_count += existing_in_group
            overlapping_groups.append(group_name)

    if overlap_count == 0:
        return {"has_alert": False, "alert_level": "NONE",
                "overlapping_group": None,
                "recommended_risk_multiplier": 1.0,
                "message": "相関リスクなし"}
    elif overlap_count == 1:
        return {"has_alert": True, "alert_level": "WARNING",
                "overlapping_group": overlapping_groups[0],
                "recommended_risk_multiplier": 0.75,
                "message": f"相関警告: {overlapping_groups[0]}方向に集中(既存1ポジ)"}
    else:
        return {"has_alert": True, "alert_level": "HIGH",
                "overlapping_group": ",".join(overlapping_groups),
                "recommended_risk_multiplier": 0.5,
                "message": f"高相関警告: {overlap_count}ポジが同方向リスク"}
```

> **運用ルール**: `has_alert=True` の場合:
> 1. AIプロンプトに `correlation_alert` として渡す（AIがrisk_multiplier調整）
> 2. alert_level="HIGH" の場合は Discord WARNING通知も送信
> 3. AIが `recommended_risk_multiplier` を無視して高めに設定した場合、
>    コード側で `min(ai_multiplier, recommended_risk_multiplier)` を強制適用

**サーキットブレーカー発動時の処理:**

1. ステータスを LOCKED に変更
2. 全ポジション成行決済（失敗してもログして継続）
3. circuit_breaker_log に記録
4. Discord に 🔴 緊急通知
5. LOCKEDのまま自動復旧しない（翌日手動解除）

---

### 7.7 `ingestion/economic_calendar.py` — 経済指標カレンダー

**責務**: Forex FactoryのRSSから重要指標を取得し、エントリー停止フラグを管理する。

**データソース:** `https://nfs.faireconomy.media/ff_calendar_thisweek.xml`（RSS）

**取得・フィルター条件:**

- 重要度: 高（★★★・red）のみ対象
- 対象通貨: USD, EUR, GBP, JPY, XAU（対象銘柄の構成通貨）
- キャッシュ: 1時間ごとに再取得

**実装すべきメソッド:**

- `is_high_impact_event_soon(minutes_ahead=30) -> tuple[bool, str]`今から30分以内に重要指標があれば (True, "イベント名 XMT時間") を返す
- `get_todays_events() -> list[dict]`
  今日の重要指標一覧（XMT時間に変換済み）をDiscordモーニングレポートに含める

**注意**: 取得失敗時は (False, "") を返し、システムを止めない。エラーはログのみ。

---

### 7.8 `ingestion/webhook_receiver.py` — TradingView Webhook受信

**責務**: TradingViewからのWebhook JSONを受信・認証・バリデーション・重複排除する。

**FastAPIエンドポイント:** `POST /webhook/tradingview`

**認証（必須）:**

```python
# TradingViewのAlert MessageにシークレットトークンをJSON内に含める
# .envに WEBHOOK_SECRET を設定
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

# バリデーション
def verify_webhook(payload: dict) -> bool:
    return payload.get("secret") == CONFIG.WEBHOOK_SECRET
    # 不一致 → 403 Forbidden + ログに送信元IPを記録
```

**受信JSONスキーマ:**

カスタムインジケータと外部インジケータで含まれるフィールドが異なる。
詳細は Section 8「Webhook送信JSON」および Section 7.1b `WebhookPayload` モデルを参照。

```python
# カスタムインジケータ: 全フィールド含む（rsi, atr, ema21等）
# 外部インジケータ:     部分フィールド（rsi, atr, ema21等はNone）
# → needs_supplement() で判定し、MT5からデータ補完
```

**バリデーション:**

- シークレットトークン一致確認（最優先）
- 必須フィールドの存在チェック
- symbolがCONFIG.SYMBOLSに含まれているか
- directionが LONG / SHORT のいずれか
- price, rsi, atr が合理的な範囲内か（0以上など）
- Webhookは即座に200 OKを返し、処理は `asyncio.create_task()` で非同期実行

**重複排除（Deduplication）:**

```python
# TradingViewは同一アラートを複数回送信することがある
# symbol + direction + price + timeframe のハッシュを5分間キャッシュ
import hashlib
from datetime import datetime, timedelta

_recent_signals: dict[str, datetime] = {}
DEDUP_WINDOW_SEC: int = 300  # 5分

def is_duplicate(payload: dict) -> bool:
    key = hashlib.md5(
        f"{payload['symbol']}:{payload['direction']}:{payload['price']}".encode()
    ).hexdigest()
    now = datetime.utcnow()
    if key in _recent_signals and (now - _recent_signals[key]).seconds < DEDUP_WINDOW_SEC:
        return True  # 重複 → 破棄
    _recent_signals[key] = now
    # 古いキーをクリーンアップ
    _recent_signals = {k: v for k, v in _recent_signals.items()
                       if (now - v).seconds < DEDUP_WINDOW_SEC}
    return False
```

**スプレッドチェック（エントリー前に実施）:**

```python
# entry_evaluator.py内で実施するが、webhookパイプラインの一部として記載
# MT5からリアルタイムスプレッドを取得し、通常値の2倍以上なら拒否

SPREAD_LIMITS_POINTS: dict = {
    "USDJPY": 30,   # 通常1.5pips → 3.0pipsで拒否
    "EURUSD": 25,   # 通常1.2pips → 2.5pipsで拒否
    "GOLD":   50,   # 通常2.5pips → 5.0pipsで拒否
}

def is_spread_acceptable(symbol: str) -> tuple[bool, float]:
    """
    現在のスプレッドが許容範囲内か判定する。

    【単位正規化の重要な注意】
    tick.ask - tick.bid は「価格差」であり、「points」ではない。
    例: USDJPY の ask=150.030, bid=150.015 の場合
         価格差 = 0.015
         symbol_info.point = 0.001
         points = 0.015 / 0.001 = 15 points

    SPREAD_LIMITS_POINTS は points 単位で定義されているため、
    必ず symbol_info.point で割って正規化すること。

    Returns: (許容範囲内か, 現在のスプレッド(points))
    """
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        logger.error(f"スプレッド取得失敗: {symbol}")
        return (False, 0.0)  # 取得失敗は安全側に拒否

    # 価格差 → points に正規化
    spread_price = tick.ask - tick.bid
    spread_points = spread_price / info.point  # ← これが重要

    limit = CONFIG.SPREAD_LIMITS_POINTS.get(symbol, 50)
    return (spread_points <= limit, spread_points)
```

> **設計根拠**: `tick.ask - tick.bid` は価格差（例: 0.015）であり、
> points単位（例: 15）とは数値が全く異なる。
> `symbol_info().point` で割ることで、全銘柄統一のpoints単位に変換する。
> 正規化しないと、USDJPYで 0.015 < 30 の比較になり、
> スプレッドが異常に広くても常に許容されてしまう致命的バグになる。

**サーバー側データ補完（外部インジケータ対応）:**

```python
import numpy as np

async def supplement_technical_data(payload: WebhookPayload) -> WebhookPayload:
    """
    外部インジケータ（LuxAlgo, Lorentzian等）からのWebhookに
    不足しているテクニカルデータをMT5から取得して補完する。

    カスタムインジケータからのWebhookは全フィールド含むため補完不要。
    """
    if not payload.needs_supplement():
        return payload  # カスタムインジケータ → 補完不要

    # M15足データをMT5から取得（EMA200に必要な210本）
    rates = mt5.copy_rates_from_pos(payload.symbol, mt5.TIMEFRAME_M15, 0, 210)
    if rates is None or len(rates) < 200:
        logger.warning(f"MT5データ取得失敗: {payload.symbol}, 補完スキップ")
        return payload

    closes = np.array([r['close'] for r in rates])
    highs  = np.array([r['high']  for r in rates])
    lows   = np.array([r['low']   for r in rates])

    # EMA計算（pandas不使用・numpy版）
    def calc_ema(data, period):
        ema = np.zeros_like(data)
        ema[0] = data[0]
        k = 2 / (period + 1)
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i-1] * (1 - k)
        return ema[-1]

    # RSI計算
    def calc_rsi(data, period=14):
        deltas = np.diff(data[-(period+1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)

    # ATR計算
    def calc_atr(highs, lows, closes, period=14):
        tr = np.maximum(
            highs[-period:] - lows[-period:],
            np.maximum(
                np.abs(highs[-period:] - np.roll(closes, 1)[-period:]),
                np.abs(lows[-period:]  - np.roll(closes, 1)[-period:])
            )
        )
        return float(np.mean(tr))

    # 補完
    if payload.ema21 is None:
        payload.ema21 = round(calc_ema(closes, 21), 5)
    if payload.ema50 is None:
        payload.ema50 = round(calc_ema(closes, 50), 5)
    if payload.ema200 is None:
        payload.ema200 = round(calc_ema(closes, 200), 5)
    if payload.rsi is None:
        payload.rsi = calc_rsi(closes)
    if payload.atr is None:
        payload.atr = round(calc_atr(highs, lows, closes), 5)
    if payload.atr_ratio is None and payload.atr is not None:
        atr_sma = float(np.mean([calc_atr(highs[:i+14], lows[:i+14], closes[:i+14])
                                  for i in range(max(0, len(closes)-20), len(closes)-13)]))
        payload.atr_ratio = round(payload.atr / atr_sma, 2) if atr_sma > 0 else 1.0

    # H1トレンド補完（空の場合）
    if not payload.h1_trend:
        h1_ema21 = calc_ema(closes, 21 * 4)  # M15×4 ≈ H1
        h1_ema50 = calc_ema(closes, 50 * 4)
        payload.h1_trend = "BULLISH" if h1_ema21 > h1_ema50 else "BEARISH"

    logger.info(f"データ補完完了: {payload.symbol} ({payload.source})")
    return payload
```

> **呼び出し位置**: `webhook_receiver.py` のバリデーション通過後、
> `entry_evaluator.py` にペイロードを渡す前に実行する。

---

### 7.9 `ai/prompt_builder.py` — プロンプト構築

**責務**: エントリー評価・H1監視の両プロンプトを構築する。

**エントリー評価プロンプト（GPT-4o用）:**

```
[SYSTEM]
あなたはプロのFXトレーダーの思考を持つトレード判断AIです。
提供されるテクニカルデータ・市場コンテキストを分析し、
厳格なJSON形式のみで回答してください。前置き・説明不要。

判断基準:
- テクニカル・ファンダメンタルズの整合性が取れている場合のみAPPROVE
- ai_confidence < 0.6 は必ずREJECT
- 重要指標30分以内はWAIT
- DEAD_ZONEセッション（XMT 22:00-23:59）はREJECT（システムで自動拒否）
- 金曜XMT 20:00以降はREJECT（週末前カットオフ・システムで自動拒否）
- 市場クローズまで4時間未満の場合は、短期TP設定を推奨
- 相関アラートがある場合は risk_multiplier を下げること

出力JSON:
{
  "decision": "APPROVE|REJECT|WAIT",
  "confidence": 0.0〜1.0,
  "thesis": "根拠テキスト200字程度",
  "invalidation_conditions": ["条件1", "条件2", "条件3"],
  "initial_tp": 数値,
  "emergency_sl": 数値,
  "risk_multiplier": 0.5〜1.5,
  "market_regime": "TRENDING|RANGING|HIGH_VOLATILITY|PRE_EVENT",
  "reject_reason": "文字列またはnull"
}

[USER]
テクニカルシグナル: {webhook_data}
現在値: {price}
セッション(XMT): {session}
H1トレンド: {h1_trend}
口座状況: 総エクスポージャー {exposure_pct}% / 保有ポジ {pos_count}件
相関アラート: {correlation_alert}
今日の重要指標: {todays_events}
```

**H1バッチ監視プロンプト（GPT-4o用）:**

```
[SYSTEM]
あなたはポジション管理専門のAIです。
エントリー時のThesis（根拠）と現在状況を比較し、
各ポジションへの指示をJSON形式のみで返してください。

判断の優先順位:
1. Invalidation Conditionsに抵触していないか（最優先）
2. 価格アクションがThesisを支持しているか
3. TP更新・分割決済の余地があるか

出力JSON:
{
  "positions": [
    {
      "trade_id": "ID",
      "thesis_status": "VALID|WEAKENING|BROKEN",
      "action": "HOLD|UPDATE_TP|PARTIAL_CLOSE|FULL_CLOSE",
      "new_tp": 数値またはnull,
      "close_percentage": 0|50|100,
      "reasoning": "判断理由100字程度",
      "urgency": "NORMAL|HIGH"
    }
  ]
}

[USER]
共通コンテキスト:
  セッション(XMT): {session}
  重要ニュース: {major_news}
  ボラティリティ: {volatility_regime}

査定対象ポジション:
{positions_list}
  ※各ポジション: trade_id / symbol / direction / thesis要約(150字) /
    invalidation_conditions / pnl_pips / 保有時間 / 現在TP
```

**緊急判定プロンプト（GPT-4o-mini用）:**

```
[SYSTEM]
FXポジション緊急判定AIです。
「今すぐ人間に通知すべきか」だけを判断してください。
JSONのみで回答。

出力JSON:
{
  "action": "ALERT_HUMAN|CONTINUE_MONITORING",
  "reason": "理由50字以内"
}

[USER]
トリガー理由: {trigger_reason}
ポジション: {symbol} {direction} PnL:{pnl_pips}pips
Thesis概要: {thesis_summary}
Invalidation: {invalidation_conditions}
```

**トークン最適化:**

- H1バッチは共通コンテキストを1回だけ記述（重複排除）
- thesis_textは先頭150文字のみ渡す（DBには全文保存）
- api_cost_logに毎回記録してコストを可視化

---

### 7.10 `ai/entry_evaluator.py` — エントリー評価

**責務**: Webhookデータを受け取りAIでエントリー可否を判断・執行する。

**処理フロー:**

1. RiskGuardianで事前ガードチェック → NG は即リターン
2. 相関アラートチェック → alert情報をAIプロンプトに注入
3. EconomicCalendarで指標チェック → 30分以内ならWAIT
4. GPT-4o呼び出し（tools: web_search付き）
5. レスポンスバリデーション + セマンティックバリデーション
6. AI監査ログ記録（ai_audit_log）
7. APPROVE なら LotCalculator でロット計算 → verify_calculation
8. MT5で注文執行
9. ThesisDBに保存
10. Discord通知

**WAITハンドリング（コスト最適化設計）:**

> WAIT判定を受けた場合、AIを再呼び出しするとコストが倍増する。
> 代わりに「条件付きキャッシュ」方式で再利用する。

```python
# WAIT判定時のフロー
WAIT_TTL_MINUTES: int = 15        # WAITの有効期限（M15足1本分）
WAIT_MAX_RETRIES: int = 2         # 最大再評価回数
WAIT_RECHECK_INTERVAL_SEC: int = 300  # 再チェック間隔（5分）

_wait_queue: dict[str, dict] = {}   # symbol -> {ai_response, webhook_data, created_at, retry_count}

async def handle_wait_decision(symbol: str, ai_response: dict, webhook_data: dict):
    """
    WAIT判定時の処理:
    1. AIレスポンスをキャッシュ（15分TTL）
    2. 5分後にブロック条件（指標等）が解除されたか再チェック
    3. 解除されていれば、元のAI判定を「AIを呼び直さず」にそのまま使用
    4. 解除されていなければ破棄（Discord通知）
    """
    _wait_queue[symbol] = {
        "ai_response": ai_response,
        "webhook_data": webhook_data,
        "created_at": BrokerTime.now(),
        "retry_count": 0,
    }
    await notifier.send(
        f"⏳ WAIT: {symbol} - {ai_response.get('reject_reason', '条件未達')}",
        level="INFO"
    )
    # 5分後に再チェックをスケジュール
    scheduler.add_job(
        recheck_wait, "date",
        run_date=BrokerTime.now() + timedelta(seconds=WAIT_RECHECK_INTERVAL_SEC),
        args=[symbol],
        id=f"wait_recheck_{symbol}",
        replace_existing=True,
    )

async def recheck_wait(symbol: str):
    """WAIT中のシグナルを再チェック（AI再呼び出しなし）"""
    entry = _wait_queue.get(symbol)
    if not entry:
        return
    elapsed = (BrokerTime.now() - entry["created_at"]).total_seconds()
    if elapsed > WAIT_TTL_MINUTES * 60:
        del _wait_queue[symbol]
        await notifier.send(f"⏳ WAIT期限切れ: {symbol}（{WAIT_TTL_MINUTES}分超過）", level="INFO")
        return

    # ブロック条件だけ再チェック（AI呼び出しなし）
    can_enter, reason = guardian.can_enter_new_trade(
        symbol, entry["webhook_data"]["direction"]
    )
    event_soon, _ = calendar.is_high_impact_event_soon()

    if can_enter and not event_soon:
        # ブロック解除 → 元のAI判定でエントリー実行
        logger.info(f"WAIT解除: {symbol} → 元のAI判定でエントリー実行")
        await execute_entry(symbol, entry["ai_response"], entry["webhook_data"])
        del _wait_queue[symbol]
    else:
        entry["retry_count"] += 1
        if entry["retry_count"] >= WAIT_MAX_RETRIES:
            del _wait_queue[symbol]
            await notifier.send(f"⏳ WAIT最終破棄: {symbol}（再チェック{WAIT_MAX_RETRIES}回超過）", level="INFO")
        # 次回再チェックをスケジュール
```

> **コスト効果**: AI再呼び出し0回。WAIT→APPROVE転換時のコストは$0。
> 15分超過時は市場状況が変化している可能性が高いため、安全に破棄する。

**部分操作失敗リカバリー（AI承認〜MT5注文の途中失敗）:**

```python
async def execute_entry(symbol: str, ai_response: dict, webhook_data: dict):
    """エントリー実行（各ステップの失敗をDiscordに通知）"""
    trade_id = str(uuid.uuid4())

    try:
        # Step 1: ロット計算
        lot = lot_calculator.calculate(
            symbol, sl_pips, CONFIG.MAX_RISK_PER_TRADE_PCT,
            await mt5_client.get_account_balance(),
            ai_response["risk_multiplier"]
        )
        safety = lot_calculator.verify_calculation(symbol, lot, sl_pips)
        if not safety["is_safe"]:
            await notifier.send(
                f"🔴 ロット計算異常: {symbol} lot={lot} - {safety['reason']}",
                level="CRITICAL"
            )
            return

        # Step 2: MT5注文
        result = await mt5_client.open_position(
            symbol, ai_response["decision_direction"], lot,
            ai_response["emergency_sl"], ai_response["initial_tp"],
            comment=trade_id[:8]
        )
        if result is None or not result.success:
            await notifier.send(
                f"🔴 MT5注文失敗: {symbol} {ai_response.get('thesis','')[:50]}...\n"
                f"エラー: {result.error if result else 'MT5接続不可'}",
                level="CRITICAL"
            )
            return

        # Step 3: DB保存
        try:
            await thesis_db.save_thesis(trade_id, result.ticket, ...)
        except Exception as e:
            # DB保存失敗 → ポジションは存在するがThesisなし（危険）
            await notifier.send(
                f"🔴 DB保存失敗: {symbol} ticket={result.ticket}\n"
                f"ポジションは開いています！手動確認必須\nエラー: {e}",
                level="CRITICAL"
            )
            return

        # Step 4: 全成功 → Discord通知
        await notifier.send_entry_notification(trade_id, result, ai_response)

    except Exception as e:
        await notifier.send(
            f"🔴 エントリー処理で予期せぬエラー: {symbol}\n{type(e).__name__}: {e}",
            level="CRITICAL"
        )
        logger.exception(f"execute_entry failed: {symbol}")
```

> **設計方針**: 失敗箇所ごとに異なるDiscordメッセージを送信し、
> 手動対応が必要な状況を正確に伝える。MT5注文成功＋DB失敗は最も危険な状態。

**web_search フォールバック設計:**

```python
# OpenAI web_search toolsが失敗した場合の処理
# web_searchはAI側で自律的に呼ばれるため、Python側で直接制御できない。
# 代わりに以下のフォールバック設計を行う。

WEB_SEARCH_TIMEOUT_SEC: int = 10  # web_search含む全体タイムアウト

async def call_entry_evaluation(webhook_data: dict, context: dict) -> dict:
    """
    エントリー評価AI呼び出し（web_searchフォールバック付き）
    
    フォールバック戦略:
    1. web_search有効で呼び出し（通常パス）
    2. タイムアウト or web_search関連エラー
       → web_searchなしでリトライ（ニュースなしで判断）
    3. それでも失敗 → GPT-4o-miniにフォールバック
    4. 全失敗 → REJECT（安全側に倒す）
    """
    # 試行1: web_search付き
    try:
        response = await asyncio.wait_for(
            call_ai_with_retry(
                CONFIG.MODEL_MAIN, messages,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
            ),
            timeout=CONFIG.AI_TIMEOUT_MAIN_SEC + WEB_SEARCH_TIMEOUT_SEC
        )
        if response:
            return parse_ai_response(response)
    except asyncio.TimeoutError:
        logger.warning("web_search付きAI呼び出しタイムアウト")

    # 試行2: web_searchなし（ニュース情報なしで判断）
    try:
        response = await asyncio.wait_for(
            call_ai_with_retry(CONFIG.MODEL_MAIN, messages),
            timeout=CONFIG.AI_TIMEOUT_MAIN_SEC
        )
        if response:
            result = parse_ai_response(response)
            result["_web_search_failed"] = True  # 監査ログ用フラグ
            await notifier.send(
                f"⚠️ web_search失敗: {webhook_data['symbol']} - ニュースなしで判断",
                level="WARNING"
            )
            return result
    except asyncio.TimeoutError:
        pass

    # 試行3: GPT-4o-mini フォールバック
    try:
        response = await call_ai_with_retry(CONFIG.MODEL_FAST, messages)
        if response:
            result = parse_ai_response(response)
            result["_fallback_model"] = True
            return result
    except Exception:
        pass

    # 全失敗 → 安全側にREJECT
    await notifier.send(
        f"🔴 AI完全障害: {webhook_data['symbol']} エントリー拒否",
        level="CRITICAL"
    )
    return {"decision": "REJECT", "reject_reason": "AI障害"}
```

**web_search統合:**

```python
# OpenAI APIのtoolsにweb_searchを追加するだけ
tools = [{
    "type": "web_search_20250305",
    "name": "web_search"
}]
# AIが自分で "{symbol} latest forex news" などを検索してThesisに組み込む
```

---

### 7.11 `ai/position_monitor.py` — ポジション監視

**責務**: H1バッチ処理・価格近接チェック・フォールバック制御。

**H1バッチ処理（毎時01分）:**

- MT5の全ポジションとThesisDBを結合
- 共通コンテキスト（セッション・ニュース・ボラ）を一度だけ構築
- GPT-4oへ一括送信
- レスポンス各項目をバリデーション後に実行
- AI監査ログに記録（ai_audit_log）
- 全結果をreview_logに保存
- Discord通知（査定サマリー）

**H1バッチ部分失敗時のリカバリー:**

```python
async def run_h1_batch_review():
    """H1バッチ：各ポジションのアクション実行を個別にtry/catchし、
    1ポジションの失敗が他に波及しないようにする"""
    positions = await mt5_client.get_all_positions()
    theses = await thesis_db.get_active_theses()
    ai_response = await call_h1_batch_ai(positions, theses)

    results_summary = []
    failed_actions = []

    for pos_instruction in ai_response["positions"]:
        try:
            result = await execute_position_action(pos_instruction)
            results_summary.append(result)
        except Exception as e:
            failed_actions.append({
                "trade_id": pos_instruction["trade_id"],
                "action": pos_instruction["action"],
                "error": str(e),
            })
            logger.exception(f"H1バッチ個別アクション失敗: {pos_instruction['trade_id']}")

    # 部分失敗があった場合はDiscord CRITICAL通知
    if failed_actions:
        fail_msg = "\n".join(
            f"  ・{f['trade_id'][:8]} {f['action']}: {f['error']}"
            for f in failed_actions
        )
        await notifier.send(
            f"🔴 H1バッチ部分失敗（{len(failed_actions)}/{len(ai_response['positions'])}件）\n"
            f"{fail_msg}\n手動確認してください",
            level="CRITICAL"
        )

    # 成功分は通常通りサマリー通知
    if results_summary:
        await notifier.send_h1_summary(results_summary)
```

**価格近接チェック（60秒ごと・Layer 1）:**

```python
# ルールベースのトリップワイヤー（AIなし）
for position in positions:
    tp_distance = abs(position.tp - current_price)
    sl_distance = abs(position.sl - current_price)
  
    # TP/SLの80%地点に到達
    if tp_distance < (initial_tp_distance * 0.2):
        trigger_layer2(position, "TP近接80%")
  
    # 急激な逆行（ATR×2以上の1本足）
    if sudden_reversal_detected(position):
        trigger_layer2(position, "急激な逆行検知")
```

**フォールバック実装:**

```
試行1: GPT-4o（timeout=20秒）
試行2: GPT-4o-mini（timeout=15秒）
試行3: ルールベース
  → 全ポジHOLD
  → 新規エントリー停止（MONITOR_ONLYに変更）
  → Discord警告通知（"AI障害: ルールベースで運用中"）
```

---

### 7.12 `notifications/discord_notifier.py` — Discord通知

**責務**: システム全イベントをDiscord Webhookで通知する。

**通知レベルと色:**

```
🟢 INFO     (緑 #00ff00): エントリー承認・査定完了・TP更新
🟡 WARNING  (黄 #ffff00): 相関アラート・AI信頼度低・MONITOR_ONLY移行
🔴 CRITICAL (赤 #ff0000): CB発動・MT5切断・手動介入要請・AI障害
⚪ DAILY    (白 #ffffff): 日次レポート・DBステータス
```

**通知フォーマット（Embedを使用）:**

```
🟢 新規エントリー承認
━━━━━━━━━━━━━━━━━━━━
📊 USDJPY LONG  |  0.03lot
💰 リスク: 1,500円 (1.0%)
🎯 TP: 150.20  |  🛡 SL: 148.80
🤖 GPT-4o  信頼度: 82%  |  TRENDING
📝 Thesis: ドル高期待のH1ブレイクアウト。
   EMA21>EMA50、RSI58で過熱なし...
⚠️ 無効化条件:
   1. 149.00を終値で下回った場合
   2. FOMC発言でドル売り加速
   3. リスクオフによるゴールド急騰
⏰ XMT: 2024-01-15 14:30:00 XMT+2
━━━━━━━━━━━━━━━━━━━━

🔴 手動介入が必要です
━━━━━━━━━━━━━━━━━━━━
⚠️ MT5接続断絶を検知
📍 未管理ポジション: USDJPY #12345
🔗 MT5を確認してください
⏰ XMT: 2024-01-15 14:30:00 XMT+2
━━━━━━━━━━━━━━━━━━━━
```

**日次レポート（毎朝 XMT 07:00）:**

```
⚪ 日次レポート - 2024-01-15
━━━━━━━━━━━━━━━━━━━━
📈 本日の損益: +3,200円 (+0.64%)
📊 トレード: 2件（勝:1 負:1）
💾 DB: thesis=2件 / history=45件
💰 API費用（今月累計）: $3.20
🔋 システム: ACTIVE
⏰ 今日の重要指標:
   16:30 XMT USD NFP ★★★
━━━━━━━━━━━━━━━━━━━━
```

**実装注意:**

- 通知失敗時はログのみでシステムを止めない
- リクエストはタイムアウト5秒に設定
- 同じ内容の連続通知を30秒以内に送らない（重複防止）

---

### 7.13 `main.py` — エントリーポイント

**責務**: 全コンポーネントの初期化・FastAPI起動・スケジューラー管理。

**APSchedulerのタイムゾーン設定（最重要）:**

> ⭐ **APSchedulerのcronジョブはデフォルトで「システムのローカルタイムゾーン」で発火する。**
> Windows VPSが日本時間（JST）の場合、`hour=20` は JST 20:00 に発火し、
> XMT 20:00 ではない。また `day_of_week="fri"` もシステムの金曜日であり、
> XMTの金曜日とは異なる（日本時間では土曜日早朝がXMT金曜日晩に当たる）。
>
> **解決策**: `AsyncIOScheduler` の生成時に `timezone` を指定する。
> XMTサーバー時間（冬:GMT+2 / 夏:GMT+3）は **EET (Eastern European Time)** に完全一致する。
> `Europe/Athens` を指定すれば、欧州夏時間切替も自動追従されるため、
> **冬時間・夏時間の切替を意識する必要がなくなる。**

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo  # Python 3.9+ 標準ライブラリ

# ★ XMTサーバー時間 = EET (Europe/Athens)
#   冬: GMT+2 (EET) / 夏: GMT+3 (EEST)
#   欧州夏時間ルールに完全準拠
XMT_TIMEZONE = ZoneInfo("Europe/Athens")

scheduler = AsyncIOScheduler(timezone=XMT_TIMEZONE)
# ↑ これで全cronジョブの hour/minute/day_of_week が XMT時間で評価される
# ↑ day_of_week="fri" は XMTの金曜日に発火する（システムの金曜日ではない）

# スケジューラー設定（全時刻はXMT時間）
scheduler.add_job(
    monitor.run_h1_batch_review,
    "cron", minute=CONFIG.H1_BATCH_CRON_MINUTE  # 毎時XMT XX:01
)
scheduler.add_job(
    guardian.check_all_guards,
    "interval", seconds=CONFIG.DD_CHECK_INTERVAL_SEC  # intervalはTZ無関係
)
scheduler.add_job(
    monitor.check_price_proximity,
    "interval", seconds=CONFIG.PRICE_CHECK_INTERVAL_SEC  # intervalはTZ無関係
)
scheduler.add_job(
    db.run_maintenance,
    "cron", hour=0, minute=5  # XMT 00:05
)
scheduler.add_job(
    notifier.send_daily_report,
    "cron", hour=7, minute=0  # XMT 07:00
)
# 週末管理（day_of_week="fri" = XMTの金曜日）
scheduler.add_job(
    guardian.check_friday_cutoff,
    "cron", day_of_week="fri", hour=CONFIG.FRIDAY_ENTRY_CUTOFF_HOUR, minute=0
    # → XMT金曜 20:00 に発火
)
scheduler.add_job(
    monitor.weekend_force_close,
    "cron", day_of_week="fri", hour=CONFIG.FRIDAY_FORCE_CLOSE_HOUR, minute=0
    # → XMT金曜 22:00 に発火
)
scheduler.add_job(
    monitor.weekend_final_check,
    "cron", day_of_week="fri",
    hour=CONFIG.FRIDAY_FINAL_CHECK_HOUR,
    minute=CONFIG.FRIDAY_FINAL_CHECK_MINUTE
    # → XMT金曜 22:30 に発火
)
scheduler.add_job(
    guardian.check_weekly_open,
    "cron", day_of_week="sun", hour=23, minute=20
    # → XMT日曜 23:20 に発火
)
```

> **設計根拠**: `timezone=ZoneInfo("Europe/Athens")` をスケジューラー全体に適用することで:
> - 全cronジョブがXMT時間基準で発火する
> - `day_of_week="fri"` はXMTの金曜日（システムの金曜日ではない）
> - 夏時間↔冬時間の切替は `ZoneInfo` が自動処理するため、
>   コード側でDSTを意識する必要は一切ない
> - `interval` ジョブ（秒単位）はタイムゾーンに依存しないため影響なし

**起動時チェックリスト:**

1. 設定値バリデーション（validate_config）
2. MT5接続確認（失敗したら起動中止）
3. 起動時リカバリー（孤立Thesis検出・未追跡ポジション警告）
4. OpenAI API疎通確認
5. Discord Webhook疎通確認
6. DBスキーマ初期化
7. 市場状態確認（週末中か・市場オープン中か）
8. デモ/本番モードをDiscordに通知
9. スケジューラー開始
10. グレースフルシャットダウンのシグナルハンドラー登録

---

## 8. TradingView Pine Script仕様

> **戦略コード・セットアップ手順の詳細**: `pinescript/` ディレクトリを参照。
> - `pinescript/multi_strategy_signal.pine` — カスタムインジケータのソースコード
> - `pinescript/SETUP_GUIDE.md` — インジケータ配置・アラート設定・運用チューニング手順

### インジケータ構成（5スロット制約）

| # | インジケータ | 種別 | 役割 |
|---|-------------|------|------|
| 1 | **AI Trading Signal Generator v1.0** | カスタム | 5戦略統合メインシグナル |
| 2 | **LuxAlgo - Fair Value Gap** | 外部 | 構造的インバランス（FVG充填） |
| 3 | **LuxAlgo - Liquidity Sweeps** | 外部 | 流動性スイープ反転 |
| 4 | **Lorentzian Classification** | 外部 | ML分類（確認・コンテキスト） |
| 5 | **Q-Trend** | 外部 | トレンド方向の視覚確認のみ |

### 戦略定義（カスタムインジケータ・5戦略）

```
┌─────────────────────────────────────────────────────────────────┐
│  戦略1: EMA_CROSS（トレンド開始）          頻度: 週1〜2回/銘柄  │
├─────────────────────────────────────────────────────────────────┤
│  LONG:  EMA21がEMA50を上抜け AND 終値 > EMA200               │
│  SHORT: EMA21がEMA50を下抜け AND 終値 < EMA200               │
│  用途:  新しいトレンドの「始まり」を捉える                      │
│  特徴:  発生頻度が低く、品質が高い                              │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  戦略2: TREND_PULLBACK（トレンド継続）     頻度: 週3〜5回/銘柄  │
├─────────────────────────────────────────────────────────────────┤
│  LONG:  上昇トレンド(EMA21>EMA50>EMA200)中に                  │
│         安値がEMA21に到達 + 終値がEMA21の上で引ける             │
│         + RSI 40〜65（ニュートラルゾーン）                      │
│  SHORT: 下降トレンド中に高値がEMA21に到達 + 終値がEMA21の下     │
│         + RSI 35〜60                                           │
│  用途:  トレンドの「押し目/戻り」を捉える                      │
│  特徴:  勝率が高いが、トレンドレス相場では不発                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  戦略3: BREAKOUT（レンジブレイク）         頻度: 週1〜3回/銘柄  │
├─────────────────────────────────────────────────────────────────┤
│  LONG:  終値 > 20期間高値(Donchian上限) + 出来高 > 平均×1.3    │
│         + 強い下降トレンドでないこと                             │
│  SHORT: 終値 < 20期間安値(Donchian下限) + 出来高 > 平均×1.3    │
│         + 強い上昇トレンドでないこと                             │
│  用途:  レンジからの「脱出」を捉える                           │
│  特徴:  出来高フィルターでダマシを抑制                          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  戦略4: RSI_DIVERGENCE（モメンタム乖離）   頻度: 週1〜2回/銘柄  │
├─────────────────────────────────────────────────────────────────┤
│  LONG:  価格が安値更新 + RSIが安値を更新しない（強気乖離）      │
│         + RSI < 45 + ピボットポイント確認済み(3バー)            │
│  SHORT: 価格が高値更新 + RSIが高値を更新しない（弱気乖離）      │
│         + RSI > 55 + ピボットポイント確認済み                   │
│  用途:  トレンド「転換」の兆候を捉える                         │
│  特徴:  ピボット確認のため3バー(M15=45分)の遅延あり            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  戦略5: MEAN_REVERSION（過延伸反転）       頻度: 週2〜4回/銘柄  │
├─────────────────────────────────────────────────────────────────┤
│  LONG:  終値 <= BB下限 + RSI < 30 + 強い下降トレンドでない     │
│  SHORT: 終値 >= BB上限 + RSI > 70 + 強い上昇トレンドでない     │
│  用途:  行き過ぎた動きの「逆張り」                             │
│  特徴:  トレンド相場では危険。レンジ相場での有効性が高い        │
└─────────────────────────────────────────────────────────────────┘
```

### 外部インジケータ戦略

| パターン名 | インジケータ | 検出対象 |
|-----------|-------------|---------|
| `FVG_FILL` | LuxAlgo FVG | Fair Value Gap（価格の空白地帯）が充填される構造的エントリー |
| `LIQUIDITY_SWEEP` | LuxAlgo Sweeps | 流動性プール（前回高値/安値）をスイープ後の反転 |
| `LORENTZIAN` | Lorentzian Classification | 機械学習ベースの方向分類（Lorentzian距離法） |

### シグナル優先度（1バー最大1シグナル）

カスタムインジケータ内で複数条件が同時成立した場合の選択順序:

```
優先度1: BREAKOUT        ← 最もレア・高確信度
優先度2: EMA_CROSS       ← トレンド転換
優先度3: RSI_DIVERGENCE  ← 反転シグナル
優先度4: TREND_PULLBACK  ← 継続シグナル
優先度5: MEAN_REVERSION  ← 逆張り（最も慎重に扱う）
```

### グローバルフィルター（Pine Script内蔵）

全戦略に共通で適用されるフィルター:

| フィルター | 条件 | 目的 |
|-----------|------|------|
| セッションフィルター | XMT 9:00〜22:00のみ | ロンドン+NY時間に限定 |
| ATRフィルター | ATR / ATR20平均 >= 0.8 | 低ボラティリティ期間の除外 |
| クールダウン | 同方向シグナル間 4バー(1時間)以上 | 連続シグナルの抑制 |
| データ量チェック | EMA200計算に十分なバー数 | 起動直後の不正シグナル防止 |

### アラート配分（20スロット制約）

```
カスタム Signal Generator:  3アラート（GOLD/USDJPY/EURUSD × 1）
  → 「任意のalert()関数の呼び出し」条件で全5戦略を1アラートに集約

LuxAlgo FVG:               6アラート（3銘柄 × BUY/SELL）
LuxAlgo Sweeps:            6アラート（3銘柄 × BUY/SELL）
Lorentzian Classification: 4アラート（GOLD/USDJPY × BUY/SELL）
予備:                      1アラート
合計:                      20アラート
```

### Webhook送信JSON

> **⭐ `{{timenow}}` のタイムゾーンに関する重要な注意:**
> TradingViewの `{{timenow}}` は **Exchange timezone**（取引所のタイムゾーン）で出力される。
> これはXMTサーバー時間とは異なる可能性がある（例: USDJPYはCME timezoneで出力される）。
>
> **推奨**: `broker_time` を「参考値」に留め、実際の処理時刻はサーバー側で `BrokerTime.now()` を使用。
> これにより、Pine Script側でタイムゾーン変換を行う必要がなくなる。

**カスタムインジケータ（全フィールド自動生成）:**

```json
{
  "secret":       "webhook_secret_value",
  "symbol":       "GOLD",
  "direction":    "LONG",
  "timeframe":    "15",
  "h1_trend":     "BULLISH",
  "pattern":      "BREAKOUT",
  "price":        2650.50,
  "ema21":        2648.123,
  "ema50":        2645.678,
  "ema200":       2620.345,
  "rsi":          58.42,
  "atr":          3.25,
  "atr_ratio":    1.15,
  "macd":         0.85,
  "macd_signal":  0.62,
  "macd_hist":    0.23,
  "bb_upper":     2660.12,
  "bb_lower":     2635.45,
  "volume_ratio": 1.82,
  "dc_upper":     2655.00,
  "dc_lower":     2630.00,
  "broker_time":  "2026-03-05T15:30:00",
  "source":       "custom_multistrat"
}
```

**外部インジケータ（部分フィールド ─ サーバー側で補完）:**

```json
{
  "secret":      "webhook_secret_value",
  "symbol":      "GOLD",
  "direction":   "LONG",
  "timeframe":   "15",
  "h1_trend":    "",
  "pattern":     "FVG_FILL",
  "price":       2650.50,
  "broker_time": "2026-03-05T15:30:00",
  "source":      "luxalgo_fvg"
}
```

> **サーバー側データ補完**: 外部インジケータのWebhookにはRSI/ATR/EMA等が含まれない。
> `webhook_receiver.py` がMT5の `copy_rates_from_pos()` を使用して不足データを取得し、
> WebhookPayloadを補完してからAI評価に渡す（Section 7.1b WebhookPayloadモデル参照）。

### Webhook共通設定

- URL: `http://{VPS_IP}:{PORT}/webhook/tradingview`
- 対象銘柄: USDJPY, EURUSD, GOLD それぞれにM15チャートで設定
- Alert条件: 「Once Per Bar Close」に設定（M15足確定時のみ送信）
- Alert有効期限: 「Open-ended alert」（無期限）

### シグナル推定頻度

```
カスタム:      8〜16 /週/銘柄 × 3 = 24〜48 /週
LuxAlgo FVG:  3〜8  /週/銘柄 × 3 = 9〜24  /週
Sweeps:       2〜5  /週/銘柄 × 3 = 6〜15  /週
Lorentzian:   5〜10 /週/銘柄 × 2 = 10〜20 /週

合計: 約50〜107 シグナル/週
AI承認率 15〜30% → 約8〜32 トレード/週
目標: 月30〜80トレード
```

---

## 9. AIプロンプト設計

### 出力バリデーションルール

AIが返すJSONに対して以下を必ず検証すること：

| フィールド              | バリデーション                | NGの場合                   |
| ----------------------- | ----------------------------- | -------------------------- |
| decision                | APPROVE/REJECT/WAITのいずれか | REJECT扱い                 |
| confidence              | 0.0〜1.0の数値                | REJECT扱い                 |
| confidence              | < 0.6                         | REJECT扱い                 |
| initial_tp              | 現在値から5%以内              | 棄却・注文しない           |
| emergency_sl            | 現在値から10%以内             | 物理SL候補として使用しない |
| risk_multiplier         | 0.5〜1.5の範囲内              | 1.0に丸める                |
| invalidation_conditions | 3件存在するか                 | 空の場合はREJECT           |

### temperatureとmax_tokens設定

```
エントリー評価（GPT-4o）: temperature=0.2, max_tokens=800
H1バッチ監視（GPT-4o）:   temperature=0.2, max_tokens=1000
緊急判定（GPT-4o-mini）:  temperature=0.1, max_tokens=200
```

### セマンティックバリデーション（方向整合性チェック）

AIが返したJSON値の論理的一貫性を検証する。形式バリデーション（上記テーブル）を
通過した後に実施する。

```python
def validate_semantic_consistency(
    ai_response: dict,
    webhook_data: dict,
) -> tuple[bool, str]:
    """
    AIレスポンスとWebhookシグナルの方向整合性を検証する。
    矛盾があればREJECTに上書きし、ai_audit_logに記録する。

    Returns: (is_valid, reason_if_invalid)
    """
    errors = []

    # 1. 方向整合性: WebhookのdirectionとAIのthesisが矛盾していないか
    direction = webhook_data["direction"]  # LONG or SHORT
    thesis = ai_response.get("thesis", "").upper()
    decision = ai_response.get("decision", "")

    if decision == "APPROVE":
        # LONG承認なのにthesisに下落示唆ワードが含まれる場合
        bearish_words = ["下落", "ショート", "売り圧力", "BEARISH", "SHORT"]
        bullish_words = ["上昇", "ロング", "買い圧力", "BULLISH", "LONG"]

        if direction == "LONG" and any(w in thesis for w in bearish_words):
            if not any(w in thesis for w in bullish_words):
                errors.append(f"LONG承認だがthesisに下落示唆: {thesis[:100]}")

        if direction == "SHORT" and any(w in thesis for w in bullish_words):
            if not any(w in thesis for w in bearish_words):
                errors.append(f"SHORT承認だがthesisに上昇示唆: {thesis[:100]}")

    # 2. TP方向整合性: LONGなのにTPが現在値より低い、など
    price = webhook_data.get("price", 0)
    tp = ai_response.get("initial_tp", 0)
    sl = ai_response.get("emergency_sl", 0)

    if decision == "APPROVE" and price > 0 and tp > 0:
        if direction == "LONG" and tp < price:
            errors.append(f"LONG承認だがTP({tp})が現在値({price})より低い")
        if direction == "SHORT" and tp > price:
            errors.append(f"SHORT承認だがTP({tp})が現在値({price})より高い")

    # 3. SL方向整合性: LONGなのにSLが現在値より高い、など
    if decision == "APPROVE" and price > 0 and sl > 0:
        if direction == "LONG" and sl > price:
            errors.append(f"LONG承認だがSL({sl})が現在値({price})より高い")
        if direction == "SHORT" and sl < price:
            errors.append(f"SHORT承認だがSL({sl})が現在値({price})より低い")

    # 4. confidence vs decision 整合性
    confidence = ai_response.get("confidence", 0)
    if decision == "APPROVE" and confidence < 0.65:
        errors.append(f"APPROVE判定だがconfidence={confidence}が低い（<0.65）")

    # 5. market_regime vs risk_multiplier 整合性
    regime = ai_response.get("market_regime", "")
    risk_mult = ai_response.get("risk_multiplier", 1.0)
    if regime == "HIGH_VOLATILITY" and risk_mult > 1.0:
        errors.append(f"HIGH_VOLATILITYだがrisk_multiplier={risk_mult}が1.0超")

    if errors:
        return False, " / ".join(errors)
    return True, ""
```

> **運用ルール**: セマンティックバリデーション失敗時:
> 1. 判定をREJECTに上書き
> 2. ai_audit_logに `validation_result="FAIL_SEMANTIC"`, `override_reason` を記録
> 3. Discord WARNING通知（AI幻覚の可能性を報告）

---

## 10. DBスキーマ・パージポリシー

### パージスケジュール

| テーブル            | パージ条件           | 保持期間 | 理由                 |
| ------------------- | -------------------- | -------- | -------------------- |
| review_log          | created_at < 90日前  | 90日     | 直近3ヶ月で分析十分  |
| api_cost_log        | created_at < 30日前  | 30日     | 月次コスト管理用     |
| ai_audit_log        | created_at < 14日前  | 14日     | 容量管理（full_response大） |
| thesis (CLOSED)     | created_at < 180日前 | 180日    | ACTIVEは絶対削除禁止 |
| trade_history       | 削除しない           | 永続     | 分析資産             |
| circuit_breaker_log | 削除しない           | 永続     | 障害記録             |

**VACUUM実行**: パージ後に必ず `VACUUM` を実行してファイルサイズを縮小する。

### DBファイルサイズ目安（3銘柄・1年運用時）

```
trade_history:  約500トレード × 2KB = 約1MB
review_log:     90日分 × 24回 × 3銘柄 × 1KB = 約6MB
api_cost_log:   30日分 × 30回 × 0.1KB = 約90KB
ai_audit_log:   14日分 × 25回 × 2KB = 約700KB
合計: 約10MB以下（パージ運用時）
※ DB50MB超過時はai_audit_logを7日保持に自動短縮
```

---

## 11. リスク管理・サーキットブレーカー設計

### ガード階層

```
Level 1 [即時・物理]: MT5上の物理SL
  → AI障害・Python障害に関係なく常に有効

Level 2 [30秒]: サーキットブレーカー
  → 日次DD 3%超で全決済・LOCKED

Level 3 [60秒]: 総エクスポージャー・ポジション数
  → MONITOR_ONLYに移行（既存ポジは管理継続）

Level 4 [エントリー時]: 事前ガードチェック
  → 通貨集中・指標前・重複ポジ確認
```

### 物理SL設定の考え方

```
AIが提案するemergency_slを参考値として使用するが、
以下の制限を必ずコードで強制する:

USDJPY:  現在値から最大 80pips 以内
EURUSD:  現在値から最大 60pips 以内
GOLD:    現在値から最大 300pips ($3.00) 以内

これを超えるSLはコードで棄却し、上限値に丸める。
```

---

## 12. 市場クローズ・週末ポジション管理

### 基本方針

**ポジションは市場クローズ時間をまたがない。** 週末ギャップリスクは物理SLでも防御不可能であり、最も危険なリスクの一つ。日次クローズ前にも全ポジションの整理を行う。

### 市場タイムテーブル（XMT時間基準）

```
【FX通貨ペア（USDJPY, EURUSD）】
市場オープン:  日曜 23:05 XMT
市場クローズ:  金曜 23:55 XMT
日次ロールオーバー: 23:55〜00:05 XMT（スプレッド拡大・取引非推奨）

【GOLD（ゴールド = XAUUSD）】
市場オープン:  日曜 23:05 XMT
市場クローズ:  金曜 23:55 XMT
日次ブレイク:  23:00〜23:05 XMT（毎日・取引停止）

【年末年始・祝日】
クリスマス:    12/25 全日クローズ
元旦:          1/1 全日クローズ
※ その他はブローカー通知に従い手動でカレンダー追加
```

### ポジション管理タイムライン

```
┌─────────────────────────────────────────────────────┐
│  週次ポジション管理フロー（金曜日）                    │
├─────────────────────────────────────────────────────┤
│                                                     │
│  金曜 XMT 20:00  新規エントリー停止                  │
│  ├─ RiskGuardianがFRIDAY_CUTOFFフラグを立てる       │
│  ├─ Webhookシグナルは全てREJECT                     │
│  └─ Discord通知: "週末前エントリー停止"              │
│                                                     │
│  金曜 XMT 22:00  全ポジション強制決済                │
│  ├─ GPT-4oに最終査定リクエスト                      │
│  │   └─ 各ポジの推奨（即時決済 or TPトレール）       │
│  ├─ AI推奨に関わらず全ポジ成行決済を実行             │
│  ├─ trade_historyにexit_reason="WEEKEND_CLOSE"記録  │
│  └─ Discord通知: "週末前全決済完了"                  │
│                                                     │
│  金曜 XMT 22:30  最終確認                           │
│  ├─ MT5上にポジションが残っていないか再確認          │
│  ├─ 残存ポジがあれば再度決済試行 + Discord CRITICAL  │
│  └─ システムステータスをWEEKEND_CLOSEDに変更        │
│                                                     │
│  日曜 XMT 23:15  週明けオープン処理                  │
│  ├─ MT5接続確認                                     │
│  ├─ スプレッド安定確認（オープン直後は拡大）          │
│  ├─ システムステータスをACTIVEに復帰                │
│  └─ Discord通知: "週明け稼働再開"                   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 日次クローズ管理

```
【日次ロールオーバー保護（毎日）】
XMT 22:30  新規エントリー一時停止（DEAD_ZONEガードで強制ブロック）
XMT 22:55  GOLD保有中なら:
           ├─ 含み益 → AIに決済判断を委任
           └─ 含み損 → 翌日持ち越し判断をAIに委任（ロールオーバーコスト考慮）
XMT 23:05  ロールオーバー完了後、エントリー可能に復帰
           ※ ただしスプレッドが正常値に戻るまでエントリー待機

【DEAD_ZONEの扱い変更】
旧: XMT 22:00-23:59 → 新規エントリー「非推奨」
新: XMT 22:00-23:59 → 新規エントリー「ハードブロック」（RiskGuardianで強制拒否）
```

### 銘柄別クローズ時間設定（config.py追加）

```python
# 銘柄別の取引時間制約
MARKET_HOURS: dict = {
    "USDJPY": {
        "daily_close_start": "23:55",  # XMT
        "daily_close_end":   "00:05",
        "weekly_close":      "23:55",  # 金曜XMT
        "weekly_open":       "23:05",  # 日曜XMT
    },
    "EURUSD": {
        "daily_close_start": "23:55",
        "daily_close_end":   "00:05",
        "weekly_close":      "23:55",
        "weekly_open":       "23:05",
    },
    "GOLD": {
        "daily_close_start": "22:58",  # ゴールドは早めにクローズ
        "daily_close_end":   "23:06",
        "weekly_close":      "23:55",
        "weekly_open":       "23:05",
    },
}

# 週末前エントリー停止（金曜XMT何時以降か）
FRIDAY_ENTRY_CUTOFF_HOUR: int = 20

# 週末前強制決済（金曜XMT何時に実行か）
FRIDAY_FORCE_CLOSE_HOUR: int = 22

# 週末前強制決済の最終確認（金曜XMT何時に残存チェックか）
FRIDAY_FINAL_CHECK_HOUR: int = 22
FRIDAY_FINAL_CHECK_MINUTE: int = 30

# 週明けスプレッド安定待機（日曜オープン後何分待つか）
WEEKLY_OPEN_WAIT_MINUTES: int = 15
```

### broker_time.pyへの追加メソッド

```python
# 以下のメソッドをBrokerTimeクラスに追加
- is_market_open(symbol) -> bool         # 銘柄ごとの取引可能判定
- is_friday_cutoff() -> bool             # 金曜エントリー停止時間か
- is_weekend() -> bool                   # 土日（市場クローズ中）か
- is_near_daily_close(symbol) -> bool    # 日次クローズ直前か
- minutes_to_weekly_close() -> int       # 週末クローズまでの残り分数
- is_holiday() -> bool                   # 祝日チェック（年末年始等）
```

### risk_guardian.pyへの追加ガード

| ガード                  | 条件                    | アクション                    |
| ----------------------- | ----------------------- | ----------------------------- |
| 金曜エントリー停止      | 金曜 XMT 20:00以降      | 新規エントリー全拒否          |
| 週末前強制決済          | 金曜 XMT 22:00          | 全ポジション成行決済          |
| 週末残存チェック        | 金曜 XMT 22:30          | 残存ポジ再決済 + CRITICAL通知 |
| DEAD_ZONEハードブロック | XMT 22:00-23:59（毎日） | 新規エントリー強制拒否        |
| 日次クローズ前          | XMT 22:55 (GOLD)         | AI判断で決済/持越し判定       |
| 週明け安定待機          | 日曜オープン後15分      | スプレッド正常化まで待機      |

### trade_history.exit_reasonへの追加値

```
既存: TP_HIT / THESIS_BROKEN / SL_HIT / MANUAL / CB
追加: WEEKEND_CLOSE / DAILY_CLOSE / HOLIDAY_CLOSE
```

### スケジューラー追加ジョブ（main.py）

> **注意**: 以下のジョブはすべて Section 7.13 main.py のスケジューラー設定に統合済み。
> `AsyncIOScheduler(timezone=ZoneInfo("Europe/Athens"))` でXMT時間基準で発火するため、
> `day_of_week="fri"` はXMTの金曜日、`hour=20` はXMT 20:00 を意味する。

```python
# ※ 重複掲載（Section 7.13と同じ設定。実装は1箇所のみ）
# 金曜エントリー停止チェック → XMT金曜 20:00
scheduler.add_job(
    guardian.check_friday_cutoff,
    "cron", day_of_week="fri", hour=20, minute=0
)
# 金曜強制決済 → XMT金曜 22:00
scheduler.add_job(
    monitor.weekend_force_close,
    "cron", day_of_week="fri", hour=22, minute=0
)
# 金曜残存確認 → XMT金曜 22:30
scheduler.add_job(
    monitor.weekend_final_check,
    "cron", day_of_week="fri", hour=22, minute=30
)
# 日曜復帰チェック → XMT日曜 23:20
scheduler.add_job(
    guardian.check_weekly_open,
    "cron", day_of_week="sun", hour=23, minute=20
)
```

---

## 13. Discord通知仕様

### 通知必須イベント一覧

| イベント               | レベル   | タイミング           |
| ---------------------- | -------- | -------------------- |
| システム起動           | INFO     | 起動時               |
| エントリー承認         | INFO     | 注文執行後           |
| エントリー拒否         | INFO     | 拒否時（理由付き）   |
| H1査定完了             | INFO     | 毎時（サマリー形式） |
| TP更新                 | INFO     | 更新時               |
| 分割決済               | INFO     | 50%決済時            |
| 全決済（Thesis崩壊）   | INFO     | 決済時               |
| 相関アラート           | WARNING  | 検知時               |
| AI信頼度低（<0.65）    | WARNING  | エントリー時         |
| MONITOR_ONLY移行       | WARNING  | 状態変化時           |
| CB発動                 | CRITICAL | 即時                 |
| MT5切断検知            | CRITICAL | 60秒以内             |
| MT5再接続成功          | INFO     | 再接続時             |
| MT5再接続失敗(5回)     | CRITICAL | 全試行失敗時         |
| AI障害・フォールバック | CRITICAL | 発生時               |
| 手動介入要請           | CRITICAL | Layer2判定時         |
| Webhook認証失敗        | WARNING  | 不正アクセス時       |
| スプレッド異常で拒否   | WARNING  | エントリー時         |
| web_search失敗         | WARNING  | エントリー評価時     |
| セマンティック矛盾検知 | WARNING  | AI応答バリデーション |
| 相関アラート（HIGH）   | WARNING  | エントリー評価時     |
| 部分操作失敗           | CRITICAL | MT5/DB操作失敗時     |
| WAIT期限切れ           | INFO     | WAIT→破棄時         |
| 金曜エントリー停止     | INFO     | 金曜 XMT 20:00       |
| 週末前全決済完了       | INFO     | 金曜 XMT 22:00       |
| 週末残存ポジ検知       | CRITICAL | 金曜 XMT 22:30       |
| 週明け稼働再開         | INFO     | 日曜 XMT 23:20       |
| システムシャットダウン | WARNING  | シャットダウン時     |
| 未追跡ポジション検出   | WARNING  | 起動時リカバリー     |
| 日次レポート           | DAILY    | XMT 07:00            |

---

## 14. デモ・本番切り替え設計

### 切り替え方法

```python
# config.py の1行だけ変更する
IS_DEMO_MODE: bool = True   # デモ
IS_DEMO_MODE: bool = False  # 本番
```

### 切り替え時の自動変化

| 項目            | デモ           | 本番            |
| --------------- | -------------- | --------------- |
| MT5ログイン     | MT5_LOGIN_DEMO | MT5_LOGIN_LIVE  |
| サーバー        | XMTrading-Demo | XMTrading-Real3 |
| Discord起動通知 | 🧪 DEMO MODE   | 🔴 LIVE MODE    |

### 本番切り替え前チェックリスト

```
□ デモで30トレード以上の実績確認
□ AIのThesis崩壊判定精度を手動評価（trade_historyで確認）
□ CB発動テストをデモで実施済み
□ 週末クローズ・週明けオープンのサイクルを最低2回テスト済み
□ Discord通知が全て正常動作
□ ロット計算のverify_calculationが全ケースでis_safe=True
□ Webhook認証が正常動作（不正リクエスト拒否確認）
□ MT5再接続が正常動作（VPS再起動テスト）
□ グレースフルシャットダウン動作確認
□ .envのLIVEログイン情報を設定
□ IS_DEMO_MODE = False に変更
□ 起動後にDiscordで口座番号・残高を確認
```

---

## 15. 運用堅牢性設計

### ヘルスチェックエンドポイント

VPSのサイレントクラッシュを検知するため、外部監視サービスから死活監視を行う。

**FastAPIエンドポイント:** `GET /health`

```python
@app.get("/health")
async def health_check():
    """外部監視サービス（UptimeRobot等）からのポーリング用"""
    checks = {
        "mt5_connected": mt5_client.is_connected(),
        "db_accessible": thesis_db.ping(),
        "scheduler_running": scheduler.running,
        "system_status": guardian.status,  # ACTIVE / MONITOR_ONLY / LOCKED / WEEKEND_CLOSED
        "active_positions": len(mt5_client.get_all_positions()),
        "uptime_seconds": (datetime.utcnow() - app_start_time).total_seconds(),
        "last_h1_batch": monitor.last_h1_batch_time,  # 最後のH1バッチ実行時刻
    }
    status_code = 200 if checks["mt5_connected"] and checks["scheduler_running"] else 503
    return JSONResponse(content=checks, status_code=status_code)
```

**追加エンドポイント:** `GET /status`

```python
# より詳細なシステム状態（Discord通知のJSON版）
# 認証付き（Bearer Token）
@app.get("/status")
async def system_status(token: str = Header()):
    if token != CONFIG.STATUS_API_TOKEN:
        raise HTTPException(status_code=401)
    return {
        "mode": "DEMO" if CONFIG.IS_DEMO_MODE else "LIVE",
        "balance_jpy": await mt5_client.get_account_balance(),
        "daily_pnl_jpy": await mt5_client.get_daily_pnl(),
        "positions": await mt5_client.get_all_positions(),
        "db_stats": thesis_db.get_db_stats(),
        "broker_time": BrokerTime.now_str(),
    }
```

### OpenAI APIレート制限・エラーハンドリング

```python
# 429 Too Many Requestsへの対応
AI_MAX_RETRIES: int = 3
AI_RETRY_BACKOFF_SEC: list = [2, 5, 10]

async def call_ai_with_retry(model, messages, **kwargs):
    for attempt, wait in enumerate(AI_RETRY_BACKOFF_SEC):
        try:
            response = await openai_client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},  # JSON mode強制
                **kwargs
            )
            return response
        except openai.RateLimitError:
            logger.warning(f"AI rate limit hit, retry {attempt+1}")
            await asyncio.sleep(wait)
        except openai.APITimeoutError:
            logger.warning(f"AI timeout, retry {attempt+1}")
            continue
    return None  # 全リトライ失敗 → フォールバックへ
```

> **重要**: `response_format={"type": "json_object"}` を全AI呼び出しで使用し、
> JSON解析の不確実性を排除する。

### グレースフルシャットダウン

> **Windows互換性に関する重要な注意:**
> `signal.signal()` のコールバック内で `asyncio.create_task()` を呼ぶと、
> Windows環境では以下の理由で動作しない:
> 1. シグナルハンドラーはメインスレッドで**同期的**に実行される
> 2. `asyncio.create_task()` は実行中のイベントループが必要だが、
>    シグナルハンドラー内ではイベントループにアクセスできない
> 3. `loop.add_signal_handler()` はWindows（ProactorEventLoop）で `NotImplementedError`
>
> **解決策**: `loop.call_soon_threadsafe()` でコルーチンを安全にスケジュールする。

```python
import signal
import asyncio

async def graceful_shutdown(sig):
    """Ctrl+C / SIGTERM受信時の安全なシャットダウン"""
    logger.info(f"シャットダウンシグナル受信: {sig}")
  
    # 1. 新規エントリーを即時停止
    guardian.set_status("LOCKED")
  
    # 2. 実行中のスケジューラージョブ完了を待機（最大30秒）
    scheduler.shutdown(wait=True)
  
    # 3. 保有ポジションは決済しない（物理SLが守る）
    #    ただしDB上のACTIVE thesisは維持
  
    # 4. Discord通知
    await notifier.send("⚠️ システムシャットダウン。ポジション維持中。", level="WARNING")
  
    # 5. DB接続クローズ
    await thesis_db.close()
  
    # 6. MT5切断
    mt5.shutdown()

    # 7. PIDファイル削除
    release_exclusive_lock()

    # 8. イベントループ停止
    asyncio.get_event_loop().stop()


def setup_shutdown_handlers(loop: asyncio.AbstractEventLoop):
    """
    Windows互換のグレースフルシャットダウンハンドラー登録。
    main.py の起動時（イベントループ取得後）に呼び出すこと。

    【なぜ loop.call_soon_threadsafe() が必要か】
    signal.signal() のコールバックはメインスレッドで同期的に実行され、
    asyncioイベントループとは別コンテキストで動作する。
    call_soon_threadsafe() はスレッドセーフにイベントループへ
    コルーチンのスケジュールを依頼する唯一の安全な方法。
    """
    _shutdown_triggered = False

    def _signal_handler(sig, frame):
        nonlocal _shutdown_triggered
        if _shutdown_triggered:
            return  # 二重シグナル防止
        _shutdown_triggered = True
        # メインスレッドからイベントループへ安全にスケジュール
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(graceful_shutdown(sig))
        )

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
```

**main.pyでの使用:**

```python
# main.py 起動時
async def main():
    loop = asyncio.get_running_loop()
    setup_shutdown_handlers(loop)  # ← ループ取得後に登録
    # ... FastAPI起動等 ...
```

### 起動時リカバリー

```python
async def startup_recovery():
    """前回のシャットダウンが正常でなかった場合のリカバリー"""
  
    # 0. 前回の異常終了検知（PIDファイルが残っている場合）
    if os.path.exists(PID_FILE_PATH):
        await notifier.send(
            "⚠️ 前回の異常終了を検出しました。起動時リカバリーを実行します。\n"
            f"前回PID: {open(PID_FILE_PATH).read().strip()}",
            level="WARNING"
        )

    # 1. DB内のACTIVE thesisとMT5上のポジションを照合
    active_theses = thesis_db.get_active_theses()
    mt5_positions = mt5_client.get_all_positions()
  
    mt5_tickets = {p.ticket for p in mt5_positions}
    recovery_actions = []
  
    for thesis in active_theses:
        if thesis.ticket not in mt5_tickets:
            # MT5上にポジションがないのにDBがACTIVE → CLOSEDに修正
            logger.warning(f"孤立Thesis検出: {thesis.trade_id}")
            thesis_db.update_status(thesis.trade_id, "CLOSED")
            recovery_actions.append(
                f"孤立Thesis修正: {thesis.trade_id[:8]} ({thesis.symbol})"
            )
            # trade_historyにexit_reason="UNKNOWN_CLOSE"で記録
  
    # 2. MT5上にあるがDB上にないポジション → 手動ポジションとして警告
    db_tickets = {t.ticket for t in active_theses}
    for pos in mt5_positions:
        if pos.ticket not in db_tickets:
            recovery_actions.append(
                f"未追跡ポジション: {pos.symbol} #{pos.ticket}"
            )

    # 3. スケジューラー状態の確認（未実行ジョブの検知）
    # 前回の最後のH1バッチ実行時刻をDBから取得
    last_h1 = await thesis_db.get_last_review_time()
    if last_h1:
        hours_missed = (BrokerTime.now() - last_h1).total_seconds() / 3600
        if hours_missed > 2:
            recovery_actions.append(
                f"H1バッチ未実行検知: 最終実行から{hours_missed:.1f}時間経過"
            )

    # 4. リカバリー結果をDiscordに通知
    if recovery_actions:
        actions_text = "\n".join(f"  ・{a}" for a in recovery_actions)
        await notifier.send(
            f"⚠️ 起動時リカバリー実行完了\n"
            f"検出・修正項目({len(recovery_actions)}件):\n{actions_text}",
            level="WARNING"
        )
    else:
        await notifier.send(
            "✅ 起動時リカバリー: 異常なし（正常状態で起動）",
            level="INFO"
        )
```

### マルチインスタンス防止（排他制御）

> 同一設定で2つのプロセスが同時起動すると、MT5への重複注文・DB競合が発生する。
> PIDファイル + ポートバインドの二重チェックで排他制御を行う。

```python
import os
import sys

PID_FILE_PATH = "trading_system.pid"
FASTAPI_PORT = 8080  # config.pyで設定

def acquire_exclusive_lock() -> bool:
    """
    排他制御の取得（起動時に呼出・main.pyの最初に実行）

    二重チェック方式:
    1. PIDファイル: 前回プロセスが生存中かチェック
    2. ポートバインド: FastAPIのポートが既に使用中かチェック

    Returns: True=ロック取得成功, False=別インスタンスが稼働中
    """
    # Check 1: PIDファイル
    if os.path.exists(PID_FILE_PATH):
        old_pid = int(open(PID_FILE_PATH).read().strip())
        if _is_process_alive(old_pid):
            logger.error(f"別インスタンスが稼働中 (PID={old_pid})")
            return False
        else:
            logger.warning(f"前回のPIDファイル残存 (PID={old_pid}, 既に停止)")
            # 古いPIDファイルを削除して続行

    # Check 2: ポートバインド
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", FASTAPI_PORT))
        sock.close()
    except OSError:
        logger.error(f"ポート {FASTAPI_PORT} が既に使用中")
        return False

    # ロック取得成功: PIDファイルを書き込み
    with open(PID_FILE_PATH, "w") as f:
        f.write(str(os.getpid()))

    return True

def _is_process_alive(pid: int) -> bool:
    """Windowsでプロセスが生存中か確認"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False

def release_exclusive_lock():
    """シャットダウン時にPIDファイルを削除"""
    if os.path.exists(PID_FILE_PATH):
        os.remove(PID_FILE_PATH)
```

**main.pyでの使用:**

```python
# main.py 先頭
if not acquire_exclusive_lock():
    print("エラー: 別のインスタンスが既に稼働中です。起動を中止します。")
    sys.exit(1)

# graceful_shutdown内
async def graceful_shutdown(sig):
    # ... 既存のシャットダウン処理 ...
    release_exclusive_lock()  # PIDファイル削除
```

### ログ設計

```python
import logging
from logging.handlers import RotatingFileHandler

# 構造化ログ（JSON形式）
LOG_FORMAT = '{"time":"%(asctime)s","level":"%(levelname)s","module":"%(name)s","msg":"%(message)s"}'

# ファイルローテーション
handler = RotatingFileHandler(
    "logs/trading_system.log",
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5,          # 5世代保持
    encoding="utf-8"
)

# ログレベル別ファイル分離
# trading_system.log     → INFO以上（全ログ）
# trading_system_error.log → ERROR以上（エラーのみ）
```

### 設定値の起動時バリデーション

```python
def validate_config():
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
```

---

## 16. バックテスト設計

### 基本方針

> TradingViewのアラートログ + MT5の過去価格データを組み合わせて、
> Thesis駆動型トレードの事後検証を行う。リアルタイムのAI呼び出しを
> バックテスト中に行うため、完全な再現性よりも「AIの判断傾向分析」に重点を置く。

### データソース

```
1. TradingViewアラートログ（CSV/JSON）
   ・実際に発火したM15シグナルの履歴
   ・symbol, direction, price, broker_time, pattern, RSI, ATR等
   ・TradingViewの「アラートログ」機能からエクスポート

2. MT5過去価格データ
   ・mt5.copy_rates_range(symbol, timeframe, date_from, date_to)
   ・H1足データ（Thesis評価のコンテキスト用）
   ・M15足データ（エントリー価格の検証用）
   ・スプレッド情報は過去データに含まれないため、固定値で代用

3. trade_history テーブル（実運用開始後）
   ・実際のトレード結果との比較検証
```

### バックテストパイプライン

```
┌────────────────────────────────────────────────────────┐
│  backtest/thesis_backtester.py                         │
├────────────────────────────────────────────────────────┤
│                                                        │
│  Step 1: アラートログ読み込み                           │
│  ├─ TradingViewアラートCSV/JSONをパース                │
│  └─ Webhookペイロード形式に変換                        │
│                                                        │
│  Step 2: 各シグナルに対してAI評価を実行                 │
│  ├─ mt5.copy_rates_range()でH1/M15足を取得            │
│  ├─ その時点の市場コンテキストを構築                    │
│  ├─ GPT-4o-mini（コスト削減）でエントリー評価          │
│  │   └─ web_searchは無効化（過去のニュースは取得不可） │
│  └─ AI判定: APPROVE / REJECT / WAIT                   │
│                                                        │
│  Step 3: 仮想ポートフォリオで実行                      │
│  ├─ APPROVE → 仮想エントリー（entry_price = 当時の価格）│
│  ├─ SL/TPは物理SL範囲制限を適用                       │
│  └─ 以降のH1足で結果を判定:                           │
│      ├─ SLヒット → 損失記録                           │
│      ├─ TPヒット → 利益記録                           │
│      └─ 24時間経過 → 強制クローズ（終値で決済）       │
│                                                        │
│  Step 4: レポート生成                                  │
│  ├─ 勝率 / PF(プロフィットファクター) / 最大DD        │
│  ├─ AI承認率（APPROVE / REJECT / WAIT の比率）        │
│  ├─ 銘柄別・セッション別の成績分析                     │
│  ├─ AIのconfidenceと実結果の相関分析                   │
│  └─ Discord or HTMLレポート出力                        │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### バックテスト設定

```python
@dataclass
class BacktestConfig:
    # データ期間
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"

    # AIモデル（コスト削減のためminiを使用）
    model: str = "gpt-4o-mini"

    # web_searchは無効化（過去のニュース取得不可）
    enable_web_search: bool = False

    # 仮想口座
    initial_balance_jpy: float = 500_000
    risk_per_trade_pct: float = 1.0

    # 決済ルール（H1バッチのAI判定は省略し、TP/SLのみで判定）
    max_hold_hours: int = 24     # 最大保有時間
    use_fixed_spread: bool = True
    fixed_spread_points: dict = None  # {"USDJPY": 15, "EURUSD": 12, "GOLD": 25}

    # コスト制御
    max_signals_to_test: int = 200   # テスト対象シグナル上限
    api_cost_limit_usd: float = 5.0  # 1回のバックテストのAPI費用上限
```

### 制約事項

```
1. web_searchが使えないため、ファンダメンタルズ判断の精度は実運用より低い
2. スプレッドは固定値代用のため、指標発表前後の拡大は再現できない
3. H1バッチによるTP更新・Thesis崩壊判定は省略（TP/SLのみで決済）
4. スリッページは考慮しない
→ 楽観的な結果が出るため、実運用の成績は10-20%程度低下を想定すること
```

---

## 17. テスト戦略

### 基本方針

> 個人開発のため本格的なCI/CDテストスイートは不要。
> 「資金を失うバグ」だけは確実に防ぐ最小限のテスト + デモ運用での実地検証を行う。

### テスト階層

```
┌────────────────────────────────────────────────────────┐
│  Level 1: ユニットテスト（pytest・必須・自動実行）      │
│                                                        │
│  ・lot_calculator.py → 各銘柄のロット計算正確性        │
│  ・broker_time.py    → 冬/夏時間変換・セッション判定   │
│  ・risk_guardian.py   → CB発動閾値・ガード条件判定     │
│  ・セマンティックバリデーション → 方向矛盾検知          │
│  ・相関アラート     → グループ検知ロジック             │
│                                                        │
│  → 起動時に自動実行。失敗したら起動中止。              │
├────────────────────────────────────────────────────────┤
│  Level 2: モック統合テスト（AIレスポンスを固定値で検証） │
│                                                        │
│  ・entry_evaluator.py にモックAIレスポンスを注入        │
│  ・「AI→APPROVE→ロット計算→MT5注文→DB保存→Discord」    │
│    の一連フローが通ることを確認                         │
│  ・異常系テスト:                                       │
│    - AI→APPROVE だがconfidence=0.3 → REJECT上書き     │
│    - AI→APPROVE だがTP方向矛盾 → REJECT上書き         │
│    - MT5注文失敗 → Discord CRITICAL通知                │
│    - DB保存失敗 → Discord CRITICAL通知                 │
│                                                        │
│  → デプロイ前に手動実行。                              │
├────────────────────────────────────────────────────────┤
│  Level 3: デモ口座実地テスト（実運用と同一コード）      │
│                                                        │
│  ・IS_DEMO_MODE=True でデモ口座に接続                  │
│  ・実際のWebhookシグナルでフルパイプラインを実行        │
│  ・最低30トレード収集してから本番移行                   │
│  ・週末クローズ・週明けオープンを最低2サイクル確認      │
│  ・CB（サーキットブレーカー）を意図的に発動テスト       │
│  ・グレースフルシャットダウン → 再起動の動作確認        │
│                                                        │
│  → 本番切替チェックリスト（Section 14）を全て通過。    │
└────────────────────────────────────────────────────────┘
```

### モックAIレスポンスの設計

```python
# tests/mock_ai.py
MOCK_RESPONSES = {
    "approve_normal": {
        "decision": "APPROVE", "confidence": 0.82,
        "thesis": "テスト用Thesis: EMA21>EMA50でトレンド継続...",
        "invalidation_conditions": ["149.00下抜け", "FOMC反転", "ゴールド急騰"],
        "initial_tp": 150.50, "emergency_sl": 148.80,
        "risk_multiplier": 1.0, "market_regime": "TRENDING",
        "reject_reason": None
    },
    "approve_low_confidence": {
        "decision": "APPROVE", "confidence": 0.3,  # バリデーションでREJECTされるべき
        ...
    },
    "approve_wrong_direction": {
        "decision": "APPROVE", "confidence": 0.85,
        "thesis": "下落トレンドが継続...",  # LONGシグナルに対して矛盾
        "initial_tp": 148.00,  # LONGなのにTP<現在値
        ...
    },
}

class MockAIClient:
    """テスト用AIクライアント（OpenAI APIを呼ばない）"""
    def __init__(self, scenario: str = "approve_normal"):
        self.scenario = scenario
        self.call_count = 0

    async def chat_completions_create(self, **kwargs):
        self.call_count += 1
        return MockResponse(MOCK_RESPONSES[self.scenario])
```

### デモ運用チェックリスト（本番移行判定基準）

```
□ ユニットテスト全パス（lot_calculator / broker_time / risk_guardian）
□ モック統合テスト全パス（正常系 + 異常系5パターン以上）
□ デモで30トレード以上の実績
□ AI APPROVE率が30-70%の範囲（偏りすぎは設定ミス疑い）
□ セマンティックバリデーション発動が0件（AIが安定している）
□ 週末クローズ・週明けオープン 2サイクル正常動作
□ CB発動テスト（日次DD 3%超で全決済+LOCKED確認）
□ MT5切断→再接続テスト（VPS再起動）
□ マルチインスタンス防止テスト（2重起動でエラー確認）
□ グレースフルシャットダウン→再起動→リカバリー確認
□ Discord全通知パターンの目視確認
```

---

## 18. 実装ロードマップ

### Phase 1：基盤（Week 1-2）

```
Step 1: config.py + .env設定 + 起動時バリデーション
Step 2: broker_time.py + テスト（市場クローズ判定含む）
Step 3: lot_calculator.py + テスト（symbol_info()ベース・最重要）
Step 4: mt5_client.py（デモ口座で接続・注文テスト・再接続・ロック機構）
Step 5: thesis_db.py（スキーマ + Dedicated Writer + パージ + ai_audit_log）
Step 6: discord_notifier.py（通知テスト）
```

### Phase 2：入力層（Week 3）

```
Step 7: economic_calendar.py（ForexFactory RSS）
Step 8: webhook_receiver.py（FastAPI + 認証 + 重複排除）
Step 9: TradingView Pine Script作成・Webhook疎通テスト
Step 10: ヘルスチェックエンドポイント実装
Step 10b: マルチインスタンス防止（PIDファイル+ポートバインド）
```

### Phase 3：AIエンジン（Week 4-5）

```
Step 11: prompt_builder.py
Step 12: entry_evaluator.py（デモで1銘柄からテスト・スプレッドチェック・WAITハンドリング・web_searchフォールバック含む）
Step 13: position_monitor.py（H1バッチ + Layer1/2 + 部分失敗リカバリー）
Step 14: risk_guardian.py（CB発動テスト + 相関アラート + 市場クローズガード + 週末管理テスト必須）
```

### Phase 4：統合・検証（Week 6-8）

```
Step 15: main.py（全コンポーネント統合 + グレースフルシャットダウン + 起動時リカバリー + 排他制御）
Step 15b: ユニットテスト + モック統合テスト実装（Level 1-2）
Step 16: デモ運用開始（3銘柄）
Step 17: 週末クローズ・週明けオープンの実地テスト（最低2週末）
Step 18: 30トレード収集・AIプロンプト改善
Step 19: backtest/thesis_backtester.py で過去検証
```

### Phase 5：本番移行（Month 3+）

```
Step 20: 本番切り替えチェックリスト実施（Section 14 + Section 17）
Step 21: 少額（1〜3万円相当ポジ）で本番開始
Step 22: 継続的なプロンプトチューニング
```

---

## 19. コスト試算

### OpenAI APIコスト（月間・デモ運用時）

```
【GPT-4o 料金基準】
Input:  $2.50 / 1M tokens
Output: $10.00 / 1M tokens

【GPT-4o-mini 料金基準】
Input:  $0.15 / 1M tokens
Output: $0.60 / 1M tokens

【月間使用量試算（3銘柄・H1監視）】
エントリー評価（GPT-4o）: 月30回 × 1,000tokens = 30K tokens
H1バッチ（GPT-4o）:       月720回 × 1,200tokens = 864K tokens
緊急判定（GPT-4o-mini）:  月60回  × 300tokens  = 18K tokens

月間コスト概算: $3〜6
```

### システム全体コスト（月間）

| 項目           | 費用                                  |
| -------------- | ------------------------------------- |
| OpenAI API     | $3〜6                                 |
| Windows VPS    | $10〜20                               |
| TradingView    | 無料〜$15                             |
| Forex Factory  | 無料                                  |
| **合計** | **$13〜41（約2,000〜6,000円）** |

---

## 付録：依存ライブラリ（requirements.txt）

```
fastapi>=0.110.0
uvicorn>=0.27.0
MetaTrader5>=5.0.45
openai>=1.30.0
apscheduler>=3.10.4
python-dotenv>=1.0.0
httpx>=0.27.0
pandas>=2.2.0
aiohttp>=3.9.0
aiosqlite>=0.20.0
pydantic>=2.6.0
```

---

*本設計書はClaude Sonnet 4.6との討論に基づき作成（v5.0改訂: Claude Opus 4.6）。実装はClaude Opus（VSCode Agent）を使用すること。*
