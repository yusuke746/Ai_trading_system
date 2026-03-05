# Pine Script セットアップガイド

## 概要

TradingViewプラン制約（インジケータ5つ・アラート20個）の中で、
3銘柄（GOLD, USDJPY, EURUSD）に対して最適なシグナル生成環境を構築する。

---

## インジケータ構成（5スロット）

各銘柄のM15チャートに以下5つのインジケータを設定する。

| # | インジケータ | 用途 | アラート使用 |
|---|-------------|------|:---:|
| 1 | **AI Trading Signal Generator v1.0** (カスタム) | メイン戦略シグナル（5戦略統合） | ✅ |
| 2 | **LuxAlgo - Fair Value Gap** | 構造的インバランス検出 | ✅ |
| 3 | **LuxAlgo - Liquidity Sweeps (Alerts)** | 流動性スイープ検出 | ✅ |
| 4 | **Lorentzian Classification** | ML分類（確認・コンテキスト用） | ✅ |
| 5 | **Q-Trend** | トレンド方向の視覚確認 | ❌ (視覚のみ) |

### 各インジケータの役割

```
カスタム Signal Generator
  ├─ EMA_CROSS:      トレンド開始を検出（EMA21×EMA50クロス）
  ├─ TREND_PULLBACK:  トレンド中の押し目/戻りを検出
  ├─ BREAKOUT:        レンジブレイク + 出来高確認
  ├─ RSI_DIVERGENCE:  ダイバージェンスで転換を検出
  └─ MEAN_REVERSION:  BB + RSI極値での反転を検出

LuxAlgo FVG
  └─ FVG_FILL:        Fair Value Gap充填で構造的エントリー

LuxAlgo Sweeps
  └─ LIQUIDITY_SWEEP:  流動性スイープ後の反転エントリー

Lorentzian Classification
  └─ LORENTZIAN:       機械学習ベースの方向分類
```

---

## アラート配分（20スロット）

### 配分表

| # | アラート名 | 銘柄 | TF | インジケータ | 条件 |
|---|-----------|------|-----|------------|------|
| 1 | MultiStrat GOLD | GOLD | M15 | Custom Signal Generator | Any alert() function call |
| 2 | MultiStrat USDJPY | USDJPY | M15 | Custom Signal Generator | Any alert() function call |
| 3 | MultiStrat EURUSD | EURUSD | M15 | Custom Signal Generator | Any alert() function call |
| 4 | FVG BUY GOLD | GOLD | M15 | LuxAlgo FVG | Bullish FVG |
| 5 | FVG SELL GOLD | GOLD | M15 | LuxAlgo FVG | Bearish FVG |
| 6 | FVG BUY USDJPY | USDJPY | M15 | LuxAlgo FVG | Bullish FVG |
| 7 | FVG SELL USDJPY | USDJPY | M15 | LuxAlgo FVG | Bearish FVG |
| 8 | FVG BUY EURUSD | EURUSD | M15 | LuxAlgo FVG | Bullish FVG |
| 9 | FVG SELL EURUSD | EURUSD | M15 | LuxAlgo FVG | Bearish FVG |
| 10 | Sweep BUY GOLD | GOLD | M15 | LuxAlgo Sweeps | Sweep Buy |
| 11 | Sweep SELL GOLD | GOLD | M15 | LuxAlgo Sweeps | Sweep Sell |
| 12 | Sweep BUY USDJPY | USDJPY | M15 | LuxAlgo Sweeps | Sweep Buy |
| 13 | Sweep SELL USDJPY | USDJPY | M15 | LuxAlgo Sweeps | Sweep Sell |
| 14 | Sweep BUY EURUSD | EURUSD | M15 | LuxAlgo Sweeps | Sweep Buy |
| 15 | Sweep SELL EURUSD | EURUSD | M15 | LuxAlgo Sweeps | Sweep Sell |
| 16 | Lorentzian BUY GOLD | GOLD | M15 | Lorentzian | Buy Signal |
| 17 | Lorentzian SELL GOLD | GOLD | M15 | Lorentzian | Sell Signal |
| 18 | Lorentzian BUY USDJPY | USDJPY | M15 | Lorentzian | Buy Signal |
| 19 | Lorentzian SELL USDJPY | USDJPY | M15 | Lorentzian | Sell Signal |
| 20 | (空き ─ 予備) | | | | |

> **GOLDとUSDJPYに全シグナル集中**：EURUSDはFVG+カスタムの9アラートで十分。
> Lorentzian+SweepのEURUSD分は予備スロット（必要に応じて追加）。

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
カスタム:     8〜16 /週/銘柄 × 3 = 24〜48 /週
LuxAlgo FVG:  3〜8  /週/銘柄 × 3 = 9〜24  /週
Sweeps:       2〜5  /週/銘柄 × 3 = 6〜15  /週
Lorentzian:   5〜10 /週/銘柄 × 2 = 10〜20 /週

合計: 約50〜107 シグナル/週
AI承認率 15〜30% → 約8〜32 トレード/週
目標: 月30〜80トレード
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
   
2. LuxAlgo - Liquidity Sweeps (Alerts)
   - Liquidity: 5, Only Wicks: ON, Min Length: 300（GOLDの場合）
   
3. Lorentzian Classification - Advanced Trading Dashboard
   - Neighbors: 8, Settings はデフォルト

4. Q-Trend
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

各銘柄 × BUY/SELL で6個作成。メッセージは以下をコピペ:

**FVG BUY（LONG）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"LONG","timeframe":"{{interval}}","h1_trend":"","pattern":"FVG_FILL","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_fvg"}
```

**FVG SELL（SHORT）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"SHORT","timeframe":"{{interval}}","h1_trend":"","pattern":"FVG_FILL","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_fvg"}
```

> ⚠️ `YOUR_SECRET_HERE` を実際のWebhook Secretに置換すること

#### LuxAlgo Liquidity Sweeps のアラート (6個)

**Sweep BUY（LONG ─ 安値スイープ後の反転）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"LONG","timeframe":"{{interval}}","h1_trend":"","pattern":"LIQUIDITY_SWEEP","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_sweep"}
```

**Sweep SELL（SHORT ─ 高値スイープ後の反転）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"SHORT","timeframe":"{{interval}}","h1_trend":"","pattern":"LIQUIDITY_SWEEP","price":{{close}},"broker_time":"{{timenow}}","source":"luxalgo_sweep"}
```

#### Lorentzian Classification のアラート (4個)

**Lorentzian BUY（LONG）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"LONG","timeframe":"{{interval}}","h1_trend":"","pattern":"LORENTZIAN","price":{{close}},"broker_time":"{{timenow}}","source":"lorentzian"}
```

**Lorentzian SELL（SHORT）:**
```
{"secret":"YOUR_SECRET_HERE","symbol":"{{ticker}}","direction":"SHORT","timeframe":"{{interval}}","h1_trend":"","pattern":"LORENTZIAN","price":{{close}},"broker_time":"{{timenow}}","source":"lorentzian"}
```

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
  "pattern": "FVG_FILL",
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
| `FVG_FILL` | LuxAlgo FVG | Fair Value Gap充填 |
| `LIQUIDITY_SWEEP` | LuxAlgo Sweeps | 流動性スイープ反転 |
| `LORENTZIAN` | Lorentzian | ML分類シグナル |

---

## チャートレイアウト（推奨）

```
TradingViewレイアウト: 3タブ構成

Tab 1: GOLD M15
  ├─ AI Trading Signal Generator v1.0
  ├─ LuxAlgo - Fair Value Gap
  ├─ LuxAlgo - Liquidity Sweeps
  ├─ Lorentzian Classification
  └─ Q-Trend

Tab 2: USDJPY M15
  ├─ (同上5つ)
  └─ ※ Q-Trendの設定はGOLDと同じ

Tab 3: EURUSD M15
  ├─ (同上5つ)
  └─ ※ Lorentzian/Sweepのアラートは未設定（予備スロット）
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
