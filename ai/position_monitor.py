"""
ai/position_monitor.py — ポジション監視エンジン

H1バッチ処理（毎時01分）・価格近接チェック（60秒ごと）・週末決済。
フォールバック: GPT-5.2 → GPT-5-mini → ルールベース。
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import MetaTrader5 as mt5
from openai import AsyncOpenAI

from config import CONFIG, estimate_api_cost
from core.broker_time import BrokerTime
from core.models import Direction, SystemStatus
from ai.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

PARTIAL_CLOSE_MAX_COUNT_PER_TRADE: int = 1
PARTIAL_CLOSE_MIN_VOLUME: float = 0.20
PARTIAL_CLOSE_MIN_REMAINING_VOLUME: float = 0.10


class PositionMonitor:
    """ポジション監視・H1バッチ査定エンジン"""

    def __init__(self):
        self._client: Optional[AsyncOpenAI] = None
        self._mt5_client = None
        self._thesis_db = None
        self._notifier = None
        self._guardian = None
        self._calendar = None
        self._prompt_builder = PromptBuilder()

    def set_dependencies(self, mt5_client, thesis_db, notifier, guardian, calendar):
        self._mt5_client = mt5_client
        self._thesis_db = thesis_db
        self._notifier = notifier
        self._guardian = guardian
        self._calendar = calendar
        self._client = AsyncOpenAI(api_key=CONFIG.OPENAI_API_KEY)

    # ──────────── H1バッチ処理 ────────────

    async def run_h1_batch_review(self):
        """毎時01分のH1バッチ査定"""
        try:
            positions = await self._mt5_client.get_all_positions()
            theses = await self._thesis_db.get_active_theses()

            if not positions and not theses:
                logger.debug("H1バッチ: ポジション・Thesisなし → スキップ")
                return

            # ポジションとThesisを結合
            positions_data = self._merge_positions_theses(positions, theses)
            if not positions_data:
                return

            # AI呼び出し
            session = BrokerTime.get_session()
            event_soon, event_desc = await self._calendar.is_high_impact_event_soon()
            major_news = event_desc if event_soon else "直近の重要指標なし"
            volatility = "NORMAL"  # 簡易版

            messages = self._prompt_builder.build_h1_batch_prompt(
                positions_data=positions_data,
                session=session,
                major_news=major_news,
                volatility_regime=volatility,
            )

            request_id = str(uuid.uuid4())
            start_time = time.time()

            ai_response = await self._call_h1_ai(messages)
            latency_ms = int((time.time() - start_time) * 1000)

            if not ai_response or "positions" not in ai_response:
                logger.warning("H1バッチ: AIレスポンス無効 → ルールベースフォールバック")
                return

            # 個別アクション実行
            results_summary = []
            failed_actions = []

            for instruction in ai_response["positions"]:
                try:
                    result = await self._execute_position_action(instruction, positions_data)
                    if result:
                        results_summary.append(result)
                except Exception as e:
                    failed_actions.append({
                        "trade_id": instruction.get("trade_id", "N/A"),
                        "action": instruction.get("action", "N/A"),
                        "error": str(e),
                    })
                    logger.exception(f"H1バッチ個別アクション失敗: {instruction.get('trade_id', 'N/A')}")

            # 部分失敗通知
            if failed_actions:
                fail_msg = "\n".join(
                    f"  ・{f['trade_id'][:8]} {f['action']}: {f['error']}"
                    for f in failed_actions
                )
                await self._notifier.send(
                    f"🔴 H1バッチ部分失敗（{len(failed_actions)}/{len(ai_response['positions'])}件）\n"
                    f"{fail_msg}\n手動確認してください",
                    level="CRITICAL",
                )

            # サマリー通知
            if results_summary:
                await self._notifier.send_h1_summary(results_summary)

            # 監査ログ
            tokens_in = ai_response.get("_tokens_in", 0)
            tokens_out = ai_response.get("_tokens_out", 0)
            model = ai_response.get("_model_used", CONFIG.MODEL_MAIN)

            await self._thesis_db.save_audit_log(
                request_id=request_id,
                purpose="H1_MONITOR",
                model=model,
                prompt_summary=messages[-1]["content"][:500],
                full_response=json.dumps(ai_response, ensure_ascii=False),
                parsed_decision=f"{len(results_summary)} actions",
                validation_result="PASS",
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

            # APIコスト記録
            cost = self._estimate_cost(model, tokens_in, tokens_out)
            await self._thesis_db.save_api_cost(model, tokens_in, tokens_out, cost, "H1_MONITOR")

        except Exception as e:
            logger.exception(f"H1バッチ処理エラー: {e}")
            await self._notifier.send(
                f"🔴 H1バッチ処理エラー: {type(e).__name__}: {e}",
                level="CRITICAL",
            )

    def _merge_positions_theses(
        self, positions: list, theses: list[dict]
    ) -> list[dict]:
        """ポジションとThesisを結合して統合データを作成"""
        thesis_map = {t["ticket"]: t for t in theses}
        merged = []

        for pos in positions:
            thesis = thesis_map.get(pos.ticket, {})
            hold_hours = 0
            if pos.open_time:
                hold_hours = (BrokerTime.now() - pos.open_time).total_seconds() / 3600

            # PnL pips計算
            point = 0.001 if "JPY" in pos.symbol else 0.00001
            if pos.symbol == "GOLD":
                point = 0.01
            pnl_pips = (pos.current_price - pos.open_price) / (point * 10)
            if pos.direction == Direction.SHORT:
                pnl_pips = -pnl_pips

            merged.append({
                "trade_id": thesis.get("trade_id", f"unknown_{pos.ticket}"),
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "direction": pos.direction.value,
                "entry_price": pos.open_price,
                "current_price": pos.current_price,
                "initial_tp": thesis.get("initial_tp", pos.tp),
                "emergency_sl": thesis.get("emergency_sl", pos.sl),
                "pnl_pips": pnl_pips,
                "hold_hours": hold_hours,
                "thesis_text": thesis.get("thesis_text", ""),
                "invalidation": thesis.get("invalidation", []),
                "market_regime": thesis.get("market_regime", "UNKNOWN"),
                "profit_jpy": pos.profit,
                "volume": pos.volume,
            })

        return merged

    async def _execute_position_action(
        self, instruction: dict, positions_data: list[dict]
    ) -> Optional[dict]:
        """H1バッチの個別ポジションアクションを実行"""
        trade_id = instruction.get("trade_id", "")
        action = instruction.get("action", "HOLD")
        thesis_status = instruction.get("thesis_status", "VALID")
        reasoning = instruction.get("reasoning", "")

        # 対応するポジションデータを取得
        pos_data = next(
            (p for p in positions_data if p["trade_id"] == trade_id), None
        )

        if not pos_data:
            logger.warning(f"H1バッチ: trade_id {trade_id[:8]} のポジションデータ未発見")
            return None

        ticket = pos_data["ticket"]

        # レビューログ用の値（アクション補正後に保存）
        old_tp = pos_data.get("initial_tp")
        new_tp = instruction.get("new_tp")

        # アクション実行
        if action == "HOLD":
            pass  # 何もしない

        elif action == "UPDATE_TP":
            if new_tp:
                success = await self._mt5_client.modify_position(ticket, new_tp=new_tp)
                if success:
                    await self._thesis_db.update_thesis_tp(trade_id, new_tp)
                    logger.info(f"TP更新: {trade_id[:8]} → {new_tp}")

        elif action == "PARTIAL_CLOSE":
            pct = instruction.get("close_percentage", 50)
            current_volume = float(pos_data.get("volume", 0) or 0)
            partial_count = await self._thesis_db.get_partial_close_count(trade_id)
            fallback_action = None

            if partial_count >= PARTIAL_CLOSE_MAX_COUNT_PER_TRADE:
                logger.info(f"部分決済回数上限でスキップ: {trade_id[:8]} count={partial_count}")
                fallback_action = "UPDATE_TP" if new_tp else "HOLD"
            elif current_volume < PARTIAL_CLOSE_MIN_VOLUME:
                logger.info(
                    f"ロットが小さすぎるため部分決済スキップ: {trade_id[:8]} volume={current_volume}"
                )
                fallback_action = "UPDATE_TP" if new_tp else "HOLD"
            else:
                remaining_volume = round(current_volume * max(0.0, (100 - pct)) / 100, 2)
                if remaining_volume < PARTIAL_CLOSE_MIN_REMAINING_VOLUME:
                    logger.info(
                        f"残ロット最小閾値未満で部分決済スキップ: {trade_id[:8]} remain={remaining_volume}"
                    )
                    fallback_action = "UPDATE_TP" if new_tp else "HOLD"
                else:
                    result = await self._mt5_client.close_position(ticket, percentage=pct)
                    if result and result.success:
                        logger.info(f"部分決済: {trade_id[:8]} {pct}%")
                        await self._move_sl_to_breakeven_if_needed(pos_data)
                    else:
                        logger.warning(f"部分決済失敗: {trade_id[:8]} {pct}%")

            if fallback_action == "UPDATE_TP" and new_tp:
                success = await self._mt5_client.modify_position(ticket, new_tp=new_tp)
                if success:
                    await self._thesis_db.update_thesis_tp(trade_id, new_tp)
                    logger.info(f"部分決済代替でTP更新: {trade_id[:8]} → {new_tp}")
                action = "UPDATE_TP"
            elif fallback_action == "HOLD":
                action = "HOLD"

        elif action == "FULL_CLOSE":
            result = await self._mt5_client.close_position(ticket)
            if result and result.success:
                await self._finalize_trade(trade_id, pos_data, "THESIS_BROKEN")
                logger.info(f"全決済: {trade_id[:8]}")

        await self._thesis_db.save_review(
            trade_id=trade_id,
            thesis_status=thesis_status,
            action=action,
            old_tp=old_tp,
            new_tp=new_tp,
            reasoning=reasoning,
            ai_model_used=CONFIG.MODEL_MAIN,
            tokens_used=0,
        )

        return {
            "trade_id": trade_id,
            "symbol": pos_data["symbol"],
            "thesis_status": thesis_status,
            "action": action,
            "reasoning": reasoning[:80],
        }

    async def _move_sl_to_breakeven_if_needed(self, pos_data: dict):
        """部分決済後、必要ならSLを建値に引き上げる"""
        trade_id = pos_data.get("trade_id", "")
        ticket = pos_data.get("ticket")
        entry_price = pos_data.get("entry_price")
        current_sl = pos_data.get("emergency_sl")
        direction = pos_data.get("direction")

        if not ticket or entry_price is None or direction not in ("LONG", "SHORT"):
            return

        needs_move = False
        if direction == "LONG":
            needs_move = current_sl is None or current_sl < entry_price
        else:
            needs_move = current_sl is None or current_sl > entry_price

        if not needs_move:
            return

        success = await self._mt5_client.modify_position(ticket, new_sl=entry_price)
        if success:
            await self._thesis_db.update_thesis_sl(trade_id, entry_price)
            logger.info(f"建値SL適用: {trade_id[:8]} ticket={ticket} sl={entry_price}")
        else:
            logger.warning(f"建値SL適用失敗: {trade_id[:8]} ticket={ticket}")

    async def _finalize_trade(
        self, trade_id: str, pos_data: dict, exit_reason: str
    ):
        """トレード終了処理（trade_history保存 + thesis CLOSE）"""
        try:
            pnl_pips = pos_data.get("pnl_pips", 0)
            pnl_jpy = pos_data.get("profit_jpy", 0)
            hold_hours = pos_data.get("hold_hours", 0)

            await self._thesis_db.save_trade_history(
                trade_id=trade_id,
                symbol=pos_data["symbol"],
                direction=pos_data["direction"],
                entry_price=pos_data["entry_price"],
                exit_price=pos_data["current_price"],
                exit_reason=exit_reason,
                pnl_pips=pnl_pips,
                pnl_jpy=pnl_jpy,
                lot_size=pos_data.get("volume", 0),
                thesis_text=pos_data.get("thesis_text", ""),
                ai_confidence=0,
                hold_hours=hold_hours,
                broker_entry_time=None,
                broker_exit_time=BrokerTime.now_str(),
            )

            await self._thesis_db.close_thesis(trade_id)

            await self._notifier.send_close_notification(
                trade_id=trade_id,
                symbol=pos_data["symbol"],
                direction=pos_data["direction"],
                exit_reason=exit_reason,
                pnl_pips=pnl_pips,
                pnl_jpy=pnl_jpy,
                hold_hours=hold_hours,
            )
        except Exception as e:
            logger.exception(f"トレード終了処理エラー: {trade_id[:8]}")

    # ──────────── 価格近接チェック（Layer 1） ────────────

    async def check_price_proximity(self):
        """60秒ごとの価格近接チェック（ルールベース）"""
        try:
            positions = await self._mt5_client.get_all_positions()
            theses = await self._thesis_db.get_active_theses()
            thesis_map = {t["ticket"]: t for t in theses}

            for pos in positions:
                thesis = thesis_map.get(pos.ticket)
                if not thesis:
                    continue

                tp = thesis.get("initial_tp", pos.tp)
                sl = thesis.get("emergency_sl", pos.sl)
                current = pos.current_price

                if tp and tp > 0:
                    # TP/SLまでの距離
                    tp_distance = abs(tp - current)
                    entry_tp_distance = abs(tp - pos.open_price)

                    # TP 50%到達 → nano一次審査をスキップして直接精密評価
                    if entry_tp_distance > 0 and tp_distance <= entry_tp_distance * 0.5:
                        await self._trigger_layer2(
                            pos, thesis, "TP近接50%", use_nano_triage=False
                        )

                # 急激な逆行チェック（ATRベース）
                atr = self._get_thesis_atr(thesis)
                if atr and atr > 0:
                    # 含み損がATR×1.5以上
                    loss_distance = abs(current - pos.open_price)
                    if pos.profit < 0 and loss_distance > atr * 1.5:
                        await self._trigger_layer2(
                            pos, thesis, "急激な逆行（ATR×1.5超）", use_nano_triage=True
                        )

        except Exception as e:
            logger.error(f"価格近接チェックエラー: {e}")

    async def _trigger_layer2(
        self,
        pos,
        thesis: dict,
        trigger_reason: str,
        use_nano_triage: bool = True,
    ):
        """Layer 2評価。

        - use_nano_triage=True: nano一次審査 → gpt-5.2精密評価
        - use_nano_triage=False: nanoをスキップして gpt-5.2精密評価
        """
        try:
            trade_id = thesis.get("trade_id", "N/A")
            point = 0.001 if "JPY" in pos.symbol else 0.00001
            if pos.symbol == "GOLD":
                point = 0.01
            pnl_pips = (pos.current_price - pos.open_price) / (point * 10)
            if pos.direction == Direction.SHORT:
                pnl_pips = -pnl_pips

            nano_reason = "一次審査スキップ"
            if use_nano_triage:
                # === Step 1: gpt-5-nano 一次審査 ===
                triage_messages = self._prompt_builder.build_nano_triage_prompt(
                    trigger_reason=trigger_reason,
                    symbol=pos.symbol,
                    direction=pos.direction.value,
                    entry_price=pos.open_price,
                    current_price=pos.current_price,
                    pnl_pips=pnl_pips,
                    thesis_summary=thesis.get("thesis_text", "")[:150],
                    invalidation_conditions=thesis.get("invalidation", []),
                    tp=thesis.get("initial_tp", pos.tp),
                    sl=thesis.get("emergency_sl", pos.sl),
                )

                triage_result = await self._call_nano_triage(triage_messages)

                if not triage_result or not triage_result.get("alert"):
                    # nanoが問題なしと判断 → スルー（コストほぼゼロ）
                    logger.debug(
                        f"nano triage OK: {pos.symbol} {trade_id[:8]} - "
                        f"{triage_result.get('reason', 'no issue') if triage_result else 'parse_fail'}"
                    )
                    return

                nano_reason = triage_result.get("reason", "異常検知")
                logger.info(
                    f"🚨 nano異常検知: {pos.symbol} {trade_id[:8]} - {nano_reason} → gpt-5.2精密評価へ"
                )
            else:
                nano_reason = "TP近接50%到達のため一次審査をスキップ"
                logger.info(
                    f"🚨 直接精密評価: {pos.symbol} {trade_id[:8]} - {nano_reason}"
                )

            # === Step 2: gpt-5.2 精密評価 ===
            hold_hours = 0
            if pos.open_time:
                hold_hours = (BrokerTime.now() - pos.open_time).total_seconds() / 3600

            pos_data = {
                "trade_id": trade_id,
                "ticket": pos.ticket,
                "symbol": pos.symbol,
                "direction": pos.direction.value,
                "entry_price": pos.open_price,
                "current_price": pos.current_price,
                "initial_tp": thesis.get("initial_tp", pos.tp),
                "emergency_sl": thesis.get("emergency_sl", pos.sl),
                "pnl_pips": pnl_pips,
                "hold_hours": hold_hours,
                "thesis_text": thesis.get("thesis_text", ""),
                "invalidation": thesis.get("invalidation", []),
                "market_regime": thesis.get("market_regime", "UNKNOWN"),
                "profit_jpy": pos.profit,
                "volume": pos.volume,
            }

            session = BrokerTime.get_session()
            eval_messages = self._prompt_builder.build_single_position_eval_prompt(
                pos_data=pos_data,
                trigger_reason=trigger_reason,
                nano_reason=nano_reason,
                session=session,
            )

            eval_result = await self._call_deep_eval(eval_messages)

            if not eval_result:
                # gpt-5.2も失敗 → 安全のためDiscord通知のみ
                await self._notifier.send(
                    f"🚨 異常検知（AI評価失敗）: {pos.symbol} {pos.direction.value}\n"
                    f"トリガー: {trigger_reason}\n"
                    f"nano判断: {nano_reason}\n"
                    f"PnL: {pnl_pips:+.1f}pips\n"
                    f"🆔 {trade_id[:8]}\n"
                    f"❗ AI評価失敗のため手動確認してください",
                    level="CRITICAL",
                )
                return

            # === Step 3: アクション実行 ===
            action = eval_result.get("action", "HOLD")
            eval_result["trade_id"] = trade_id  # trade_idを確実にセット

            if action != "HOLD":
                # H1バッチと同じアクション実行ロジックを再利用
                result = await self._execute_position_action(eval_result, [pos_data])

                # Discord通知
                reasoning = eval_result.get("reasoning", "N/A")[:120]
                thesis_status = eval_result.get("thesis_status", "N/A")
                await self._notifier.send(
                    f"🚨 精密評価実行: {pos.symbol} {pos.direction.value}\n"
                    f"トリガー: {trigger_reason}\n"
                    f"Thesis: {thesis_status} → {action}\n"
                    f"PnL: {pnl_pips:+.1f}pips\n"
                    f"理由: {reasoning}\n"
                    f"🆔 {trade_id[:8]}",
                    level="CRITICAL" if action == "FULL_CLOSE" else "WARNING",
                )
            else:
                logger.info(
                    f"gpt-5.2 HOLD判定: {pos.symbol} {trade_id[:8]} - "
                    f"{eval_result.get('reasoning', 'N/A')[:80]}"
                )

        except Exception as e:
            logger.error(f"Layer2 2階層評価エラー: {e}")

    @staticmethod
    def _get_thesis_atr(thesis: dict) -> Optional[float]:
        """ThesisからATR値を取得"""
        tech = thesis.get("technical_ctx", {})
        if isinstance(tech, str):
            try:
                tech = json.loads(tech)
            except json.JSONDecodeError:
                return None
        return tech.get("atr")

    # ──────────── 週末管理 ────────────

    async def weekend_force_close(self):
        """金曜 XMT 22:00 全ポジション強制決済"""
        logger.info("週末前強制決済開始")

        positions = await self._mt5_client.get_all_positions()
        if not positions:
            await self._notifier.send("🔸 週末決済: ポジションなし", level="INFO")
            return

        results = await self._mt5_client.close_all_positions()
        success = sum(1 for r in results if r.success)
        fail = sum(1 for r in results if not r.success)

        # Thesisクローズ
        theses = await self._thesis_db.get_active_theses()
        merged = self._merge_positions_theses(positions, theses)
        for pos_data in merged:
            await self._finalize_trade(pos_data["trade_id"], pos_data, "WEEKEND_CLOSE")

        await self._notifier.send(
            f"🔸 週末全決済完了: 成功={success} 失敗={fail}",
            level="INFO" if fail == 0 else "CRITICAL",
        )

    async def weekend_final_check(self):
        """金曜 XMT 22:30 最終残存チェック"""
        positions = await self._mt5_client.get_all_positions()
        if positions:
            await self._notifier.send(
                f"🔴 週末最終チェック: {len(positions)}件のポジション残存！\n"
                f"手動確認してください",
                level="CRITICAL",
            )
        else:
            self._guardian.status = SystemStatus.WEEKEND_CLOSED
            await self._notifier.send("✅ 週末最終チェック: ポジション0件", level="INFO")

    # ──────────── AI呼び出し ────────────

    async def _call_h1_ai(self, messages: list[dict]) -> Optional[dict]:
        """H1バッチAI呼び出し（フォールバック付き）"""
        # 試行1: GPT-5
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_MAIN, messages),
                timeout=CONFIG.AI_TIMEOUT_MAIN_SEC,
            )
            if response:
                result = self._parse_response(response)
                if result:
                    result["_model_used"] = CONFIG.MODEL_MAIN
                    return result
        except asyncio.TimeoutError:
            logger.warning("H1バッチ: GPT-5.2タイムアウト")
        except Exception as e:
            logger.warning(f"H1バッチ: GPT-5.2エラー: {e}")

        # 試行2: GPT-5-mini
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_FAST, messages),
                timeout=CONFIG.AI_TIMEOUT_FAST_SEC,
            )
            if response:
                result = self._parse_response(response)
                if result:
                    result["_model_used"] = CONFIG.MODEL_FAST
                    return result
        except Exception as e:
            logger.warning(f"H1バッチ: GPT-5-miniもエラー: {e}")

        # 試行3: ルールベースフォールバック
        logger.warning("H1バッチ: AI全障害 → ルールベースで全ポジHOLD")
        self._guardian.status = SystemStatus.MONITOR_ONLY
        await self._notifier.send(
            "🔴 AI障害: ルールベースで運用中（全ポジHOLD・新規エントリー停止）",
            level="CRITICAL",
        )
        return None

    async def _call_fast_ai(self, messages: list[dict]) -> Optional[dict]:
        """緊急判定AI（GPT-5-mini）"""
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_FAST, messages),
                timeout=CONFIG.AI_TIMEOUT_FAST_SEC,
            )
            if response:
                return self._parse_response(response)
        except Exception as e:
            logger.error(f"緊急AI呼び出しエラー: {e}")
        return None

    async def _call_nano_triage(self, messages: list[dict]) -> Optional[dict]:
        """nano一次審査（gpt-5-nano）"""
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.WAIT_RECHECK_MODEL, messages),
                timeout=CONFIG.AI_TIMEOUT_FAST_SEC,
            )
            if response:
                return self._parse_response(response)
        except Exception as e:
            # nano失敗時は安全側にalert=Trueを返す
            logger.warning(f"nano triageエラー: {e} → 安全側でalert=true")
            return {"alert": True, "reason": "nano失敗・安全側にエスカレーション"}

    async def _call_deep_eval(self, messages: list[dict]) -> Optional[dict]:
        """単一ポジション精密評価（gpt-5.2）"""
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_MAIN, messages),
                timeout=CONFIG.AI_TIMEOUT_MAIN_SEC,
            )
            if response:
                result = self._parse_response(response)
                if result:
                    result["_model_used"] = CONFIG.MODEL_MAIN
                    return result
        except asyncio.TimeoutError:
            logger.warning("精密評価: gpt-5.2タイムアウト")
        except Exception as e:
            logger.error(f"精密評価エラー: {e}")

        # gpt-5.2失敗 → gpt-5-miniフォールバック
        try:
            response = await asyncio.wait_for(
                self._call_ai(CONFIG.MODEL_FAST, messages),
                timeout=CONFIG.AI_TIMEOUT_FAST_SEC,
            )
            if response:
                result = self._parse_response(response)
                if result:
                    result["_model_used"] = CONFIG.MODEL_FAST
                    return result
        except Exception as e:
            logger.error(f"精密評価フォールバックも失敗: {e}")

        return None

    async def _call_ai(self, model: str, messages: list[dict]) -> Optional[str]:
        """OpenAI Responses API呼び出し"""
        try:
            kwargs = {
                "model": model,
                "input": messages,
            }

            # gpt-5系推論モデルのreasoning_effort制御
            if model == CONFIG.MODEL_MAIN:
                kwargs["reasoning"] = {"effort": CONFIG.AI_REASONING_EFFORT_MAIN}
            elif model == CONFIG.MODEL_FAST:
                kwargs["reasoning"] = {"effort": CONFIG.AI_REASONING_EFFORT_FAST}
            elif model == CONFIG.WAIT_RECHECK_MODEL:
                kwargs["reasoning"] = {"effort": CONFIG.WAIT_RECHECK_REASONING_EFFORT}

            response = await self._client.responses.create(**kwargs)

            text = ""
            tokens_in = 0
            tokens_out = 0

            if hasattr(response, "output"):
                for item in response.output:
                    # reasoning itemはcontent=Noneなのでスキップ
                    if getattr(item, "type", "") != "message":
                        continue
                    content = getattr(item, "content", None)
                    if content is None:
                        continue
                    for block in content:
                        if hasattr(block, "text"):
                            text += block.text

            if hasattr(response, "usage"):
                tokens_in = response.usage.input_tokens
                tokens_out = response.usage.output_tokens

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

    @staticmethod
    def _parse_response(response_json: str) -> Optional[dict]:
        """AIレスポンスのJSON部分をパース"""
        try:
            meta = json.loads(response_json)
            text = meta.get("_text", "")

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            text = text.strip()
            result = json.loads(text)
            result["_tokens_in"] = meta.get("_tokens_in", 0)
            result["_tokens_out"] = meta.get("_tokens_out", 0)
            return result
        except (json.JSONDecodeError, IndexError) as e:
            logger.error(f"AIレスポンスパースエラー: {e}")
            return None

    @staticmethod
    def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
        return estimate_api_cost(model, tokens_in, tokens_out)
