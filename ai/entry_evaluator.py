"""
ai/entry_evaluator.py — エントリー評価エンジン

Webhookデータを受け取り、AIでエントリー可否を判断・執行する。
web_search付きGPT-4o → web_searchなし → GPT-4o-mini の3段フォールバック。
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from openai import AsyncOpenAI

from config import CONFIG
from core.broker_time import BrokerTime
from core.models import WebhookPayload, AIEntryResponse, Direction
from ai.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

# WAIT管理
WAIT_TTL_MINUTES: int = 15
WAIT_MAX_RETRIES: int = 2
WAIT_RECHECK_INTERVAL_SEC: int = 300
_wait_queue: dict[str, dict] = {}

# web_search タイムアウト
WEB_SEARCH_TIMEOUT_SEC: int = 10


class EntryEvaluator:
    """エントリー評価・実行エンジン"""

    def __init__(self):
        self._client: Optional[AsyncOpenAI] = None
        self._mt5_client = None
        self._thesis_db = None
        self._notifier = None
        self._guardian = None
        self._calendar = None
        self._scheduler = None
        self._prompt_builder = PromptBuilder()
        self._lot_calculator = None
        self._eval_lock = asyncio.Lock()  # 同時エントリー評価を直列化

    def set_dependencies(
        self, mt5_client, thesis_db, notifier, guardian, calendar, scheduler, lot_calculator
    ):
        self._mt5_client = mt5_client
        self._thesis_db = thesis_db
        self._notifier = notifier
        self._guardian = guardian
        self._calendar = calendar
        self._scheduler = scheduler
        self._lot_calculator = lot_calculator
        self._client = AsyncOpenAI(api_key=CONFIG.OPENAI_API_KEY)

    # ──────────── メインエントリーポイント ────────────

    async def evaluate(self, payload: WebhookPayload):
        """Webhookペイロードからエントリー評価を実行"""
        async with self._eval_lock:
            await self._evaluate_inner(payload)

    async def _evaluate_inner(self, payload: WebhookPayload):
        """エントリー評価の実処理（Lock内で実行）"""
        symbol = payload.symbol
        direction = payload.direction

        # 1. RiskGuardianで事前ガードチェック
        can_enter, reason = await self._guardian.can_enter_new_trade(symbol, direction)
        if not can_enter:
            logger.info(f"ガードチェック拒否: {symbol} {direction} - {reason}")
            await self._notifier.send(
                f"🛡 ガード拒否: {symbol} {direction}\n理由: {reason}",
                level="INFO",
            )
            # 拒否もai_audit_logに記録
            await self._thesis_db.save_audit_log(
                request_id=str(uuid.uuid4()),
                purpose="ENTRY_REJECTED",
                model="N/A",
                prompt_summary=f"ガード拒否: {reason}",
                full_response="",
                parsed_decision="REJECT",
                validation_result=f"GUARD_BLOCKED: {reason}",
            )
            return

        # 2. 相関アラートチェック
        positions = await self._mt5_client.get_all_positions()
        active_positions = [
            {"symbol": p.symbol, "direction": p.direction.value}
            for p in positions
        ]
        correlation_alert = self._guardian.check_correlation_alert(
            symbol, direction, active_positions
        )

        if correlation_alert.get("alert_level") == "HIGH":
            await self._notifier.send(
                f"⚠️ 高相関警告: {correlation_alert['message']}",
                level="WARNING",
            )

        # 3. 経済指標チェック
        event_soon, event_desc = await self._calendar.is_high_impact_event_soon()

        # 4. プロンプト構築
        session = BrokerTime.get_session()
        balance = await self._mt5_client.get_account_balance()
        h1_trend = payload.h1_trend or "UNKNOWN"
        todays_events = await self._calendar.get_todays_events()

        webhook_dict = payload.model_dump()
        messages = self._prompt_builder.build_entry_prompt(
            webhook_data=webhook_dict,
            session=session,
            h1_trend=h1_trend,
            exposure_pct=0.0,  # 簡易版
            pos_count=len(positions),
            correlation_alert=correlation_alert,
            todays_events=todays_events,
        )

        # 5. AI呼び出し（3段フォールバック）
        request_id = str(uuid.uuid4())
        start_time = time.time()

        ai_response = await self._call_entry_evaluation(messages, request_id)
        latency_ms = int((time.time() - start_time) * 1000)

        if not ai_response:
            return

        # 6. バリデーション
        decision = ai_response.get("decision", "REJECT")
        confidence = ai_response.get("confidence", 0)
        validation_result = "PASS"

        # confidence下限チェック
        if decision == "APPROVE" and confidence < 0.6:
            ai_response["decision"] = "REJECT"
            ai_response["reject_reason"] = f"confidence {confidence} < 0.6"
            validation_result = "FAIL_CONFIDENCE"
            decision = "REJECT"

        # 相関アラートによるrisk_multiplier強制制限
        if correlation_alert.get("has_alert"):
            rec = correlation_alert["recommended_risk_multiplier"]
            ai_mult = ai_response.get("risk_multiplier", 1.0)
            ai_response["risk_multiplier"] = min(ai_mult, rec)

        # 7. AI監査ログ記録
        await self._thesis_db.save_audit_log(
            request_id=request_id,
            purpose="ENTRY_EVAL",
            model=ai_response.get("_model_used", CONFIG.MODEL_MAIN),
            prompt_summary=messages[-1]["content"][:500],
            full_response=json.dumps(ai_response, ensure_ascii=False),
            parsed_decision=decision,
            validation_result=validation_result,
            was_overridden=validation_result != "PASS",
            override_reason=validation_result if validation_result != "PASS" else None,
            latency_ms=latency_ms,
            tokens_in=ai_response.get("_tokens_in", 0),
            tokens_out=ai_response.get("_tokens_out", 0),
        )

        # APIコスト記録
        tokens_in = ai_response.get("_tokens_in", 0)
        tokens_out = ai_response.get("_tokens_out", 0)
        model = ai_response.get("_model_used", CONFIG.MODEL_MAIN)
        cost = self._estimate_cost(model, tokens_in, tokens_out)
        purpose = "ENTRY_EVAL" if decision == "APPROVE" else "ENTRY_REJECTED"
        await self._thesis_db.save_api_cost(model, tokens_in, tokens_out, cost, purpose)

        # 8. 判定分岐
        if decision == "APPROVE":
            await self._execute_entry(symbol, direction, ai_response, webhook_dict)
        elif decision == "WAIT":
            await self._handle_wait_decision(symbol, ai_response, webhook_dict)
        else:
            reject_reason = ai_response.get("reject_reason", "不明")
            logger.info(f"エントリー拒否: {symbol} {direction} - {reject_reason}")
            await self._notifier.send(
                f"❌ エントリー拒否: {symbol} {direction}\n理由: {reject_reason}",
                level="INFO",
            )

    # ──────────── AI呼び出し（3段フォールバック） ────────────

    async def _call_entry_evaluation(
        self, messages: list[dict], request_id: str
    ) -> Optional[dict]:
        """web_searchフォールバック付きAI呼び出し"""
        # 試行1: web_search付き
        try:
            response = await asyncio.wait_for(
                self._call_ai(
                    CONFIG.MODEL_MAIN,
                    messages,
                    tools=[{"type": "web_search_20250305", "search_context_size": "low"}],
                ),
                timeout=CONFIG.AI_TIMEOUT_MAIN_SEC + WEB_SEARCH_TIMEOUT_SEC,
            )
            if response:
                result = self._parse_ai_response(response)
                if result:
                    result["_model_used"] = CONFIG.MODEL_MAIN
                    return result
        except asyncio.TimeoutError:
            logger.warning("web_search付きAI呼び出しタイムアウト")
        except Exception as e:
            logger.warning(f"web_search付きAI呼び出しエラー: {e}")

        # 試行2: web_searchなし
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_MAIN, messages),
                timeout=CONFIG.AI_TIMEOUT_MAIN_SEC,
            )
            if response:
                result = self._parse_ai_response(response)
                if result:
                    result["_web_search_failed"] = True
                    result["_model_used"] = CONFIG.MODEL_MAIN
                    await self._notifier.send(
                        f"⚠️ web_search失敗: ニュースなしで判断",
                        level="WARNING",
                    )
                    return result
        except asyncio.TimeoutError:
            logger.warning("web_searchなしAI呼び出しタイムアウト")
        except Exception as e:
            logger.warning(f"web_searchなしAI呼び出しエラー: {e}")

        # 試行3: GPT-4o-mini フォールバック
        try:
            response = await self._call_ai(CONFIG.MODEL_FAST, messages)
            if response:
                result = self._parse_ai_response(response)
                if result:
                    result["_fallback_model"] = True
                    result["_model_used"] = CONFIG.MODEL_FAST
                    return result
        except Exception as e:
            logger.error(f"GPT-4o-miniフォールバックも失敗: {e}")

        # 全失敗
        await self._notifier.send(
            f"🔴 AI完全障害: エントリー拒否",
            level="CRITICAL",
        )
        return {"decision": "REJECT", "reject_reason": "AI障害", "_model_used": "N/A"}

    async def _call_ai(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list] = None,
    ) -> Optional[str]:
        """OpenAI API呼び出し"""
        try:
            kwargs = {
                "model": model,
                "input": messages,
            }
            if tools:
                kwargs["tools"] = tools

            response = await self._client.responses.create(**kwargs)

            # レスポンスからテキスト抽出
            text = ""
            tokens_in = 0
            tokens_out = 0

            if hasattr(response, "output"):
                for item in response.output:
                    if hasattr(item, "content"):
                        for content_block in item.content:
                            if hasattr(content_block, "text"):
                                text += content_block.text

            if hasattr(response, "usage"):
                tokens_in = response.usage.input_tokens
                tokens_out = response.usage.output_tokens

            # メタデータをテキストに埋め込み（後でパース時に利用）
            if text:
                return json.dumps({
                    "_text": text,
                    "_tokens_in": tokens_in,
                    "_tokens_out": tokens_out,
                })

            return None
        except Exception as e:
            logger.error(f"OpenAI API呼び出しエラー ({model}): {e}")
            raise

    def _parse_ai_response(self, response_json: str) -> Optional[dict]:
        """AIレスポンスをパースしてdict化"""
        try:
            meta = json.loads(response_json)
            text = meta.get("_text", "")
            tokens_in = meta.get("_tokens_in", 0)
            tokens_out = meta.get("_tokens_out", 0)

            # JSONブロックを抽出
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            # 先頭/末尾のゴミを除去
            text = text.strip()

            result = json.loads(text)
            result["_tokens_in"] = tokens_in
            result["_tokens_out"] = tokens_out
            return result

        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"AIレスポンスパースエラー: {e}\nraw: {response_json[:500]}")
            return None

    # ──────────── エントリー実行 ────────────

    async def _execute_entry(
        self,
        symbol: str,
        direction: str,
        ai_response: dict,
        webhook_data: dict,
    ):
        """AIがAPPROVEした取引を実行"""
        trade_id = str(uuid.uuid4())
        import core.lot_calculator as lot_calculator

        try:
            # Step 1: TP/SLからSL pips計算
            entry_price = webhook_data.get("price", 0)
            emergency_sl = ai_response.get("emergency_sl", 0)
            initial_tp = ai_response.get("initial_tp", 0)

            point = 0.001 if "JPY" in symbol else 0.00001
            if symbol == "GOLD":
                point = 0.01

            sl_pips = abs(entry_price - emergency_sl) / (point * 10) if point > 0 else 50

            # SL上限チェック
            max_sl = CONFIG.MAX_SL_PIPS.get(symbol, 50)
            if sl_pips > max_sl:
                await self._notifier.send(
                    f"⚠️ SL pips異常: {symbol} {sl_pips:.1f}pips > {max_sl}pips → 拒否",
                    level="WARNING",
                )
                return

            # Step 2: ロット計算
            balance = await self._mt5_client.get_account_balance()
            risk_multiplier = ai_response.get("risk_multiplier", 1.0)

            lot = lot_calculator.calculate(
                symbol,
                sl_pips,
                CONFIG.MAX_RISK_PER_TRADE_PCT,
                balance,
                risk_multiplier,
            )

            safety = lot_calculator.verify_calculation(symbol, lot, sl_pips, balance)
            if not safety["is_safe"]:
                await self._notifier.send(
                    f"🔴 ロット計算異常: {symbol} lot={lot}\n{safety['warnings']}",
                    level="CRITICAL",
                )
                return

            # Step 3: MT5注文
            dir_enum = Direction.LONG if direction == "LONG" else Direction.SHORT
            result = await self._mt5_client.open_position(
                symbol=symbol,
                direction=dir_enum,
                lot=lot,
                sl_price=emergency_sl,
                tp_price=initial_tp,
                comment=trade_id[:8],
            )

            if result is None or not result.success:
                error_msg = result.error if result else "MT5接続不可"
                await self._notifier.send(
                    f"🔴 MT5注文失敗: {symbol} {direction}\n"
                    f"エラー: {error_msg}",
                    level="CRITICAL",
                )
                return

            # Step 4: DB保存
            try:
                technical_ctx = {
                    "rsi": webhook_data.get("rsi"),
                    "atr": webhook_data.get("atr"),
                    "ema21": webhook_data.get("ema21"),
                    "ema50": webhook_data.get("ema50"),
                    "ema200": webhook_data.get("ema200"),
                    "strategy": webhook_data.get("strategy"),
                    "source": webhook_data.get("source"),
                }
                fundamental_ctx = {
                    "session": BrokerTime.get_session(),
                    "h1_trend": webhook_data.get("h1_trend"),
                }

                await self._thesis_db.save_thesis(
                    trade_id=trade_id,
                    ticket=result.ticket,
                    symbol=symbol,
                    direction=direction,
                    technical_ctx=technical_ctx,
                    fundamental_ctx=fundamental_ctx,
                    thesis_text=ai_response.get("thesis", ""),
                    invalidation=ai_response.get("invalidation_conditions", []),
                    entry_price=result.price,
                    initial_tp=initial_tp,
                    emergency_sl=emergency_sl,
                    risk_multiplier=risk_multiplier,
                    market_regime=ai_response.get("market_regime", "UNKNOWN"),
                    ai_confidence=ai_response.get("confidence", 0),
                    lot_size=lot,
                )
            except Exception as e:
                await self._notifier.send(
                    f"🔴 DB保存失敗: {symbol} ticket={result.ticket}\n"
                    f"ポジションは開いています！手動確認必須\nエラー: {e}",
                    level="CRITICAL",
                )
                return

            # Step 5: 成功通知
            await self._notifier.send_entry_notification(
                trade_id, result, ai_response, webhook_data
            )

            logger.info(
                f"エントリー完了: {symbol} {direction} lot={lot} "
                f"ticket={result.ticket} trade_id={trade_id[:8]}"
            )

        except Exception as e:
            await self._notifier.send(
                f"🔴 エントリー処理エラー: {symbol}\n{type(e).__name__}: {e}",
                level="CRITICAL",
            )
            logger.exception(f"execute_entry failed: {symbol}")

    # ──────────── WAITハンドリング ────────────

    async def _handle_wait_decision(
        self, symbol: str, ai_response: dict, webhook_data: dict
    ):
        """WAIT判定時のキャッシュ・再チェック"""
        _wait_queue[symbol] = {
            "ai_response": ai_response,
            "webhook_data": webhook_data,
            "created_at": BrokerTime.now(),
            "retry_count": 0,
        }

        await self._notifier.send(
            f"⏳ WAIT: {symbol} - {ai_response.get('reject_reason', '条件未達')}",
            level="INFO",
        )

        # 5分後に再チェックをスケジュール
        if self._scheduler:
            recheck_time = BrokerTime.now() + timedelta(seconds=WAIT_RECHECK_INTERVAL_SEC)
            self._scheduler.add_job(
                self._recheck_wait,
                "date",
                run_date=recheck_time,
                args=[symbol],
                id=f"wait_recheck_{symbol}",
                replace_existing=True,
            )

    async def _recheck_wait(self, symbol: str):
        """WAIT中のシグナルを再チェック（AI再呼び出しなし）"""
        entry = _wait_queue.get(symbol)
        if not entry:
            return

        elapsed = (BrokerTime.now() - entry["created_at"]).total_seconds()
        if elapsed > WAIT_TTL_MINUTES * 60:
            del _wait_queue[symbol]
            await self._notifier.send(
                f"⏳ WAIT期限切れ: {symbol}（{WAIT_TTL_MINUTES}分超過）",
                level="INFO",
            )
            return

        # ブロック条件だけ再チェック
        direction = entry["webhook_data"].get("direction", "LONG")
        can_enter, _ = await self._guardian.can_enter_new_trade(symbol, direction)
        event_soon, _ = await self._calendar.is_high_impact_event_soon()

        if can_enter and not event_soon:
            logger.info(f"WAIT解除: {symbol} → 元のAI判定でエントリー実行")
            await self._execute_entry(
                symbol, direction, entry["ai_response"], entry["webhook_data"]
            )
            del _wait_queue[symbol]
        else:
            entry["retry_count"] += 1
            if entry["retry_count"] >= WAIT_MAX_RETRIES:
                del _wait_queue[symbol]
                await self._notifier.send(
                    f"⏳ WAIT最終破棄: {symbol}（再チェック{WAIT_MAX_RETRIES}回超過）",
                    level="INFO",
                )

    # ──────────── ユーティリティ ────────────

    @staticmethod
    def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
        """APIコスト概算"""
        if "4o-mini" in model:
            return tokens_in * 0.15 / 1_000_000 + tokens_out * 0.6 / 1_000_000
        elif "4o" in model:
            return tokens_in * 2.5 / 1_000_000 + tokens_out * 10.0 / 1_000_000
        return 0.0
