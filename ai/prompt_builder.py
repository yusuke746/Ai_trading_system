"""
ai/prompt_builder.py — プロンプト構築

エントリー評価・H1監視・緊急判定の3種類のプロンプトを構築する。
トークン最適化: thesis_textは150文字に制限、共通コンテキストは1回のみ。
"""

import json
import logging
from typing import Optional

from core.broker_time import BrokerTime

logger = logging.getLogger(__name__)


# ──────────── システムプロンプト ────────────

ENTRY_SYSTEM_PROMPT = """あなたはプロのFXトレーダーの思考を持つトレード判断AIです。
提供されるテクニカルデータ・市場コンテキストを分析し、
厳格なJSON形式のみで回答してください。前置き・説明不要。

判断基準:
- テクニカル・ファンダメンタルズの整合性が取れている場合のみAPPROVE
- ai_confidence < 0.6 は必ずREJECT
- 重要指標30分以内はWAIT
- DEAD_ZONEセッション（XMT 22:00-23:59）はREJECT
- 金曜XMT 20:00以降はREJECT
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
}"""

H1_SYSTEM_PROMPT = """あなたはポジション管理専門のAIです。
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
}"""

EMERGENCY_SYSTEM_PROMPT = """FXポジション緊急判定AIです。
「今すぐ人間に通知すべきか」だけを判断してください。
JSONのみで回答。

出力JSON:
{
  "action": "ALERT_HUMAN|CONTINUE_MONITORING",
  "reason": "理由50字以内"
}"""


class PromptBuilder:
    """3種のプロンプトを構築する"""

    def build_entry_prompt(
        self,
        webhook_data: dict,
        session: str,
        h1_trend: str,
        exposure_pct: float,
        pos_count: int,
        correlation_alert: dict,
        todays_events: list[dict],
        all_patterns: list[str] | None = None,
    ) -> list[dict]:
        """エントリー評価プロンプトを構築"""
        # テクニカルデータ整形
        tech_data = {
            "symbol": webhook_data.get("symbol"),
            "direction": webhook_data.get("direction"),
            "pattern": webhook_data.get("pattern"),
            "source": webhook_data.get("source"),
            "price": webhook_data.get("price"),
            "rsi": webhook_data.get("rsi"),
            "atr": webhook_data.get("atr"),
            "atr_ratio": webhook_data.get("atr_ratio"),
            "ema21": webhook_data.get("ema21"),
            "ema50": webhook_data.get("ema50"),
            "ema200": webhook_data.get("ema200"),
            "h1_trend": h1_trend or webhook_data.get("h1_trend"),
            "timeframe": webhook_data.get("timeframe"),
        }

        # 経済指標情報
        events_text = "なし"
        if todays_events:
            events_list = [
                f"{e['time_str']} XMT {e['currency']} {e['title']}"
                for e in todays_events
            ]
            events_text = "\n".join(events_list)

        # 相関情報
        correlation_text = "なし"
        if correlation_alert.get("has_alert"):
            correlation_text = (
                f"{correlation_alert['alert_level']}: {correlation_alert['message']} "
                f"(推奨risk_multiplier: {correlation_alert['recommended_risk_multiplier']})"
            )

        # シグナル集約情報
        signal_confluence_text = ""
        if all_patterns and len(all_patterns) > 1:
            signal_confluence_text = (
                f"\n🔔 シグナル集約: {len(all_patterns)}戦略が同時発火 → "
                f"patterns={all_patterns}\n"
                f"複数戦略の一致は信頼度向上要因として考慮してください。"
            )

        now_xmt = BrokerTime.now()
        weekday_ja = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"][now_xmt.weekday()]

        user_content = (
            f"テクニカルシグナル: {json.dumps(tech_data, ensure_ascii=False)}\n"
            f"現在値: {webhook_data.get('price')}\n"
            f"セッション(XMT): {session}\n"
            f"現在時刻(XMT): {BrokerTime.now_str()} ({weekday_ja})\n"
            f"H1トレンド: {h1_trend}\n"
            f"口座状況: 総エクスポージャー {exposure_pct:.1f}% / 保有ポジ {pos_count}件\n"
            f"相関アラート: {correlation_text}\n"
            f"今日の重要指標:\n{events_text}"
            f"{signal_confluence_text}"
        )

        return [
            {"role": "system", "content": ENTRY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def build_h1_batch_prompt(
        self,
        positions_data: list[dict],
        session: str,
        major_news: str,
        volatility_regime: str,
    ) -> list[dict]:
        """H1バッチ監視プロンプトを構築"""
        # 各ポジションの情報を整形（thesis_textは150文字制限）
        positions_text = ""
        for pos in positions_data:
            thesis = pos.get("thesis_text", "N/A")[:150]
            invalidation = pos.get("invalidation", [])
            inv_text = ", ".join(invalidation) if isinstance(invalidation, list) else str(invalidation)

            positions_text += (
                f"\n---\n"
                f"trade_id: {pos.get('trade_id', 'N/A')}\n"
                f"symbol: {pos.get('symbol')} {pos.get('direction')}\n"
                f"entry_price: {pos.get('entry_price')}\n"
                f"current_price: {pos.get('current_price')}\n"
                f"current_tp: {pos.get('initial_tp')}\n"
                f"current_sl: {pos.get('emergency_sl')}\n"
                f"pnl_pips: {pos.get('pnl_pips', 0):.1f}\n"
                f"hold_hours: {pos.get('hold_hours', 0):.1f}\n"
                f"thesis: {thesis}\n"
                f"invalidation_conditions: {inv_text}\n"
                f"market_regime: {pos.get('market_regime', 'N/A')}"
            )

        user_content = (
            f"共通コンテキスト:\n"
            f"  セッション(XMT): {session}\n"
            f"  現在時刻(XMT): {BrokerTime.now_str()}\n"
            f"  重要ニュース: {major_news}\n"
            f"  ボラティリティ: {volatility_regime}\n"
            f"\n査定対象ポジション:{positions_text}"
        )

        return [
            {"role": "system", "content": H1_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def build_emergency_prompt(
        self,
        trigger_reason: str,
        symbol: str,
        direction: str,
        pnl_pips: float,
        thesis_summary: str,
        invalidation_conditions: list[str],
    ) -> list[dict]:
        """緊急判定プロンプトを構築"""
        inv_text = ", ".join(invalidation_conditions) if invalidation_conditions else "N/A"
        user_content = (
            f"トリガー理由: {trigger_reason}\n"
            f"ポジション: {symbol} {direction} PnL:{pnl_pips:+.1f}pips\n"
            f"Thesis概要: {thesis_summary[:150]}\n"
            f"Invalidation: {inv_text}"
        )

        return [
            {"role": "system", "content": EMERGENCY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
