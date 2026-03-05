"""
core/lot_calculator.py — JPY口座専用ロット計算

mt5.symbol_info() による動的パラメータ取得を使用し、
ハードコードされたpip値・コントラクトサイズは使用しない。
"""

import logging
from datetime import datetime
from typing import Optional

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

# シンボル情報キャッシュ
_symbol_cache: dict[str, dict] = {}
_cache_updated_at: Optional[datetime] = None
CACHE_TTL_SEC: int = 3600  # 1時間


def get_symbol_params(symbol: str) -> dict:
    """MT5からシンボルの取引パラメータを動的に取得する"""
    global _symbol_cache, _cache_updated_at

    now = datetime.utcnow()
    if (
        symbol in _symbol_cache
        and _cache_updated_at
        and (now - _cache_updated_at).total_seconds() < CACHE_TTL_SEC
    ):
        return _symbol_cache[symbol]

    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"symbol_info取得失敗: {symbol}")

    params = {
        "trade_contract_size": info.trade_contract_size,
        "point": info.point,
        "digits": info.digits,
        "volume_min": info.volume_min,
        "volume_max": info.volume_max,
        "volume_step": info.volume_step,
        "currency_profit": info.currency_profit,
    }
    _symbol_cache[symbol] = params
    _cache_updated_at = now
    return params


def _get_jpy_conversion_rate(currency: str) -> float:
    """利益通貨をJPY変換するレートを取得"""
    if currency == "JPY":
        return 1.0
    pair = f"{currency}JPY"
    tick = mt5.symbol_info_tick(pair)
    if tick is None:
        raise ValueError(f"JPY変換レート取得失敗: {pair}")
    return (tick.bid + tick.ask) / 2  # mid price


def calculate(
    symbol: str,
    sl_pips: float,
    risk_pct: float,
    balance_jpy: float,
    risk_multiplier: float = 1.0,
) -> float:
    """
    JPY口座のロットサイズを計算する。

    Args:
        symbol: 銘柄名 (USDJPY, EURUSD, GOLD)
        sl_pips: ストップロスまでのpips数
        risk_pct: リスク率 (1.0 = 1%)
        balance_jpy: 口座残高（JPY）
        risk_multiplier: AIからのリスク倍率 (0.5〜1.5)

    Returns:
        lot: ロットサイズ（ブローカー制限内で丸め済み）
    """
    if sl_pips <= 0:
        raise ValueError(f"SL pipsが不正: {sl_pips}")

    params = get_symbol_params(symbol)
    pip_size = params["point"] * 10
    pip_value_raw = params["trade_contract_size"] * pip_size

    # 利益通貨 → JPY変換
    if params["currency_profit"] == "JPY":
        pip_value_jpy = pip_value_raw
    else:
        conversion_rate = _get_jpy_conversion_rate(params["currency_profit"])
        pip_value_jpy = pip_value_raw * conversion_rate

    risk_jpy = balance_jpy * (risk_pct / 100) * risk_multiplier
    lot = risk_jpy / (sl_pips * pip_value_jpy)

    # ブローカー制限に丸め
    lot = max(params["volume_min"], min(params["volume_max"], lot))
    lot = round(lot / params["volume_step"]) * params["volume_step"]

    return round(lot, 2)


def verify_calculation(
    symbol: str,
    lot: float,
    sl_pips: float,
    balance_jpy: float,
) -> dict:
    """
    注文前の最終サニティチェック。

    Returns:
        {
            "is_safe": bool,
            "risk_jpy": float,
            "risk_pct": float,
            "pip_value_jpy": float,
            "lot": float,
            "warnings": list[str],
        }
    """
    params = get_symbol_params(symbol)
    pip_size = params["point"] * 10
    pip_value_raw = params["trade_contract_size"] * pip_size

    if params["currency_profit"] == "JPY":
        pip_value_jpy = pip_value_raw
    else:
        conversion_rate = _get_jpy_conversion_rate(params["currency_profit"])
        pip_value_jpy = pip_value_raw * conversion_rate

    risk_jpy = lot * sl_pips * pip_value_jpy
    risk_pct = (risk_jpy / balance_jpy * 100) if balance_jpy > 0 else 100

    warnings = []
    is_safe = True

    if risk_pct > 3.0:
        warnings.append(f"リスク率が{risk_pct:.1f}%（3%超過・危険）")
        is_safe = False
    elif risk_pct > 2.0:
        warnings.append(f"リスク率が{risk_pct:.1f}%（2%超過・注意）")

    if lot < params["volume_min"]:
        warnings.append(f"ロット({lot})が最小ロット({params['volume_min']})未満")
        is_safe = False

    if lot > params["volume_max"]:
        warnings.append(f"ロット({lot})が最大ロット({params['volume_max']})超過")
        is_safe = False

    return {
        "is_safe": is_safe,
        "risk_jpy": round(risk_jpy, 0),
        "risk_pct": round(risk_pct, 2),
        "pip_value_jpy": round(pip_value_jpy, 2),
        "lot": lot,
        "warnings": warnings,
    }


def refresh_cache() -> None:
    """キャッシュを強制更新（毎時の定期更新用）"""
    global _symbol_cache, _cache_updated_at
    _symbol_cache.clear()
    _cache_updated_at = None
    logger.info("シンボルキャッシュをクリアしました")
