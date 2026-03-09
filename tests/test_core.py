"""
tests/test_core.py — コアモジュールのユニットテスト

broker_time, lot_calculator, models, risk_guardian のテスト
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# パス設定
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ──────────── BrokerTime テスト ────────────

class TestBrokerTime:
    def test_now_returns_aware_datetime(self):
        from core.broker_time import BrokerTime
        now = BrokerTime.now()
        assert now.tzinfo is not None

    def test_now_str_contains_xmt(self):
        from core.broker_time import BrokerTime
        s = BrokerTime.now_str()
        assert "XMT" in s

    def test_get_session_returns_valid(self):
        from core.broker_time import BrokerTime
        session = BrokerTime.get_session()
        valid_sessions = {
            "SYDNEY_TOKYO", "TOKYO_LONDON_OVERLAP", "LONDON",
            "LONDON_NY_OVERLAP", "NEW_YORK", "DEAD_ZONE", "UNKNOWN"
        }
        assert session in valid_sessions

    def test_from_utc_conversion(self):
        from core.broker_time import BrokerTime, XMT_TZ
        utc_dt = datetime(2024, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
        xmt_dt = BrokerTime.from_utc(utc_dt)
        # 夏時間: UTC+3
        assert xmt_dt.hour == 13

    def test_to_utc_conversion(self):
        from core.broker_time import BrokerTime, XMT_TZ
        xmt_dt = datetime(2024, 7, 15, 13, 0, 0, tzinfo=XMT_TZ)
        utc_dt = BrokerTime.to_utc(xmt_dt)
        assert utc_dt.hour == 10

    def test_today_start(self):
        from core.broker_time import BrokerTime
        start = BrokerTime.today_start()
        assert start.hour == 0
        assert start.minute == 0
        assert start.second == 0

    def test_is_dst_returns_bool(self):
        from core.broker_time import BrokerTime
        assert isinstance(BrokerTime.is_dst(), bool)

    def test_is_holiday_christmas(self):
        from core.broker_time import BrokerTime
        with patch("core.broker_time.BrokerTime.now") as mock_now:
            mock_now.return_value = datetime(2024, 12, 25, 12, 0, tzinfo=timezone.utc)
            assert BrokerTime.is_holiday() is True

    def test_is_holiday_normal_day(self):
        from core.broker_time import BrokerTime
        with patch("core.broker_time.BrokerTime.now") as mock_now:
            mock_now.return_value = datetime(2024, 7, 15, 12, 0, tzinfo=timezone.utc)
            assert BrokerTime.is_holiday() is False


# ──────────── Models テスト ────────────

class TestModels:
    def test_webhook_payload_xauusd_mapping(self):
        from core.models import WebhookPayload
        payload = WebhookPayload(
            secret="test_secret",
            symbol="XAUUSD",
            direction="LONG",
            price=2000.0,
            timeframe="M15",
            pattern="BREAKOUT",
            source="custom",
            broker_time="2024-01-15T10:00:00Z",
        )
        assert payload.symbol == "GOLD"

    def test_webhook_payload_needs_supplement(self):
        from core.models import WebhookPayload
        # 外部インジケータ（RSIなし）
        payload = WebhookPayload(
            secret="test_secret",
            symbol="USDJPY",
            direction="LONG",
            price=150.0,
            timeframe="M15",
            pattern="FVG_FILL",
            source="luxalgo",
            broker_time="2024-01-15T10:00:00Z",
        )
        assert payload.needs_supplement() is True

    def test_webhook_payload_custom_no_supplement(self):
        from core.models import WebhookPayload
        payload = WebhookPayload(
            secret="test_secret",
            symbol="USDJPY",
            direction="LONG",
            price=150.0,
            timeframe="M15",
            pattern="EMA_CROSS",
            source="custom",
            rsi=55.0,
            atr=0.15,
            ema21=149.5,
            ema50=149.0,
            ema200=148.0,
            broker_time="2024-01-15T10:00:00Z",
        )
        assert payload.needs_supplement() is False

    def test_ai_entry_response_validation(self):
        from core.models import AIEntryResponse
        # 正常ケース
        resp = AIEntryResponse(
            decision="APPROVE",
            confidence=0.8,
            thesis="テスト根拠",
            invalidation_conditions=["条件1", "条件2", "条件3"],
            initial_tp=150.5,
            emergency_sl=149.0,
            risk_multiplier=1.0,
            market_regime="TRENDING",
        )
        assert resp.decision == "APPROVE"

    def test_ai_entry_response_approve_requires_conditions(self):
        from core.models import AIEntryResponse
        from pydantic import ValidationError
        # APPROVE時にinvalidation_conditionsが空→エラー
        with pytest.raises(ValidationError):
            AIEntryResponse(
                decision="APPROVE",
                confidence=0.8,
                thesis="テスト",
                invalidation_conditions=[],
                initial_tp=150.5,
                emergency_sl=149.0,
                risk_multiplier=1.0,
                market_regime="TRENDING",
            )

    def test_direction_enum(self):
        from core.models import Direction
        assert Direction.LONG.value == "LONG"
        assert Direction.SHORT.value == "SHORT"

    def test_order_result_defaults(self):
        from core.models import OrderResult
        result = OrderResult(success=True, ticket=12345)
        assert result.price == 0.0
        assert result.lot == 0.0


# ──────────── RiskGuardian テスト ────────────

class TestRiskGuardian:
    def test_correlation_groups_no_overlap(self):
        from core.risk_guardian import RiskGuardian
        g = RiskGuardian()
        result = g.check_correlation_alert("USDJPY", "LONG", [])
        assert result["has_alert"] is False
        assert result["recommended_risk_multiplier"] == 1.0

    def test_correlation_groups_warning(self):
        from core.risk_guardian import RiskGuardian
        g = RiskGuardian()
        active = [{"symbol": "EURUSD", "direction": "SHORT"}]
        result = g.check_correlation_alert("USDJPY", "LONG", active)
        # USDJPY_LONG と EURUSD_SHORT は共にUSD_LONGグループ
        assert result["has_alert"] is True
        assert result["alert_level"] == "WARNING"

    def test_correlation_groups_high(self):
        from core.risk_guardian import RiskGuardian
        g = RiskGuardian()
        active = [
            {"symbol": "EURUSD", "direction": "SHORT"},
            {"symbol": "GOLD", "direction": "SHORT"},
        ]
        result = g.check_correlation_alert("USDJPY", "LONG", active)
        assert result["has_alert"] is True
        assert result["alert_level"] == "HIGH"
        assert result["recommended_risk_multiplier"] == 0.5

    def test_status_report(self):
        from core.risk_guardian import RiskGuardian
        g = RiskGuardian()
        report = g.get_status_report()
        assert "status" in report
        assert "broker_time" in report
        assert "session" in report


# ──────────── Webhook Receiver テスト ────────────

class TestWebhookReceiver:
    def test_verify_webhook_success(self):
        from config import CONFIG
        from ingestion.webhook_receiver import verify_webhook
        with patch.object(CONFIG, "WEBHOOK_SECRET", "test_secret"):
            assert verify_webhook({"secret": "test_secret"}) is True

    def test_verify_webhook_failure(self):
        from config import CONFIG
        from ingestion.webhook_receiver import verify_webhook
        with patch.object(CONFIG, "WEBHOOK_SECRET", "test_secret"):
            assert verify_webhook({"secret": "wrong"}) is False

    def test_validate_payload_valid(self):
        from ingestion.webhook_receiver import validate_payload
        data = {
            "symbol": "USDJPY",
            "direction": "LONG",
            "price": 150.0,
            "timeframe": "M15",
        }
        is_valid, reason = validate_payload(data)
        assert is_valid is True

    def test_validate_payload_missing_symbol(self):
        from ingestion.webhook_receiver import validate_payload
        data = {"direction": "LONG", "price": 150.0, "timeframe": "M15"}
        is_valid, reason = validate_payload(data)
        assert is_valid is False
        assert "symbol" in reason

    def test_validate_payload_invalid_direction(self):
        from ingestion.webhook_receiver import validate_payload
        data = {
            "symbol": "USDJPY",
            "direction": "UP",
            "price": 150.0,
            "timeframe": "M15",
        }
        is_valid, reason = validate_payload(data)
        assert is_valid is False

    def test_is_duplicate_first_time(self):
        from ingestion.webhook_receiver import is_duplicate, _recent_signals
        _recent_signals.clear()
        data = {"symbol": "USDJPY", "direction": "LONG", "price": 150.0}
        assert is_duplicate(data) is False

    def test_is_duplicate_second_time(self):
        from ingestion.webhook_receiver import is_duplicate, register_dedup, _recent_signals
        _recent_signals.clear()
        data = {"symbol": "USDJPY", "direction": "LONG", "price": 150.0}
        register_dedup(data)  # 1回目登録
        assert is_duplicate(data) is True  # 2回目は重複


# ──────────── PromptBuilder テスト ────────────

class TestPromptBuilder:
    def test_build_entry_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_entry_prompt(
            webhook_data={"symbol": "USDJPY", "direction": "LONG", "price": 150.0},
            session="LONDON",
            h1_trend="BULLISH",
            exposure_pct=1.0,
            pos_count=1,
            correlation_alert={"has_alert": False},
            todays_events=[],
        )
        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"  # Semi-Static
        assert messages[2]["role"] == "user"  # Dynamic
        assert "USDJPY" in messages[2]["content"]

    def test_build_entry_prompt_with_confluence(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_entry_prompt(
            webhook_data={"symbol": "GOLD", "direction": "SHORT", "price": 2000.0},
            session="LONDON",
            h1_trend="BEARISH",
            exposure_pct=0.0,
            pos_count=0,
            correlation_alert={"has_alert": False},
            todays_events=[],
            all_patterns=["BREAKOUT", "FVG_FILL", "LORENTZIAN"],
        )
        assert "3戦略が同時発火" in messages[2]["content"]
        assert "BREAKOUT" in messages[2]["content"]
        assert "FVG_FILL" in messages[2]["content"]

    def test_build_entry_prompt_with_mtf(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        mtf_data = {
            "h4": {
                "current": {"open": 150.0, "high": 150.5, "low": 149.5, "close": 150.3},
                "prev_close": 149.8,
                "trend": "BULLISH",
                "range_high": 150.5,
                "range_low": 149.2,
            },
            "d1": {
                "current": {"open": 149.0, "high": 150.5, "low": 148.8, "close": 150.3},
                "prev_close": 149.0,
                "trend": "BULLISH",
                "week_high": 151.0,
                "week_low": 147.5,
            },
        }
        messages = pb.build_entry_prompt(
            webhook_data={"symbol": "USDJPY", "direction": "LONG", "price": 150.0},
            session="LONDON",
            h1_trend="BULLISH",
            exposure_pct=0.0,
            pos_count=0,
            correlation_alert={"has_alert": False},
            todays_events=[],
            mtf_data=mtf_data,
        )
        # MTFデータがSemi-Static層（messages[1]）に含まれること
        assert "H4足コンテキスト" in messages[1]["content"]
        assert "日足コンテキスト" in messages[1]["content"]
        assert "BULLISH" in messages[1]["content"]
        assert "150.5" in messages[1]["content"]

    def test_build_wait_recheck_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_wait_recheck_prompt(
            original_wait_reason="レジスタンス付近、ブレイクを待て",
            original_ai_response={
                "decision": "WAIT",
                "confidence": 0.55,
                "thesis": "上昇トレンドだがレジスタンス接触中",
                "reject_reason": "レジスタンス付近、ブレイクを待て",
            },
            webhook_data={"symbol": "USDJPY", "direction": "LONG", "price": 150.0, "pattern": "BREAKOUT", "source": "multi_strategy_signal"},
            current_price=150.25,
            session="LONDON",
            mtf_data={
                "h4": {"trend": "BULLISH"},
                "d1": {"trend": "BULLISH"},
            },
        )
        assert len(messages) == 2
        assert "WAIT再評価" in messages[1]["content"]
        assert "レジスタンス付近" in messages[1]["content"]
        assert "150.25" in messages[1]["content"]
        assert "150.0" in messages[1]["content"]
        assert "H4トレンド: BULLISH" in messages[1]["content"]
        # WAITは出さないこと、という指示がシステムプロンプトにある
        assert "WAITは出さないこと" in messages[0]["content"]

    def test_build_h1_batch_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        positions = [{
            "trade_id": "test-id",
            "symbol": "USDJPY",
            "direction": "LONG",
            "entry_price": 149.5,
            "current_price": 150.0,
            "initial_tp": 150.5,
            "emergency_sl": 149.0,
            "pnl_pips": 5.0,
            "hold_hours": 2.5,
            "thesis_text": "テスト根拠テキスト",
            "invalidation": ["条件1"],
            "market_regime": "TRENDING",
        }]
        messages = pb.build_h1_batch_prompt(
            positions_data=positions,
            session="LONDON",
            major_news="なし",
            volatility_regime="NORMAL",
        )
        assert len(messages) == 3
        assert "test-id" in messages[2]["content"]

    def test_build_emergency_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_emergency_prompt(
            trigger_reason="TP近接80%",
            symbol="USDJPY",
            direction="LONG",
            pnl_pips=10.5,
            thesis_summary="テスト",
            invalidation_conditions=["条件1"],
        )
        assert len(messages) == 2
        assert "TP近接80%" in messages[1]["content"]

    def test_build_nano_triage_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_nano_triage_prompt(
            trigger_reason="TP近接80%",
            symbol="USDJPY",
            direction="LONG",
            entry_price=149.5,
            current_price=150.3,
            pnl_pips=8.0,
            thesis_summary="上昇トレンド継続で押し目買い",
            invalidation_conditions=["149.0割れ", "H4陰線確定"],
            tp=150.5,
            sl=149.0,
        )
        assert len(messages) == 2
        # システムプロンプト: 異常検知AI
        assert "異常検知" in messages[0]["content"]
        assert "alert" in messages[0]["content"]
        # ユーザーコンテンツ: 全パラメータ含む
        user = messages[1]["content"]
        assert "USDJPY" in user
        assert "LONG" in user
        assert "149.5" in user
        assert "150.3" in user
        assert "+8.0pips" in user
        assert "上昇トレンド継続" in user
        assert "149.0割れ" in user
        assert "H4陰線確定" in user
        assert "TP近接80%" in user
        assert "150.5" in user  # TP
        assert "149.0" in user  # SL

    def test_build_nano_triage_prompt_no_invalidation(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        messages = pb.build_nano_triage_prompt(
            trigger_reason="ATR逆行",
            symbol="EURUSD",
            direction="SHORT",
            entry_price=1.085,
            current_price=1.088,
            pnl_pips=-3.0,
            thesis_summary="ユーロ弱含み",
            invalidation_conditions=[],
            tp=None,
            sl=None,
        )
        assert len(messages) == 2
        user = messages[1]["content"]
        assert "N/A" in user  # invalidation空→N/A
        assert "EURUSD" in user
        assert "SHORT" in user

    def test_build_single_position_eval_prompt(self):
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        pos_data = {
            "trade_id": "eval-test-001",
            "symbol": "GOLD",
            "direction": "LONG",
            "entry_price": 2350.0,
            "current_price": 2380.0,
            "initial_tp": 2400.0,
            "emergency_sl": 2330.0,
            "pnl_pips": 30.0,
            "hold_hours": 3.5,
            "thesis_text": "ゴールド上昇トレンド、リスクオフで買い",
            "invalidation": ["2340割れ", "ドル高加速"],
            "market_regime": "TRENDING",
        }
        messages = pb.build_single_position_eval_prompt(
            pos_data=pos_data,
            trigger_reason="TP近接80%",
            nano_reason="TP到達間近、利確タイミング検討",
            session="LONDON_NY_OVERLAP",
        )
        assert len(messages) == 2
        # システムプロンプト: アクション定義含む
        sys_prompt = messages[0]["content"]
        assert "FULL_CLOSE" in sys_prompt
        assert "PARTIAL_CLOSE" in sys_prompt
        assert "UPDATE_TP" in sys_prompt
        assert "HOLD" in sys_prompt
        assert "thesis_status" in sys_prompt
        # ユーザーコンテンツ: 全フィールド含む
        user = messages[1]["content"]
        assert "eval-test-001" in user
        assert "GOLD" in user
        assert "LONG" in user
        assert "2350.0" in user
        assert "2380.0" in user
        assert "2400.0" in user  # TP
        assert "2330.0" in user  # SL
        assert "30.0" in user    # pnl
        assert "3.5" in user     # hold_hours
        assert "ゴールド上昇トレンド" in user
        assert "2340割れ" in user
        assert "ドル高加速" in user
        assert "TRENDING" in user
        assert "TP近接80%" in user
        assert "TP到達間近" in user
        assert "LONDON_NY_OVERLAP" in user

    def test_build_single_position_eval_prompt_minimal(self):
        """最小限のpos_dataでもエラーにならないことを確認"""
        from ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        pos_data = {
            "symbol": "USDJPY",
            "direction": "SHORT",
            "entry_price": 150.0,
            "current_price": 149.5,
        }
        messages = pb.build_single_position_eval_prompt(
            pos_data=pos_data,
            trigger_reason="ATR逆行",
            nano_reason="テスト理由",
            session="TOKYO",
        )
        assert len(messages) == 2
        user = messages[1]["content"]
        assert "USDJPY" in user
        assert "SHORT" in user
        assert "N/A" in user  # missing trade_id → N/A


# ──────────── Semantic Validator テスト ────────────

class TestSemanticValidator:
    """TP/SL方向矛盾チェックのユニットテスト"""

    def _check_contradiction(self, direction: str, tp: float, sl: float, price: float) -> tuple[bool, str]:
        """entry_evaluator内のSemantic Validatorロジックを抽出してテスト"""
        contradiction = False
        reason_detail = ""
        if direction == "LONG":
            if tp <= price:
                contradiction = True
                reason_detail = f"LONG but TP({tp}) <= price({price})"
            if sl >= price:
                contradiction = True
                reason_detail = f"LONG but SL({sl}) >= price({price})"
        elif direction == "SHORT":
            if tp >= price:
                contradiction = True
                reason_detail = f"SHORT but TP({tp}) >= price({price})"
            if sl <= price:
                contradiction = True
                reason_detail = f"SHORT but SL({sl}) <= price({price})"
        return contradiction, reason_detail

    def test_long_valid(self):
        ok, _ = self._check_contradiction("LONG", tp=151.0, sl=149.0, price=150.0)
        assert ok is False

    def test_long_tp_below_price(self):
        ok, reason = self._check_contradiction("LONG", tp=149.0, sl=148.0, price=150.0)
        assert ok is True
        assert "TP" in reason

    def test_long_sl_above_price(self):
        ok, reason = self._check_contradiction("LONG", tp=151.0, sl=151.0, price=150.0)
        assert ok is True
        assert "SL" in reason

    def test_short_valid(self):
        ok, _ = self._check_contradiction("SHORT", tp=149.0, sl=151.0, price=150.0)
        assert ok is False

    def test_short_tp_above_price(self):
        ok, reason = self._check_contradiction("SHORT", tp=151.0, sl=152.0, price=150.0)
        assert ok is True
        assert "TP" in reason

    def test_short_sl_below_price(self):
        ok, reason = self._check_contradiction("SHORT", tp=149.0, sl=149.0, price=150.0)
        assert ok is True
        assert "SL" in reason


# ──────────── ThesisDB テスト ────────────

class TestThesisDB:
    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "test.db")

    @pytest.mark.asyncio
    async def test_db_start_and_close(self, db_path):
        from core.thesis_db import ThesisDB
        db = ThesisDB(db_path)
        await db.start()
        assert db._conn is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_save_and_read_thesis(self, db_path):
        from core.thesis_db import ThesisDB
        db = ThesisDB(db_path)
        await db.start()

        success = await db.save_thesis(
            trade_id="test-uuid-1234",
            ticket=12345,
            symbol="USDJPY",
            direction="LONG",
            technical_ctx={"rsi": 55},
            fundamental_ctx={"session": "LONDON"},
            thesis_text="テストThesis",
            invalidation=["条件1", "条件2", "条件3"],
            entry_price=150.0,
            initial_tp=150.5,
            emergency_sl=149.5,
            risk_multiplier=1.0,
            market_regime="TRENDING",
            ai_confidence=0.85,
            lot_size=0.03,
        )
        assert success is True

        theses = await db.get_active_theses()
        assert len(theses) == 1
        assert theses[0]["symbol"] == "USDJPY"
        assert theses[0]["trade_id"] == "test-uuid-1234"

        await db.close()

    @pytest.mark.asyncio
    async def test_close_thesis(self, db_path):
        from core.thesis_db import ThesisDB
        db = ThesisDB(db_path)
        await db.start()

        await db.save_thesis(
            trade_id="close-test",
            ticket=99999,
            symbol="EURUSD",
            direction="SHORT",
            technical_ctx={},
            fundamental_ctx={},
            thesis_text="test",
            invalidation=["a"],
            entry_price=1.1,
            initial_tp=1.09,
            emergency_sl=1.11,
            risk_multiplier=1.0,
            market_regime="RANGING",
            ai_confidence=0.7,
            lot_size=0.01,
        )

        await db.close_thesis("close-test")
        theses = await db.get_active_theses()
        assert len(theses) == 0

        await db.close()

    @pytest.mark.asyncio
    async def test_get_db_stats(self, db_path):
        from core.thesis_db import ThesisDB
        db = ThesisDB(db_path)
        await db.start()

        stats = await db.get_db_stats()
        assert "thesis_active" in stats
        assert "monthly_cost_usd" in stats

        await db.close()


# ──────────── SpreadTracker テスト ────────────

class TestSpreadTracker:
    """適応型スプレッド上限管理のユニットテスト"""

    def test_warmup_returns_fixed_fallback(self):
        """サンプル不足時はconfig固定値を返す"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        # サンプル0 → config固定値
        limit = tracker.get_limit("USDJPY")
        assert limit == 30  # CONFIG.SPREAD_LIMITS_POINTS["USDJPY"]

    def test_warmup_with_few_samples(self):
        """MIN_SAMPLES未満ではまだ固定値"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        for i in range(30):  # 60未満
            tracker.record("USDJPY", 15.0)
        limit = tracker.get_limit("USDJPY")
        assert limit == 30  # まだ固定値

    def test_adaptive_after_warmup(self):
        """MIN_SAMPLES以上で適応型に切り替わる"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        # 100サンプル: ほとんど10pts、数個だけ20pts
        for i in range(95):
            tracker.record("USDJPY", 10.0)
        for i in range(5):
            tracker.record("USDJPY", 20.0)

        limit = tracker.get_limit("USDJPY")
        # p95 ≈ 20.0, × 1.2 = 24.0、hard_max(60)以下 → 24.0
        assert limit != 30  # 固定値ではない
        assert limit <= 60  # hard_max以下
        assert limit > 10   # p95以上の値

    def test_hard_max_clamp(self):
        """異常値でもhard_maxを超えない"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        # 全て100pts（通常ありえない高スプレッド）
        for i in range(100):
            tracker.record("USDJPY", 100.0)

        limit = tracker.get_limit("USDJPY")
        assert limit == 60  # hard_max: USDJPY=60

    def test_stats_warmup_mode(self):
        """ウォームアップ中のstats"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        tracker.record("GOLD", 30.0)
        stats = tracker.get_stats("GOLD")
        assert stats["mode"] == "warmup"
        assert stats["samples"] == 1

    def test_stats_adaptive_mode(self):
        """適応型完了後のstats"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        for i in range(100):
            tracker.record("EURUSD", 12.0 + (i % 5))
        stats = tracker.get_stats("EURUSD")
        assert stats["mode"] == "adaptive"
        assert stats["samples"] == 100
        assert "p50" in stats
        assert "p95" in stats
        assert "current_limit" in stats

    def test_unknown_symbol_hard_max_default(self):
        """未知銘柄はhard_maxデフォルト100でクランプ"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        for i in range(100):
            tracker.record("GBPJPY", 200.0)  # 極端に高い値
        limit = tracker.get_limit("GBPJPY")
        assert limit == 100  # hard_max default でクランプ

    def test_stats_empty_symbol(self):
        """サンプル0のstats"""
        from core.spread_tracker import SpreadTracker
        tracker = SpreadTracker()
        stats = tracker.get_stats("USDJPY")
        assert stats["mode"] == "fixed_fallback"
        assert stats["samples"] == 0
