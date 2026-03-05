"""
ingestion/webhook_receiver.py — TradingView Webhook受信

FastAPIエンドポイント: POST /webhook
認証 + バリデーション + 重複排除 + シグナル集約バッファ + データ補完 + 非同期パイプライン
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

# シグナル集約バッファ: 同一 symbol+direction を短時間で束ねる
SIGNAL_BUFFER_SEC: float = 3.0  # 3秒間バッファ
_signal_buffer: dict[str, list[WebhookPayload]] = {}
_buffer_tasks: dict[str, asyncio.Task] = {}

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
    # priceは除外 — 同一銘柄・同一方向は価格差があっても5分間で1回のみ処理
    return hashlib.md5(
        f"{payload.get('symbol', '')}:{payload.get('direction', '')}".encode()
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

    # 重複排除（5分間の同一symbol+direction）
    if is_duplicate(data):
        logger.info(f"重複シグナル排除: {data.get('symbol')} {data.get('direction')}")
        return {"status": "duplicate", "message": "シグナルは既に処理中"}

    # Pydanticモデルに変換
    try:
        payload = WebhookPayload(**data)
    except Exception as e:
        logger.error(f"ペイロード変換エラー: {e}")
        raise HTTPException(status_code=422, detail=str(e))

    logger.info(
        f"Webhook受信: {payload.symbol} {payload.direction} "
        f"price={payload.price} pattern={payload.pattern} source={payload.source}"
    )

    # シグナルバッファに追加（3秒間同一symbol+directionを集約）
    buffer_key = f"{payload.symbol}:{payload.direction}"

    if buffer_key not in _signal_buffer:
        # 初回: バッファ作成 + 3秒後に処理するタスク起動
        _signal_buffer[buffer_key] = [payload]
        _buffer_tasks[buffer_key] = asyncio.create_task(
            _flush_buffer(buffer_key)
        )
        logger.info(f"バッファ開始: {buffer_key} (3秒後に処理)")
    else:
        # 追加: 既にバッファ中 → 追加のみ
        _signal_buffer[buffer_key].append(payload)
        logger.info(
            f"バッファ追加: {buffer_key} pattern={payload.pattern} "
            f"(現在{len(_signal_buffer[buffer_key])}件)"
        )

    return {"status": "received", "message": f"シグナル受信: {payload.symbol} {payload.direction}"}


async def _flush_buffer(buffer_key: str):
    """バッファ時間経過後、集約されたシグナルを1回のAI評価に流す"""
    await asyncio.sleep(SIGNAL_BUFFER_SEC)

    payloads = _signal_buffer.pop(buffer_key, [])
    _buffer_tasks.pop(buffer_key, None)

    if not payloads:
        return

    # dedup登録（最初のペイロードのデータで登録）
    first = payloads[0]
    register_dedup({"symbol": first.symbol, "direction": first.direction})

    # 全パターンを集約
    patterns = [p.pattern for p in payloads]
    sources = list(set(p.source for p in payloads))

    logger.info(
        f"バッファフラッシュ: {buffer_key} | "
        f"{len(payloads)}件集約 | patterns={patterns} | sources={sources}"
    )

    # ベストなペイロードを選定（カスタムインジケータ優先、なければ最初のもの）
    best_payload = next(
        (p for p in payloads if p.source == "custom_multistrat"), payloads[0]
    )

    try:
        # データ補完（外部インジケータの場合）
        best_payload = await supplement_technical_data(best_payload)

        # エントリー評価（集約パターン情報付き）
        if _entry_evaluator:
            await _entry_evaluator.evaluate(best_payload, all_patterns=patterns)
        else:
            logger.error("entry_evaluatorが未設定")

    except Exception as e:
        logger.exception(f"シグナル処理エラー: {buffer_key} - {e}")
        if _notifier:
            await _notifier.send(
                f"🔴 シグナル処理エラー: {buffer_key}\n{type(e).__name__}: {e}",
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
