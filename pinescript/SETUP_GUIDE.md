# Pine Script セットアップガイド

## 概要

TradingViewプラン制約（インジケータ5つ・アラート20個）の中で、
3銘柄（GOLD, USDJPY, EURUSD）に対して最適なシグナル生成環境を構築する。

---

## インジケータ構成（4スロット + 1視覚専用）

各銘柄のM15チャートに以下のインジケータを設定する。

| # | インジケータ | 用途 | アラート使用 |
|---|-------------|------|:---:|
| 1 | **AI Trading Signal Generator v1.0** (カスタム) | メイン戦略シグナル（5戦略統合） | ✅ |
| 2 | **LuxAlgo - Fair Value Gap** | FVG充填（Mitigation）でエントリー | ✅ |
| 3 | **LuxAlgo - Liquidity Sweeps (Alerts)** | 流動性スイープ検出 | ✅ |
| 4 | **Q-Trend** | トレンド方向の視覚確認 | ❌ (視覚のみ) |
| 5 | *(空き — 将来用)* | | |

> **v2.0変更**: Lorentzian Classification を廃止。
> 理由: ①webhook JSON未対応（alertconditionのみでalert()なし）
> ②カスタム5戦略がRSI/EMA/モメンタムを既にカバーしており情報量の追加が少ない
> ③空いたアラート枠を全3銘柄均等カバーに再配分

### 各インジケータの役割

```
カスタム Signal Generator
  ├─ EMA_CROSS:      トレンド開始を検出（EMA21×EMA50クロス）
  ├─ TREND_PULLBACK:  トレンド中の押し目/戻りを検出
  ├─ BREAKOUT:        レンジブレイク + 出来高確認
  ├─ RSI_DIVERGENCE:  ダイバージェンスで転換を検出
  └─ MEAN_REVERSION:  BB + RSI極値での反転を検出

LuxAlgo FVG
  └─ FVG_MITIGATION:  Fair Value Gap充填（価格がFVGゾーンを埋めた瞬間）
     ※「検出」ではなく「充填」でアラートを発火させる

LuxAlgo Sweeps
  └─ LIQUIDITY_SWEEP:  流動性スイープ後の反転エントリー
```

---

## アラート配分（20スロット）

### 配分表

| # | アラート名 | 銘柄 | TF | インジケータ | 条件 |
|---|-----------|------|-----|------------|------|
| 1 | MultiStrat GOLD | GOLD | M15 | Custom Signal Generator | Any alert() function call |
| 2 | MultiStrat USDJPY | USDJPY | M15 | Custom Signal Generator | Any alert() function call |
| 3 | MultiStrat EURUSD | EURUSD | M15 | Custom Signal Generator | Any alert() function call |
| 4 | FVG Mitigation LONG GOLD | GOLD | M15 | LuxAlgo FVG | Bullish FVG Mitigation |
| 5 | FVG Mitigation SHORT GOLD | GOLD | M15 | LuxAlgo FVG | Bearish FVG Mitigation |
| 6 | FVG Mitigation LONG USDJPY | USDJPY | M15 | LuxAlgo FVG | Bullish FVG Mitigation |
| 7 | FVG Mitigation SHORT USDJPY | USDJPY | M15 | LuxAlgo FVG | Bearish FVG Mitigation |
| 8 | FVG Mitigation LONG EURUSD | EURUSD | M15 | LuxAlgo FVG | Bullish FVG Mitigation |
| 9 | FVG Mitigation SHORT EURUSD | EURUSD | M15 | LuxAlgo FVG | Bearish FVG Mitigation |
| 10 | Sweep LONG GOLD | GOLD | M15 | LuxAlgo Sweeps | Any alert() function call |
| 11 | Sweep SHORT GOLD | GOLD | M15 | LuxAlgo Sweeps | Any alert() function call |
| 12 | Sweep LONG USDJPY | USDJPY | M15 | LuxAlgo Sweeps | Any alert() function call |
| 13 | Sweep SHORT USDJPY | USDJPY | M15 | LuxAlgo Sweeps | Any alert() function call |
| 14 | Sweep LONG EURUSD | EURUSD | M15 | LuxAlgo Sweeps | Any alert() function call |
| 15 | Sweep SHORT EURUSD | EURUSD | M15 | LuxAlgo Sweeps | Any alert() function call |
| 16-20 | *(予備 — 5スロット空き)* | | | | |

> **v2.0変更**: Lorentzian 4スロットを廃止 → 全3銘柄均等にSweep+FVGカバー。
> 予備5スロットは将来のインジケータ追加や銘柄追加に使用可能。

> **重要: FVGアラート条件の変更**
> 旧: 「Bullish FVG」（FVG検出＝ギャップ発生）→ タイミングが早すぎてエッジなし
> 新: 「Bullish FVG Mitigation」（FVG充填＝価格がギャップを埋めた瞬間）→ 実際のエントリーポイント

> **重要: Sweepアラート条件**
> Sweepsスクリプトは改修済み（`alert()` でwebhook JSONを自動生成）のため、
> 条件を **「Any alert() function call」** に設定し、メッセージは**空欄のまま**にする。

---

## シグナル推定頻度

### カスタム Signal Generator（推定・1銘柄あたり）

| 戦略 | 週間頻度 | 特徴 |
|------|---------|------|
| EMA_CROSS | 1〜2回 | レア・高品質 |
| TREND_PULLBACK | 3〜5回 | 中頻度・トレンド依存 |
| BREAKOUT | 1〜3回 | 出来高確認付き |
| RSI_DIVERGENCE | 1〜2回 | 転換検出・遅延あり |
| MEAN_REVERSION | 2〜4回 | 逆張り・レンジ相場向け |

### 全インジケータ合計（推定・3銘柄合計）

```
カスタム:         8〜16 /週/銘柄 × 3 = 24〜48 /週
FVG Mitigation:   2〜5  /週/銘柄 × 3 = 6〜15  /週
Sweeps:           2〜5  /週/銘柄 × 3 = 6〜15  /週

合計: 約36〜78 シグナル/週
AI承認率 15〜25%（confidence≥0.65で厳選） → 約5〜20 トレード/週
目標: 月20〜60トレード（量より質重視）
```

---

## セットアップ手順

### Step 1: カスタムインジケータの登録

1. TradingViewの「Pine Editor」を開く
2. `multi_strategy_signal.pine` の内容を全文コピー＆ペースト
3. 「保存」→ 名前: `AI Trading Signal Generator v1.0`
4. 「チャートに追加」

**設定変更:**
- `Webhook Secret` → FastAPIの `WEBHOOK_SECRET` と同じ値を入力
- `Session Filter` → ON（デフォルト）
- 各戦略の ON/OFF は初期状態で全ON、運用データを見て調整

### Step 2: 他のインジケータを追加

各M15チャート（GOLD, USDJPY, EURUSD）に以下を追加:

1. LuxAlgo - Fair Value Gap
   - 設定: デフォルトのまま（M15足で動作）
   - ⚠️ **Mitigation Levels**: ON（充填ラインを表示・アラート対象）
   
2. LuxAlgo - Liquidity Sweeps (Alerts)
   - Enable Alerts: ON
   - Liquidity: 5, Only Wicks: ON

3. Q-Trend
   - Period: 200, ATR: 14（アラート不要・視覚のみ）

### Step 3: アラート作成

**全アラート共通設定:**
- ✅ Webhook URL: `http://{VPS_IP}:{PORT}/webhook/tradingview`
- ✅ トリガー条件: 「Once Per Bar Close」
- ✅ 有効期限: 「Open-ended alert」（無期限）

#### カスタム Signal Generator のアラート (3個)

各銘柄のM15チャートで:
1. 「アラート追加」
2. 条件: `AI Trading Signal Generator v1.0` → **「任意のalert()関数の呼び出し」**
3. メッセージ: **空欄のまま**（スクリプトが自動生成するJSONが送信される）
4. Webhook URL を設定

#### LuxAlgo FVG のアラート (6個)

各銘柄 × LONG/SHORT で6個作成。

**アラート条件:**
- LONG: `Fair Value Gap [LuxAlgo]` → **「Bullish FVG Mitigation」**
- SHORT: `Fair Value Gap [LuxAlgo]` → **「Bearish FVG Mitigation」**

> ⚠️ **「Bullish FVG」ではなく「Bullish FVG Mitigation」を選ぶこと！**
> FVG検出 = ギャップが生まれた瞬間（情報のみ、エントリーポイントではない）
> FVG Mitigation = 価格がギャップを埋めた瞬間（反転の高確率ポイント）

**メッセージ（共通テンプレート）:**

FVG Mitigation LONG:
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"LONG","timeframe":"{{interval}}","h1_trend":"","pattern":"FVG_MITIGATION","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_fvg"}
```

FVG Mitigation SHORT:
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"SHORT","timeframe":"{{interval}}","h1_trend":"","pattern":"FVG_MITIGATION","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_fvg"}
```

> ⚠️ `YOUR_SECRET_HERE` を実際のWebhook Secretに置換すること

#### LuxAlgo Liquidity Sweeps のアラート (6個)

改修済みスクリプトが `alert()` でJSON を自動生成するため：

1. 条件: `Liquidity Sweeps [LuxAlgo] (Alerts)` → **「任意のalert()関数の呼び出し」**
2. メッセージ: **空欄のまま**（スクリプトが自動生成するJSONが送信される）

> 旧方式の手動JSONメッセージは不要。スクリプト側の `f_json()` が
> symbol, price, direction, source 等を全て含むJSONを自動構築する。

---

## Webhook JSON フォーマット

### カスタム Signal Generator（全フィールド含む）

```json
{
  "secret": "your_webhook_secret",
  "symbol": "GOLD",
  "direction": "LONG",
  "timeframe": "15",
  "h1_trend": "BULLISH",
  "pattern": "BREAKOUT",
  "price": 2650.50,
  "ema21": 2648.123,
  "ema50": 2645.678,
  "ema200": 2620.345,
  "rsi": 58.42,
  "atr": 3.25,
  "atr_ratio": 1.15,
  "macd": 0.85,
  "macd_signal": 0.62,
  "macd_hist": 0.23,
  "bb_upper": 2660.12,
  "bb_lower": 2635.45,
  "volume_ratio": 1.82,
  "dc_upper": 2655.00,
  "dc_lower": 2630.00,
  "broker_time": "2026-03-05T15:30:00Z",
  "source": "custom_multistrat"
}
```

### 外部インジケータ（部分フィールド）

```json
{
  "secret": "your_webhook_secret",
  "symbol": "GOLD",
  "direction": "LONG",
  "timeframe": "15",
  "h1_trend": "",
  "pattern": "FVG_MITIGATION",
  "price": 2650.50,
  "broker_time": "2026-03-05T15:30:00Z",
  "source": "luxalgo_fvg"
}
```

> **サーバー側補完**: 外部インジケータからのWebhookにはRSI/ATR/EMA等が含まれない。
> `webhook_receiver.py` がMT5から不足データを取得して補完する（設計書 Section 7.1b 参照）。

---

## パターン名一覧（pattern フィールド値）

| pattern | ソース | 説明 |
|---------|--------|------|
| `EMA_CROSS` | Custom | EMA21/50クロスオーバー |
| `TREND_PULLBACK` | Custom | トレンド中のEMA21プルバック |
| `BREAKOUT` | Custom | ドンチャンチャネルブレイク |
| `RSI_DIVERGENCE` | Custom | RSIダイバージェンス |
| `MEAN_REVERSION` | Custom | BB+RSI極値反転 |
| `FVG_MITIGATION` | LuxAlgo FVG | Fair Value Gap充填（反転ポイント） |
| `LIQUIDITY_SWEEP` | LuxAlgo Sweeps | 流動性スイープ反転 |

---

## チャートレイアウト（推奨）

```
TradingViewレイアウト: 3タブ構成

Tab 1: GOLD M15
  ├─ AI Trading Signal Generator v1.0
  ├─ LuxAlgo - Fair Value Gap
  ├─ LuxAlgo - Liquidity Sweeps (Alerts)
  └─ Q-Trend

Tab 2: USDJPY M15
  ├─ (同上4つ)
  └─ ※ Q-Trendの設定はGOLDと同じ

Tab 3: EURUSD M15
  ├─ (同上4つ)
  └─ ※ 全インジケータのアラートをフル設定
```

---

## 運用チューニング

### 初期運用（デモ口座・最初の2週間）

1. 全5戦略をONにして、シグナル頻度と品質を観察
2. AI承認率を銘柄×パターン別に集計
3. 承認率 5%未満 の戦略は無効化を検討
4. 承認率 50%超 の戦略はフィルター条件を緩和を検討

### パラメータ調整ガイド

| 課題 | 調整 |
|------|------|
| シグナル多すぎ | Cooldown Bars を 4→8 に増加 |
| シグナル少なすぎ | Min ATR Ratio を 0.8→0.6 に緩和 |
| ダマシが多い | Volume Multiplier を 1.3→1.5 に引き上げ |
| ダイバージェンスが遅い | Pivot Lookback を 3→2 に短縮 |
| 東京セッションも欲しい | Session Start を 9→2 に変更（XMT 2:00=東京9:00） |

### 銘柄別推奨設定

| パラメータ | GOLD | USDJPY | EURUSD |
|-----------|------|--------|--------|
| EMA Fast | 21 | 21 | 21 |
| EMA Mid | 50 | 50 | 50 |
| EMA Slow | 200 | 200 | 200 |
| Breakout Period | 20 | 20 | 20 |
| Volume Multiplier | 1.3 | 1.5 | 1.5 |
| Min ATR Ratio | 0.8 | 0.8 | 0.8 |
| Session Start (XMT) | 9 | 2 | 9 |
| Session End (XMT) | 22 | 22 | 22 |

> **USDJPY注意**: 東京セッション（XMT 2:00-9:00）もアクティブ。
> 東京時間のシグナルも取りたい場合は Session Start を 2 に変更。

---

## トラブルシューティング

| 症状 | 原因 | 対策 |
|------|------|------|
| アラートが全く来ない | Secret未設定 / Webhook URL間違い | Secret確認 + /health エンドポイントで接続テスト |
| 「Too many alerts」エラー | 20個上限に到達 | 不要なアラートを削除 |
| シグナルは来るがAIがすべてREJECT | 戦略とAIプロンプトの不一致 | prompt_builder.py の戦略コンテキストを確認 |
| 同じシグナルが連発する | Cooldown設定が短い | Cooldown Bars を増やす |
| 「Session OFF」の時間にシグナルなし | 正常動作 | Session Filter をOFF にしたい場合はチェック外す |
| FVG/Sweep JSONにRSIがない | 正常動作（外部インジケータ） | サーバー側で補完される |
