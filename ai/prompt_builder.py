"""
ai/prompt_builder.py — プロンプト構築（キャッシュ最適化3層構造）

プロンプトキャッシュ（Prefix Caching）のヒット率を最大化するため、
3層構造で構築する:

  Layer 1 (Static):      システム命令・取引ルール・JSONスキーマ（全リクエスト共通、1,200+トークン）
  Layer 2 (Semi-Static):  当日の経済指標・直近の市場概況（H1単位で更新）
  Layer 3 (Dynamic):      テクニカルシグナル・現在値・ポジション状況（リクエストごとに変化）

OpenAIのプロンプトキャッシュは先頭からのプレフィックス完全一致で動作するため、
不変部分を先頭に、変動部分を末尾に配置する。
"""

import json
import logging
from typing import Optional

from core.broker_time import BrokerTime

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Layer 1: Static Block（全リクエスト共通、キャッシュ対象）
#
# 1,200トークン以上を確保してキャッシュヒットを保証する。
# ★変更禁止★ このブロックを変更するとキャッシュが全て無効化される。
# ═══════════════════════════════════════════════════════════════

ENTRY_SYSTEM_PROMPT = """あなたはプロのFXトレーダーの思考を持つトレード判断AIです。
提供されるテクニカルデータ・市場コンテキストを精密に分析し、
厳格なJSON形式のみで回答してください。前置き・説明・マークダウンは不要です。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【取引対象】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- USDJPY（ドル円）: point=0.001, pip=0.01, 1lot=100,000通貨
- EURUSD（ユーロドル）: point=0.00001, pip=0.0001, 1lot=100,000通貨
- GOLD（XAU/USD）: point=0.01, pip=0.1, 1lot=100oz

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【判断基準（厳守事項）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. テクニカル・ファンダメンタルズの整合性が取れている場合のみAPPROVE
2. confidence < 0.6 は必ずREJECT（サーバー側でも強制REJECT）
3. 重要経済指標の発表30分以内はWAIT
4. DEAD_ZONEセッション（XMT 22:00-23:59）はREJECT
5. 金曜日のXMT 20:00以降はREJECT（週末持ち越しリスク回避）
6. 市場クローズまで4時間未満の場合は、短期TP設定を推奨
7. 相関アラートがある場合は risk_multiplier を下げること
8. LONGの場合: initial_tp > 現在値, emergency_sl < 現在値 であること
9. SHORTの場合: initial_tp < 現在値, emergency_sl > 現在値 であること
10. thesisとdirectionが矛盾しないこと（LONGなのに下落根拠は不可）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【テクニカル分析指針】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- EMA配列: 21 > 50 > 200 で強い上昇トレンド、逆順で下降トレンド
- RSI: 30以下は売られすぎ反発警戒、70以上は買われすぎ反落警戒
- ATR ratio: 1.0が平均、1.5以上は高ボラティリティ（SL拡大検討）
- H1トレンドとエントリー方向の一致は信頼度向上要因
- 複数戦略の同時発火（シグナル集約）は強い信頼度向上要因

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【リスク管理ルール】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 1トレードあたりのリスク: 口座残高の2%以内
- 最大同時ポジション数: 3件
- 相関ペア（例: USDJPY_LONG + EURUSD_SHORT は共にUSD_LONG）の
  同時保有時は risk_multiplier を0.5〜0.7に制限
- SL上限: USDJPY=80pips, EURUSD=60pips, GOLD=300pips

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【セッション特性（XMTサーバー時間基準）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- SYDNEY_TOKYO (00:00-07:00): 低ボラ、レンジ相場多い。JPYペアに注意。
- TOKYO_LONDON_OVERLAP (07:00-09:00): ボラ上昇開始。トレンド初動警戒。
- LONDON (09:00-12:00): 高ボラ、トレンド相場多い。最もAPPROVE適性が高い。
- LONDON_NY_OVERLAP (12:00-16:00): 最大ボラ。指標発表集中帯。
- NEW_YORK (16:00-22:00): 後半はボラ低下。20:00以降は短期のみ。
- DEAD_ZONE (22:00-24:00): 流動性枯渇。REJECT推奨。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSONスキーマ（厳密に従うこと）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "decision": "APPROVE|REJECT|WAIT",
  "confidence": 0.0,
  "thesis": "エントリー根拠テキスト（200字程度）",
  "invalidation_conditions": [
    "根拠崩壊条件1（具体的な価格水準を含むこと）",
    "根拠崩壊条件2",
    "根拠崩壊条件3"
  ],
  "initial_tp": 0.0,
  "emergency_sl": 0.0,
  "risk_multiplier": 1.0,
  "market_regime": "TRENDING|RANGING|HIGH_VOLATILITY|PRE_EVENT",
  "reject_reason": "REJECT/WAIT時の理由文字列、APPROVEならnull"
}

注意:
- invalidation_conditionsは必ず3件以上
- initial_tp/emergency_slは具体的な価格水準（pipsではなく絶対値）
- risk_multiplierは0.5〜1.5の範囲
- reject_reasonはREJECT/WAIT時は必須、APPROVE時はnull"""


H1_SYSTEM_PROMPT = """あなたはFXポジション管理専門のAIです。
エントリー時のThesis（根拠）と現在の市場状況を精密に比較し、
各ポジションへの指示をJSON形式のみで返してください。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【判断の優先順位】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Invalidation Conditions（根拠崩壊条件）に抵触していないか（最優先→FULL_CLOSE）
2. Thesisの前提が崩れていないか（WEAKENING→分割決済検討）
3. 価格アクションがThesisを支持し続けているか
4. TP更新（トレーリング）・分割決済の余地があるか
5. 保有時間が長すぎないか（8時間以上はWEAKENING要因）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【アクション定義】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- HOLD: 現状維持。Thesisが有効で追加アクション不要。
- UPDATE_TP: TPを更新（トレーリング）。new_tpに新しいTP価格を設定。
- PARTIAL_CLOSE: 分割決済。close_percentageに決済割合（50%推奨）を設定。
- FULL_CLOSE: 全決済。Thesis崩壊・SL近接・緊急時。close_percentage=100。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSONスキーマ】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "positions": [
    {
      "trade_id": "ポジション識別ID",
      "thesis_status": "VALID|WEAKENING|BROKEN",
      "action": "HOLD|UPDATE_TP|PARTIAL_CLOSE|FULL_CLOSE",
      "new_tp": null,
      "close_percentage": 0,
      "reasoning": "判断理由（100字程度、具体的な価格水準を含む）",
      "urgency": "NORMAL|HIGH"
    }
  ]
}"""


EMERGENCY_SYSTEM_PROMPT = """FXポジション緊急判定AIです。
「今すぐ人間に通知すべきか」だけを高速に判断してください。
JSONのみで回答。余計な説明は不要です。

緊急通知の基準:
- SL/TPまで残り20%以内に急接近
- 予想外の急変動（ATRの2倍以上のスパイク）
- 重要指標の予想外の結果による逆行

出力JSON:
{
  "action": "ALERT_HUMAN|CONTINUE_MONITORING",
  "reason": "理由50字以内"
}"""


# ═══════════════════════════════════════════════════════════════
# Layer 2/3 構築
# ═══════════════════════════════════════════════════════════════

class PromptBuilder:
    """3種のプロンプトをキャッシュ最適化3層構造で構築する

    Layer 1 (Static):      system prompt（全リクエスト共通）
    Layer 2 (Semi-Static):  経済指標・市場概況（H1単位で更新）
    Layer 3 (Dynamic):      シグナル・現在値・ポジション状況
    """

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
        """エントリー評価プロンプトを構築（3層構造）"""

        # ── Layer 2: Semi-Static（経済指標・市場概況）──
        events_text = "なし"
        if todays_events:
            events_list = [
                f"{e['time_str']} XMT {e['currency']} {e['title']}"
                for e in todays_events
            ]
            events_text = "\n".join(events_list)

        semi_static_content = (
            f"【本日の重要経済指標】\n{events_text}\n\n"
            f"【市場概況】\n"
            f"セッション(XMT): {session}\n"
            f"H1トレンド: {h1_trend}"
        )

        # ── Layer 3: Dynamic（シグナル・口座状況）──
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

        correlation_text = "なし"
        if correlation_alert.get("has_alert"):
            correlation_text = (
                f"{correlation_alert['alert_level']}: {correlation_alert['message']} "
                f"(推奨risk_multiplier: {correlation_alert['recommended_risk_multiplier']})"
            )

        signal_confluence_text = ""
        if all_patterns and len(all_patterns) > 1:
            signal_confluence_text = (
                f"\n🔔 シグナル集約: {len(all_patterns)}戦略が同時発火 → "
                f"patterns={all_patterns}\n"
                f"複数戦略の一致は信頼度向上要因として考慮してください。"
            )

        now_xmt = BrokerTime.now()
        weekday_ja = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"][now_xmt.weekday()]

        dynamic_content = (
            f"【テクニカルシグナル】\n{json.dumps(tech_data, ensure_ascii=False)}\n\n"
            f"現在値: {webhook_data.get('price')}\n"
            f"現在時刻(XMT): {BrokerTime.now_str()} ({weekday_ja})\n"
            f"口座状況: 総エクスポージャー {exposure_pct:.1f}% / 保有ポジ {pos_count}件\n"
            f"相関アラート: {correlation_text}"
            f"{signal_confluence_text}"
        )

        return [
            {"role": "system", "content": ENTRY_SYSTEM_PROMPT},       # Layer 1: Static
            {"role": "user", "content": semi_static_content},          # Layer 2: Semi-Static
            {"role": "user", "content": dynamic_content},              # Layer 3: Dynamic
        ]

    def build_h1_batch_prompt(
        self,
        positions_data: list[dict],
        session: str,
        major_news: str,
        volatility_regime: str,
    ) -> list[dict]:
        """H1バッチ監視プロンプトを構築（3層構造）"""

        # ── Layer 2: Semi-Static ──
        semi_static_content = (
            f"【共通コンテキスト】\n"
            f"セッション(XMT): {session}\n"
            f"現在時刻(XMT): {BrokerTime.now_str()}\n"
            f"重要ニュース: {major_news}\n"
            f"ボラティリティ: {volatility_regime}"
        )

        # ── Layer 3: Dynamic（各ポジション情報）──
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

        dynamic_content = f"【査定対象ポジション】{positions_text}"

        return [
            {"role": "system", "content": H1_SYSTEM_PROMPT},           # Layer 1: Static
            {"role": "user", "content": semi_static_content},          # Layer 2: Semi-Static
            {"role": "user", "content": dynamic_content},              # Layer 3: Dynamic
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
        """緊急判定プロンプトを構築（2層: Static + Dynamic）

        緊急判定はレイテンシ最優先のため、Semi-Staticレイヤーは省略。
        """
        inv_text = ", ".join(invalidation_conditions) if invalidation_conditions else "N/A"
        dynamic_content = (
            f"トリガー理由: {trigger_reason}\n"
            f"ポジション: {symbol} {direction} PnL:{pnl_pips:+.1f}pips\n"
            f"Thesis概要: {thesis_summary[:150]}\n"
            f"Invalidation: {inv_text}"
        )

        return [
            {"role": "system", "content": EMERGENCY_SYSTEM_PROMPT},    # Layer 1: Static
            {"role": "user", "content": dynamic_content},              # Layer 3: Dynamic (直接)
        ]
