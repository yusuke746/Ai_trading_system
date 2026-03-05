"""
ingestion/webhook_receiver.py — TradingView Webhook受信

FastAPIエンドポイント: POST /webhook
認証 + バリデーション + 重複排除 + データ補完 + 非同期パイプライン
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import MetaTrader5 as mt5
from fastapi import APIRouter, Request, HTTPException

from config import CONFIG
from core.models import WebhookPayload
from core.broker_time import BrokerTime

logger = logging.getLogger(__name__)

router = APIRouter()

# 重複排除キャッシュ
_recent_signals: dict[str, datetime] = {}
DEDUP_WINDOW_SEC: int = 300  # 5分

# コンポーネント参照（main.pyからset_dependenciesで注入）
_entry_evaluator = None
_notifier = None


def set_dependencies(entry_evaluator=None, notifier=None):
    global _entry_evaluator, _notifier
    _entry_evaluator = entry_evaluator
    _notifier = notifier


# ──────────── 認証 ────────────

def verify_webhook(payload: dict) -> bool:
    """Webhookシークレットトークン検証"""
    return payload.get("secret") == CONFIG.WEBHOOK_SECRET


# ──────────── 重複排除 ────────────

def _dedup_key(payload: dict) -> str:
    return hashlib.md5(
        f"{payload.get('symbol', '')}:{payload.get('direction', '')}:{payload.get('price', '')}".encode()
    ).hexdigest()


def is_duplicate(payload: dict) -> bool:
    """同一シグナルの重複を5分間排除（チェックのみ、登録しない）"""
    key = _dedup_key(payload)
    now = datetime.now(timezone.utc)
    return key in _recent_signals and (now - _recent_signals[key]).total_seconds() < DEDUP_WINDOW_SEC


def register_dedup(payload: dict) -> None:
    """処理確定後にデdup登録する"""
    global _recent_signals
    key = _dedup_key(payload)
    now = datetime.now(timezone.utc)
    _recent_signals[key] = now
    # 古いキーをクリーンアップ
    _recent_signals = {
        k: v for k, v in _recent_signals.items()
        if (now - v).total_seconds() < DEDUP_WINDOW_SEC
    }


# ──────────── データ補完 ────────────

async def supplement_technical_data(payload: WebhookPayload) -> WebhookPayload:
    """
    外部インジケータからのWebhookに不足しているテクニカルデータを
    MT5から取得して補完する。カスタムインジケータは補完不要。
    """
    if not payload.needs_supplement():
        return payload

    # M15足データをMT5から取得（EMA200に必要な210本）
    rates = mt5.copy_rates_from_pos(payload.symbol, mt5.TIMEFRAME_M15, 0, 210)
    if rates is None or len(rates) < 200:
        logger.warning(f"MT5データ取得失敗: {payload.symbol}, 補完スキップ")
        return payload

    closes = np.array([r[4] for r in rates])  # close price
    highs = np.array([r[2] for r in rates])   # high
    lows = np.array([r[3] for r in rates])    # low

    # EMA計算
    def calc_ema(data, period):
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]
        k = 2 / (period + 1)
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i - 1] * (1 - k)
        return float(ema[-1])

    # RSI計算
    def calc_rsi(data, period=14):
        deltas = np.diff(data[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)

    # ATR計算
    def calc_atr(highs_arr, lows_arr, closes_arr, period=14):
        prev_close = np.roll(closes_arr, 1)
        prev_close[0] = closes_arr[0]
        tr = np.maximum(
            highs_arr[-period:] - lows_arr[-period:],
            np.maximum(
                np.abs(highs_arr[-period:] - prev_close[-period:]),
                np.abs(lows_arr[-period:] - prev_close[-period:]),
            ),
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
        # ATR SMAを計算
        atr_values = []
        for i in range(max(0, len(closes) - 20), len(closes) - 13):
            atr_values.append(calc_atr(highs[:i + 14], lows[:i + 14], closes[:i + 14]))
        atr_sma = float(np.mean(atr_values)) if atr_values else payload.atr
        payload.atr_ratio = round(payload.atr / atr_sma, 2) if atr_sma > 0 else 1.0

    # H1トレンド補完
    if not payload.h1_trend:
        h1_ema21 = calc_ema(closes, 21 * 4)
        h1_ema50 = calc_ema(closes, 50 * 4)
        payload.h1_trend = "BULLISH" if h1_ema21 > h1_ema50 else "BEARISH"

    logger.info(f"データ補完完了: {payload.symbol} ({payload.source})")
    return payload


# ──────────── バリデーション ────────────

def validate_payload(data: dict) -> tuple[bool, str]:
    """Webhookペイロードの基本バリデーション"""
    # 必須フィールド
    required = ["symbol", "direction", "price", "timeframe"]
    for field in required:
        if field not in data or data[field] is None:
            return False, f"必須フィールド欠落: {field}"

    # シンボルチェック
    symbol = data["symbol"]
    # XAUUSD → GOLD 変換はモデル側で行うのでここでは元値もOK
    if symbol not in CONFIG.SYMBOLS and symbol != "XAUUSD":
        return False, f"未対応シンボル: {symbol}"

    # 方向チェック
    if data["direction"] not in ("LONG", "SHORT"):
        return False, f"不正なdirection: {data['direction']}"

    # 価格チェック
    try:
        price = float(data["price"])
        if price <= 0:
            return False, f"不正な価格: {price}"
    except (TypeError, ValueError):
        return False, f"価格が数値でない: {data['price']}"

    return True, "OK"


# ──────────── エンドポイント ────────────

@router.post("/webhook")
async def receive_webhook(request: Request):
    """TradingView Webhook受信エンドポイント"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 認証チェック
    if not verify_webhook(data):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Webhook認証失敗: IP={client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    # バリデーション
    is_valid, reason = validate_payload(data)
    if not is_valid:
        logger.warning(f"Webhookバリデーション失敗: {reason}")
        raise HTTPException(status_code=422, detail=reason)

    # 重複排除
    if is_duplicate(data):
        logger.info(f"重複シグナル排除: {data.get('symbol')} {data.get('direction')}")
        return {"status": "duplicate", "message": "シグナルは既に処理中"}

    # Pydanticモデルに変換
    try:
        payload = WebhookPayload(**data)
    except Exception as e:
        logger.error(f"ペイロード変換エラー: {e}")
        raise HTTPException(status_code=422, detail=str(e))

    # 変換成功後にdedup登録（処理失敗時にリトライが通るよう、成功確定後に登録）
    register_dedup(data)

    logger.info(
        f"Webhook受信: {payload.symbol} {payload.direction} "
        f"price={payload.price} pattern={payload.pattern} source={payload.source}"
    )

    # 非同期でエントリー評価パイプラインを起動
    asyncio.create_task(_process_signal(payload))

    return {"status": "received", "message": f"シグナル受信: {payload.symbol} {payload.direction}"}


async def _process_signal(payload: WebhookPayload):
    """バックグラウンドでシグナル処理（Webhook即200 OK後に実行）"""
    try:
        # データ補完（外部インジケータの場合）
        payload = await supplement_technical_data(payload)

        # エントリー評価
        if _entry_evaluator:
            await _entry_evaluator.evaluate(payload)
        else:
            logger.error("entry_evaluatorが未設定")

    except Exception as e:
        logger.exception(f"シグナル処理エラー: {payload.symbol} - {e}")
        if _notifier:
            await _notifier.send(
                f"🔴 シグナル処理エラー: {payload.symbol}\n{type(e).__name__}: {e}",
                level="CRITICAL",
            )


# ──────────── ステータス API ────────────

@router.get("/status")
async def get_status():
    """システムステータスAPI"""
    # main.pyからguardian参照を取得
    from core.broker_time import BrokerTime

    return {
        "status": "running",
        "broker_time": BrokerTime.now_str(),
        "session": BrokerTime.get_session(),
    }
