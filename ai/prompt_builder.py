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
2. confidence < 0.65 は必ずREJECT（サーバー側でも強制REJECT）
3. 重要経済指標の発表30分以内はWAIT
4. DEAD_ZONEセッション（XMT 22:00-23:59）はREJECT
5. 金曜日のXMT 20:00以降はREJECT（週末持ち越しリスク回避）
6. 市場クローズまで4時間未満の場合は、短期TP設定を推奨
7. 相関アラートがある場合は risk_multiplier を下げること
8. LONGの場合: initial_tp > 現在値, emergency_sl < 現在値 であること
9. SHORTの場合: initial_tp < 現在値, emergency_sl > 現在値 であること
10. thesisとdirectionが矛盾しないこと（LONGなのに下落根拠は不可）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【Web検索活用指針】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 提供された todays_events 以外に、価格へ影響し得る「要人発言」「地政学ニュース」「Flash News」がないか、Web検索ツールで必ず確認すること
- ATR ratio > 1.2 の高ボラ局面では、変動要因が一時的ノイズか、トレンド転換を伴う構造変化かを検索結果から判別すること
- 検索結果で重大な不確実性が残る場合は WAIT を優先し、方向性が明確に出た場合のみ APPROVE を検討すること

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【テクニカル分析指針】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- EMA配列: 21 > 50 > 200 で強い上昇トレンド、逆順で下降トレンド
- RSI: 30以下は売られすぎ反発警戒、70以上は買われすぎ反落警戒
- ATR ratio: 1.0が平均、1.5以上は高ボラティリティ（SL拡大検討）
- H1トレンドとエントリー方向の一致は信頼度向上要因

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【マルチタイムフレーム分析（MTF）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- H4足・日足データが提供される場合は、必ずエントリー方向との整合性を確認
- 上位足トレンドと同方向のエントリーは信頼度を上げる
- 上位足トレンドに逆行するエントリーは信頼度を下げるか、REJECTを検討
- 日足/H4の重要なサポート・レジスタンス付近ではTP/SLを調整
- 週間レンジの上限/下限付近でのエントリーは反転リスクに注意
- 複数戦略の同時発火（シグナル集約）は強い信頼度向上要因

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【シグナルソース別の特性】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
■ multi_strategy_signal（5戦略統合インジケーター）:
  - BREAKOUT: ドンチャンチャネル突破+出来高急増。トレンド方向への順張り。
  - EMA_CROSS: EMA21/50クロス。トレンド初動シグナル。
  - TREND_PULLBACK: トレンド中の押し目/戻り。EMA21タッチ+RSI反転。
  - RSI_DIVERGENCE: 価格と RSI の乖離。反転の先行指標。
  - MEAN_REVERSION: ボリンジャーバンド2σ超え+RSI極値。過延伸からの反転。

■ LiquiditySweepsLuxAlgo（流動性スイープ検出）:
  - アラート発火時点で、スイング高値/安値の流動性がすでに掃かれ、
    ローソク足が反転方向にクローズ済み（ヒゲで突破→実体で戻る）。
  - つまりシグナル自体が「反転確認済み」を意味する。
  - buy = 安値の流動性スイープ後の強気反転（ロング期待）
  - sell = 高値の流動性スイープ後の弱気反転（ショート期待）
  - 他の戦略と同時発火した場合、特に高い信頼度となる。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【リスク管理ルール】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 1トレードあたりのリスク: 口座残高の2%以内
- 最大同時ポジション数: 5件
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
5. 保有時間が長すぎないか（時間経過はWEAKENING要因だが、閾値は固定せず相場状況に応じて判断）
6. ロンドン時間（09:00-12:00 XMT）で建てたポジションが NEW_YORK 開始（16:00 XMT）時点で含み益が乏しい場合、優位性低下として WEAKENING 判定を強める
7. セッションを跨ぐ際にボラティリティが低下し、thesisの伸びしろが縮小した場合は PARTIAL_CLOSE または FULL_CLOSE を優先検討する
8. TIME_STOP: 保有時間が長いのに進展が乏しい場合は、価格目標未到達でも戦略的撤退（FULL_CLOSE）を検討する

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【アクション定義】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- HOLD: 現状維持。Thesisが有効で追加アクション不要。
- UPDATE_TP: TPを更新（トレーリング）。new_tpに新しいTP価格を設定。
- PARTIAL_CLOSE: 利益が十分に乗り、勢いが鈍化した場合に一度だけ実行。2回目以降の分割決済は非推奨。
- FULL_CLOSE: 全決済。Thesis崩壊・SL近接・緊急時。close_percentage=100。

重要制約:
- previous_action が PARTIAL_CLOSE の場合、action=PARTIAL_CLOSE は選択禁止。
- その場合は HOLD / UPDATE_TP / FULL_CLOSE のいずれかを選択すること。

運用原則:
- 利が伸びている局面では、安易な分割決済より UPDATE_TP を優先する
- 分割決済を選ぶ場合でも、残ポジションは建値以上の保護（Breakeven）を意識し、利を伸ばす余地を残す
- TIME_STOPの目安:
    - 保有時間が長いほど、PnLの伸び率・高値安値更新・セッション特性を厳しめに評価する
    - 進展が乏しい「長時間保有」は FULL_CLOSE 候補とするが、固定時間の機械判定はしない
- 分割エグジット方針（現行実装整合）:
    - 初回の防御的利確（TP50到達での部分利確）後、残ポジションは UPDATE_TP または FULL_CLOSE で管理する
    - 連続的な多段分割（1/3, 1/3, 1/3 など）は現行では想定しない

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


WAIT_RECHECK_SYSTEM_PROMPT = """あなたはFXトレード再評価AIです。
以前のAI判断で「WAIT（様子見）」となったシグナルについて、
最新の市場データを基に「今エントリーすべきか」を素早く判断してください。
JSONのみで回答。前置き・説明は不要です。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【判断基準】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. 元のWAIT理由が解消されているかを最優先で確認
2. 現在価格が元のエントリー価格から大きく乖離していたらREJECT
   - USDJPY: 30pips以上, EURUSD: 20pips以上, GOLD: 200pips以上
3. エントリー方向と現在のトレンドが一致しているか確認
4. 状況が改善していればAPPROVEし、新しいTP/SLを設定
5. 不明確な場合はREJECT（安全側に倒す）
6. 元のWAIT理由が「経済指標待ち」「ニュース不確実性」の場合、Web検索で結果・市場初動・出尽くし/加速の有無を確認し、不確実性が解消した時のみAPPROVEすること

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSONスキーマ】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "decision": "APPROVE|REJECT",
  "confidence": 0.0,
  "thesis": "判断根拠（100字程度）",
  "invalidation_conditions": [
    "根拠崩壊条件1",
    "根拠崩壊条件2",
    "根拠崩壊条件3"
  ],
  "initial_tp": 0.0,
  "emergency_sl": 0.0,
  "risk_multiplier": 1.0,
  "market_regime": "TRENDING|RANGING|HIGH_VOLATILITY|PRE_EVENT",
  "reject_reason": "REJECT時の理由、APPROVEならnull"
}

注意:
- WAITは出さないこと（APPROVEかREJECTの二択）
- APPROVEする場合は必ず最新価格に基づくTP/SLを設定"""


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


NANO_TRIAGE_SYSTEM_PROMPT = """あなたはFXポジションの異常検知AIです。
エントリー時のThesis（根拠）と撤退ルールを、現在の市場状況と比較し、
異常があるかだけを判断してください。
JSONのみで回答。

判断基準:
- 撤退ルール（invalidation_conditions）に触れている→ alert=true
- Thesisの前提が崩れている→ alert=true
- 価格がSLに急接近→ alert=true
- 問題なさそう→ alert=false
- 迷ったら alert=true（安全側に倒す）

出力JSON:
{
  "alert": true,
  "reason": "理由50字以内"
}"""


SINGLE_POSITION_EVAL_SYSTEM_PROMPT = """あなたはFXポジション管理専門のAIです。
異常が検知されたポジションを精密に評価し、
具体的なアクションをJSONで返してください。前置き不要。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【判断の優先順位】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Invalidation Conditionsに抵触 → FULL_CLOSE
2. Thesis前提崩壊（WEAKENING）→ 分割決済検討
3. 価格が順調に推移 → TPトレーリング
4. 保有時間が長すぎる場合はWEAKENING要因（固定時間で機械判定せず、進展度とボラを加味）
5. TIME_STOP: 保有時間が長いのに価格進展が乏しい場合、価格目標未達でも撤退を検討

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【アクション定義】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- HOLD: 現状維持
- UPDATE_TP: TPトレーリング（new_tpに新TP価格）
- PARTIAL_CLOSE: 分割決済（close_percentage=50推奨）
- FULL_CLOSE: 全決済（Thesis崩壊・SL近接・緊急時）

重要制約:
- previous_action が PARTIAL_CLOSE の場合、action=PARTIAL_CLOSE は選択禁止。
- その場合は HOLD / UPDATE_TP / FULL_CLOSE のいずれかを選択すること。

追加方針:
- TPに接近している場合の判断基準:
  - thesis_status=VALID かつ confidence が高い（モメンタム継続が明確）→ UPDATE_TP を優先
  - thesis_status=WEAKENING または方向性に自信がない → 戦略的撤退として PARTIAL_CLOSE または FULL_CLOSE を検討
  - 「伸びそうだが根拠が薄い」状態で UPDATE_TP するのは禁止。不確実な場合は確実な利益確保を優先すること
- 分割決済は「利益確保 + 残玉で伸ばす」場面に限定し、連続的な細切れ決済は避ける
- タイムストップ指針:
    - 高値/安値更新が乏しく、thesisの伸びしろが縮小している長時間保有は FULL_CLOSE を検討
    - 時間経過だけでなく、セッション移行・ボラ低下・ニュース変化を合わせて総合判断する

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【出力JSONスキーマ】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "trade_id": "ポジションID",
  "thesis_status": "VALID|WEAKENING|BROKEN",
  "action": "HOLD|UPDATE_TP|PARTIAL_CLOSE|FULL_CLOSE",
  "new_tp": null,
  "close_percentage": 0,
  "reasoning": "判断理由（150字程度、具体的な価格水準を含む）",
  "urgency": "NORMAL|HIGH"
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
        mtf_data: dict | None = None,
    ) -> list[dict]:
        """エントリー評価プロンプトを構築（3層構造）"""

        # ── Layer 2: Semi-Static（経済指標・市場概況・MTF）──
        events_text = "なし"
        if todays_events:
            events_list = [
                f"{e['time_str']} XMT {e['currency']} {e['title']}"
                for e in todays_events
            ]
            events_text = "\n".join(events_list)

        # MTF（マルチタイムフレーム）コンテキスト構築
        mtf_text = ""
        if mtf_data:
            if "h4" in mtf_data:
                h4 = mtf_data["h4"]
                cur = h4["current"]
                mtf_text += (
                    f"\n\n【H4足コンテキスト（直近24時間）】\n"
                    f"現在H4足: O={cur['open']} H={cur['high']} L={cur['low']} C={cur['close']}\n"
                    f"前回H4終値: {h4['prev_close']}\n"
                    f"H4トレンド: {h4['trend']}\n"
                    f"24h レンジ: {h4['range_low']} - {h4['range_high']}"
                )
            if "d1" in mtf_data:
                d1 = mtf_data["d1"]
                cur = d1["current"]
                mtf_text += (
                    f"\n\n【日足コンテキスト（直近1週間）】\n"
                    f"本日: O={cur['open']} H={cur['high']} L={cur['low']} C={cur['close']}\n"
                    f"前日終値: {d1['prev_close']}\n"
                    f"日足トレンド: {d1['trend']}\n"
                    f"週間レンジ: {d1['week_low']} - {d1['week_high']}"
                )

        semi_static_content = (
            f"【本日の重要経済指標】\n{events_text}\n\n"
            f"【市場概況】\n"
            f"セッション(XMT): {session}\n"
            f"H1トレンド: {h1_trend}"
            f"{mtf_text}"
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

    def build_wait_recheck_prompt(
        self,
        original_wait_reason: str,
        original_ai_response: dict,
        webhook_data: dict,
        current_price: float,
        session: str,
        mtf_data: dict | None = None,
    ) -> list[dict]:
        """WAIT再評価プロンプトを構築（軽量版）"""

        # MTF要約（あれば簡潔に）
        mtf_summary = ""
        if mtf_data:
            if "h4" in mtf_data:
                mtf_summary += f"H4トレンド: {mtf_data['h4']['trend']}  "
            if "d1" in mtf_data:
                mtf_summary += f"日足トレンド: {mtf_data['d1']['trend']}"

        original_price = webhook_data.get("price", 0)
        price_diff = abs(current_price - original_price) if original_price else 0

        now_xmt = BrokerTime.now()
        weekday_ja = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"][now_xmt.weekday()]

        user_content = (
            f"【WAIT再評価】\n"
            f"銘柄: {webhook_data.get('symbol')}\n"
            f"方向: {webhook_data.get('direction')}\n"
            f"元のエントリー価格: {original_price}\n"
            f"現在価格: {current_price}\n"
            f"価格差: {price_diff:.5f}\n"
            f"元のWAIT理由: {original_wait_reason}\n"
            f"元のconfidence: {original_ai_response.get('confidence', 'N/A')}\n"
            f"元のthesis: {original_ai_response.get('thesis', 'N/A')}\n\n"
            f"セッション(XMT): {session}\n"
            f"現在時刻(XMT): {BrokerTime.now_str()} ({weekday_ja})\n"
            f"パターン: {webhook_data.get('pattern')}\n"
            f"ソース: {webhook_data.get('source')}\n"
        )
        if mtf_summary:
            user_content += f"上位足: {mtf_summary}\n"

        return [
            {"role": "system", "content": WAIT_RECHECK_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
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
                f"previous_action: {pos.get('previous_action', 'NONE')}\n"
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

    def build_nano_triage_prompt(
        self,
        trigger_reason: str,
        symbol: str,
        direction: str,
        entry_price: float,
        current_price: float,
        pnl_pips: float,
        thesis_summary: str,
        invalidation_conditions: list[str],
        tp: float | None = None,
        sl: float | None = None,
    ) -> list[dict]:
        """ナノ一次審査プロンプト（最小トークン）"""
        inv_text = ", ".join(invalidation_conditions) if invalidation_conditions else "N/A"
        user_content = (
            f"【トレードのメモ】\n"
            f"銀柄: {symbol} {direction}\n"
            f"根拠の要約: {thesis_summary[:150]}\n"
            f"撤退ルール: {inv_text}\n\n"
            f"【今の状況】\n"
            f"エントリー価格: {entry_price}\n"
            f"現在価格: {current_price}\n"
            f"PnL: {pnl_pips:+.1f}pips\n"
            f"TP: {tp or 'N/A'}  SL: {sl or 'N/A'}\n"
            f"トリガー: {trigger_reason}"
        )

        return [
            {"role": "system", "content": NANO_TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def build_single_position_eval_prompt(
        self,
        pos_data: dict,
        trigger_reason: str,
        nano_reason: str,
        session: str,
    ) -> list[dict]:
        """単一ポジション精密評価プロンプト（gpt-5.2用）"""
        thesis = pos_data.get("thesis_text", "N/A")[:200]
        invalidation = pos_data.get("invalidation", [])
        inv_text = ", ".join(invalidation) if isinstance(invalidation, list) else str(invalidation)

        user_content = (
            f"【異常検知による精密評価】\n"
            f"トリガー: {trigger_reason}\n"
            f"一次審査の判断: {nano_reason}\n\n"
            f"trade_id: {pos_data.get('trade_id', 'N/A')}\n"
            f"symbol: {pos_data.get('symbol')} {pos_data.get('direction')}\n"
            f"previous_action: {pos_data.get('previous_action', 'NONE')}\n"
            f"entry_price: {pos_data.get('entry_price')}\n"
            f"current_price: {pos_data.get('current_price')}\n"
            f"current_tp: {pos_data.get('initial_tp')}\n"
            f"current_sl: {pos_data.get('emergency_sl')}\n"
            f"pnl_pips: {pos_data.get('pnl_pips', 0):.1f}\n"
            f"hold_hours: {pos_data.get('hold_hours', 0):.1f}\n"
            f"thesis: {thesis}\n"
            f"invalidation_conditions: {inv_text}\n"
            f"market_regime: {pos_data.get('market_regime', 'N/A')}\n\n"
            f"セッション(XMT): {session}\n"
            f"現在時刻(XMT): {BrokerTime.now_str()}"
        )

        return [
            {"role": "system", "content": SINGLE_POSITION_EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
