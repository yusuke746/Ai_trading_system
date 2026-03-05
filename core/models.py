"""
core/models.py — データ型定義（Pydantic / Enum）

システム全体で使用するデータ構造を型安全に定義する。
モジュール間の受け渡しはすべてこれらの型を使用し、
dictの暗黙的なキーアクセスを排除する。
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum


# ═══════════════════════════════════════════════════════════════
# Enum定義
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
# Webhook受信データ
# ═══════════════════════════════════════════════════════════════

class WebhookPayload(BaseModel):
    """
    TradingViewから受信するWebhook JSON。
    カスタムインジケータは全フィールドを含む。
    外部インジケータ（LuxAlgo, Lorentzian等）は部分フィールドのみ。
    不足データは webhook_receiver.py がMT5から補完する。
    """
    secret: str
    symbol: str
    direction: Direction
    timeframe: str = "M15"
    h1_trend: str = ""
    price: float = Field(gt=0)
    pattern: str

    # テクニカルコンテキスト（Optional: 外部インジケータでは None → サーバー側で補完）
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
    broker_time: str = ""
    source: str = "custom_multistrat"

    @field_validator("symbol")
    @classmethod
    def symbol_must_be_valid(cls, v: str) -> str:
        symbol_map = {"XAUUSD": "GOLD"}
        v = symbol_map.get(v, v)
        valid = ("USDJPY", "EURUSD", "GOLD")
        if v not in valid:
            raise ValueError(f"無効なsymbol: {v} (有効: {valid})")
        return v

    def needs_supplement(self) -> bool:
        """MT5からのデータ補完が必要か判定"""
        return self.rsi is None or self.atr is None or self.ema21 is None


# ═══════════════════════════════════════════════════════════════
# MT5注文結果
# ═══════════════════════════════════════════════════════════════

class OrderResult(BaseModel):
    """MT5の注文・決済結果"""
    success: bool
    ticket: Optional[int] = None
    symbol: str = ""
    direction: Optional[Direction] = None
    lot: float = 0.0
    price: float = 0.0
    requested_price: float = 0.0
    slippage_points: float = 0.0
    retcode: int = 0
    error: Optional[str] = None
    comment: str = ""


# ═══════════════════════════════════════════════════════════════
# AIエントリー評価レスポンス
# ═══════════════════════════════════════════════════════════════

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

    @field_validator("invalidation_conditions")
    @classmethod
    def must_have_three_conditions(cls, v: list[str]) -> list[str]:
        if len(v) < 3:
            raise ValueError(
                f"invalidation_conditionsは3件必要（現在{len(v)}件）"
            )
        return v


# ═══════════════════════════════════════════════════════════════
# AIポジション監視レスポンス
# ═══════════════════════════════════════════════════════════════

class AIPositionInstruction(BaseModel):
    """H1バッチ監視の個別ポジション指示"""
    trade_id: str
    thesis_status: ThesisStatus
    action: PositionAction
    new_tp: Optional[float] = None
    close_percentage: int = Field(ge=0, le=100, default=0)
    reasoning: str
    urgency: str = "NORMAL"


class AIH1BatchResponse(BaseModel):
    """H1バッチ監視のGPT-4o出力"""
    positions: list[AIPositionInstruction]


# ═══════════════════════════════════════════════════════════════
# AI緊急判定レスポンス
# ═══════════════════════════════════════════════════════════════

class AIEmergencyResponse(BaseModel):
    """GPT-4o-miniの緊急判定出力"""
    action: str  # ALERT_HUMAN / CONTINUE_MONITORING
    reason: str


# ═══════════════════════════════════════════════════════════════
# ポジション情報（MT5 + Thesis統合）
# ═══════════════════════════════════════════════════════════════

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
    thesis_text: str = ""
    invalidation_conditions: list[str] = Field(default_factory=list)
    risk_multiplier: float = 1.0
    market_regime: str = ""
    ai_confidence: float = 0.0
    hold_hours: float = 0.0
    broker_entry_time: str = ""
    spread_at_entry: float = 0.0


# ═══════════════════════════════════════════════════════════════
# 相関アラート結果
# ═══════════════════════════════════════════════════════════════

class CorrelationAlert(BaseModel):
    """相関チェックの結果"""
    has_alert: bool
    alert_level: str = "NONE"
    overlapping_group: Optional[str] = None
    recommended_risk_multiplier: float = 1.0
    message: str = ""
