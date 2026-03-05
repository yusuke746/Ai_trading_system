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
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "USDJPY" in messages[1]["content"]

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
        assert len(messages) == 2
        assert "test-id" in messages[1]["content"]

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
