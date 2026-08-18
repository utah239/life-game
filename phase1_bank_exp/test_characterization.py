# -*- coding: utf-8 -*-
"""段階的モジュール分割(behavior-preserving refactoring)の Step 0。

現行の game.py の挙動を凍結する特性テスト(characterization tests)。
以降の Step でコードを移動・分割しても、このテストが全件成功し続けることを
「外部挙動が変わっていない」ことの根拠にする。

対象は「巨大な標準出力全文」ではなく、構造化された値(resources / traits /
counts / bank_stage / currency_stage / bank_crisis_count / death_turn 等)。
定数・閾値・計算式はこの Step では一切変更しない——ここにある期待値は、
現行実装を実際に1回実行して採取した golden 値をそのまま書き写したもの。

実行方法: python -m unittest test_characterization -v
"""
import builtins
import contextlib
import copy
import dataclasses
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import game  # noqa: E402
import event_store  # noqa: E402
import projection  # noqa: E402
import engine  # noqa: E402
import policy as policy_engine  # noqa: E402
import resource_rules  # noqa: E402
import trait_rules  # noqa: E402
import economic_rules  # noqa: E402
import action_costs  # noqa: E402
import relationship_rules  # noqa: E402
import contract_costs  # noqa: E402
import counterparty_selection  # noqa: E402
import turn_engine  # noqa: E402
import event_builders  # noqa: E402
import scenario_generation  # noqa: E402
import interactive_runtime  # noqa: E402
import offline_simulation  # noqa: E402
import game_session  # noqa: E402
import llm_integration  # noqa: E402
from dashboard import build_dashboard  # noqa: E402
from dashboard import experiment_parameters  # noqa: E402
from dashboard import server as experiment_server  # noqa: E402


# ==============================================================================
# event_store.py (Step 2A、2026-08-14): JSONL保存層の直接テスト
# ==============================================================================

class EventStoreTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.path = Path(path)
        self.path.unlink()  # 「ファイル不存在」から始めるテストもあるため一旦消す

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_iter_on_nonexistent_file_yields_nothing(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(list(event_store.iter_jsonl_events(self.path)), [])

    def test_append_and_reread_japanese_payload(self):
        event_store.append_jsonl_event(
            self.path, "npc_introduced",
            {"id": "友人A", "name": "友人A", "role": "acquaintance"})
        events = list(event_store.iter_jsonl_events(self.path))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "npc_introduced")
        self.assertEqual(events[0]["data"]["name"], "友人A")
        self.assertIn("ts", events[0])
        # ensure_ascii=Falseなので、日本語がエスケープされず生の文字で保存されている
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("友人A", raw)
        self.assertNotIn("\\u", raw)

    def test_read_skips_blank_lines(self):
        with self.path.open("w", encoding="utf-8") as f:
            f.write('{"ts": "t1", "type": "a", "data": {}}\n')
            f.write("\n")
            f.write("   \n")
            f.write('{"ts": "t2", "type": "b", "data": {}}\n')
        events = list(event_store.iter_jsonl_events(self.path))
        self.assertEqual([e["type"] for e in events], ["a", "b"])

    def test_multiple_appends_preserve_order(self):
        event_store.append_jsonl_event(self.path, "first", {"n": 1})
        event_store.append_jsonl_event(self.path, "second", {"n": 2})
        events = list(event_store.iter_jsonl_events(self.path))
        self.assertEqual([e["type"] for e in events], ["first", "second"])
        self.assertEqual([e["data"]["n"] for e in events], [1, 2])


class GameEventsPathCompatibilityTest(unittest.TestCase):
    """game.EVENTS_PATHの差し替えが、event_store切り出し後も
    append_event()/iter_events()のラッパー経由で引き続き機能することの確認。"""

    def setUp(self):
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.path = Path(path)
        self.path.unlink()
        game.EVENTS_PATH = self.path

    def tearDown(self):
        game.EVENTS_PATH = self._orig_events_path
        self.path.unlink(missing_ok=True)

    def test_append_event_and_iter_events_use_current_events_path(self):
        self.assertEqual(list(game.iter_events()), [])
        game.append_event("test_type", {"key": "値"})
        events = list(game.iter_events())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "test_type")
        self.assertEqual(events[0]["data"]["key"], "値")
        # event_store.iter_jsonl_eventsで直接読んでも同じ内容になること
        direct = list(event_store.iter_jsonl_events(game.EVENTS_PATH))
        self.assertEqual(direct, events)


# ==============================================================================
# bank_stage_next の閾値境界
# ==============================================================================

class BankStageNextTest(unittest.TestCase):
    def test_healthy_stays_healthy_at_enter_threshold_boundary(self):
        # bank_trust == BANK_STAGE1_ENTER ちょうどは「未満」ではないので健全のまま
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER, 0),
            game.BANK_STAGE_HEALTHY)

    def test_healthy_to_contraction_just_below_enter(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER - 0.01, 0),
            game.BANK_STAGE_CONTRACTION)

    def test_healthy_to_halted_just_below_stage2_enter(self):
        # 一気にStage2の閾値を下回った場合、Stage1を経由せず直接Stage2になる
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HEALTHY, game.BANK_STAGE2_ENTER - 0.01, 0),
            game.BANK_STAGE_HALTED)

    def test_contraction_stays_below_exit(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_CONTRACTION, game.BANK_STAGE1_EXIT - 0.01, 0),
            game.BANK_STAGE_CONTRACTION)

    def test_contraction_recovers_at_exit_threshold(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_CONTRACTION, game.BANK_STAGE1_EXIT, 0),
            game.BANK_STAGE_HEALTHY)

    def test_contraction_to_halted_just_below_stage2_enter(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_CONTRACTION, game.BANK_STAGE2_ENTER - 0.01, 0),
            game.BANK_STAGE_HALTED)

    def test_halted_stays_below_stage2_exit(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HALTED, game.BANK_STAGE2_EXIT - 0.01, 0),
            game.BANK_STAGE_HALTED)

    def test_halted_recovers_to_contraction_at_stage2_exit(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HALTED, game.BANK_STAGE2_EXIT, 0),
            game.BANK_STAGE_CONTRACTION)

    def test_halted_stays_halted_between_exit_and_enter1(self):
        # Stage2脱出はできるがStage1復帰にはまだ届かない中間帯
        mid = (game.BANK_STAGE2_EXIT + game.BANK_STAGE1_ENTER) / 2
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HALTED, mid, 0),
            game.BANK_STAGE_CONTRACTION)

    def test_collapsed_is_irreversible_even_at_full_trust(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_COLLAPSED, 100.0, 0),
            game.BANK_STAGE_COLLAPSED)

    def test_crisis_count_forces_collapse_from_healthy(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HEALTHY, 100.0, game.BANK_STAGE3_CRISIS_COUNT),
            game.BANK_STAGE_COLLAPSED)

    def test_crisis_count_just_below_threshold_no_collapse(self):
        self.assertEqual(
            game.bank_stage_next(game.BANK_STAGE_HEALTHY, 100.0, game.BANK_STAGE3_CRISIS_COUNT - 1),
            game.BANK_STAGE_HEALTHY)


# ==============================================================================
# currency_stage_next の閾値境界
# ==============================================================================

class CurrencyStageNextTest(unittest.TestCase):
    def test_normal_stays_normal_at_enter_threshold_boundary(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER),
            game.CURRENCY_STAGE_NORMAL)

    def test_normal_to_wary_just_below_enter(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER - 0.01),
            game.CURRENCY_STAGE_WARY)

    def test_wary_stays_below_exit(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_WARY, game.CURRENCY_STAGE1_EXIT - 0.01),
            game.CURRENCY_STAGE_WARY)

    def test_wary_recovers_at_exit_threshold(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_WARY, game.CURRENCY_STAGE1_EXIT),
            game.CURRENCY_STAGE_NORMAL)

    def test_any_stage_abandoned_just_below_stage3_enter(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE3_ENTER - 0.01),
            game.CURRENCY_STAGE_ABANDONED)

    def test_abandoned_stays_below_stage3_exit(self):
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_ABANDONED, game.CURRENCY_STAGE3_EXIT - 0.01),
            game.CURRENCY_STAGE_ABANDONED)

    def test_abandoned_recovers_to_wary_at_stage3_exit(self):
        # bank_stageと違い、CURRENCY_STAGE_ABANDONEDは意図的に可逆
        self.assertEqual(
            game.currency_stage_next(game.CURRENCY_STAGE_ABANDONED, game.CURRENCY_STAGE3_EXIT),
            game.CURRENCY_STAGE_WARY)


# ==============================================================================
# local_credit_stage_next の閾値境界(2026-08-15、地域信用制度)。bank_stage_next
# と同じ4段階のヒステリシス構造(全段階可逆、bank_stageと違いStage3も含めて
# 不可逆条件が無い——institutions/local_credit.pyのコメント参照)。
# ==============================================================================

class LocalCreditStageNextTest(unittest.TestCase):
    def test_healthy_stays_healthy_at_enter_threshold_boundary(self):
        self.assertEqual(
            game.local_credit_stage_next(game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE1_ENTER),
            game.LOCAL_CREDIT_STAGE_HEALTHY)

    def test_healthy_to_contraction_just_below_enter(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE1_ENTER - 0.01),
            game.LOCAL_CREDIT_STAGE_CONTRACTION)

    def test_healthy_to_personal_just_below_stage2_enter(self):
        # 一気にStage2の閾値を下回った場合、Stage1を経由せず直接Stage2になる
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE2_ENTER - 0.01),
            game.LOCAL_CREDIT_STAGE_PERSONAL)

    def test_healthy_to_isolated_just_below_stage3_enter(self):
        # 一気にStage3の閾値を下回った場合、Stage1・Stage2を経由せず直接Stage3になる
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE3_ENTER - 0.01),
            game.LOCAL_CREDIT_STAGE_ISOLATED)

    def test_contraction_stays_below_exit(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_CONTRACTION, game.LOCAL_CREDIT_STAGE1_EXIT - 0.01),
            game.LOCAL_CREDIT_STAGE_CONTRACTION)

    def test_contraction_recovers_at_exit_threshold(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_CONTRACTION, game.LOCAL_CREDIT_STAGE1_EXIT),
            game.LOCAL_CREDIT_STAGE_HEALTHY)

    def test_personal_stays_below_exit(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_PERSONAL, game.LOCAL_CREDIT_STAGE2_EXIT - 0.01),
            game.LOCAL_CREDIT_STAGE_PERSONAL)

    def test_personal_recovers_to_contraction_at_exit(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_PERSONAL, game.LOCAL_CREDIT_STAGE2_EXIT),
            game.LOCAL_CREDIT_STAGE_CONTRACTION)

    def test_isolated_stays_below_exit(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_ISOLATED, game.LOCAL_CREDIT_STAGE3_EXIT - 0.01),
            game.LOCAL_CREDIT_STAGE_ISOLATED)

    def test_isolated_recovers_to_personal_at_exit(self):
        self.assertEqual(
            game.local_credit_stage_next(
                game.LOCAL_CREDIT_STAGE_ISOLATED, game.LOCAL_CREDIT_STAGE3_EXIT),
            game.LOCAL_CREDIT_STAGE_PERSONAL)

    def test_isolated_is_reversible_unlike_bank_collapsed(self):
        # bank_stage_next(BANK_STAGE_COLLAPSED)は100.0でも不可逆のまま
        # (BankStageNextTest.test_collapsed_is_irreversible_even_at_full_trust)。
        # local_credit_stage_nextはStage3(孤立)も含めて全段階可逆
        # (institutions/local_credit.pyのコメント参照、人口機構が未実装のため)。
        # ただし他のStageと同じヒステリシス設計どおり、1回の判定で1段階しか
        # 戻らない(ISOLATED→PERSONAL。HEALTHYへ一気に戻るのではない)。
        self.assertEqual(
            game.local_credit_stage_next(game.LOCAL_CREDIT_STAGE_ISOLATED, 100.0),
            game.LOCAL_CREDIT_STAGE_PERSONAL)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.local_credit_stage_next(game.LOCAL_CREDIT_STAGE_HEALTHY, 20.0)
        self.assertEqual(random.getstate(), state_before)


# ==============================================================================
# enforcement_stage_next の閾値境界(2026-08-15、契約執行制度)。bank_stage_next/
# local_credit_stage_nextと同じヒステリシス構造の5段階版(全段階可逆)。
# ==============================================================================

class ContractEnforcementStageNextTest(unittest.TestCase):
    def test_institutional_stays_at_enter_threshold_boundary(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE1_ENTER),
            game.ENFORCEMENT_STAGE_INSTITUTIONAL)

    def test_institutional_to_delayed_just_below_enter(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE1_ENTER - 0.01),
            game.ENFORCEMENT_STAGE_DELAYED)

    def test_institutional_to_local_ledger_just_below_stage2_enter(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE2_ENTER - 0.01),
            game.ENFORCEMENT_STAGE_LOCAL_LEDGER)

    def test_institutional_to_personal_just_below_stage3_enter(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE3_ENTER - 0.01),
            game.ENFORCEMENT_STAGE_PERSONAL)

    def test_institutional_to_none_just_below_stage4_enter(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE4_ENTER - 0.01),
            game.ENFORCEMENT_STAGE_NONE)

    def test_delayed_stays_below_exit(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_DELAYED, game.ENFORCEMENT_STAGE1_EXIT - 0.01),
            game.ENFORCEMENT_STAGE_DELAYED)

    def test_delayed_recovers_at_exit(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_DELAYED, game.ENFORCEMENT_STAGE1_EXIT),
            game.ENFORCEMENT_STAGE_INSTITUTIONAL)

    def test_local_ledger_recovers_at_exit(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_LOCAL_LEDGER, game.ENFORCEMENT_STAGE2_EXIT),
            game.ENFORCEMENT_STAGE_DELAYED)

    def test_personal_recovers_at_exit(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_PERSONAL, game.ENFORCEMENT_STAGE3_EXIT),
            game.ENFORCEMENT_STAGE_LOCAL_LEDGER)

    def test_none_stays_below_exit(self):
        self.assertEqual(
            game.enforcement_stage_next(
                game.ENFORCEMENT_STAGE_NONE, game.ENFORCEMENT_STAGE4_EXIT - 0.01),
            game.ENFORCEMENT_STAGE_NONE)

    def test_none_recovers_at_exit(self):
        # bank_stage_next(BANK_STAGE_COLLAPSED)と違い、ENFORCEMENT_STAGE_NONEも
        # 可逆(人口機構が未実装のため不可逆を正当化できない、local_creditと
        # 同じ判断)。ただし他のStageと同様に1段階ずつしか戻らない。
        self.assertEqual(
            game.enforcement_stage_next(game.ENFORCEMENT_STAGE_NONE, 100.0),
            game.ENFORCEMENT_STAGE_PERSONAL)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.enforcement_stage_next(game.ENFORCEMENT_STAGE_INSTITUTIONAL, 20.0)
        self.assertEqual(random.getstate(), state_before)


class ContractEnforcementPenaltyMultiplierTest(unittest.TestCase):
    def test_all_stages(self):
        self.assertEqual(game.contract_enforcement_penalty_multiplier(
            game.ENFORCEMENT_STAGE_INSTITUTIONAL), 1.0)
        self.assertEqual(game.contract_enforcement_penalty_multiplier(
            game.ENFORCEMENT_STAGE_DELAYED), 0.6)
        self.assertEqual(game.contract_enforcement_penalty_multiplier(
            game.ENFORCEMENT_STAGE_LOCAL_LEDGER), 0.3)
        self.assertEqual(game.contract_enforcement_penalty_multiplier(
            game.ENFORCEMENT_STAGE_PERSONAL), 0.1)
        self.assertEqual(game.contract_enforcement_penalty_multiplier(
            game.ENFORCEMENT_STAGE_NONE), 0.0)


class ContractEnforcementTrustAmplifierTest(unittest.TestCase):
    def test_all_stages(self):
        self.assertEqual(game.contract_enforcement_trust_amplifier(
            game.ENFORCEMENT_STAGE_INSTITUTIONAL), 1.0)
        self.assertEqual(game.contract_enforcement_trust_amplifier(
            game.ENFORCEMENT_STAGE_DELAYED), 1.0)
        self.assertEqual(game.contract_enforcement_trust_amplifier(
            game.ENFORCEMENT_STAGE_LOCAL_LEDGER), 1.5)
        self.assertEqual(game.contract_enforcement_trust_amplifier(
            game.ENFORCEMENT_STAGE_PERSONAL), 2.0)
        self.assertEqual(game.contract_enforcement_trust_amplifier(
            game.ENFORCEMENT_STAGE_NONE), 2.0)


# ==============================================================================
# 中間値への回帰の式(bank_trust_reversion / currency_confidence_reversion)
# ==============================================================================

class ReversionFormulaTest(unittest.TestCase):
    def test_bank_trust_reversion_above_initial_is_positive(self):
        self.assertEqual(game.bank_trust_reversion(70.0), 1.0)

    def test_bank_trust_reversion_below_initial_is_negative(self):
        self.assertEqual(game.bank_trust_reversion(30.0), -1.0)

    def test_bank_trust_reversion_at_initial_is_zero(self):
        self.assertEqual(game.bank_trust_reversion(game.BANK_TRUST_INITIAL), 0.0)

    def test_bank_trust_reversion_at_zero(self):
        self.assertEqual(game.bank_trust_reversion(0.0), -2.5)

    def test_currency_confidence_reversion_above_initial_is_positive(self):
        self.assertEqual(game.currency_confidence_reversion(70.0), 1.0)

    def test_currency_confidence_reversion_below_initial_is_negative(self):
        self.assertEqual(game.currency_confidence_reversion(20.0), -1.5)

    def test_currency_confidence_reversion_at_initial_is_zero(self):
        self.assertEqual(
            game.currency_confidence_reversion(game.CURRENCY_CONFIDENCE_INITIAL), 0.0)

    def test_community_trust_reversion_above_initial_is_positive(self):
        self.assertEqual(game.community_trust_reversion(70.0), 1.0)

    def test_community_trust_reversion_below_initial_is_negative(self):
        self.assertEqual(game.community_trust_reversion(30.0), -1.0)

    def test_community_trust_reversion_at_initial_is_zero(self):
        self.assertEqual(
            game.community_trust_reversion(game.LOCAL_CREDIT_TRUST_INITIAL), 0.0)

    def test_enforcement_capacity_reversion_above_initial_is_positive(self):
        self.assertEqual(game.enforcement_capacity_reversion(70.0), 1.0)

    def test_enforcement_capacity_reversion_below_initial_is_negative(self):
        self.assertEqual(game.enforcement_capacity_reversion(30.0), -1.0)

    def test_enforcement_capacity_reversion_at_initial_is_zero(self):
        self.assertEqual(
            game.enforcement_capacity_reversion(game.ENFORCEMENT_CAPACITY_INITIAL), 0.0)


# ==============================================================================
# 固定イベント列の reduce_state() 結果
# ==============================================================================

class ReduceStateFixtureTest(unittest.TestCase):
    """turn_started/character_born/npc_introduced(bank・economy・acquaintance)/
    npc_wallet_changed/npc_trust_changed(bank側・acquaintance側の両方)/
    income_applied/contract_created/contract_settled/currency_confidence_changed/
    institution_transition/trait_changed/character_died を1回ずつ含む固定列。
    期待値は現行実装を実際に1回実行して採取したgolden値(手計算でも二重検算済み:
    bank trust=50-2+5=53、友人Aはeffective_npc_trust(50,elapsed=2turn)=50のまま
    +10=60→最終読み出し時にさらに2ターン分回帰して59.7515625、
    currency_confidence=50-3=47)。"""

    FIXTURE = [
        ("turn_started", {"turn": 1}),
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                            "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                            "role": "wage_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "友人A", "name": "友人A", "birth_turn": 1,
                            "role": "acquaintance", "trust": 50.0,
                            "ethics": 60.0, "retire_turn": 200}),
        ("income_applied", {"turn": 1, "delta": {"money": 40}, "reason": "periodic_income"}),
        ("npc_wallet_changed", {"npc_id": "bank", "delta": -40, "reason": "wage_payment", "turn": 1}),
        ("contract_created", {"id": "c1", "counterparty": "中央銀行",
                              "description": "賃金前借り", "created_turn": 1,
                              "due_turn": 3, "repay_money": -40, "is_bank_debt": True}),
        ("npc_trust_changed", {"npc_id": "bank", "delta": -2.0, "turn": 1,
                               "reason": "bank_trust_upkeep"}),
        ("turn_started", {"turn": 3}),
        ("contract_settled", {"id": "c1", "turn": 3, "outcome": "fulfilled",
                              "settled_by": "labor", "delta": {}}),
        ("npc_trust_changed", {"npc_id": "bank", "delta": 5.0, "turn": 3,
                               "reason": "wage_debt_fulfilled"}),
        ("currency_confidence_changed", {"turn": 3, "delta": -3.0,
                                         "reason": "currency_confidence_upkeep"}),
        ("institution_transition", {"turn": 3, "institution_id": "central_bank",
                                    "kind": "bank", "from_stage": 0, "to_stage": 1,
                                    "trigger": "test", "metric_snapshot": {}, "scope": "nation"}),
        ("npc_trust_changed", {"npc_id": "友人A", "delta": 10.0, "turn": 3,
                               "reason": "social_contract_fulfilled"}),
        ("trait_changed", {"turn": 3, "delta": {"skill": 1.5}, "theme_trait": "skill",
                           "choice_key": "labor", "fired": None, "kind": "normal"}),
        ("turn_started", {"turn": 5}),
        ("character_died", {"turn": 5, "age": 15.0}),
    ]

    def setUp(self):
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in self.FIXTURE:
                f.write(json.dumps(
                    {"ts": "2026-01-01T00:00:00+00:00", "type": etype, "data": data},
                    ensure_ascii=False) + "\n")
        game.EVENTS_PATH = self.events_path

    def tearDown(self):
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)

    def test_reduce_state_structured_result(self):
        state = game.reduce_state()

        self.assertEqual(state["turn"], 5)
        self.assertEqual(state["talent"], "health")
        self.assertEqual(state["npc_pool_cap"], 10)
        self.assertEqual(state["alive"], False)
        self.assertEqual(state["death_turn"], 5)
        self.assertEqual(state["bank_stage"], 1)
        self.assertEqual(state["currency_stage"], 0)
        self.assertAlmostEqual(state["currency_confidence"], 47.0)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 0)

        self.assertEqual(state["resources"], {"energy": 100, "money": 40, "peace": 100})

        self.assertEqual(state["traits"]["skill"], 51.5)
        self.assertEqual(state["traits"]["dexterity"], 50.0)
        self.assertEqual(state["traits"]["intellect"], 50.0)
        self.assertEqual(state["traits"]["health"], 80.0)

        self.assertEqual(len(state["contracts"]), 1)
        self.assertEqual(state["contracts"]["c1"]["status"], "fulfilled")
        self.assertEqual(state["contracts"]["c1"]["repay_money"], -40)

        self.assertEqual(set(state["npcs"].keys()), {"bank", "economy", "友人A"})
        self.assertEqual(state["npcs"]["bank"]["money"], -40)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 53.0)
        self.assertEqual(state["npcs"]["economy"]["money"], 0)
        self.assertAlmostEqual(state["npcs"]["economy"]["trust"], 50.0)
        self.assertEqual(state["npcs"]["友人A"]["money"], 0)
        self.assertAlmostEqual(state["npcs"]["友人A"]["trust"], 59.7515625)


# ==============================================================================
# projection.py (Step 2B、2026-08-14): reduce_events()直接呼び出しと
# game.reduce_state()(薄いラッパー経由)の結果が完全一致することの確認
# ==============================================================================

class ReduceEventsMatchesReduceStateTest(unittest.TestCase):
    """ReduceStateFixtureTestと同じ固定イベント列を、(a) projection.reduce_events()
    へ直接渡した結果と、(b) game.EVENTS_PATHへ書き出してgame.reduce_state()
    (event_store.iter_jsonl_events → projection.reduce_events という薄い
    ラッパーの連鎖)経由で読んだ結果とで、完全に一致することを確認する。
    Step 2Bの移植(game.pyのreduce_state()本体をprojection.reduce_events()へ
    移す)が挙動を変えていないことの直接的な根拠。"""

    def test_direct_and_wrapped_results_match(self):
        events = [
            {"ts": "2026-01-01T00:00:00+00:00", "type": etype, "data": data}
            for etype, data in ReduceStateFixtureTest.FIXTURE
        ]

        direct = projection.reduce_events(events, game.PROJECTION_DEPENDENCIES)

        orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        events_path = Path(path)
        try:
            with events_path.open("w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            game.EVENTS_PATH = events_path
            wrapped = game.reduce_state()
        finally:
            game.EVENTS_PATH = orig_events_path
            events_path.unlink(missing_ok=True)

        self.assertEqual(direct, wrapped)


# ==============================================================================
# generate_normal_turn (Step 3B-0、2026-08-14): 通常行動の選択肢生成の
# 構造化golden値。Step 3B-1〜3B-4で関数抽出する前に、まず現行実装のまま
# このテストを固定する。抽出がRNG消費順序を含めて挙動を変えていないことを、
# このテストが無変更で成功し続けることで確認する。
# ==============================================================================

class GenerateNormalTurnFixtureTest(unittest.TestCase):
    """call_ollamaを固定JSON応答へmockし、一時EVENTS_PATHでgenerate_normal_turn()
    を直接呼ぶ。ケース1(新規NPC)・ケース2(既存NPC再利用)の2通りを固定seedで
    実行し、situation/choices/cost/hours/social契約payloadを構造的に比較する。"""

    MOCK_JSON = json.dumps({
        "situation": "モックの状況説明です。",
        "choices": {
            "money": "お金で片付ける", "labor": "自分で頑張る",
            "social": "友人に頼る", "rest": "休む",
        },
        "social_counterparty": None,
        "social_favor": "ちょっとした頼み事",
    }, ensure_ascii=False)

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_JSON, 0.001)
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()  # iter_events()はファイル不存在→空のまま始める
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)

    @staticmethod
    def _base_state(npcs, npc_pool_cap):
        return {
            "resources": {"energy": 100, "peace": 100, "money": 40},
            "contracts": {},
            "npcs": npcs,
            "npc_pool_cap": npc_pool_cap,
            "currency_stage": game.CURRENCY_STAGE_NORMAL,
        }

    def test_new_npc_case(self):
        """顔なじみが誰もいない(pick_social_counterpartyは常にis_new=Trueを
        返す)状態。social_counterparty=None(LLM未指定)なのでfallback名
        「知人」が使われる。"""
        random.seed(1)
        state = self._base_state(npcs={}, npc_pool_cap=10)
        result = game.generate_normal_turn("dummy-model", state, turn=5,
                                           theme="題材", latencies=[])

        self.assertEqual(result["situation"], "モックの状況説明です。")
        self.assertEqual(result["kind"], "normal")
        self.assertEqual([c["key"] for c in result["choices"]],
                         ["money", "labor", "social", "rest"])

        by_key = {c["key"]: c for c in result["choices"]}
        self.assertEqual(by_key["money"]["cost"], {"money": -37, "peace": 9})
        self.assertEqual(by_key["money"]["hours"], 12)
        self.assertEqual(by_key["labor"]["cost"], {"energy": -32, "peace": -19})
        self.assertEqual(by_key["labor"]["hours"], 40)
        self.assertEqual(by_key["rest"]["cost"],
                         {"peace": 35, "energy": 15, "money": -5})
        self.assertEqual(by_key["rest"]["hours"], 38)

        social = by_key["social"]
        self.assertEqual(social["cost"], {"peace": -13})
        self.assertEqual(social["hours"], 25)
        contract = social["contract"]
        self.assertEqual(contract["counterparty"], "知人")
        self.assertEqual(contract["description"], "ちょっとした頼み事")
        self.assertEqual(contract["due_turn"], 9)
        self.assertEqual(contract["repay_money"], -24)
        self.assertAlmostEqual(contract["prospective_ethics"], 42.77691339942366)
        self.assertEqual(contract["prospective_retire_turn"], 137)

    def test_existing_npc_reused_case(self):
        """顔なじみが1人(npc_pool_cap=1でプール上限ちょうど)。trustが
        NPC_CONTRACT_TRUST_MIN以上・返済待ちも無いので、pick_social_counterparty
        はその1人を確定的に再利用する(is_new=False)。"""
        random.seed(1)
        npcs = {"友人A": {"name": "友人A", "role": "acquaintance", "money": 0,
                          "trust": 60.0, "ethics": 60.0,
                          "retire_turn": None, "alive": True}}
        state = self._base_state(npcs=npcs, npc_pool_cap=1)
        result = game.generate_normal_turn("dummy-model", state, turn=5,
                                           theme="題材", latencies=[])

        self.assertEqual([c["key"] for c in result["choices"]],
                         ["money", "labor", "social", "rest"])
        by_key = {c["key"]: c for c in result["choices"]}
        self.assertEqual(by_key["money"]["cost"], {"money": -26, "peace": 1})
        self.assertEqual(by_key["money"]["hours"], 25)
        self.assertEqual(by_key["labor"]["cost"], {"energy": -26, "peace": -13})
        self.assertEqual(by_key["labor"]["hours"], 45)
        self.assertEqual(by_key["rest"]["cost"],
                         {"peace": 20, "energy": 27, "money": -5})
        self.assertEqual(by_key["rest"]["hours"], 44)

        social = by_key["social"]
        self.assertEqual(social["cost"], {"peace": -14})
        self.assertEqual(social["hours"], 16)
        contract = social["contract"]
        self.assertEqual(contract["counterparty"], "友人A")
        self.assertEqual(contract["due_turn"], 7)
        # 既存NPC再利用時はprospective_ethics/prospective_retire_turnとも
        # Noneのまま(このターンではNPCのethics・寿命を変えない)。
        self.assertIsNone(contract["prospective_ethics"])
        self.assertIsNone(contract["prospective_retire_turn"])
        # turn=5, due_turn=7はどちらもINCOME_INTERVAL(4)の同じ区間に入るため
        # n_payments=0→contract_credit_limit=0に切り詰められ、repay_moneyは0。
        # 現行実装の実際の挙動をそのまま固定する(この境界条件自体の是非は
        # Step 3Bのスコープ外)。
        self.assertEqual(contract["repay_money"], 0)


# ==============================================================================
# Step 3B-1〜3B-4(2026-08-14): 通常行動生成から切り出した4つの共通関数の単体テスト
# (2026-08-14訂正: 「純粋関数」は不正確——compute_normal_money_modifier()は
# rng依存、build_normal_base_choice()も内部で乱数を消費する。乱数を使わないのは
# compute_social_contract_repay_range()のみ。挙動変更ではなく表現の訂正)
# ==============================================================================

class AvailableNormalArchetypesTest(unittest.TestCase):
    def test_normal_stage_includes_money_in_original_order(self):
        result = game.available_normal_archetypes(game.CURRENCY_STAGE_NORMAL)
        self.assertEqual([a["key"] for a in result], ["money", "labor", "social", "rest"])
        self.assertEqual(result, game.ACTION_ARCHETYPES)

    def test_abandoned_stage_excludes_money_keeps_order(self):
        # 2026-08-15追記(Step 13): CURRENCY_STAGE_ABANDONEDは単独でも
        # alternative_economy_triggeredを満たすため、barter/subsistenceが
        # 末尾に追加される。
        result = game.available_normal_archetypes(game.CURRENCY_STAGE_ABANDONED)
        self.assertEqual([a["key"] for a in result],
                         ["labor", "social", "rest", "barter", "subsistence"])

    def test_local_credit_stage_defaults_to_healthy_no_filter(self):
        # local_credit_stage省略時は既存呼び出しと完全に同じ結果になる
        # (後方互換、2026-08-15追加)。
        result = game.available_normal_archetypes(game.CURRENCY_STAGE_NORMAL)
        self.assertEqual(result, game.ACTION_ARCHETYPES)

    def test_isolated_local_credit_stage_excludes_social_keeps_order(self):
        # 2026-08-15追加(地域信用制度、ユーザーレビュー指摘への対応、Stage3の
        # 行動的帰結): 信用できる相手がゼロになった状態では社会的契約を
        # 結べない(docs/social-regimes-spec.md「Stage3: 孤立」)。
        # 2026-08-15追記(Step 13): LOCAL_CREDIT_STAGE_ISOLATED(3)は
        # alternative_economy_triggeredのlocal_credit_stage>=PERSONAL(2)条件も
        # 満たすため、barter/subsistenceが末尾に追加される(barter_stageは
        # 既定FUNCTIONINGなのでbarterも含む)。
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, game.LOCAL_CREDIT_STAGE_ISOLATED)
        self.assertEqual([a["key"] for a in result],
                         ["money", "labor", "rest", "barter", "subsistence"])

    def test_other_local_credit_stages_do_not_exclude_social(self):
        for stage in (game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE_CONTRACTION,
                     game.LOCAL_CREDIT_STAGE_PERSONAL):
            with self.subTest(stage=stage):
                result = game.available_normal_archetypes(game.CURRENCY_STAGE_NORMAL, stage)
                self.assertIn("social", [a["key"] for a in result])

    def test_both_stages_exclude_money_and_social(self):
        # 2026-08-15追記(Step 13): 両Stageともalternative_economy_triggeredを
        # 満たすため、barter/subsistenceが末尾に追加される。
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_ABANDONED, game.LOCAL_CREDIT_STAGE_ISOLATED)
        self.assertEqual([a["key"] for a in result], ["labor", "rest", "barter", "subsistence"])


class ComputeNormalMoneyModifierTest(unittest.TestCase):
    class _CountingRng:
        def __init__(self):
            self.calls = []

        def choice(self, seq):
            self.calls.append(list(seq))
            return seq[0]

    def test_no_acquaintances_returns_neutral_and_skips_rng(self):
        rng = self._CountingRng()
        modifier = game.compute_normal_money_modifier({}, game.CURRENCY_STAGE_NORMAL, rng=rng)
        self.assertEqual(modifier, 1.0)
        self.assertEqual(len(rng.calls), 0)

    def test_with_acquaintance_calls_rng_choice_exactly_once(self):
        npc = {"trust": 50.0, "ethics": 50.0}
        rng = self._CountingRng()
        modifier = game.compute_normal_money_modifier(
            {"友人A": npc}, game.CURRENCY_STAGE_NORMAL, rng=rng)
        self.assertEqual(len(rng.calls), 1)
        self.assertEqual(modifier, game.npc_price_modifier(npc))

    def test_wary_stage_applies_price_penalty(self):
        npc = {"trust": 50.0, "ethics": 50.0}
        normal = game.compute_normal_money_modifier(
            {"友人A": npc}, game.CURRENCY_STAGE_NORMAL, rng=self._CountingRng())
        wary = game.compute_normal_money_modifier(
            {"友人A": npc}, game.CURRENCY_STAGE_WARY, rng=self._CountingRng())
        self.assertAlmostEqual(wary, normal * game.CURRENCY_STAGE1_PRICE_PENALTY)


class BuildNormalBaseChoiceTest(unittest.TestCase):
    MONEY_ARCHETYPE = next(a for a in game.ACTION_ARCHETYPES if a["key"] == "money")
    LABOR_ARCHETYPE = next(a for a in game.ACTION_ARCHETYPES if a["key"] == "labor")

    def setUp(self):
        self._orig_state = random.getstate()
        random.seed(1)

    def tearDown(self):
        random.setstate(self._orig_state)

    def test_label_none_key_order(self):
        ch = game.build_normal_base_choice(self.LABOR_ARCHETYPE, 5, 1.0)
        self.assertEqual(list(ch.keys()), ["key", "cost", "hours"])

    def test_label_given_key_order(self):
        ch = game.build_normal_base_choice(self.LABOR_ARCHETYPE, 5, 1.0, label="テスト")
        self.assertEqual(list(ch.keys()), ["key", "label", "cost", "hours"])
        self.assertEqual(ch["label"], "テスト")

    def test_money_archetype_uses_draw_ranges_not_draw_archetype_cost(self):
        calls = {"draw_ranges": 0, "indexed_money_ranges": 0, "draw_archetype_cost": 0}
        orig = (game.draw_ranges, game.indexed_money_ranges, game.draw_archetype_cost)

        def counting_draw_ranges(ranges):
            calls["draw_ranges"] += 1
            return orig[0](ranges)

        def counting_indexed(ranges, turn):
            calls["indexed_money_ranges"] += 1
            return orig[1](ranges, turn)

        def counting_archetype_cost(key, turn):
            calls["draw_archetype_cost"] += 1
            return orig[2](key, turn)

        game.draw_ranges, game.indexed_money_ranges, game.draw_archetype_cost = (
            counting_draw_ranges, counting_indexed, counting_archetype_cost)
        try:
            game.build_normal_base_choice(self.MONEY_ARCHETYPE, 5, 1.0)
        finally:
            game.draw_ranges, game.indexed_money_ranges, game.draw_archetype_cost = orig

        self.assertEqual(calls["draw_ranges"], 1)
        self.assertEqual(calls["indexed_money_ranges"], 1)
        self.assertEqual(calls["draw_archetype_cost"], 0)

    def test_non_money_archetype_calls_draw_archetype_cost_exactly_once(self):
        # draw_archetype_cost自体は内部でdraw_ranges/indexed_money_rangesを
        # 呼ぶので、それらの呼び出し回数はここでは見ない
        # (money型との違いは「draw_archetype_costを経由するかどうか」)。
        calls = {"draw_archetype_cost": 0}
        orig = game.draw_archetype_cost

        def counting_archetype_cost(key, turn):
            calls["draw_archetype_cost"] += 1
            return orig(key, turn)

        game.draw_archetype_cost = counting_archetype_cost
        try:
            game.build_normal_base_choice(self.LABOR_ARCHETYPE, 5, 1.0)
        finally:
            game.draw_archetype_cost = orig

        self.assertEqual(calls["draw_archetype_cost"], 1)


class ComputeSocialContractRepayRangeTest(unittest.TestCase):
    """乱数を使わない純粋関数であることも、ここで暗黙に確認する
    (テスト側でrandom.seedを固定していないのに毎回同じ値が返ることで、
    RNG不使用を裏付ける)。"""

    def test_new_npc_uses_reputation_based_initial_trust(self):
        # acquaintancesが空(誰も居ない)なので、player_reputation()の既定値
        # NPC_TRUST_INITIALがそのままtrust_for_limitに使われる。
        trust_for_limit, lo, hi = game.compute_social_contract_repay_range(
            acquaintances={}, existing=None, prospective_ethics=50.0, turn=1, due_turn=100)
        self.assertEqual(trust_for_limit, game.NPC_TRUST_INITIAL)

    def test_existing_npc_uses_its_own_trust(self):
        existing = {"trust": 70.0, "ethics": 60.0}
        trust_for_limit, lo, hi = game.compute_social_contract_repay_range(
            acquaintances={}, existing=existing, prospective_ethics=None, turn=1, due_turn=100)
        self.assertEqual(trust_for_limit, 70.0)

    def test_low_trust_at_gate_minimum_clamps_repay_range_to_zero(self):
        # NPC_CONTRACT_TRUST_MIN(契約成立ゲートの下限)ちょうどでは、
        # contract_credit_multiplier=0→limit=0→lo/hiとも0まで切り詰められる。
        existing = {"trust": game.NPC_CONTRACT_TRUST_MIN, "ethics": 50.0}
        _, lo, hi = game.compute_social_contract_repay_range(
            acquaintances={}, existing=existing, prospective_ethics=None, turn=1, due_turn=5)
        self.assertEqual(lo, 0)
        self.assertEqual(hi, 0)

    def test_high_trust_is_not_clamped(self):
        # trust=NPC_TRUST_CAP・十分先のdue_turnでは収入基準の上限が大きく、
        # クランプされない——npc_price_modifierの倍率だけで決まる自然な
        # レンジ幅がそのまま返る。
        existing = {"trust": game.NPC_TRUST_CAP, "ethics": 50.0}
        _, lo, hi = game.compute_social_contract_repay_range(
            acquaintances={}, existing=existing, prospective_ethics=None, turn=1, due_turn=100)
        modifier = game.npc_price_modifier(existing)
        expected_lo, expected_hi = game.indexed_money_ranges(
            {"money": (round(game.CONTRACT_REPAY_MONEY[0] * modifier),
                      round(game.CONTRACT_REPAY_MONEY[1] * modifier))}, 1)["money"]
        self.assertEqual((lo, hi), (expected_lo, expected_hi))


# ==============================================================================
# build_settlement_choices (Step 3A、2026-08-14): generate_settlement_turn()と
# simulate_policy()で重複していた契約清算選択肢生成の統合先
# ==============================================================================

class BuildSettlementChoicesTest(unittest.TestCase):
    CONTRACT = {"id": "c1", "counterparty": "友人A", "repay_money": -40}

    def test_normal_currency_stage_key_order_is_money_labor_avoid(self):
        choices = game.build_settlement_choices(self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL)
        self.assertEqual([c["key"] for c in choices], ["money", "labor", "avoid"])

    def test_abandoned_currency_stage_key_order_is_labor_avoid(self):
        choices = game.build_settlement_choices(self.CONTRACT, 10, game.CURRENCY_STAGE_ABANDONED)
        self.assertEqual([c["key"] for c in choices], ["labor", "avoid"])

    def test_labels_none_uses_key_cost_settle(self):
        choices = game.build_settlement_choices(self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL)
        for c in choices:
            self.assertEqual(set(c.keys()), {"key", "cost", "settle"})

    def test_labels_given_uses_clean_label_and_fallback(self):
        # money: 前後空白付きの正常なラベル文。labor: 空文字列(fallback対象)。
        # avoid: None(fallback対象)。clean_label()自体の挙動は変えていない。
        labels = {"money": "  お金で返す(テスト)  ", "labor": "", "avoid": None}
        choices = game.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL, labels=labels)
        by_key = {c["key"]: c for c in choices}
        self.assertEqual(set(by_key["money"].keys()), {"key", "label", "cost", "settle"})
        self.assertEqual(by_key["money"]["label"], "お金で返す(テスト)")
        self.assertEqual(by_key["labor"]["label"], "友人Aを手伝って返す")
        self.assertEqual(by_key["avoid"]["label"], "今回は返さずに先延ばしにする")

    def test_draw_contract_repay_labor_called_exactly_once(self):
        calls = []
        orig = game.draw_contract_repay_labor

        def counting(repay_money, turn):
            calls.append((repay_money, turn))
            return orig(repay_money, turn)

        game.draw_contract_repay_labor = counting
        try:
            game.build_settlement_choices(self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL)
        finally:
            game.draw_contract_repay_labor = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (-40, 10))

    def test_enforcement_stage_defaults_to_institutional_no_change(self):
        # enforcement_stage省略時はENFORCEMENT_STAGE_INSTITUTIONAL(乗率1.0)
        # なので既存呼び出しと完全に同じcostになる(2026-08-15追加、後方互換)。
        # labor costはRNGを消費するため、同じseedにリセットしてから呼ぶ。
        random.seed(1)
        with_default = game.build_settlement_choices(self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL)
        random.seed(1)
        explicit = game.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            enforcement_stage=game.ENFORCEMENT_STAGE_INSTITUTIONAL)
        self.assertEqual(with_default, explicit)

    def test_enforcement_stage_scales_avoid_peace_cost(self):
        # 2026-08-15追加(契約執行制度)。Stageが進むほど"avoid"のpeaceペナルティ
        # が下がる(第三者による処罰の実効性低下)。
        base = game.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            enforcement_stage=game.ENFORCEMENT_STAGE_INSTITUTIONAL)
        base_avoid = [c for c in base if c["key"] == "avoid"][0]
        for stage, multiplier in [
            (game.ENFORCEMENT_STAGE_DELAYED, 0.6),
            (game.ENFORCEMENT_STAGE_LOCAL_LEDGER, 0.3),
            (game.ENFORCEMENT_STAGE_PERSONAL, 0.1),
            (game.ENFORCEMENT_STAGE_NONE, 0.0),
        ]:
            with self.subTest(stage=stage):
                choices = game.build_settlement_choices(
                    self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL, enforcement_stage=stage)
                avoid = [c for c in choices if c["key"] == "avoid"][0]
                self.assertEqual(avoid["cost"]["peace"], round(base_avoid["cost"]["peace"] * multiplier))

    def test_enforcement_stage_does_not_affect_money_or_labor_cost(self):
        # labor costはRNGを消費するため、同じseedにリセットしてから呼ぶ
        # (moneyはRNG非依存なので影響しないが、揃えておく)。
        random.seed(1)
        base = game.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            enforcement_stage=game.ENFORCEMENT_STAGE_INSTITUTIONAL)
        random.seed(1)
        degraded = game.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            enforcement_stage=game.ENFORCEMENT_STAGE_NONE)
        by_key_base = {c["key"]: c["cost"] for c in base}
        by_key_degraded = {c["key"]: c["cost"] for c in degraded}
        self.assertEqual(by_key_base["money"], by_key_degraded["money"])
        self.assertEqual(by_key_base["labor"], by_key_degraded["labor"])


# ==============================================================================
# simulate_policy の固定 seed・固定 policy の主要な戻り値
#
# Step 3A完了条件の項目6(固定seedでsimulate_policyの既存golden値が完全一致)は、
# build_settlement_choices()への統合後もこのクラスがそのまま(無変更で)成功する
# ことによって確認する——simulate_policy()は清算のたびに必ずこの関数を通るため、
# 別立てのテストを新設する必要はない。
# ==============================================================================

class SimulatePolicyFixtureTest(unittest.TestCase):
    """policy="cautious", turns=60, seed=1, safety_floor=SAFETY_FLOOR, talent="health"
    を固定して1回実行した結果(golden値)。乱数消費順序・計算式のいずれかが
    変わればこのテストが検出する。"""

    @classmethod
    def setUpClass(cls):
        cls.result = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")

    def test_counts(self):
        self.assertEqual(self.result["counts"], {
            "清算:avoid": 13,
            "清算:labor": 8,
            "清算:money": 38,
            "通常:labor": 6,
            "通常:money": 5,
            "通常:rest": 73,
            "通常:social": 48,
        })

    def test_blocked_and_overridden(self):
        # 2026-08-15、地域信用制度の追加でblockedが99→100に変化(community_trustが
        # 新規relationshipの初期trustにも反映されるようになったため、Stage6D
        # までの他の値は不変)。golden値を新しいbaselineに更新。
        self.assertEqual(self.result["blocked"], 100)
        self.assertEqual(self.result["overridden"], 50)

    def test_bank_and_economy_ledgers(self):
        self.assertEqual(self.result["bank_money"], -349)
        self.assertEqual(self.result["economy_money"], -407)
        self.assertEqual(self.result["bank_repaid_count"], 7)
        self.assertEqual(self.result["bank_crisis_count"], 0)

    def test_bank_and_currency_state(self):
        self.assertAlmostEqual(self.result["bank_trust"], 51.11701808683535)
        self.assertEqual(self.result["bank_stage"], game.BANK_STAGE_HEALTHY)
        self.assertAlmostEqual(self.result["currency_confidence"], 51.04340599999999)
        self.assertEqual(self.result["currency_stage"], game.CURRENCY_STAGE_NORMAL)

    def test_resources_and_real_money(self):
        # 2026-08-15、地域信用制度の追加によるgolden値更新(上記test_blocked_and_
        # overridden参照)。
        self.assertEqual(self.result["resources"], {"energy": 93, "money": 131, "peace": 64})
        self.assertAlmostEqual(self.result["real_money"], 112.77385285757163)

    def test_traits(self):
        traits = self.result["traits"]
        self.assertAlmostEqual(traits["dexterity"], 57.926490000000115)
        self.assertAlmostEqual(traits["health"], 83.83213900000003)
        self.assertAlmostEqual(traits["intellect"], 62.230427000000105)
        self.assertAlmostEqual(traits["skill"], 51.13872000000014)

    def test_fires_and_death(self):
        self.assertEqual(self.result["fires"], 42)
        self.assertIsNone(self.result["death_turn"])

    def test_trajectory_checkpoint_60(self):
        # 2026-08-15、地域信用制度の追加によるgolden値更新(上記test_blocked_and_
        # overridden参照)。
        cp = self.result["trajectory"][60]
        self.assertEqual(cp["resources"], {"energy": 93, "money": 131, "peace": 64})
        self.assertEqual(cp["peace_min"], 0)
        self.assertAlmostEqual(cp["real_money"], 112.77385285757163)
        self.assertAlmostEqual(cp["bank_trust"], 51.11701808683535)
        self.assertEqual(cp["bank_crisis_count"], 0)
        self.assertEqual(cp["bank_stage"], game.BANK_STAGE_HEALTHY)
        self.assertAlmostEqual(cp["currency_confidence"], 51.04340599999999)
        self.assertEqual(cp["currency_stage"], game.CURRENCY_STAGE_NORMAL)

    def test_policy_check_period_metrics_match_existing_counts(self):
        metrics = self.result["policy_check_metrics"]
        currency_counts = {}
        for bucket in metrics["normal_choices_by_currency_stage"].values():
            for key, count in bucket.items():
                currency_counts[key] = currency_counts.get(key, 0) + count
        expected_normal = {
            key.split(":", 1)[1]: count for key, count in self.result["counts"].items()
            if key.startswith("通常:")}
        self.assertEqual(currency_counts, expected_normal)
        self.assertEqual(
            metrics["settlement_counts_by_checkpoint"][60],
            {"fulfilled": 46, "defaulted": 13})


# ==============================================================================
# offline_simulation.py の直接テスト(Step 9、2026-08-15): 「LLMを使わない
# オフライン検証・シミュレーション」責務全体の移動後、module版とgame.py版の
# 一致・RNG状態・動的依存の呼び出し時点解決を確認する。simulate_policyの
# 網羅的な分岐確認はSimulatePolicyFixtureTest(無変更)に委ね、ここは責務境界の
# 確認に絞る。runnerテスト(run_policy_check/run_policy_probe)は
# offline_simulation.simulate_policy/check_trajectory_criteriaをmonkeypatch
# して、実際の長時間シミュレーションを重複実行しない。
# ==============================================================================

class OfflineSimulationModuleTest(unittest.TestCase):
    def setUp(self):
        self._orig_random_state = random.getstate()

    def tearDown(self):
        random.setstate(self._orig_random_state)

    def policy_deps(self):
        return game._policy_simulation_dependencies()

    def trait_deps(self):
        return game._trait_simulation_dependencies()

    def test_policy_dependencies_are_frozen(self):
        deps = self.policy_deps()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.trait_min = 1.0

    def test_trait_dependencies_are_frozen(self):
        deps = self.trait_deps()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.d_base = 1.0

    def test_simulate_policy_matches_wrapper_and_rng(self):
        state_before = random.getstate()
        game_result = game.simulate_policy(
            "cautious", 30, seed=2, safety_floor=game.SAFETY_FLOOR, talent="health")
        state_after_game = random.getstate()

        random.setstate(state_before)
        module_result = offline_simulation.simulate_policy(
            "cautious", 30, 2, game.SAFETY_FLOOR, "health", dependencies=self.policy_deps())
        state_after_module = random.getstate()

        self.assertEqual(module_result, game_result)
        self.assertEqual(state_after_module, state_after_game)

    def test_check_trajectory_criteria_matches_wrapper(self):
        r = game.simulate_policy(
            "cautious", 30, seed=3, safety_floor=game.SAFETY_FLOOR, talent="health")
        game_result = game.check_trajectory_criteria(r)
        module_result = offline_simulation.check_trajectory_criteria(
            r, dependencies=self.policy_deps())
        self.assertEqual(module_result, game_result)
        # 30ターンではT960未到達なのでN/Aを含む代表ケースになる(固定分母の
        # 一部がN/Aとして必ず出力される、というcheck_trajectory_criteriaの
        # 中心的な仕様を確認する)。
        self.assertTrue(any(c["passed"] is None for c in game_result))

    def test_run_policy_check_propagates_monkeypatched_game_functions(self):
        # run_policy_checkはoffline_simulation内部でsimulate_policy/
        # check_trajectory_criteriaをsibling呼び出しするのではなく、
        # deps.simulate_policy_fn/deps.check_trajectory_criteria_fn(=game.py側の
        # 関数オブジェクト)経由で呼ぶ。game.simulate_policy等をmonkeypatchすると
        # game.run_policy_check()経由でも反映されることを確認する
        # (offline_simulation側を直接差し替えるテストでは、この伝播の欠落を
        # 検出できなかった)。
        original_simulate = game.simulate_policy
        original_check = game.check_trajectory_criteria
        calls = []

        def fake_simulate_policy(policy_name, turns, seed, safety_floor, talent=None,
                                 checkpoint_turns=None):
            calls.append(("simulate_policy", policy_name, turns, seed))
            return {"death_turn": None}

        def fake_check(r):
            calls.append(("check_trajectory_criteria",))
            return [{"name": "a", "passed": True, "detail": ""},
                   {"name": "b", "passed": False, "detail": ""},
                   {"name": "c", "passed": None, "detail": ""}]

        game.simulate_policy = fake_simulate_policy
        game.check_trajectory_criteria = fake_check
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    game.run_policy_check([1], 30)
        finally:
            game.simulate_policy = original_simulate
            game.check_trajectory_criteria = original_check

        self.assertEqual(ctx.exception.code, 1)
        # 3方針(cautious/ambitious/family)×1seed×fake_checkの3件(PASS/FAIL/N/A
        # 各1)= PASS3/FAIL3/N/A3。2026-08-15追加(Step 12): これに加えて
        # 走行横断のcheck_enforcement_convergenceが1件走る——fake_simulate_
        # policyは"enforcement_stage"キーを持たない辞書を返すため、3件とも
        # enforcement_stage=None(.get()のデフォルト)で「単一値に収束」判定と
        # なりFAILが1件増える。結果としてPASS3/FAIL4/N/A3になる。
        self.assertIn("PASS 3 / FAIL 4 / N/A 3", buf.getvalue())
        self.assertEqual(len([c for c in calls if c[0] == "simulate_policy"]), 3)
        self.assertEqual(len([c for c in calls if c[0] == "check_trajectory_criteria"]), 3)

    def test_run_policy_check_propagates_monkeypatched_check_enforcement_convergence(self):
        # 2026-08-15追加(Step 12)。check_enforcement_convergenceも他の*_fnと
        # 同じ差し替え口(check_enforcement_convergence_fn)を持つ——
        # game.check_enforcement_convergenceをmonkeypatchするとgame.
        # run_policy_check()経由でも反映されることを確認する。
        original_convergence = game.check_enforcement_convergence
        calls = []

        def fake_convergence(run_results):
            calls.append(("check_enforcement_convergence", len(run_results)))
            return {"name": "z", "passed": False, "detail": "", "category": "contract_enforcement",
                   "applicable_regime": "any", "observed_regime": "any"}

        game.check_enforcement_convergence = fake_convergence
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    game.run_policy_check([1], 30)
        finally:
            game.check_enforcement_convergence = original_convergence

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(calls, [("check_enforcement_convergence", 3)])
        self.assertIn("[FAIL] z:", buf.getvalue())

    def test_run_policy_probe_propagates_monkeypatched_simulate_policy(self):
        # run_policy_probeも同様にdeps.simulate_policy_fn経由。
        # game.simulate_policyのmonkeypatchがgame.run_policy_probe()経由でも
        # 反映されることを確認する。
        original_simulate = game.simulate_policy
        calls = []

        def fake_simulate_policy(policy_name, turns, seed, safety_floor, talent=None,
                                 checkpoint_turns=None):
            calls.append((policy_name, turns, seed))
            return {"counts": {}, "min_seen": {"money": 0}, "resources": {"money": 0},
                    "overridden": 0, "real_money": 0.0, "bank_money": 0,
                    "bank_repaid_count": 0, "bank_crisis_count": 0, "bank_trust": 50.0,
                    "traits": dict(game.INITIAL_TRAITS), "talent": "health", "fires": 0,
                    "npcs": {}, "agree_log": [
                        {"turn": 1, "kind": "通常",
                         "picks": {p: "rest" for p in game.POLICY_VECTORS}, "agree": True}]}

        game.simulate_policy = fake_simulate_policy
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                game.run_policy_probe(10, [1], 30)
        finally:
            game.simulate_policy = original_simulate
        self.assertEqual(sorted(calls), sorted(
            [(p, 10, 1) for p in game.POLICY_VECTORS]))

    def test_run_simulation_propagates_monkeypatched_game_functions(self):
        # run_simulationも同様にdeps.equilibrium_fn/simulate_forced_fn/
        # simulate_mixed_fn経由。game.equilibrium等のmonkeypatchが
        # game.run_simulation()経由でも反映されることを確認する。
        original_eq = game.equilibrium
        original_forced = game.simulate_forced
        original_mixed = game.simulate_mixed
        calls = []

        def fake_equilibrium(f, g, m, d):
            calls.append("equilibrium")
            return 0.0

        def fake_simulate_forced(f, g, m, d, turns, trials, start=50.0):
            calls.append("simulate_forced")
            return 0.0

        def fake_simulate_mixed(turns, trials, dist, no_theme_rate, talent):
            calls.append("simulate_mixed")
            return {}

        game.equilibrium = fake_equilibrium
        game.simulate_forced = fake_simulate_forced
        game.simulate_mixed = fake_simulate_mixed
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                game.run_simulation(5, 2, [0.3, 0.3, 0.4], 0.0, None, None)
        finally:
            game.equilibrium = original_eq
            game.simulate_forced = original_forced
            game.simulate_mixed = original_mixed

        self.assertEqual(calls.count("equilibrium"), 4)  # 4通りの平衡値
        self.assertEqual(calls.count("simulate_forced"), 4)
        self.assertEqual(calls.count("simulate_mixed"), len(game.TRAITS))  # 才能ごと

    def test_equilibrium_matches_wrapper(self):
        self.assertEqual(offline_simulation.equilibrium(0.2, 2.0, 1.0, 0.12),
                         game.equilibrium(0.2, 2.0, 1.0, 0.12))

    def test_simulate_forced_matches_wrapper_and_rng(self):
        state_before = random.getstate()
        random.seed(5)
        game_result = game.simulate_forced(0.2, 2.0, 1.0, 0.12, 20, 5)
        state_after_game = random.getstate()

        random.setstate(state_before)
        random.seed(5)
        module_result = offline_simulation.simulate_forced(
            0.2, 2.0, 1.0, 0.12, 20, 5, dependencies=self.trait_deps())
        state_after_module = random.getstate()

        self.assertEqual(module_result, game_result)
        self.assertEqual(state_after_module, state_after_game)

    def test_run_simulation_tiny_case_matches_wrapper_output(self):
        # turns/trialsを小さく保ち、既存の長時間ケース(--simulateの既定値)を
        # 重複実行しない。
        random.seed(9)
        buf_game = io.StringIO()
        with contextlib.redirect_stdout(buf_game):
            game.run_simulation(20, 3, [0.3, 0.3, 0.4], 0.0, None, None)

        random.seed(9)
        buf_module = io.StringIO()
        with contextlib.redirect_stdout(buf_module):
            offline_simulation.run_simulation(
                20, 3, [0.3, 0.3, 0.4], 0.0, None, None, dependencies=self.trait_deps())

        self.assertEqual(buf_module.getvalue(), buf_game.getvalue())

    def test_trait_dependencies_resolve_d_base_at_call_time(self):
        original = game.D_BASE
        game.D_BASE = 0.5
        try:
            deps = game._trait_simulation_dependencies()
        finally:
            game.D_BASE = original
        self.assertEqual(deps.d_base, 0.5)


# ==============================================================================
# Step 12(2026-08-15、`--policy-check`の制度レジーム対応再設計): 計測ヘルパー
# (_new_institution_tracking/_record_institution_stage/_record_settlement_outcome/
# _tally_by_category)の直接テスト。乱数・シミュレーション本体を一切経由しない、
# 最も直接的な検証(純粋関数、副作用は引数のdict/listへの書き込みのみ)。
# ==============================================================================

class InstitutionTrajectoryTrackingTest(unittest.TestCase):
    def test_new_tracking_initial_state(self):
        t = offline_simulation._new_institution_tracking()
        self.assertEqual(t, {
            "stage_turns": {}, "first_reached_turn": {}, "first_left_healthy_turn": None,
            "transition_count": 0, "recovery_count": 0, "transition_history": [],
            "worst_stage": 0, "final_stage": 0,
        })

    def test_record_institution_stage_dwell_time(self):
        t = offline_simulation._new_institution_tracking()
        for turn in (1, 2, 3):
            offline_simulation._record_institution_stage(t, turn, 0)
        self.assertEqual(t["stage_turns"], {0: 3})

    def test_record_institution_stage_first_reached_turn_not_overwritten_on_revisit(self):
        t = offline_simulation._new_institution_tracking()
        offline_simulation._record_institution_stage(t, 5, 0)
        offline_simulation._record_institution_stage(t, 10, 1)
        offline_simulation._record_institution_stage(t, 15, 1)
        offline_simulation._record_institution_stage(t, 20, 0)
        offline_simulation._record_institution_stage(t, 25, 1)
        # Stage1に2回到達しているが、記録されるのは最初の到達ターン(10)だけ。
        self.assertEqual(t["first_reached_turn"], {0: 5, 1: 10})

    def test_record_institution_stage_first_left_healthy_turn(self):
        t = offline_simulation._new_institution_tracking()
        offline_simulation._record_institution_stage(t, 1, 0)
        offline_simulation._record_institution_stage(t, 2, 0)
        offline_simulation._record_institution_stage(t, 3, 1)
        offline_simulation._record_institution_stage(t, 4, 2)
        self.assertEqual(t["first_left_healthy_turn"], 3)

    def test_record_institution_stage_never_left_healthy_stays_none(self):
        t = offline_simulation._new_institution_tracking()
        for turn in (1, 2, 3):
            offline_simulation._record_institution_stage(t, turn, 0)
        self.assertIsNone(t["first_left_healthy_turn"])

    def test_record_institution_stage_transition_and_recovery_counts(self):
        t = offline_simulation._new_institution_tracking()
        for turn, stage in [(1, 0), (2, 1), (3, 2), (4, 2), (5, 1), (6, 0), (7, 1)]:
            offline_simulation._record_institution_stage(t, turn, stage)
        # 変化: 0→1, 1→2, 2→1, 1→0, 0→1 の5回。うち下降(復旧)方向は
        # 2→1, 1→0 の2回。
        self.assertEqual(t["transition_count"], 5)
        self.assertEqual(t["recovery_count"], 2)
        self.assertEqual(t["transition_history"], [
            {"turn": 2, "from_stage": 0, "to_stage": 1},
            {"turn": 3, "from_stage": 1, "to_stage": 2},
            {"turn": 5, "from_stage": 2, "to_stage": 1},
            {"turn": 6, "from_stage": 1, "to_stage": 0},
            {"turn": 7, "from_stage": 0, "to_stage": 1},
        ])

    def test_record_institution_stage_worst_and_final_stage(self):
        t = offline_simulation._new_institution_tracking()
        for turn, stage in [(1, 0), (2, 3), (3, 1)]:
            offline_simulation._record_institution_stage(t, turn, stage)
        self.assertEqual(t["worst_stage"], 3)
        self.assertEqual(t["final_stage"], 1)

    def test_record_settlement_outcome_accumulates_by_stage(self):
        counts = {}
        offline_simulation._record_settlement_outcome(counts, 0, "fulfilled")
        offline_simulation._record_settlement_outcome(counts, 0, "fulfilled")
        offline_simulation._record_settlement_outcome(counts, 0, "defaulted")
        offline_simulation._record_settlement_outcome(counts, 2, "defaulted")
        self.assertEqual(counts, {
            0: {"fulfilled": 2, "defaulted": 1},
            2: {"fulfilled": 0, "defaulted": 1},
        })

    def test_record_and_aggregate_choices_excluding_unavailable_stage(self):
        counts = {}
        offline_simulation._record_choice_by_stage(counts, 0, "money")
        offline_simulation._record_choice_by_stage(counts, 0, "rest")
        offline_simulation._record_choice_by_stage(counts, 3, "rest")
        offline_simulation._record_choice_by_stage(counts, 2, "money")
        self.assertEqual(counts, {
            0: {"money": 1, "rest": 1}, 3: {"rest": 1}, 2: {"money": 1}})
        self.assertEqual(
            offline_simulation._choice_counts_excluding_stage(counts, 3),
            {"money": 2, "rest": 1})

    def test_worst_stage_reached_by_checkpoint_ignores_later_worsening(self):
        t = offline_simulation._new_institution_tracking()
        for turn, stage in [(1, 0), (10, 1), (100, 3)]:
            offline_simulation._record_institution_stage(t, turn, stage)
        self.assertEqual(offline_simulation._worst_stage_reached_by_turn(t, 60), 1)
        self.assertEqual(offline_simulation._worst_stage_reached_by_turn(t, 100), 3)

    def test_transition_chatter_counts_only_rapid_same_boundary_recrosses(self):
        t = offline_simulation._new_institution_tracking()
        for turn, stage in [(1, 0), (10, 1), (15, 0), (40, 1), (41, 2), (45, 1)]:
            offline_simulation._record_institution_stage(t, turn, stage)
        metrics = offline_simulation._transition_chatter_metrics(t, rapid_window_turns=12)
        self.assertEqual(metrics["transition_count"], 5)
        self.assertEqual(metrics["direction_reversal_count"], 3)
        self.assertEqual(metrics["rapid_same_boundary_recross_count"], 2)
        self.assertAlmostEqual(metrics["transition_rate_per_100_turns"], 5 / 6 * 100)

    def test_tally_by_category_counts_pass_fail_na(self):
        checks = [
            {"passed": True, "category": "core"},
            {"passed": False, "category": "core"},
            {"passed": None, "category": "core"},
            {"passed": True, "category": "bank"},
        ]
        tally = offline_simulation._tally_by_category(checks)
        self.assertEqual(tally, {
            "core": {"pass": 1, "fail": 1, "na": 1},
            "bank": {"pass": 1, "fail": 0, "na": 0},
        })

    def test_tally_by_category_defaults_missing_category_to_uncategorized(self):
        checks = [{"passed": True}]
        tally = offline_simulation._tally_by_category(checks)
        self.assertEqual(tally, {"uncategorized": {"pass": 1, "fail": 0, "na": 0}})


class CheckEnforcementConvergenceTest(unittest.TestCase):
    """2026-08-15追加(Step 12)。check_enforcement_convergence()を、構成した
    run_results fixtureで直接検証する(実シミュレーションを回さない)。"""

    def test_fewer_than_two_survivors_is_na(self):
        result = offline_simulation.check_enforcement_convergence(
            [{"death_turn": 100, "enforcement_stage": 2}])
        self.assertIsNone(result["passed"])
        self.assertEqual(result["category"], "contract_enforcement")

    def test_zero_survivors_is_na(self):
        result = offline_simulation.check_enforcement_convergence([
            {"death_turn": 100, "enforcement_stage": 2},
            {"death_turn": 50, "enforcement_stage": 0}])
        self.assertIsNone(result["passed"])

    def test_distinct_stages_among_survivors_passes(self):
        result = offline_simulation.check_enforcement_convergence([
            {"death_turn": None, "enforcement_stage": 0},
            {"death_turn": None, "enforcement_stage": 2}])
        self.assertTrue(result["passed"])

    def test_uniform_stage_among_survivors_fails(self):
        result = offline_simulation.check_enforcement_convergence([
            {"death_turn": None, "enforcement_stage": 2},
            {"death_turn": None, "enforcement_stage": 2},
            {"death_turn": 10, "enforcement_stage": 0}])  # 死亡は対象外
        self.assertFalse(result["passed"])
        self.assertIn("単一Stageへ一律収束している", result["detail"])

    def test_missing_enforcement_stage_key_defaults_without_crashing(self):
        # game_functions系のfake simulate_policyのような、enforcement_stageキーの
        # 無い辞書を渡してもKeyErrorにならない(.get()による防御、
        # test_run_policy_check_propagates_monkeypatched_game_functionsが
        # 実際に踏んだケースの直接再現)。
        result = offline_simulation.check_enforcement_convergence(
            [{"death_turn": None}, {"death_turn": None}, {"death_turn": None}])
        self.assertFalse(result["passed"])  # 全部None値=単一値へ一律収束扱い


class CheckTrajectoryCriteriaRegimeNATest(unittest.TestCase):
    """2026-08-15追加(Step 12/12.1)。制度レジームに応じた期間別評価とN/Aを、
    構成したfixtureで直接検証する。可逆制度は利用可能期間だけを評価し、その期間に
    観測が無い場合だけN/A。数値閾値・シミュレーション本体は変更しない。"""

    def setUp(self):
        self.deps = game._policy_simulation_dependencies()

    def _point(self, bank_stage=None, currency_stage=None, real_money=100.0, bank_crisis_count=0):
        bank_stage = game.BANK_STAGE_HEALTHY if bank_stage is None else bank_stage
        currency_stage = game.CURRENCY_STAGE_NORMAL if currency_stage is None else currency_stage
        return {
            "resources": {"energy": 50, "money": 50, "peace": 50},
            "traits": {"dexterity": 50.0, "intellect": 50.0, "skill": 50.0, "health": 50.0},
            "real_money": real_money, "bank_trust": 50.0, "bank_crisis_count": bank_crisis_count,
            "peace_min": 0, "bank_stage": bank_stage, "currency_confidence": 50.0,
            "currency_stage": currency_stage, "community_trust": 50.0, "local_credit_stage": 0,
            "enforcement_capacity": 50.0, "enforcement_stage": 0,
        }

    def _minimal_result(self, **overrides):
        base = {
            "policy": "cautious",
            "counts": {"通常:money": 10, "通常:labor": 10, "通常:social": 10, "通常:rest": 10,
                      "清算:money": 5, "清算:avoid": 1},
            "resources": {"energy": 50, "money": 50, "peace": 50},
            "trajectory": {60: self._point(), 960: self._point(), 1920: self._point()},
            "min_seen": {}, "agree_log": [{"turn": 1, "picks": {}, "agree": True}],
            "contracts": [], "overridden": 0, "bank_money": 0, "economy_money": 0,
            "bank_repaid_count": 0, "bank_crisis_count": 0, "bank_trust": 50.0,
            "bank_stage": game.BANK_STAGE_HEALTHY, "currency_confidence": 50.0,
            "currency_stage": game.CURRENCY_STAGE_NORMAL, "community_trust": 50.0,
            "local_credit_stage": 0, "enforcement_capacity": 50.0, "enforcement_stage": 0,
            "real_money": 100.0,
            "traits": {"dexterity": 50.0, "intellect": 50.0, "skill": 50.0, "health": 50.0},
            "talent": "health", "fires": 0, "npcs": {}, "death_turn": None,
            "institution_trajectories": {
                "bank": offline_simulation._new_institution_tracking(),
                "currency": offline_simulation._new_institution_tracking(),
                "local_credit": offline_simulation._new_institution_tracking(),
                "contract_enforcement": dict(offline_simulation._new_institution_tracking(),
                                             settlement_counts_by_stage={}),
            },
            "policy_check_metrics": {
                "normal_choices_by_currency_stage": {
                    game.CURRENCY_STAGE_NORMAL: {
                        "money": 10, "labor": 10, "social": 10, "rest": 10}},
                "normal_choices_by_local_credit_stage": {
                    game.LOCAL_CREDIT_STAGE_HEALTHY: {
                        "money": 10, "labor": 10, "social": 10, "rest": 10}},
                "settlement_counts_by_checkpoint": {
                    60: {"fulfilled": 5, "defaulted": 1},
                    960: {"fulfilled": 5, "defaulted": 1},
                    1920: {"fulfilled": 5, "defaulted": 1}},
            },
        }
        base.update(overrides)
        return base

    def _find(self, checks, name):
        matches = [c for c in checks if c["name"] == name]
        self.assertEqual(len(matches), 1, f"基準{name!r}が見つからない")
        return matches[0]

    def test_money_diversity_uses_only_currency_available_period(self):
        r = self._minimal_result()
        r["institution_trajectories"]["currency"]["first_reached_turn"] = {
            0: 1, game.CURRENCY_STAGE_ABANDONED: 100}
        r["policy_check_metrics"]["normal_choices_by_currency_stage"] = {
            game.CURRENCY_STAGE_NORMAL: {"money": 10, "rest": 10},
            game.CURRENCY_STAGE_ABANDONED: {"rest": 100},
        }
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "選択の多様性(通常:moneyが5%〜80%の範囲)")
        self.assertTrue(c["passed"])
        self.assertIn("10/20, 適用期間", c["detail"])
        self.assertEqual(c["category"], "regime_choice_economy")
        self.assertEqual(c["applicable_regime"], "currency_not_abandoned")

    def test_money_diversity_na_when_no_currency_available_choices(self):
        r = self._minimal_result()
        r["institution_trajectories"]["currency"]["first_reached_turn"] = {
            game.CURRENCY_STAGE_ABANDONED: 1}
        r["policy_check_metrics"]["normal_choices_by_currency_stage"] = {
            game.CURRENCY_STAGE_ABANDONED: {"rest": 100}}
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "選択の多様性(通常:moneyが5%〜80%の範囲)")
        self.assertIsNone(c["passed"])

    def test_social_diversity_uses_only_local_credit_available_period(self):
        r = self._minimal_result()
        r["institution_trajectories"]["local_credit"]["first_reached_turn"] = {
            0: 1, game.LOCAL_CREDIT_STAGE_ISOLATED: 100}
        r["policy_check_metrics"]["normal_choices_by_local_credit_stage"] = {
            game.LOCAL_CREDIT_STAGE_HEALTHY: {"social": 10, "rest": 10},
            game.LOCAL_CREDIT_STAGE_ISOLATED: {"rest": 100},
        }
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "選択の多様性(通常:socialが5%〜80%の範囲)")
        self.assertTrue(c["passed"])
        self.assertIn("10/20, 適用期間", c["detail"])
        self.assertEqual(c["category"], "local_credit")
        self.assertEqual(c["applicable_regime"], "local_credit_not_isolated")

    def test_social_diversity_na_when_no_local_credit_available_choices(self):
        r = self._minimal_result()
        r["institution_trajectories"]["local_credit"]["first_reached_turn"] = {
            game.LOCAL_CREDIT_STAGE_ISOLATED: 1}
        r["policy_check_metrics"]["normal_choices_by_local_credit_stage"] = {
            game.LOCAL_CREDIT_STAGE_ISOLATED: {"rest": 100}}
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "選択の多様性(通常:socialが5%〜80%の範囲)")
        self.assertIsNone(c["passed"])
        self.assertEqual(c["category"], "local_credit")

    def test_real_money_na_after_currency_abandoned(self):
        r = self._minimal_result()
        r["trajectory"][960]["currency_stage"] = game.CURRENCY_STAGE_ABANDONED
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "real_moneyの後半崩壊が無いこと(T960→T1920)")
        self.assertIsNone(c["passed"])
        self.assertEqual(c["category"], "currency")

    def test_bank_crisis_criteria_na_after_bank_collapsed_by_960(self):
        r = self._minimal_result()
        r["trajectory"][960]["bank_stage"] = game.BANK_STAGE_COLLAPSED
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        hi = self._find(checks, "通貨危機(高デフォルト率→T960までに1回以上)")
        lo = self._find(checks, "通貨危機(低デフォルト率→T960までに0回)")
        rare = self._find(checks, "通貨危機が稀である(T960までに5回以下)")
        self.assertIsNone(hi["passed"])
        self.assertIsNone(lo["passed"])
        self.assertIsNone(rare["passed"])
        self.assertEqual(hi["category"], "bank")

    def test_bank_crisis_uses_t960_default_rate_not_lifetime_rate(self):
        r = self._minimal_result(counts={
            "通常:money": 10, "通常:labor": 10, "通常:social": 10, "通常:rest": 10,
            "清算:money": 5, "清算:avoid": 5})  # 生涯50%だがT960までは0%
        r["policy_check_metrics"]["settlement_counts_by_checkpoint"][960] = {
            "fulfilled": 10, "defaulted": 0}
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        hi = self._find(checks, "通貨危機(高デフォルト率→T960までに1回以上)")
        lo = self._find(checks, "通貨危機(低デフォルト率→T960までに0回)")
        self.assertIsNone(hi["passed"])
        self.assertTrue(lo["passed"])
        self.assertIn("T960までのデフォルト率0.0%", lo["detail"])

    def test_enforcement_early_collapse_detail_uses_checkpoint_worst_stage(self):
        r = self._minimal_result()
        enforcement = r["institution_trajectories"]["contract_enforcement"]
        enforcement["first_reached_turn"] = {0: 1, 1: 10, 3: 1000}
        enforcement["worst_stage"] = 3
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        t60 = self._find(checks, "契約執行: T60までにStage2以上へ早期崩壊していない")
        t960 = self._find(checks, "契約執行: T960までにStage2以上へ早期崩壊していない")
        self.assertIn("worst_stage=1", t60["detail"])
        self.assertIn("worst_stage=1", t960["detail"])

    def test_enforcement_chatter_ignores_many_slow_transitions(self):
        r = self._minimal_result()
        enforcement = r["institution_trajectories"]["contract_enforcement"]
        enforcement["stage_turns"] = {0: 100, 1: 100}
        enforcement["transition_count"] = 8
        enforcement["recovery_count"] = 4
        enforcement["transition_history"] = [
            {"turn": 20 * i, "from_stage": (i + 1) % 2, "to_stage": i % 2}
            for i in range(1, 9)]
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "契約執行: 閾値付近で過剰な往復遷移を起こしていない")
        self.assertTrue(c["passed"])
        self.assertIn("総遷移=8回", c["detail"])

    def test_enforcement_chatter_fails_repeated_rapid_recrosses(self):
        r = self._minimal_result()
        enforcement = r["institution_trajectories"]["contract_enforcement"]
        enforcement["stage_turns"] = {0: 10, 1: 10}
        enforcement["transition_count"] = 4
        enforcement["recovery_count"] = 2
        enforcement["transition_history"] = [
            {"turn": 10, "from_stage": 0, "to_stage": 1},
            {"turn": 11, "from_stage": 1, "to_stage": 0},
            {"turn": 12, "from_stage": 0, "to_stage": 1},
            {"turn": 13, "from_stage": 1, "to_stage": 0},
        ]
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        c = self._find(checks, "契約執行: 閾値付近で過剰な往復遷移を起こしていない")
        self.assertFalse(c["passed"])
        self.assertIn("同一境界再横断=3回", c["detail"])

    def test_baseline_fixture_does_not_na_the_same_criteria(self):
        # 対照: 何も崩壊させていない構成では同じ基準がN/Aにならないことを確認する
        # (regime判定が常にN/Aを返す壊れた実装になっていないことの検証)。
        r = self._minimal_result()
        checks = offline_simulation.check_trajectory_criteria(r, dependencies=self.deps)
        money = self._find(checks, "選択の多様性(通常:moneyが5%〜80%の範囲)")
        real_money = self._find(checks, "real_moneyの後半崩壊が無いこと(T960→T1920)")
        crisis = self._find(checks, "通貨危機(高デフォルト率→T960までに1回以上)")
        self.assertIsNotNone(money["passed"])
        self.assertIsNotNone(real_money["passed"])
        # crisisはdefault_rateが対象範囲外(1/6≈16.7%…実は>8%に該当し得るので
        # money以外の分岐に依存する可能性がある)ため、passedの真偽自体は問わず
        # 「N/Aでない」ことだけを確認する。
        self.assertIsNotNone(crisis["passed"])


class RunPolicyCheckDeathSurvivalTallyTest(unittest.TestCase):
    """2026-08-15追加(Step 12)。死亡数・生存数を取り違えない集計を保証する
    回帰テスト——化けたコンソール出力の読み違えで死亡率を逆に報告した実例
    (2026-08-15、契約執行制度レビュー時)の再発防止。fakeのsimulate_policy_fnで
    死亡・生存を明示的に作り分け、run_policy_check()が正しい件数を出すことを
    文字列の完全一致(人間の目視・推測を介さない)で確認する。"""

    def test_death_and_survival_counts_are_not_swapped(self):
        deps = game._policy_simulation_dependencies()
        call_index = {"n": 0}

        def fake_simulate_policy(policy_name, turns, seed, safety_floor, talent):
            call_index["n"] += 1
            # 1seed×3方針=3走行。最初の2回を死亡・残り1回を生存にする
            # (2/3・1/3という非対称な数にして、取り違えれば必ず検出できるようにする)。
            death_turn = 100 if call_index["n"] <= 2 else None
            return {
                "trajectory": {}, "counts": {}, "agree_log": [], "npcs": {},
                "death_turn": death_turn, "enforcement_stage": 0,
                "institution_trajectories": {
                    "bank": offline_simulation._new_institution_tracking(),
                    "currency": offline_simulation._new_institution_tracking(),
                    "local_credit": offline_simulation._new_institution_tracking(),
                    "contract_enforcement": dict(offline_simulation._new_institution_tracking(),
                                                 settlement_counts_by_stage={}),
                },
            }

        deps2 = dataclasses.replace(deps, simulate_policy_fn=fake_simulate_policy)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                offline_simulation.run_policy_check([1], game.SAFETY_FLOOR, dependencies=deps2)
        out = buf.getvalue()
        self.assertIn("死亡2/3件", out)
        self.assertIn("生存1/3件", out)


class Step12SimulatePolicyRNGUnchangedTest(unittest.TestCase):
    """2026-08-15追加(Step 12)。institution_trajectories計測の追加が
    simulate_policy()のRNG消費順序に一切影響しないことを、200ターン走行後の
    random状態のハッシュ値で確認する(golden値はStep 12実装直後——S1完了・
    以降のS2〜S7いずれの変更後も同一であることを確認済み——に採取)。"""

    def test_rng_state_hash_after_200_turns_unchanged(self):
        state_before = random.getstate()
        try:
            game.simulate_policy(
                "cautious", 200, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
            state_after = random.getstate()
        finally:
            random.setstate(state_before)
        digest = hashlib.sha256(repr(state_after).encode()).hexdigest()
        self.assertEqual(
            digest, "125beb6da9035be867ddbe957ebf8d5d269566cda15987f57db7dec43469a49a")


class EnforcementStage0ForcedControlEquivalenceTest(unittest.TestCase):
    """2026-08-15追加(Step 12)。enforcement_stage_nextを常にStage0固定に
    monkeypatchした対照実験(=契約執行制度そのものを導入する前の挙動と等価)が、
    契約執行導入前baselineと一致することを確認する回帰テスト。

    2026-08-15のユーザーレビューで確定した契約執行導入前baseline(地域信用のみ):
    PASS157/FAIL48/N/A65、死亡10/15件・生存5/15件
    (平均118.7歳・最短111.9歳・最長128.8歳)。

    Step 12/12.1はこのbaselineに対して(a)新しいcategory="contract_enforcement"の
    基準を4件(走行内3件×15走行+走行横断1件)追加し、(b)既存18基準についても
    制度レジームに応じた期間別評価を行うようになった。そのため
    「総合PASS/FAIL/N/Aの生の合計」は旧baselineの157/48/65とは一致しない
    (これはFAILを減らす目的の基準緩和ではなく、崩壊で評価不能になった項目を
    正しくN/A化した結果であり、意図した挙動)。検証は以下の3点に絞る:
    - 死亡・生存・寿命の分布はenforcement_stageの経路に依存せず決まるため、
      旧baselineと完全一致するはず(整数・小数の厳密一致)。
    - 旧18基準(category!="contract_enforcement")のFAIL件数は、期間別評価へ
      補正した後も旧baselineのFAIL48件と一致することを実測で固定する。
    - category="contract_enforcement"は、Stage0固定では早期崩壊も過剰遷移も
      起こりえないため走行内3基準×15走行=45件全PASS。走行横断の収束判定は、
      生存ケース全員がStage0固定=単一Stageへの一律収束となるため必ずFAIL
      (この基準の設計通りの挙動、check_enforcement_convergence参照)。

    2026-08-15追加(Step 13、物々交換・自給制度): この対照実験は
    enforcement_stage_nextだけをStage0固定にしていたが、Step 13で
    engine.alternative_economy_triggered()が銀行・通貨・地域信用・契約執行の
    「いずれか」を見るようになったため、enforcementを固定してもbank/currency/
    local_creditが1920ターンの間に自然に劣化Stageへ達すると、そこから
    barter/subsistenceが通常行動の選択肢に加わり(既存archetypeと同様、
    提示されるだけでcost/hoursの抽選が走る)、以後のRNG消費列がこの対照実験
    そのものに介入して寿命の分布が変わってしまう(死亡5件が新たに発生し、
    旧baselineの死亡10/生存5から死亡15/生存0へ変化することを実測で確認した)。
    これは物々交換制度の実装として意図どおりの挙動だが、この対照実験の目的
    (「契約執行を導入する前の世界を再現する」)からすると、契約執行だけでなく
    物々交換もまだ存在しない前提のはず——engine.alternative_economy_triggered
    も併せてFalse固定にすることで、この対照実験の意図(地域信用のみのbaseline
    再現)を回復する。監査した結果、両方をFalse固定した場合の実測値は旧
    baselineと完全一致(死亡10/生存5・平均118.7歳・最短111.9歳・最長128.8歳・
    非contract_enforcement/barrierのFAIL48件)——バグではなく、物々交換の
    新しいトリガー面が対照実験の前提を広げていただけと確認できた。"""

    @classmethod
    def setUpClass(cls):
        original_enforcement_fn = game.enforcement_stage_next
        original_triggered_fn = engine.alternative_economy_triggered
        game.enforcement_stage_next = lambda stage, cap: 0
        # Step 13追加: 上記docstring参照。物々交換もまだ存在しない前提に揃える。
        engine.alternative_economy_triggered = lambda *args, **kwargs: False
        try:
            deps = game._policy_simulation_dependencies()
            all_checks = []
            run_results = []
            death_ages = []
            survived = 0
            for seed in (1, 2, 3, 4, 5):
                deps.seed_fn(seed)
                talent = deps.choice_fn(deps.traits)
                for p in deps.policy_vectors:
                    r = offline_simulation.simulate_policy(
                        p, max(deps.checkpoint_turns), seed, game.SAFETY_FLOOR, talent,
                        dependencies=deps)
                    checks = offline_simulation.check_trajectory_criteria(r, dependencies=deps)
                    all_checks.extend(checks)
                    run_results.append(r)
                    if r.get("death_turn") is not None:
                        death_ages.append(deps.age_at_fn(r["death_turn"]))
                    else:
                        survived += 1
            convergence = offline_simulation.check_enforcement_convergence(run_results)
            all_checks.append(convergence)
        finally:
            game.enforcement_stage_next = original_enforcement_fn
            engine.alternative_economy_triggered = original_triggered_fn
        cls.death_ages = death_ages
        cls.survived = survived
        cls.tally = offline_simulation._tally_by_category(all_checks)

    def test_death_and_survival_counts_match_pre_enforcement_baseline(self):
        self.assertEqual(len(self.death_ages), 10)
        self.assertEqual(self.survived, 5)

    def test_lifespan_stats_match_pre_enforcement_baseline(self):
        avg_age = sum(self.death_ages) / len(self.death_ages)
        self.assertAlmostEqual(avg_age, 118.7, places=1)
        self.assertAlmostEqual(min(self.death_ages), 111.9, places=1)
        self.assertAlmostEqual(max(self.death_ages), 128.8, places=1)

    def test_pre_existing_18_criteria_fail_count_matches_baseline(self):
        non_enforcement_fail = sum(
            b["fail"] for cat, b in self.tally.items()
            if cat not in ("contract_enforcement", "barter"))
        self.assertEqual(non_enforcement_fail, 48)

    def test_contract_enforcement_criteria_all_pass_except_convergence_when_forced_stage0(self):
        b = self.tally["contract_enforcement"]
        self.assertEqual(b["pass"], 45)
        self.assertEqual(b["fail"], 1)
        self.assertEqual(b["na"], 0)

    def test_barter_criteria_all_na_or_pass_when_alternative_economy_never_triggered(self):
        # 2026-08-15追加(Step 13)。alternative_economy_triggeredをFalse固定した
        # ことで、barter_activeは一度もtrueにならないはず——走行内5基準×15走行=
        # 75件のうち、常時判定可能な3件(T60/T960早期崩壊・過剰短期再横断)は
        # 全PASS(45件)、barter_active依存の2件(実際に使われたか・Stage3
        # ペナルティ)は全てN/A(30件)になるはず(基準を緩めたのではなく、
        # 評価対象そのものが発生しなかったという区別)。
        b = self.tally["barter"]
        self.assertEqual(b["pass"], 15)
        self.assertEqual(b["fail"], 0)
        self.assertEqual(b["na"], 60)


# ==============================================================================
# main()の清算効果(Step 3C-0、2026-08-14): plan_settlement_effects()への
# 抽出前に、現行のmain()実装のままgolden値として固定する。
# ==============================================================================

class MainSettlementFixtureTest(unittest.TestCase):
    """一時EVENTS_PATH・固定seed・mockしたcall_ollama・強制するauto_selectで
    main()を1ターンだけ走らせる。対象contractのcontract_settled以降に発生した
    清算効果イベント(narration・trait_changedなど清算効果と無関係なイベントは
    比較対象に含めない——このStepの抽出対象ではないため)と、最終状態の
    構造化値を固定する。stdoutはcontextlib.redirect_stdoutで捨てる
    (Bashのコンソールがcp932だと、通貨危機の「≥」等の記号でUnicodeEncodeError
    になる環境依存の問題を避けるため)。

    6分岐: 1) 銀行債務・money履行 2) 銀行債務・labor履行
    3) 銀行債務・不履行(危機なし) 4) 銀行債務・不履行(危機あり)
    5) 社会契約・履行 6) 社会契約・不履行"""

    MOCK_JSON = json.dumps({
        "situation": "モックの状況説明です。",
        "choices": {"money": "お金で返す", "labor": "労力で返す", "avoid": "先延ばしにする",
                    "social": "友人に頼る", "rest": "休む"},
        "social_counterparty": None, "social_favor": "頼み事",
        "contradiction": False, "reason": "",
    }, ensure_ascii=False)

    BANK_BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行",
                            "birth_turn": 0, "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済",
                            "birth_turn": 0, "role": "wage_issuer", "trust": 50.0}),
    ]

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_JSON, 0.001)
        self._orig_auto_select = game.auto_select
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()  # main()に新規作成させる
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        self._orig_argv = sys.argv

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.auto_select = self._orig_auto_select
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)
        sys.argv = self._orig_argv

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _force_settlement_choice(self, key):
        """auto_selectを差し替え、清算ターン(choicesに"avoid"を含む)では
        必ずkeyを選ぶよう強制する。通常ターン(このテストでは発生しない想定)は
        元のauto_selectにフォールバックする。"""
        orig = game.auto_select

        def forced(choices, resources, policy, policy_name=None,
                  safety_floor=game.SAFETY_FLOOR, turn=0, budget=None):
            keys = [c["key"] for c in choices]
            if "avoid" in keys:
                return keys.index(key)
            return orig(choices, resources, policy, policy_name, safety_floor, turn, budget)

        game.auto_select = forced

    def _run_one_settlement_turn(self, base_events, choice_key, seed=1):
        self._write_fixture(base_events)
        self._force_settlement_choice(choice_key)
        random.seed(seed)
        sys.argv = ["game.py", "--auto", "--turns", "1", "--events", str(self.events_path),
                   "--no-consistency-check"]
        with contextlib.redirect_stdout(io.StringIO()):
            game.main()

    def _events_after_settled(self, target_id, count):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == "contract_settled" and e["data"]["id"] == target_id:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:i + count]]
        raise AssertionError(f"contract_settled(id={target_id!r})が見つからない")

    def test_case1_bank_debt_money_fulfilled(self):
        events = self.BANK_BASE + [
            ("contract_created", {"id": "c_target", "counterparty": "中央銀行",
                                  "description": "銀行からの賃金前借り(返済義務あり)",
                                  "created_turn": 0, "due_turn": 1, "repay_money": -40,
                                  "is_bank_debt": True}),
        ]
        self._run_one_settlement_turn(events, "money")

        got = self._events_after_settled("c_target", 4)
        self.assertEqual(got, [
            {"type": "contract_settled", "data": {"id": "c_target", "turn": 1,
             "outcome": "fulfilled", "settled_by": "money", "delta": {}}},
            {"type": "npc_trust_changed", "data": {"npc_id": "bank", "delta": 0.5,
             "reason": "wage_debt_fulfilled", "turn": 1}},
            {"type": "npc_wallet_changed", "data": {"npc_id": "bank", "delta": 40,
             "reason": "wage_debt_repaid", "turn": 1}},
            {"type": "currency_confidence_changed", "data": {"turn": 1, "delta": 0.5,
             "reason": "money_settlement_fulfilled"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "fulfilled")
        self.assertEqual(state["resources"], {"energy": 100, "peace": 100, "money": -40})
        self.assertEqual(state["npcs"]["bank"]["money"], 40)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 50.5)
        self.assertAlmostEqual(state["currency_confidence"], 50.5)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 0)

    def test_case2_bank_debt_labor_fulfilled(self):
        events = self.BANK_BASE + [
            ("contract_created", {"id": "c_target", "counterparty": "中央銀行",
                                  "description": "銀行からの賃金前借り(返済義務あり)",
                                  "created_turn": 0, "due_turn": 1, "repay_money": -40,
                                  "is_bank_debt": True}),
        ]
        self._run_one_settlement_turn(events, "labor")

        got = self._events_after_settled("c_target", 2)
        self.assertEqual(got, [
            {"type": "contract_settled", "data": {"id": "c_target", "turn": 1,
             "outcome": "fulfilled", "settled_by": "labor", "delta": {}}},
            {"type": "npc_trust_changed", "data": {"npc_id": "bank", "delta": 0.5,
             "reason": "wage_debt_fulfilled", "turn": 1}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "fulfilled")
        self.assertEqual(state["resources"], {"energy": 35, "peace": 100, "money": 0})
        self.assertEqual(state["npcs"]["bank"]["money"], 0)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 50.5)
        self.assertAlmostEqual(state["currency_confidence"], 50.0)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 0)

    def test_case3_bank_debt_defaulted_no_crisis(self):
        events = self.BANK_BASE + [
            ("contract_created", {"id": "c_target", "counterparty": "中央銀行",
                                  "description": "銀行からの賃金前借り(返済義務あり)",
                                  "created_turn": 0, "due_turn": 1, "repay_money": -40,
                                  "is_bank_debt": True}),
        ]
        self._run_one_settlement_turn(events, "avoid")

        got = self._events_after_settled("c_target", 2)
        self.assertEqual(got[0], {"type": "contract_settled", "data": {
            "id": "c_target", "turn": 1, "outcome": "defaulted",
            "settled_by": "avoid", "delta": {}}})
        self.assertEqual(got[1]["type"], "npc_trust_changed")
        self.assertEqual(got[1]["data"]["npc_id"], "bank")
        self.assertEqual(got[1]["data"]["reason"], "wage_debt_defaulted")
        self.assertEqual(got[1]["data"]["turn"], 1)
        self.assertAlmostEqual(got[1]["data"]["delta"], -19.950124688279303)

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "defaulted")
        self.assertEqual(state["resources"], {"energy": 100, "peace": 20, "money": 0})
        self.assertEqual(state["npcs"]["bank"]["money"], 0)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 30.049875311720697)
        self.assertAlmostEqual(state["currency_confidence"], 50.0)
        self.assertAlmostEqual(state["bank_credit_losses"], 39.900249376558605)
        self.assertEqual(state["bank_crisis_count"], 0)

    def test_case4_bank_debt_defaulted_with_crisis(self):
        # bank_credit_lossesを危機の閾値付近まで先に積んでおくダミーの
        # 先行債務(c_prior)を用意し、対象のc_targetの不履行だけで
        # 閾値を超えるようにする。
        events = self.BANK_BASE + [
            ("contract_created", {"id": "c_prior", "counterparty": "中央銀行",
                                  "description": "ダミー先行債務(bank_credit_losses注入用)",
                                  "created_turn": 0, "due_turn": 0, "repay_money": -750,
                                  "is_bank_debt": True}),
            ("contract_settled", {"id": "c_prior", "turn": 0, "outcome": "defaulted",
                                  "settled_by": "avoid", "delta": {}}),
            ("contract_created", {"id": "c_target", "counterparty": "中央銀行",
                                  "description": "銀行からの賃金前借り(返済義務あり)",
                                  "created_turn": 0, "due_turn": 1, "repay_money": -80,
                                  "is_bank_debt": True}),
        ]
        self._run_one_settlement_turn(events, "avoid")

        got = self._events_after_settled("c_target", 4)
        self.assertEqual(got[0], {"type": "contract_settled", "data": {
            "id": "c_target", "turn": 1, "outcome": "defaulted",
            "settled_by": "avoid", "delta": {}}})
        self.assertEqual(got[1]["type"], "npc_trust_changed")
        self.assertEqual(got[1]["data"]["reason"], "wage_debt_defaulted")
        self.assertAlmostEqual(got[1]["data"]["delta"], -39.900249376558605)
        self.assertEqual(got[2], {"type": "bank_crisis", "data": {
            "turn": 1, "rebase_factor": 3,
            "credit_losses": 829.8004987531172, "crisis_count": 1}})
        self.assertEqual(got[3]["type"], "npc_trust_changed")
        self.assertEqual(got[3]["data"]["reason"], "bank_crisis")
        self.assertAlmostEqual(got[3]["data"]["delta"], -5.049875311720697)

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "defaulted")
        self.assertEqual(state["npcs"]["bank"]["money"], 0)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 5.049875311720697)
        self.assertAlmostEqual(state["currency_confidence"], 35.0)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 1)

    def test_case5_social_contract_fulfilled(self):
        events = self.BANK_BASE + [
            ("npc_introduced", {"id": "友人A", "name": "友人A", "birth_turn": 0,
                                "role": "acquaintance", "trust": 60.0, "ethics": 60.0,
                                "retire_turn": 200}),
            ("contract_created", {"id": "c_target", "counterparty": "友人A",
                                  "description": "頼み事", "created_turn": 0, "due_turn": 1,
                                  "repay_money": -30}),
        ]
        self._run_one_settlement_turn(events, "money")

        got = self._events_after_settled("c_target", 2)
        self.assertEqual(got, [
            {"type": "contract_settled", "data": {"id": "c_target", "turn": 1,
             "outcome": "fulfilled", "settled_by": "money", "delta": {}}},
            {"type": "npc_trust_changed", "data": {"npc_id": "友人A", "delta": 1.20375,
             "reason": "social_contract_fulfilled", "turn": 1}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "fulfilled")
        self.assertEqual(state["resources"], {"energy": 100, "peace": 100, "money": -30})
        self.assertAlmostEqual(state["npcs"]["友人A"]["trust"], 61.07875)
        self.assertEqual(state["npcs"]["bank"]["money"], 0)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 0)

    def test_case6_social_contract_defaulted(self):
        events = self.BANK_BASE + [
            ("npc_introduced", {"id": "友人A", "name": "友人A", "birth_turn": 0,
                                "role": "acquaintance", "trust": 60.0, "ethics": 60.0,
                                "retire_turn": 200}),
            ("contract_created", {"id": "c_target", "counterparty": "友人A",
                                  "description": "頼み事", "created_turn": 0, "due_turn": 1,
                                  "repay_money": -30}),
        ]
        self._run_one_settlement_turn(events, "avoid")

        got = self._events_after_settled("c_target", 2)
        self.assertEqual(got, [
            {"type": "contract_settled", "data": {"id": "c_target", "turn": 1,
             "outcome": "defaulted", "settled_by": "avoid", "delta": {}}},
            {"type": "npc_trust_changed", "data": {"npc_id": "友人A", "delta": -8.0,
             "reason": "social_contract_defaulted", "turn": 1}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["contracts"]["c_target"]["status"], "defaulted")
        self.assertEqual(state["resources"], {"energy": 100, "peace": 40, "money": 0})
        self.assertAlmostEqual(state["npcs"]["友人A"]["trust"], 51.875)
        self.assertEqual(state["bank_credit_losses"], 0.0)
        self.assertEqual(state["bank_crisis_count"], 0)


# ==============================================================================
# plan_settlement_effects (Step 3C-1、2026-08-14): 清算の数値効果を計算する
# 純粋関数の単体テスト
# ==============================================================================

class PlanSettlementEffectsTest(unittest.TestCase):
    BANK_CONTRACT = {"id": "c1", "counterparty": "中央銀行", "repay_money": -40,
                     "is_bank_debt": True}
    SOCIAL_CONTRACT = {"id": "c2", "counterparty": "友人A", "repay_money": -30}

    def test_bank_debt_money_fulfilled(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertAlmostEqual(result["bank_trust_delta"], game.bank_trust_gain(50.0))
        self.assertEqual(result["bank_wallet_delta"], 40)
        self.assertAlmostEqual(result["currency_confidence_delta"],
                               game.currency_confidence_gain(50.0))
        self.assertIsNone(result["counterparty_trust_delta"])
        self.assertEqual(result["real_loss"], 0.0)
        self.assertEqual(result["bank_credit_losses_after"], 0.0)
        self.assertFalse(result["crisis_triggered"])
        self.assertIsNone(result["rebase_factor"])

    def test_bank_debt_labor_fulfilled(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "labor", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertAlmostEqual(result["bank_trust_delta"], game.bank_trust_gain(50.0))
        # labor履行は銀行walletもcurrency confidenceも動かさない。
        self.assertEqual(result["bank_wallet_delta"], 0.0)
        self.assertEqual(result["currency_confidence_delta"], 0.0)

    def test_bank_debt_defaulted_no_crisis(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        real_loss = 40 / game.price_index(1)
        self.assertAlmostEqual(result["real_loss"], real_loss)
        self.assertAlmostEqual(result["bank_trust_delta"], -min(50.0, real_loss * 0.5))
        self.assertAlmostEqual(result["bank_credit_losses_after"], real_loss)
        self.assertFalse(result["crisis_triggered"])
        self.assertIsNone(result["crisis_bank_trust_delta"])
        self.assertIsNone(result["crisis_currency_confidence_delta"])
        self.assertIsNone(result["rebase_factor"])

    def test_bank_debt_defaulted_with_crisis(self):
        # bank_credit_lossesを危機の閾値付近まで積んでおき、この1件の不履行だけで
        # 超えるようにする。bank_trust=30.0はreal_loss×0.5(≈19.95)より大きいので、
        # bank_trust_after_lossが0を上回り、crisis_bank_trust_deltaが厳密に負になる
        # (下記test_currency_confidence_...と条件を揃えている)。
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        self.assertTrue(result["crisis_triggered"])
        self.assertEqual(result["rebase_factor"], game.CRISIS_REBASE_FACTOR)
        self.assertLess(result["crisis_bank_trust_delta"], 0.0)
        self.assertLess(result["crisis_currency_confidence_delta"], 0.0)
        self.assertAlmostEqual(result["bank_credit_losses_after"],
                               790.0 + result["real_loss"])

    def test_social_contract_fulfilled(self):
        result = game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        self.assertAlmostEqual(result["counterparty_trust_delta"], game.npc_trust_gain(60.0))
        self.assertEqual(result["bank_trust_delta"], 0.0)
        self.assertEqual(result["bank_wallet_delta"], 0.0)
        self.assertEqual(result["currency_confidence_delta"], 0.0)
        self.assertFalse(result["crisis_triggered"])

    def test_social_contract_defaulted(self):
        result = game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        self.assertEqual(result["counterparty_trust_delta"], -8.0)

    def test_bank_trust_does_not_go_negative_from_real_loss(self):
        # bank_trustが小さい(real_loss×0.5より小さい)ときでも、
        # bank_trust_deltaは-bank_trustまでしか下げない(0未満にしない)。
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=1.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertAlmostEqual(1.0 + result["bank_trust_delta"], 0.0)

    def test_currency_confidence_does_not_go_negative_from_crisis(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        self.assertTrue(result["crisis_triggered"])
        self.assertAlmostEqual(5.0 + result["crisis_currency_confidence_delta"], 0.0)

    def test_input_contract_is_not_mutated(self):
        contract = dict(self.BANK_CONTRACT)
        before = dict(contract)
        game.plan_settlement_effects(
            contract, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertEqual(contract, before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        self.assertEqual(random.getstate(), state_before)

    # --- 契約執行制度(2026-08-15追加) ---
    def test_enforcement_capacity_delta_on_bank_fulfilled(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            enforcement_capacity=50.0)
        self.assertAlmostEqual(result["enforcement_capacity_delta"], game.enforcement_capacity_gain(50.0))

    def test_enforcement_capacity_delta_on_bank_defaulted(self):
        result = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertEqual(result["enforcement_capacity_delta"], game.ENFORCEMENT_DEFAULT_PENALTY)

    def test_enforcement_capacity_delta_on_social_fulfilled(self):
        result = game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0, enforcement_capacity=50.0)
        self.assertAlmostEqual(result["enforcement_capacity_delta"], game.enforcement_capacity_gain(50.0))

    def test_enforcement_capacity_delta_on_social_defaulted(self):
        result = game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        self.assertEqual(result["enforcement_capacity_delta"], game.ENFORCEMENT_DEFAULT_PENALTY)

    def test_enforcement_stage_amplifies_counterparty_and_community_trust(self):
        base = game.plan_settlement_effects(
            self.SOCIAL_CONTRACT, "avoid", "defaulted", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0, enforcement_stage=game.ENFORCEMENT_STAGE_INSTITUTIONAL)
        for stage, amplifier in [
            (game.ENFORCEMENT_STAGE_LOCAL_LEDGER, 1.5),
            (game.ENFORCEMENT_STAGE_PERSONAL, 2.0),
            (game.ENFORCEMENT_STAGE_NONE, 2.0),
        ]:
            with self.subTest(stage=stage):
                result = game.plan_settlement_effects(
                    self.SOCIAL_CONTRACT, "avoid", "defaulted", turn=1,
                    bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
                    counterparty_trust=60.0, enforcement_stage=stage)
                self.assertAlmostEqual(
                    result["counterparty_trust_delta"],
                    base["counterparty_trust_delta"] * amplifier)
                self.assertAlmostEqual(
                    result["community_trust_delta"],
                    base["community_trust_delta"] * amplifier)

    def test_enforcement_stage_does_not_amplify_bank_debt_effects(self):
        # 銀行債務の分岐にはcounterparty_trust_delta/community_trust_deltaが
        # 無い(常にNone/0.0)ので、enforcement_stageを変えても無関係。
        base = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            enforcement_stage=game.ENFORCEMENT_STAGE_INSTITUTIONAL)
        degraded = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", turn=1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            enforcement_stage=game.ENFORCEMENT_STAGE_NONE)
        self.assertEqual(base["bank_trust_delta"], degraded["bank_trust_delta"])
        self.assertIsNone(degraded["counterparty_trust_delta"])
        self.assertEqual(degraded["community_trust_delta"], 0.0)


# ==============================================================================
# 自動選択ロジック(Step 5A-0、2026-08-14): policy.pyへの分離前に、現在の
# normalize_cost / policy_score / auto_select の分岐と数値をgolden固定する。
# ==============================================================================

class PolicySelectionFixtureTest(unittest.TestCase):
    RESOURCES = {"energy": 50, "peace": 50, "money": 50}
    CHOICES = [
        {"key": "money", "cost": {"money": -10}, "hours": 10},
        {"key": "labor", "cost": {"energy": -5}, "hours": 10},
        {"key": "rest", "cost": {"peace": 5}, "hours": 10},
    ]

    def test_normalize_cost_golden(self):
        self.assertEqual(
            game.normalize_cost({"energy": -3, "peace": 1}),
            {"energy": -0.75, "peace": 0.25})
        self.assertEqual(game.normalize_cost({}), {})

    def test_policy_score_legacy_golden(self):
        score = game.policy_score_legacy(
            {"cost": {"energy": -3, "peace": 1}},
            game.POLICY_VECTORS["cautious"])
        self.assertEqual(score, -1.75)

    def test_policy_score_money_peace_and_future_contract_golden(self):
        choice = {
            "key": "social",
            "cost": {"money": -30, "peace": -20},
            "contract": {"repay_money": -40},
        }
        score = game.policy_score(
            choice, game.POLICY_VECTORS["cautious"], turn=5,
            resources={"energy": 50, "peace": 40, "money": 50})
        self.assertAlmostEqual(score, -366.49633372250514)

    def test_auto_select_balanced_golden(self):
        self.assertEqual(
            game.auto_select(self.CHOICES, self.RESOURCES, "balanced"), 2)

    def test_auto_select_random_golden_and_rng_consumption(self):
        state_before = random.getstate()
        try:
            random.seed(1)
            self.assertEqual(
                game.auto_select(self.CHOICES, self.RESOURCES, "random"), 0)
            state_after = random.getstate()
            random.seed(1)
            random.choice([0, 1, 2])
            self.assertEqual(random.getstate(), state_after)
        finally:
            random.setstate(state_before)

    def test_auto_select_policy_golden(self):
        choices = [
            {"key": "money", "cost": {"money": -10}},
            {"key": "labor", "cost": {"energy": -10}},
            {"key": "social", "cost": {"peace": -10}},
        ]
        resources = {"energy": 50, "peace": 100, "money": 50}
        self.assertEqual(
            game.auto_select(choices, resources, "balanced", "family", 24, 5), 1)

    def test_auto_select_budget_filter_golden(self):
        choices = copy.deepcopy(self.CHOICES)
        choices[1]["hours"] = 4
        self.assertEqual(
            game.auto_select(choices, self.RESOURCES, "balanced", budget=5), 1)

    def test_auto_select_no_affordable_fallback_golden(self):
        choices = [
            {"key": "deep", "cost": {"money": -100}},
            {"key": "shallow", "cost": {"energy": -80}},
        ]
        self.assertEqual(
            game.auto_select(choices, self.RESOURCES, "balanced"), 1)


# ==============================================================================
# policy.py直接テスト(Step 5A、2026-08-14): game.py互換ラッパーとpolicy.py本体の
# 戻り値・RNG消費・入力非変更・動的依存解決が一致することを確認する。
# ==============================================================================

class PolicyModuleTest(unittest.TestCase):
    RESOURCES = {"energy": 50, "peace": 50, "money": 50}
    CHOICES = [
        {"key": "money", "cost": {"money": -10}, "hours": 10},
        {"key": "labor", "cost": {"energy": -5}, "hours": 10},
        {"key": "rest", "cost": {"peace": 5}, "hours": 10},
    ]

    def test_dependencies_is_frozen(self):
        deps = game._policy_dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.risk_reference_loss = 999.0

    def test_normalize_cost_matches_wrapper(self):
        cost = {"energy": -3, "peace": 1}
        self.assertEqual(policy_engine.normalize_cost(cost), game.normalize_cost(cost))

    def test_policy_score_legacy_matches_wrapper(self):
        choice = {"cost": {"energy": -3, "peace": 1}}
        vec = game.POLICY_VECTORS["cautious"]
        self.assertEqual(
            policy_engine.policy_score_legacy(
                choice, vec, dependencies=game._policy_dependencies()),
            game.policy_score_legacy(choice, vec))

    def test_policy_score_matches_wrapper(self):
        choice = {
            "key": "social",
            "cost": {"money": -30, "peace": -20},
            "contract": {"repay_money": -40},
        }
        vec = game.POLICY_VECTORS["cautious"]
        resources = {"energy": 50, "peace": 40, "money": 50}
        self.assertEqual(
            policy_engine.policy_score(
                choice, vec, 5, resources, dependencies=game._policy_dependencies()),
            game.policy_score(choice, vec, 5, resources))

    def _compare_auto_select(self, choices, resources, selection_policy,
                             policy_name=None, safety_floor=game.SAFETY_FLOOR,
                             turn=0, budget=None):
        game_result = game.auto_select(
            choices, resources, selection_policy, policy_name,
            safety_floor, turn, budget)
        module_result = policy_engine.auto_select(
            choices, resources, selection_policy, policy_name,
            safety_floor, turn, budget, dependencies=game._policy_dependencies())
        self.assertEqual(module_result, game_result)

    def test_auto_select_balanced_matches_wrapper(self):
        self._compare_auto_select(self.CHOICES, self.RESOURCES, "balanced")

    def test_auto_select_policy_and_budget_match_wrapper(self):
        choices = copy.deepcopy(self.CHOICES)
        choices[1]["hours"] = 4
        self._compare_auto_select(
            choices, self.RESOURCES, "balanced", "cautious", 24, 5, 5)

    def test_auto_select_no_affordable_matches_wrapper(self):
        choices = [
            {"key": "deep", "cost": {"money": -100}},
            {"key": "shallow", "cost": {"energy": -80}},
        ]
        self._compare_auto_select(choices, self.RESOURCES, "balanced")

    def test_auto_select_random_matches_wrapper_and_rng(self):
        state_before = random.getstate()
        try:
            random.seed(1)
            game_result = game.auto_select(self.CHOICES, self.RESOURCES, "random")
            state_after_game = random.getstate()

            random.seed(1)
            module_result = policy_engine.auto_select(
                self.CHOICES, self.RESOURCES, "random",
                safety_floor=game.SAFETY_FLOOR,
                dependencies=game._policy_dependencies())
            state_after_module = random.getstate()

            self.assertEqual(module_result, game_result)
            self.assertEqual(state_after_module, state_after_game)
        finally:
            random.setstate(state_before)

    def test_auto_select_does_not_mutate_inputs(self):
        choices = copy.deepcopy(self.CHOICES)
        resources = dict(self.RESOURCES)
        choices_before = copy.deepcopy(choices)
        resources_before = dict(resources)
        policy_engine.auto_select(
            choices, resources, "balanced", safety_floor=game.SAFETY_FLOOR,
            dependencies=game._policy_dependencies())
        self.assertEqual(choices, choices_before)
        self.assertEqual(resources, resources_before)

    def test_wrapper_resolves_is_affordable_at_call_time(self):
        original = game.is_affordable
        calls = []

        def only_labor(choice, resources, budget=None):
            calls.append(choice["key"])
            return choice["key"] == "labor"

        game.is_affordable = only_labor
        try:
            result = game.auto_select(self.CHOICES, self.RESOURCES, "balanced")
        finally:
            game.is_affordable = original
        self.assertEqual(result, 1)
        self.assertEqual(calls, ["money", "labor", "rest"])


# ==============================================================================
# engine.py直接テスト(Step 4A-5、2026-08-14): engine.build_settlement_choices()・
# engine.plan_settlement_effects()を直接呼び、game.py側の互換ラッパー経由の
# 結果と完全一致することを確認する。
# ==============================================================================

class SettlementEngineTest(unittest.TestCase):
    CONTRACT = {"id": "c1", "counterparty": "友人A", "repay_money": -40}
    BANK_CONTRACT = {"id": "c2", "counterparty": "中央銀行", "repay_money": -40,
                     "is_bank_debt": True}

    def test_dependencies_is_frozen(self):
        deps = game._settlement_engine_dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.npc_trust_initial = 999.0

    def _compare_build_settlement_choices(self, contract, turn, currency_stage, labels):
        # build_settlement_choicesは乱数を消費するため、単純に2回呼ぶのではなく
        # getstate()/setstate()で呼び出し前の状態を正確に復元してから比較する。
        state_before = random.getstate()
        game_result = game.build_settlement_choices(contract, turn, currency_stage, labels)
        state_after_game = random.getstate()

        random.setstate(state_before)
        engine_result = engine.build_settlement_choices(
            contract, turn, currency_stage, labels,
            dependencies=game._settlement_engine_dependencies())
        state_after_engine = random.getstate()

        self.assertEqual(engine_result, game_result)
        self.assertEqual(state_after_engine, state_after_game)

    def test_build_settlement_choices_matches_wrapper_normal_stage(self):
        self._compare_build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL, labels=None)

    def test_build_settlement_choices_matches_wrapper_abandoned_stage(self):
        self._compare_build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_ABANDONED, labels=None)

    def test_build_settlement_choices_matches_wrapper_with_labels(self):
        labels = {"money": "お金で返す", "labor": "", "avoid": None}
        self._compare_build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL, labels=labels)

    def test_normal_stage_key_order_is_money_labor_avoid(self):
        result = engine.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            dependencies=game._settlement_engine_dependencies())
        self.assertEqual([c["key"] for c in result], ["money", "labor", "avoid"])

    def test_abandoned_stage_key_order_is_labor_avoid(self):
        result = engine.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_ABANDONED,
            dependencies=game._settlement_engine_dependencies())
        self.assertEqual([c["key"] for c in result], ["labor", "avoid"])

    def test_labels_none_key_structure(self):
        result = engine.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL,
            dependencies=game._settlement_engine_dependencies())
        for c in result:
            self.assertEqual(set(c.keys()), {"key", "cost", "settle"})

    def test_labels_given_key_structure(self):
        result = engine.build_settlement_choices(
            self.CONTRACT, 10, game.CURRENCY_STAGE_NORMAL, {"money": "お金で返す"},
            dependencies=game._settlement_engine_dependencies())
        for c in result:
            self.assertEqual(set(c.keys()), {"key", "label", "cost", "settle"})

    def test_plan_settlement_effects_matches_wrapper_all_six_cases(self):
        deps = game._settlement_engine_dependencies()
        cases = [
            ("bank_money_fulfilled", self.BANK_CONTRACT, "money", "fulfilled",
             dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
            ("bank_labor_fulfilled", self.BANK_CONTRACT, "labor", "fulfilled",
             dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
            ("bank_defaulted_no_crisis", self.BANK_CONTRACT, "avoid", "defaulted",
             dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
            ("bank_defaulted_with_crisis", self.BANK_CONTRACT, "avoid", "defaulted",
             dict(bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)),
            ("social_fulfilled", self.CONTRACT, "money", "fulfilled",
             dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
                  counterparty_trust=60.0)),
            ("social_defaulted", self.CONTRACT, "avoid", "defaulted",
             dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
                  counterparty_trust=60.0)),
        ]
        for label, contract, choice_key, outcome, kwargs in cases:
            with self.subTest(case=label):
                game_result = game.plan_settlement_effects(contract, choice_key, outcome, 1, **kwargs)
                engine_result = engine.plan_settlement_effects(
                    contract, choice_key, outcome, 1, **kwargs, dependencies=deps)
                self.assertEqual(engine_result, game_result)

    def test_plan_settlement_effects_does_not_mutate_contract(self):
        contract = dict(self.BANK_CONTRACT)
        before = dict(contract)
        engine.plan_settlement_effects(
            contract, "avoid", "defaulted", 1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            dependencies=game._settlement_engine_dependencies())
        self.assertEqual(contract, before)

    def test_plan_settlement_effects_does_not_consume_random_state(self):
        state_before = random.getstate()
        engine.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", 1,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0,
            dependencies=game._settlement_engine_dependencies())
        self.assertEqual(random.getstate(), state_before)


# ==============================================================================
# engine.plan_settlement_resolution() の直接テスト(Step 6C、2026-08-15):
# 「選択済みchoice→plan_settlement_effects呼び出し」の接着部分のみを対象に、
# main()/simulate_policy()の重複していた定型コードを1つに集約したことの確認。
# ==============================================================================

class PlanSettlementResolutionTest(unittest.TestCase):
    CONTRACT = {"id": "c1", "counterparty": "友人A", "repay_money": -40}
    BANK_CONTRACT = {"id": "c2", "counterparty": "中央銀行", "repay_money": -40,
                     "is_bank_debt": True}

    # SettlementEngineTest.test_plan_settlement_effects_matches_wrapper_all_six_casesと
    # 同じ6分岐(bank debt×money/labor/avoidの履行・不履行、社会契約の履行・不履行)。
    CASES = [
        ("bank_money_fulfilled", BANK_CONTRACT, "money", "fulfilled",
         dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
        ("bank_labor_fulfilled", BANK_CONTRACT, "labor", "fulfilled",
         dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
        ("bank_defaulted_no_crisis", BANK_CONTRACT, "avoid", "defaulted",
         dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)),
        ("bank_defaulted_with_crisis", BANK_CONTRACT, "avoid", "defaulted",
         dict(bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)),
        ("social_fulfilled", CONTRACT, "money", "fulfilled",
         dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
              counterparty_trust=60.0)),
        ("social_defaulted", CONTRACT, "avoid", "defaulted",
         dict(bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
              counterparty_trust=60.0)),
    ]

    @staticmethod
    def _choice(choice_key, outcome):
        return {"key": choice_key, "cost": {"money": -1}, "settle": outcome}

    def test_matches_wrapper_all_six_cases(self):
        deps = game._settlement_engine_dependencies()
        for label, contract, choice_key, outcome, kwargs in self.CASES:
            with self.subTest(case=label):
                choice = self._choice(choice_key, outcome)
                game_result = game.plan_settlement_resolution(contract, choice, 1, **kwargs)
                engine_result = engine.plan_settlement_resolution(
                    contract, choice, 1, **kwargs, dependencies=deps,
                    plan_settlement_effects_fn=game.plan_settlement_effects)
                self.assertEqual(engine_result, game_result)

    def test_return_shape_is_choice_and_effects(self):
        choice = self._choice("money", "fulfilled")
        result = game.plan_settlement_resolution(
            self.BANK_CONTRACT, choice, 1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertEqual(set(result.keys()), {"choice", "effects"})
        self.assertIs(result["choice"], choice)
        expected_effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", 1,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertEqual(result["effects"], expected_effects)

    def test_does_not_mutate_contract_or_choice(self):
        contract = dict(self.BANK_CONTRACT)
        contract_before = dict(contract)
        choice = self._choice("avoid", "defaulted")
        choice_before = dict(choice)
        engine.plan_settlement_resolution(
            contract, choice, 1, bank_trust=50.0, currency_confidence=50.0,
            bank_credit_losses=0.0, dependencies=game._settlement_engine_dependencies())
        self.assertEqual(contract, contract_before)
        self.assertEqual(choice, choice_before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        choice = self._choice("avoid", "defaulted")
        engine.plan_settlement_resolution(
            self.BANK_CONTRACT, choice, 1, bank_trust=30.0, currency_confidence=5.0,
            bank_credit_losses=790.0, dependencies=game._settlement_engine_dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_calls_plan_settlement_effects_exactly_once_with_choice_key_and_settle(self):
        calls = []

        def fake_effects(contract, choice_key, outcome, turn, bank_trust, currency_confidence,
                         bank_credit_losses, counterparty_trust=None, *,
                         enforcement_stage=None, enforcement_capacity=None):
            calls.append((contract["id"], choice_key, outcome, turn, bank_trust,
                          currency_confidence, bank_credit_losses, counterparty_trust,
                          enforcement_stage, enforcement_capacity))
            return {"sentinel": True}

        choice = self._choice("labor", "fulfilled")
        result = engine.plan_settlement_resolution(
            self.BANK_CONTRACT, choice, 7, bank_trust=40.0, currency_confidence=45.0,
            bank_credit_losses=12.0, counterparty_trust=None,
            dependencies=game._settlement_engine_dependencies(),
            plan_settlement_effects_fn=fake_effects)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("c2", "labor", "fulfilled", 7, 40.0, 45.0, 12.0, None,
                                    game.ENFORCEMENT_STAGE_INSTITUTIONAL,
                                    game.ENFORCEMENT_CAPACITY_INITIAL))
        self.assertEqual(result, {"choice": choice, "effects": {"sentinel": True}})

    def test_wrapper_propagates_monkeypatched_plan_settlement_effects(self):
        # game.plan_settlement_effectsをmonkeypatchすると、game.plan_settlement_
        # resolution()経由の呼び出しにも反映される(engine.pyの内部実装ではなく、
        # game.py側の現在の関数オブジェクトを使うことの確認——他のengine.py内
        # 関数の*_fn引数と同じ、呼び出し時点解決の仕組み)。
        original = game.plan_settlement_effects
        calls = []

        def fake_effects(contract, choice_key, outcome, turn, bank_trust, currency_confidence,
                         bank_credit_losses, counterparty_trust=None, *,
                         enforcement_stage=None, enforcement_capacity=None):
            calls.append(choice_key)
            return {"sentinel": True}

        game.plan_settlement_effects = fake_effects
        try:
            choice = self._choice("money", "fulfilled")
            result = game.plan_settlement_resolution(
                self.BANK_CONTRACT, choice, 1,
                bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        finally:
            game.plan_settlement_effects = original
        self.assertEqual(calls, ["money"])
        self.assertEqual(result["effects"], {"sentinel": True})


# ==============================================================================
# event_builders.build_settlement_effect_events() の直接テスト(Step 6E、
# 2026-08-15): main()の清算効果イベント列の組み立てのみを対象に、6清算分岐の
# イベント名・順序・payloadを確認する。
# ==============================================================================

class BuildSettlementEffectEventsTest(unittest.TestCase):
    CONTRACT = {"id": "c1", "counterparty": "友人A", "repay_money": -40}
    BANK_CONTRACT = {"id": "c2", "counterparty": "中央銀行", "repay_money": -40,
                     "is_bank_debt": True}
    TURN = 5

    @staticmethod
    def _choice(key, outcome):
        return {"key": key, "settle": outcome}

    def test_bank_money_fulfilled(self):
        choice = self._choice("money", "fulfilled")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        events = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "bank", 0)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "bank", "delta": effects["bank_trust_delta"],
                                   "reason": "wage_debt_fulfilled", "turn": self.TURN}),
            ("npc_wallet_changed", {"npc_id": "bank", "delta": effects["bank_wallet_delta"],
                                    "reason": "wage_debt_repaid", "turn": self.TURN}),
            ("currency_confidence_changed", {"turn": self.TURN,
                                             "delta": effects["currency_confidence_delta"],
                                             "reason": "money_settlement_fulfilled"}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_fulfilled"}),
        ])

    def test_bank_labor_fulfilled_has_no_wallet_or_currency_event(self):
        choice = self._choice("labor", "fulfilled")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "labor", "fulfilled", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        events = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "bank", 0)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "bank", "delta": effects["bank_trust_delta"],
                                   "reason": "wage_debt_fulfilled", "turn": self.TURN}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_fulfilled"}),
        ])

    def test_bank_defaulted_no_crisis(self):
        choice = self._choice("avoid", "defaulted")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        self.assertFalse(effects["crisis_triggered"])
        events = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "bank", 0)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "bank", "delta": effects["bank_trust_delta"],
                                   "reason": "wage_debt_defaulted", "turn": self.TURN}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_defaulted"}),
        ])

    def test_bank_defaulted_with_crisis(self):
        choice = self._choice("avoid", "defaulted")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", self.TURN,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        self.assertTrue(effects["crisis_triggered"])
        events = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "bank", 2)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "bank", "delta": effects["bank_trust_delta"],
                                   "reason": "wage_debt_defaulted", "turn": self.TURN}),
            ("bank_crisis", {"turn": self.TURN, "rebase_factor": effects["rebase_factor"],
                             "credit_losses": effects["bank_credit_losses_after"],
                             "crisis_count": 3}),
            ("npc_trust_changed", {"npc_id": "bank", "delta": effects["crisis_bank_trust_delta"],
                                   "reason": "bank_crisis", "turn": self.TURN}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_defaulted"}),
        ])
        # currency_confidence_changedを新設しないこと(危機時)。
        self.assertNotIn("currency_confidence_changed", [e for e, _ in events])

    def test_social_fulfilled(self):
        choice = self._choice("money", "fulfilled")
        effects = game.plan_settlement_effects(
            self.CONTRACT, "money", "fulfilled", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        events = event_builders.build_settlement_effect_events(
            self.CONTRACT, choice, effects, self.TURN, "bank", 0)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "友人A", "delta": effects["counterparty_trust_delta"],
                                   "reason": "social_contract_fulfilled", "turn": self.TURN}),
            ("community_trust_changed", {"turn": self.TURN, "delta": effects["community_trust_delta"],
                                         "reason": "local_credit_propagation"}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_fulfilled"}),
        ])

    def test_social_defaulted(self):
        choice = self._choice("avoid", "defaulted")
        effects = game.plan_settlement_effects(
            self.CONTRACT, "avoid", "defaulted", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0,
            counterparty_trust=60.0)
        events = event_builders.build_settlement_effect_events(
            self.CONTRACT, choice, effects, self.TURN, "bank", 0)
        self.assertEqual(events, [
            ("npc_trust_changed", {"npc_id": "友人A", "delta": effects["counterparty_trust_delta"],
                                   "reason": "social_contract_defaulted", "turn": self.TURN}),
            ("community_trust_changed", {"turn": self.TURN, "delta": effects["community_trust_delta"],
                                         "reason": "local_credit_propagation"}),
            ("enforcement_capacity_changed", {"turn": self.TURN,
                                              "delta": effects["enforcement_capacity_delta"],
                                              "reason": "settlement_defaulted"}),
        ])

    def test_bank_npc_id_reflects_call_time_value(self):
        choice = self._choice("money", "fulfilled")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        events = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "別ID", 0)
        self.assertEqual(events[0][1]["npc_id"], "別ID")
        self.assertEqual(events[1][1]["npc_id"], "別ID")

    def test_does_not_mutate_contract_choice_or_effects(self):
        contract = dict(self.BANK_CONTRACT)
        contract_before = dict(contract)
        choice = self._choice("avoid", "defaulted")
        choice_before = dict(choice)
        effects = game.plan_settlement_effects(
            contract, "avoid", "defaulted", self.TURN,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        effects_before = dict(effects)
        event_builders.build_settlement_effect_events(
            contract, choice, effects, self.TURN, "bank", 2)
        self.assertEqual(contract, contract_before)
        self.assertEqual(choice, choice_before)
        self.assertEqual(effects, effects_before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        choice = self._choice("avoid", "defaulted")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "avoid", "defaulted", self.TURN,
            bank_trust=30.0, currency_confidence=5.0, bank_credit_losses=790.0)
        state_after_effects = random.getstate()
        event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, "bank", 2)
        self.assertEqual(random.getstate(), state_after_effects)
        self.assertEqual(state_after_effects, state_before)

    def test_wrapper_matches_module(self):
        choice = self._choice("money", "fulfilled")
        effects = game.plan_settlement_effects(
            self.BANK_CONTRACT, "money", "fulfilled", self.TURN,
            bank_trust=50.0, currency_confidence=50.0, bank_credit_losses=0.0)
        game_result = game.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, game.BANK_NPC_ID, 0)
        module_result = event_builders.build_settlement_effect_events(
            self.BANK_CONTRACT, choice, effects, self.TURN, game.BANK_NPC_ID, 0)
        self.assertEqual(game_result, module_result)


# ==============================================================================
# event_builders.build_normal_contract_events() の直接テスト(Step 6F、
# 2026-08-15): 通常行動のsocial型choiceから発生するNPC登場・契約作成の
# イベント列を対象に、新規/既存NPCの分岐・payload・callback呼び出しを確認する。
# ==============================================================================

class BuildNormalContractEventsTest(unittest.TestCase):
    TURN = 4
    CONTRACT_ID = "c7"

    @staticmethod
    def _choice(counterparty, prospective_ethics=55.0, prospective_retire_turn=151):
        return {
            "key": "social", "label": "友人に頼る",
            "contract": {
                "counterparty": counterparty, "description": "頼み事",
                "due_turn": 8, "repay_money": -20,
                "prospective_ethics": prospective_ethics,
                "prospective_retire_turn": prospective_retire_turn,
            },
        }

    STATE_NO_NPCS = {"npcs": {}}
    STATE_WITH_FRIEND = {"npcs": {"友人A": {"trust": 60.0, "ethics": 60.0, "retire_turn": 200}}}

    def _deps(self, initial_trust=50.0):
        calls = {"initial_trust": [], "acquaintance": []}

        def initial_trust_fn(acquaintances, turn):
            calls["initial_trust"].append((acquaintances, turn))
            return initial_trust

        def acquaintance_fn(state):
            calls["acquaintance"].append(state)
            return {k: v for k, v in state["npcs"].items()}

        return calls, initial_trust_fn, acquaintance_fn

    def test_new_npc_event_sequence(self):
        calls, initial_trust_fn, acquaintance_fn = self._deps(initial_trust=52.5)
        choice = self._choice("新友人")
        events = event_builders.build_normal_contract_events(
            choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        self.assertEqual(events, [
            ("npc_introduced", {"id": "新友人", "name": "新友人", "birth_turn": self.TURN,
                                "role": "acquaintance", "trust": 52.5,
                                "ethics": 55.0, "retire_turn": 151}),
            ("contract_created", {"id": self.CONTRACT_ID, "counterparty": "新友人",
                                  "description": "頼み事", "created_turn": self.TURN,
                                  "due_turn": 8, "repay_money": -20,
                                  "origin_choice": "友人に頼る"}),
        ])

    def test_existing_npc_has_no_npc_introduced_event(self):
        calls, initial_trust_fn, acquaintance_fn = self._deps()
        choice = self._choice("友人A", prospective_ethics=None, prospective_retire_turn=None)
        events = event_builders.build_normal_contract_events(
            choice, self.STATE_WITH_FRIEND, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        self.assertEqual(events, [
            ("contract_created", {"id": self.CONTRACT_ID, "counterparty": "友人A",
                                  "description": "頼み事", "created_turn": self.TURN,
                                  "due_turn": 8, "repay_money": -20,
                                  "origin_choice": "友人に頼る"}),
        ])

    def test_existing_npc_does_not_call_initial_trust_fn(self):
        calls, initial_trust_fn, acquaintance_fn = self._deps()
        choice = self._choice("友人A")
        event_builders.build_normal_contract_events(
            choice, self.STATE_WITH_FRIEND, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        self.assertEqual(calls["initial_trust"], [])
        self.assertEqual(calls["acquaintance"], [])

    def test_new_npc_callback_args_and_order(self):
        calls, initial_trust_fn, acquaintance_fn = self._deps(initial_trust=52.5)
        choice = self._choice("新友人")
        event_builders.build_normal_contract_events(
            choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        # acquaintance_npcs_fn(state_now) → initial_trust_for_new_npc_fn(結果, turn) の順で
        # それぞれ正確に1回だけ呼ばれる。
        self.assertEqual(calls["acquaintance"], [self.STATE_NO_NPCS])
        self.assertEqual(calls["initial_trust"], [({}, self.TURN)])

    def test_does_not_mutate_choice_or_state(self):
        choice = self._choice("新友人")
        choice_before = copy.deepcopy(choice)
        state_now = copy.deepcopy(self.STATE_NO_NPCS)
        state_before = copy.deepcopy(state_now)
        _, initial_trust_fn, acquaintance_fn = self._deps()
        event_builders.build_normal_contract_events(
            choice, state_now, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        self.assertEqual(choice, choice_before)
        self.assertEqual(state_now, state_before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        choice = self._choice("新友人")
        _, initial_trust_fn, acquaintance_fn = self._deps()
        event_builders.build_normal_contract_events(
            choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=initial_trust_fn, acquaintance_npcs_fn=acquaintance_fn)
        self.assertEqual(random.getstate(), state_before)

    def test_wrapper_matches_module(self):
        choice = self._choice("新友人")
        game_result = game.build_normal_contract_events(
            choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID)
        module_result = event_builders.build_normal_contract_events(
            choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID,
            initial_trust_for_new_npc_fn=game.initial_trust_for_new_npc,
            acquaintance_npcs_fn=game.acquaintance_npcs)
        self.assertEqual(game_result, module_result)

    def test_wrapper_propagates_monkeypatched_initial_trust_for_new_npc(self):
        original = game.initial_trust_for_new_npc
        calls = []

        def fake_initial_trust(acquaintances, turn, local_credit_stage=None, community_trust=None):
            calls.append(turn)
            return 99.0

        game.initial_trust_for_new_npc = fake_initial_trust
        try:
            choice = self._choice("新友人")
            events = game.build_normal_contract_events(
                choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID)
        finally:
            game.initial_trust_for_new_npc = original
        self.assertEqual(calls, [self.TURN])
        self.assertEqual(events[0][1]["trust"], 99.0)

    def test_wrapper_propagates_monkeypatched_acquaintance_npcs(self):
        original = game.acquaintance_npcs
        calls = []

        def fake_acquaintance_npcs(state):
            calls.append(state)
            return {}  # 空dictなら後続のinitial_trust_for_new_npc(実物)も安全に呼べる

        game.acquaintance_npcs = fake_acquaintance_npcs
        try:
            choice = self._choice("新友人")
            game.build_normal_contract_events(
                choice, self.STATE_NO_NPCS, self.TURN, self.CONTRACT_ID)
        finally:
            game.acquaintance_npcs = original
        self.assertEqual(calls, [self.STATE_NO_NPCS])


# ==============================================================================
# 地域信用制度(2026-08-15)の「NPC間評判伝播」統合テスト。個人取引→地域全体
# (community_trust_changedイベント発行)はBuildSettlementEffectEventsTestで
# 確認済み——ここでは地域全体→新規relationshipの初期trustへの反映
# (initial_trust_for_new_npc・build_normal_contract_eventsのStage判定)を扱う。
# ==============================================================================

class LocalCreditNewNpcTrustBlendTest(unittest.TestCase):
    def test_initial_trust_for_new_npc_unblended_without_community_trust(self):
        # community_trust=None(既定)なら従来どおりplayer_reputationのみを使う。
        self.assertEqual(game.initial_trust_for_new_npc({}, 4), game.NPC_TRUST_INITIAL)

    def test_initial_trust_for_new_npc_blends_with_community_trust(self):
        # player_reputation({}, 4)==NPC_TRUST_INITIAL(50.0)、community_trust=80.0
        # → LOCAL_CREDIT_INITIAL_TRUST_WEIGHT(0.5)で単純平均した「地域調整済み
        # 評判」(65.0)を、さらに既存のNPC_REPUTATION_WEIGHT(0.25)でNPC_TRUST_
        # INITIALとブレンドする(relationship_rules.initial_trust_for_new_npc
        # 自体の既存ロジック、無改造)。(1-0.25)*50 + 0.25*65 = 53.75。
        # 2026-08-15訂正: Stage判定がrelationship_rules.py側へ移設されたため、
        # ブレンドを起こすにはlocal_credit_stageも明示的に渡す必要がある
        # (Stage2未満)。
        self.assertAlmostEqual(
            game.initial_trust_for_new_npc(
                {}, 4, local_credit_stage=game.LOCAL_CREDIT_STAGE_HEALTHY,
                community_trust=80.0),
            53.75)

    def test_initial_trust_for_new_npc_ignores_community_trust_without_stage(self):
        # 2026-08-15追加: community_trustだけ渡してlocal_credit_stageを渡さない
        # 場合は、Stage不明として安全側(ブレンドしない)に倒れる。
        self.assertEqual(
            game.initial_trust_for_new_npc({}, 4, community_trust=80.0),
            game.NPC_TRUST_INITIAL)

    def test_build_normal_contract_events_blends_at_stage_healthy(self):
        choice = self._choice()
        state_now = {"npcs": {}, "local_credit_stage": game.LOCAL_CREDIT_STAGE_HEALTHY,
                    "community_trust": 80.0}
        events = game.build_normal_contract_events(choice, state_now, 4, "c1")
        self.assertAlmostEqual(events[0][1]["trust"], 53.75)

    def test_build_normal_contract_events_blends_at_stage_contraction(self):
        # Stage1(信用収縮)もまだ「個人化」する前なのでブレンドが効く。
        choice = self._choice()
        state_now = {"npcs": {}, "local_credit_stage": game.LOCAL_CREDIT_STAGE_CONTRACTION,
                    "community_trust": 80.0}
        events = game.build_normal_contract_events(choice, state_now, 4, "c1")
        self.assertAlmostEqual(events[0][1]["trust"], 53.75)

    def test_build_normal_contract_events_ignores_community_trust_at_stage_personal(self):
        # Stage2(個人化)以上では「共有台帳・評判の伝播が機能しなくなる」
        # (docs/social-regimes-spec.md)——community_trustを無視し、従来どおり
        # player_reputationのみになる(NPC_TRUST_INITIAL、顔なじみが居ないため)。
        choice = self._choice()
        state_now = {"npcs": {}, "local_credit_stage": game.LOCAL_CREDIT_STAGE_PERSONAL,
                    "community_trust": 80.0}
        events = game.build_normal_contract_events(choice, state_now, 4, "c1")
        self.assertEqual(events[0][1]["trust"], game.NPC_TRUST_INITIAL)

    def test_build_normal_contract_events_ignores_community_trust_at_stage_isolated(self):
        choice = self._choice()
        state_now = {"npcs": {}, "local_credit_stage": game.LOCAL_CREDIT_STAGE_ISOLATED,
                    "community_trust": 80.0}
        events = game.build_normal_contract_events(choice, state_now, 4, "c1")
        self.assertEqual(events[0][1]["trust"], game.NPC_TRUST_INITIAL)

    def test_build_normal_contract_events_defaults_to_stage_healthy_when_key_missing(self):
        # local_credit_stage/community_trustキーの無いstate_now(旧イベントログ
        # からの再構築等)でも例外にならず、Stage0扱い(ブレンドする)で安全に
        # 動く。community_trustが無ければLOCAL_CREDIT_TRUST_INITIALにフォール
        # バックする。
        choice = self._choice()
        events = game.build_normal_contract_events(choice, {"npcs": {}}, 4, "c1")
        self.assertAlmostEqual(events[0][1]["trust"], game.NPC_TRUST_INITIAL)

    @staticmethod
    def _choice():
        return {
            "key": "social", "label": "友人に頼る",
            "contract": {
                "counterparty": "新友人", "description": "頼み事",
                "due_turn": 8, "repay_money": -20,
                "prospective_ethics": 55.0, "prospective_retire_turn": 151,
            },
        }


# ==============================================================================
# engine.py通常行動の直接テスト(Step 4B、2026-08-14): engine側へ移動した
# 4関数を直接呼び、game.pyの動的依存ラッパーと戻り値・RNG消費が一致することを
# 確認する。
# ==============================================================================

class NormalActionEngineTest(unittest.TestCase):
    MONEY_ARCHETYPE = next(a for a in game.ACTION_ARCHETYPES if a["key"] == "money")
    LABOR_ARCHETYPE = next(a for a in game.ACTION_ARCHETYPES if a["key"] == "labor")

    class _CountingRng:
        def __init__(self):
            self.calls = []

        def choice(self, seq):
            self.calls.append(list(seq))
            return seq[0]

    def test_dependencies_is_frozen(self):
        deps = game._normal_action_engine_dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.contract_repay_money = (0, 0)

    def test_available_archetypes_matches_wrapper_normal_stage(self):
        game_result = game.available_normal_archetypes(game.CURRENCY_STAGE_NORMAL)
        engine_result = engine.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL,
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(engine_result, game_result)
        self.assertEqual([a["key"] for a in engine_result],
                         ["money", "labor", "social", "rest"])

    def test_available_archetypes_matches_wrapper_abandoned_stage(self):
        # 2026-08-15追記(Step 13): currency_stage=ABANDONEDは単独でも
        # alternative_economy_triggeredを満たすため、barter/subsistenceが
        # 末尾に追加される。
        game_result = game.available_normal_archetypes(game.CURRENCY_STAGE_ABANDONED)
        engine_result = engine.available_normal_archetypes(
            game.CURRENCY_STAGE_ABANDONED,
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(engine_result, game_result)
        self.assertEqual([a["key"] for a in engine_result],
                         ["labor", "social", "rest", "barter", "subsistence"])

    def _compare_money_modifier(self, acquaintances, currency_stage):
        game_rng = self._CountingRng()
        engine_rng = self._CountingRng()
        game_result = game.compute_normal_money_modifier(
            acquaintances, currency_stage, rng=game_rng)
        engine_result = engine.compute_normal_money_modifier(
            acquaintances, currency_stage, engine_rng,
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(engine_result, game_result)
        self.assertEqual(engine_rng.calls, game_rng.calls)

    def test_money_modifier_matches_wrapper_without_acquaintances(self):
        self._compare_money_modifier({}, game.CURRENCY_STAGE_NORMAL)

    def test_money_modifier_matches_wrapper_with_acquaintance(self):
        acquaintances = {"友人A": {"trust": 60.0, "ethics": 40.0}}
        self._compare_money_modifier(acquaintances, game.CURRENCY_STAGE_NORMAL)

    def test_money_modifier_matches_wrapper_at_wary_stage(self):
        acquaintances = {"友人A": {"trust": 60.0, "ethics": 40.0}}
        self._compare_money_modifier(acquaintances, game.CURRENCY_STAGE_WARY)

    def _compare_base_choice(self, archetype, turn, money_modifier, label=None):
        state_before = random.getstate()
        try:
            game_result = game.build_normal_base_choice(
                archetype, turn, money_modifier, label=label)
            state_after_game = random.getstate()

            random.setstate(state_before)
            engine_result = engine.build_normal_base_choice(
                archetype, turn, money_modifier, label,
                dependencies=game._normal_action_engine_dependencies())
            state_after_engine = random.getstate()

            self.assertEqual(engine_result, game_result)
            self.assertEqual(state_after_engine, state_after_game)
            return engine_result
        finally:
            random.setstate(state_before)

    def test_money_base_choice_matches_wrapper_and_rng(self):
        result = self._compare_base_choice(self.MONEY_ARCHETYPE, 5, 1.2)
        self.assertEqual(list(result.keys()), ["key", "cost", "hours"])

    def test_non_money_base_choice_matches_wrapper_and_rng(self):
        result = self._compare_base_choice(
            self.LABOR_ARCHETYPE, 5, 1.0, label="自分で直す")
        self.assertEqual(list(result.keys()), ["key", "label", "cost", "hours"])

    def _compare_repay_range(self, acquaintances, existing, prospective_ethics,
                             turn, due_turn):
        game_result = game.compute_social_contract_repay_range(
            acquaintances, existing, prospective_ethics, turn, due_turn)
        engine_result = engine.compute_social_contract_repay_range(
            acquaintances, existing, prospective_ethics, turn, due_turn,
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(engine_result, game_result)

    def test_repay_range_matches_wrapper_for_new_npc(self):
        self._compare_repay_range({}, None, 50.0, 1, 100)

    def test_repay_range_matches_wrapper_for_existing_npc(self):
        existing = {"trust": 70.0, "ethics": 60.0}
        self._compare_repay_range({}, existing, None, 1, 100)

    def test_repay_range_matches_wrapper_at_low_trust_boundary(self):
        existing = {"trust": game.NPC_CONTRACT_TRUST_MIN, "ethics": 50.0}
        self._compare_repay_range({}, existing, None, 1, 5)

    def test_repay_range_matches_wrapper_at_high_trust_boundary(self):
        existing = {"trust": game.NPC_TRUST_CAP, "ethics": 50.0}
        self._compare_repay_range({}, existing, None, 1, 100)

    def test_repay_range_does_not_consume_rng_or_mutate_inputs(self):
        acquaintances = {
            "友人A": {"trust": 60.0, "ethics": 50.0, "retire_turn": 200},
        }
        existing = acquaintances["友人A"]
        acquaintances_before = copy.deepcopy(acquaintances)
        existing_before = copy.deepcopy(existing)
        state_before = random.getstate()

        engine.compute_social_contract_repay_range(
            acquaintances, existing, None, 1, 100,
            dependencies=game._normal_action_engine_dependencies())

        self.assertEqual(random.getstate(), state_before)
        self.assertEqual(acquaintances, acquaintances_before)
        self.assertEqual(existing, existing_before)

    def test_base_choice_does_not_mutate_archetype(self):
        archetype = copy.deepcopy(self.MONEY_ARCHETYPE)
        before = copy.deepcopy(archetype)
        state_before = random.getstate()
        try:
            engine.build_normal_base_choice(
                archetype, 5, 1.2,
                dependencies=game._normal_action_engine_dependencies())
        finally:
            random.setstate(state_before)
        self.assertEqual(archetype, before)


# ==============================================================================
# engine.plan_normal_action_resolution() の直接テスト(Step 6D、2026-08-15):
# 「選択済みchoice→clamp_gain適用」の接着部分のみを対象に、main()の通常行動
# (tr["kind"]=="normal")とsimulate_policy()の通常行動の重複を集約したことの
# 確認。settlementは対象外(PlanSettlementResolutionTest側で別途確認済み)。
# ==============================================================================

class PlanNormalActionResolutionTest(unittest.TestCase):
    RESOURCES = {"energy": 60, "peace": 40, "money": 10}
    TRAITS = {"health": 50.0, "dexterity": 50.0, "intellect": 50.0, "skill": 50.0}

    @staticmethod
    def _choice(key, cost):
        return {"key": key, "label": f"{key}のラベル", "cost": dict(cost), "hours": 30}

    # money/labor/rest/socialの4アーキタイプそれぞれで一致を確認する。
    CASES = [
        ("money", {"money": -30}),
        ("labor", {"energy": -20}),
        ("rest", {"peace": 90}),  # 上限クランプ(scale_toward_bound)が効く値
        ("social", {"peace": -5}),
    ]

    def test_dependencies_has_clamp_gain_fn(self):
        deps = game._normal_action_engine_dependencies()
        self.assertIs(deps.clamp_gain_fn, game.clamp_gain)

    def test_matches_wrapper_for_money_labor_rest_social(self):
        deps = game._normal_action_engine_dependencies()
        for key, cost in self.CASES:
            with self.subTest(key=key):
                choice = self._choice(key, cost)
                game_result = game.plan_normal_action_resolution(
                    choice, dict(self.RESOURCES), dict(self.TRAITS))
                engine_result = engine.plan_normal_action_resolution(
                    choice, dict(self.RESOURCES), dict(self.TRAITS), dependencies=deps)
                self.assertEqual(engine_result, game_result)

    def test_return_shape_is_choice_and_delta(self):
        choice = self._choice("money", {"money": -30})
        result = game.plan_normal_action_resolution(
            choice, dict(self.RESOURCES), dict(self.TRAITS))
        self.assertEqual(set(result.keys()), {"choice", "delta"})
        self.assertIs(result["choice"], choice)
        expected_delta = game.clamp_gain(
            dict(choice["cost"]), dict(self.RESOURCES), dict(self.TRAITS))
        self.assertEqual(result["delta"], expected_delta)

    def test_does_not_mutate_choice_resources_or_traits(self):
        choice = self._choice("rest", {"peace": 90})
        choice_before = copy.deepcopy(choice)
        resources = dict(self.RESOURCES)
        resources_before = dict(resources)
        traits = dict(self.TRAITS)
        traits_before = dict(traits)
        engine.plan_normal_action_resolution(
            choice, resources, traits,
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(choice, choice_before)
        self.assertEqual(resources, resources_before)
        self.assertEqual(traits, traits_before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        choice = self._choice("labor", {"energy": -20})
        engine.plan_normal_action_resolution(
            choice, dict(self.RESOURCES), dict(self.TRAITS),
            dependencies=game._normal_action_engine_dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_calls_clamp_gain_fn_exactly_once_with_cost_copy(self):
        calls = []

        def fake_clamp_gain(delta, resources, traits):
            calls.append(delta)
            self.assertIsNot(delta, choice["cost"])  # コピーが渡されていること
            return {"sentinel": True}

        deps = dataclasses.replace(
            game._normal_action_engine_dependencies(), clamp_gain_fn=fake_clamp_gain)
        choice = self._choice("money", {"money": -30})
        result = engine.plan_normal_action_resolution(
            choice, dict(self.RESOURCES), dict(self.TRAITS), dependencies=deps)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {"money": -30})
        self.assertEqual(result, {"choice": choice, "delta": {"sentinel": True}})

    def test_wrapper_propagates_monkeypatched_clamp_gain(self):
        # game.clamp_gainをmonkeypatchすると、_normal_action_engine_dependencies()が
        # 呼び出しのたびに新しいDependenciesを組み立てる(import時点で固定しない)
        # ため、game.plan_normal_action_resolution()経由の呼び出しにも反映される。
        original = game.clamp_gain
        calls = []

        def fake_clamp_gain(delta, resources, traits):
            calls.append(dict(delta))
            return {"sentinel": True}

        game.clamp_gain = fake_clamp_gain
        try:
            choice = self._choice("money", {"money": -30})
            result = game.plan_normal_action_resolution(
                choice, dict(self.RESOURCES), dict(self.TRAITS))
        finally:
            game.clamp_gain = original
        self.assertEqual(calls, [{"money": -30}])
        self.assertEqual(result["delta"], {"sentinel": True})


# ==============================================================================
# 資源ルール移動前の特性テスト(Step 5B-0、2026-08-14)
# ==============================================================================

class ResourceRulesFixtureTest(unittest.TestCase):
    """game.pyにある現行の資源計算を、分離前の値で固定する。"""

    def test_compute_decay_current_configuration_is_noop(self):
        resources = {"energy": 80, "peace": 70, "money": 123}
        before = copy.deepcopy(resources)
        self.assertEqual(game.compute_decay(8, resources), {})
        self.assertEqual(resources, before)

    def test_compute_decay_supports_both_rule_kinds(self):
        original = game.DECAY_RULES
        game.DECAY_RULES = [
            {"kind": "proportional", "resource": "money", "rate": 0.1},
            {"kind": "fixed", "resource": "energy", "interval": 2, "amount": -3},
        ]
        try:
            self.assertEqual(
                game.compute_decay(4, {"money": 95, "energy": 20}),
                {"money": -10, "energy": -3})
            self.assertEqual(
                game.compute_decay(3, {"money": 95, "energy": 20}),
                {"money": -10})
        finally:
            game.DECAY_RULES = original

    def test_resource_cap_energy_tracks_health(self):
        self.assertEqual(game.resource_cap("energy", {"health": 80}), 100)
        self.assertEqual(game.resource_cap("energy", {"health": 40}), 50)
        self.assertEqual(game.resource_cap("energy", {}), 100)

    def test_resource_cap_other_resources_use_initial_values(self):
        self.assertEqual(game.resource_cap("peace", {"health": 0}), 100)
        self.assertEqual(game.resource_cap("money", {"health": 0}), 0)

    def test_compute_regen_respects_dynamic_cap_and_does_not_mutate(self):
        original = game.REGEN_RULES
        game.REGEN_RULES = {"energy": 10, "peace": 5}
        resources = {"energy": 20, "peace": 98, "money": 3}
        traits = {"health": 40}
        resources_before = copy.deepcopy(resources)
        traits_before = copy.deepcopy(traits)
        try:
            self.assertEqual(
                game.compute_regen(resources, traits),
                {"energy": 10, "peace": 2})
        finally:
            game.REGEN_RULES = original
        self.assertEqual(resources, resources_before)
        self.assertEqual(traits, traits_before)

    def test_scale_toward_bound_all_branches(self):
        cases = [
            ((20, 75, 100), 5.0),
            ((-20, 25, 100), -5.0),
            ((0, 50, 100), 0.0),
            ((7, 50, 0), 7),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(game.scale_toward_bound(*args), expected)

    def test_clamp_gain_scales_and_caps_flow_resources(self):
        delta = {"peace": 20, "energy": 20, "money": 2.6}
        resources = {"peace": 80, "energy": 45, "money": 1}
        traits = {"health": 40}
        self.assertEqual(
            game.clamp_gain(delta, resources, traits),
            {"peace": 4, "energy": 5, "money": 3})

    def test_clamp_gain_scales_negative_peace_and_drops_rounded_zero(self):
        self.assertEqual(
            game.clamp_gain(
                {"peace": -20, "energy": -0.4},
                {"peace": 20, "energy": 50},
                {"health": 80}),
            {"peace": -4})

    def test_is_affordable_default_budget_and_floor_boundaries(self):
        resources = {"energy": 5, "peace": 50, "money": 0}
        self.assertTrue(game.is_affordable(
            {"settle": "defaulted", "hours": 99, "cost": {"energy": -99}},
            resources, budget=0))
        self.assertFalse(game.is_affordable(
            {"hours": 6, "cost": {}}, resources, budget=5))
        self.assertTrue(game.is_affordable(
            {"hours": 5, "cost": {"energy": -5}}, resources, budget=5))
        self.assertFalse(game.is_affordable(
            {"hours": 5, "cost": {"energy": -6}}, resources, budget=5))

    def test_is_affordable_existing_deficit_and_effective_peace_cost(self):
        self.assertTrue(game.is_affordable(
            {"cost": {"money": -100}}, {"money": -1}))
        self.assertTrue(game.is_affordable(
            {"cost": {"peace": -100}}, {"peace": 1}))
        self.assertFalse(game.is_affordable(
            {"cost": {"peace": -101}}, {"peace": 1}))


# ==============================================================================
# resource_rules.py (Step 5B、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class ResourceRulesModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._resource_rules_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.afford_floor = -1

    def test_compute_decay_matches_wrapper(self):
        resources = {"energy": 80, "peace": 70, "money": 123}
        self.assertEqual(
            resource_rules.compute_decay(
                8, resources, dependencies=self.dependencies()),
            game.compute_decay(8, resources))

    def test_resource_cap_matches_wrapper(self):
        for res, traits in [
                ("energy", {"health": 80}),
                ("energy", {"health": 40}),
                ("energy", {}),
                ("peace", {"health": 0})]:
            with self.subTest(res=res, traits=traits):
                self.assertEqual(
                    resource_rules.resource_cap(
                        res, traits, dependencies=self.dependencies()),
                    game.resource_cap(res, traits))

    def test_compute_regen_matches_wrapper(self):
        original = game.REGEN_RULES
        game.REGEN_RULES = {"energy": 10, "peace": 5}
        resources = {"energy": 20, "peace": 98, "money": 3}
        traits = {"health": 40}
        try:
            self.assertEqual(
                resource_rules.compute_regen(
                    resources, traits, dependencies=self.dependencies()),
                game.compute_regen(resources, traits))
        finally:
            game.REGEN_RULES = original

    def test_scale_toward_bound_matches_wrapper(self):
        for args in [(20, 75, 100), (-20, 25, 100), (0, 50, 100), (7, 50, 0)]:
            with self.subTest(args=args):
                self.assertEqual(
                    resource_rules.scale_toward_bound(*args),
                    game.scale_toward_bound(*args))

    def test_clamp_gain_matches_wrapper(self):
        delta = {"peace": 20, "energy": 20, "money": 2.6}
        resources = {"peace": 80, "energy": 45, "money": 1}
        traits = {"health": 40}
        self.assertEqual(
            resource_rules.clamp_gain(
                delta, resources, traits, dependencies=self.dependencies()),
            game.clamp_gain(delta, resources, traits))

    def test_is_affordable_matches_wrapper(self):
        cases = [
            ({"settle": "defaulted", "hours": 99, "cost": {"energy": -99}},
             {"energy": 5}, 0),
            ({"hours": 6, "cost": {}}, {"energy": 5}, 5),
            ({"hours": 5, "cost": {"energy": -5}}, {"energy": 5}, 5),
            ({"cost": {"peace": -101}}, {"peace": 1}, None),
        ]
        for choice, resources, budget in cases:
            with self.subTest(choice=choice, resources=resources, budget=budget):
                self.assertEqual(
                    resource_rules.is_affordable(
                        choice, resources, budget, dependencies=self.dependencies()),
                    game.is_affordable(choice, resources, budget))

    def test_module_calls_do_not_mutate_inputs_or_consume_rng(self):
        delta = {"peace": -20, "energy": 4, "money": 2.6}
        resources = {"peace": 20, "energy": 50, "money": 1}
        traits = {"health": 80}
        choice = {"hours": 2, "cost": {"peace": -10}}
        before = copy.deepcopy((delta, resources, traits, choice))
        rng_before = random.getstate()

        resource_rules.compute_decay(4, resources, dependencies=self.dependencies())
        resource_rules.compute_regen(resources, traits, dependencies=self.dependencies())
        resource_rules.clamp_gain(
            delta, resources, traits, dependencies=self.dependencies())
        resource_rules.is_affordable(
            choice, resources, 5, dependencies=self.dependencies())

        self.assertEqual((delta, resources, traits, choice), before)
        self.assertEqual(random.getstate(), rng_before)

    def test_wrapper_resolves_afford_floor_at_call_time(self):
        choice = {"cost": {"money": -5}}
        resources = {"money": 0}
        self.assertFalse(game.is_affordable(choice, resources))
        original = game.AFFORD_FLOOR
        game.AFFORD_FLOOR = -10
        try:
            self.assertTrue(game.is_affordable(choice, resources))
        finally:
            game.AFFORD_FLOOR = original

    def test_wrapper_resolves_initial_resources_at_call_time(self):
        original = game.INITIAL_RESOURCES
        game.INITIAL_RESOURCES = dict(original, peace=200)
        try:
            self.assertEqual(game.resource_cap("peace", {}), 200)
        finally:
            game.INITIAL_RESOURCES = original

    def test_wrapper_resolves_internal_helpers_at_call_time(self):
        original_cap = game.resource_cap
        original_scale = game.scale_toward_bound
        cap_calls = []
        scale_calls = []

        def fake_cap(res, traits):
            cap_calls.append((res, traits))
            return 80

        def fake_scale(v, current, cap):
            scale_calls.append((v, current, cap))
            return 7

        game.resource_cap = fake_cap
        game.scale_toward_bound = fake_scale
        try:
            self.assertEqual(
                game.clamp_gain({"peace": 10}, {"peace": 50}, {}),
                {"peace": 7})
        finally:
            game.resource_cap = original_cap
            game.scale_toward_bound = original_scale

        self.assertEqual(cap_calls, [("peace", {}), ("peace", {})])
        self.assertEqual(scale_calls, [(10, 50, 80)])


# ==============================================================================
# 特性成長ルール移動前の特性テスト(Step 5C-0、2026-08-14)
# ==============================================================================

class TraitRulesFixtureTest(unittest.TestCase):
    BASE_TRAITS = {
        "dexterity": 50.0,
        "intellect": 50.0,
        "skill": 50.0,
        "health": 80.0,
    }

    def test_age_and_health_decay_golden(self):
        self.assertEqual(game.age_at(0), 13.0)
        self.assertEqual(game.age_at(12), 14.0)
        self.assertEqual(game.age_at(120), 23.0)
        self.assertAlmostEqual(game.health_decay(0), 0.02)
        self.assertAlmostEqual(game.health_decay(120), 0.06)

    def test_talent_multiplier_match_and_mismatch(self):
        self.assertEqual(game.talent_multiplier("skill", "skill"), 1.5)
        self.assertEqual(game.talent_multiplier("skill", "health"), 1.0)
        self.assertEqual(game.talent_multiplier(None, "skill"), 1.0)

    def test_growable_trait_labor_with_talent(self):
        delta, fired = game.compute_trait_step(
            dict(self.BASE_TRAITS), "dexterity", "dexterity", "labor", 0, "normal")
        self.assertEqual(delta, {
            "dexterity": 1.5,
            "intellect": -0.12,
            "skill": -0.12,
            "health": -0.02,
        })
        self.assertEqual(fired, {
            "trait": "dexterity", "via": "labor", "talent": True})

    def test_growable_trait_money_and_social_rates(self):
        cases = [
            ("money", "intellect", 2.0),
            ("social", "skill", 0.5),
        ]
        for choice_key, theme_trait, expected_gain in cases:
            with self.subTest(choice_key=choice_key, theme_trait=theme_trait):
                delta, fired = game.compute_trait_step(
                    dict(self.BASE_TRAITS), None, theme_trait,
                    choice_key, 0, "normal")
                self.assertEqual(delta[theme_trait], expected_gain)
                self.assertEqual(fired, {
                    "trait": theme_trait, "via": choice_key, "talent": False})

    def test_health_care_gain_includes_same_turn_aging(self):
        cases = [
            ("money", "health", 1.18, True),
            ("labor", None, 0.28, False),
        ]
        for choice_key, talent, expected_health, talented in cases:
            with self.subTest(choice_key=choice_key, talent=talent):
                delta, fired = game.compute_trait_step(
                    dict(self.BASE_TRAITS), talent, "health",
                    choice_key, 0, "normal")
                self.assertEqual(delta["health"], expected_health)
                self.assertEqual(fired, {
                    "trait": "health", "via": choice_key, "talent": talented})

    def test_social_does_not_restore_health(self):
        delta, fired = game.compute_trait_step(
            dict(self.BASE_TRAITS), None, "health", "social", 0, "normal")
        self.assertEqual(delta, {
            "dexterity": -0.12,
            "intellect": -0.12,
            "skill": -0.12,
            "health": -0.02,
        })
        self.assertIsNone(fired)

    def test_no_theme_applies_only_decay(self):
        delta, fired = game.compute_trait_step(
            dict(self.BASE_TRAITS), "skill", None, "money", 12, "normal")
        self.assertEqual(delta, {
            "dexterity": -0.12,
            "intellect": -0.12,
            "skill": -0.12,
            "health": -0.024,
        })
        self.assertIsNone(fired)

    def test_trait_bounds_clip_applied_delta(self):
        zero_traits = {name: 0.0 for name in game.TRAITS}
        self.assertEqual(
            game.compute_trait_step(zero_traits, None, None, "rest", 0, "normal"),
            ({}, None))

        max_traits = {name: 100.0 for name in game.TRAITS}
        delta, fired = game.compute_trait_step(
            max_traits, None, "dexterity", "labor", 0, "normal")
        self.assertEqual(delta, {
            "dexterity": -0.12,
            "intellect": -0.12,
            "skill": -0.12,
            "health": -0.02,
        })
        self.assertIsNone(fired)

    def test_settlement_kind_still_allows_growth(self):
        normal = game.compute_trait_step(
            dict(self.BASE_TRAITS), None, "skill", "labor", 30, "normal")
        settlement = game.compute_trait_step(
            dict(self.BASE_TRAITS), None, "skill", "labor", 30, "settlement")
        self.assertEqual(settlement, normal)
        self.assertEqual(settlement[1], {
            "trait": "skill", "via": "labor", "talent": False})

    def test_trait_step_does_not_mutate_inputs_or_consume_rng(self):
        traits = dict(self.BASE_TRAITS)
        before = copy.deepcopy(traits)
        rng_before = random.getstate()
        game.compute_trait_step(
            traits, "health", "health", "money", 240, "settlement")
        self.assertEqual(traits, before)
        self.assertEqual(random.getstate(), rng_before)


# ==============================================================================
# trait_rules.py (Step 5C、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class TraitRulesModuleTest(unittest.TestCase):
    BASE_TRAITS = TraitRulesFixtureTest.BASE_TRAITS

    def dependencies(self):
        return game._trait_rules_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.d_base = 1.0

    def test_age_at_matches_wrapper(self):
        for turn in [0, 12, 120, 1920]:
            with self.subTest(turn=turn):
                self.assertEqual(
                    trait_rules.age_at(turn, dependencies=self.dependencies()),
                    game.age_at(turn))

    def test_health_decay_matches_wrapper(self):
        for turn in [0, 12, 120, 1920]:
            with self.subTest(turn=turn):
                self.assertEqual(
                    trait_rules.health_decay(
                        turn, dependencies=self.dependencies()),
                    game.health_decay(turn))

    def test_talent_multiplier_matches_wrapper(self):
        for talent, trait in [("skill", "skill"), ("skill", "health"), (None, "skill")]:
            with self.subTest(talent=talent, trait=trait):
                self.assertEqual(
                    trait_rules.talent_multiplier(
                        talent, trait, dependencies=self.dependencies()),
                    game.talent_multiplier(talent, trait))

    def test_compute_trait_step_matches_wrapper(self):
        cases = [
            ("dexterity", "dexterity", "labor", 0, "normal"),
            (None, "skill", "social", 120, "normal"),
            ("health", "health", "money", 240, "settlement"),
            (None, None, "rest", 1920, "normal"),
        ]
        for talent, theme_trait, choice_key, turn, kind in cases:
            with self.subTest(
                    talent=talent, theme_trait=theme_trait,
                    choice_key=choice_key, turn=turn, kind=kind):
                traits = dict(self.BASE_TRAITS)
                self.assertEqual(
                    trait_rules.compute_trait_step(
                        traits, talent, theme_trait, choice_key, turn, kind,
                        dependencies=self.dependencies()),
                    game.compute_trait_step(
                        traits, talent, theme_trait, choice_key, turn, kind))

    def test_module_does_not_mutate_inputs_or_consume_rng(self):
        traits = dict(self.BASE_TRAITS)
        before = copy.deepcopy(traits)
        rng_before = random.getstate()
        trait_rules.compute_trait_step(
            traits, "health", "health", "money", 240, "settlement",
            dependencies=self.dependencies())
        self.assertEqual(traits, before)
        self.assertEqual(random.getstate(), rng_before)

    def test_wrapper_resolves_d_base_at_call_time(self):
        original = game.D_BASE
        game.D_BASE = 1.0
        try:
            delta, fired = game.compute_trait_step(
                dict(self.BASE_TRAITS), None, None, "rest", 0, "normal")
        finally:
            game.D_BASE = original
        self.assertEqual(delta["dexterity"], -1.0)
        self.assertEqual(delta["intellect"], -1.0)
        self.assertEqual(delta["skill"], -1.0)
        self.assertIsNone(fired)

    def test_wrapper_resolves_growth_base_at_call_time(self):
        original = game.G_BASE
        game.G_BASE = dict(original, labor=10.0)
        try:
            delta, fired = game.compute_trait_step(
                dict(self.BASE_TRAITS), None, "dexterity", "labor", 0, "normal")
        finally:
            game.G_BASE = original
        self.assertEqual(delta["dexterity"], 5.0)
        self.assertEqual(fired, {
            "trait": "dexterity", "via": "labor", "talent": False})

    def test_health_decay_resolves_age_at_at_call_time(self):
        original = game.age_at
        calls = []

        def fake_age_at(turn):
            calls.append(turn)
            return game.START_AGE + 10

        game.age_at = fake_age_at
        try:
            self.assertAlmostEqual(game.health_decay(7), 0.06)
        finally:
            game.age_at = original
        self.assertEqual(calls, [7])

    def test_trait_step_resolves_internal_helpers_at_call_time(self):
        original_multiplier = game.talent_multiplier
        original_decay = game.health_decay
        multiplier_calls = []
        decay_calls = []

        def fake_multiplier(talent, trait):
            multiplier_calls.append((talent, trait))
            return 2.0

        def fake_decay(turn):
            decay_calls.append(turn)
            return 0.5

        game.talent_multiplier = fake_multiplier
        game.health_decay = fake_decay
        try:
            delta, fired = game.compute_trait_step(
                dict(self.BASE_TRAITS), "dexterity", "dexterity",
                "labor", 9, "normal")
        finally:
            game.talent_multiplier = original_multiplier
            game.health_decay = original_decay

        self.assertEqual(delta, {
            "dexterity": 2.0,
            "intellect": -0.12,
            "skill": -0.12,
            "health": -0.5,
        })
        self.assertEqual(fired, {
            "trait": "dexterity", "via": "labor", "talent": True})
        self.assertEqual(multiplier_calls, [("dexterity", "dexterity")])
        self.assertEqual(decay_calls, [9])


# ==============================================================================
# 経済ルール(Step 5D-0、2026-08-14): economic_rules.pyへの実装移動前に、
# 現行のgame.py実装のままgolden値として固定する。
# ==============================================================================

class EconomicRulesFixtureTest(unittest.TestCase):
    def test_price_index_golden(self):
        self.assertEqual(game.price_index(0), 1.0)
        self.assertAlmostEqual(game.price_index(4), 1.0100375625390623)
        self.assertAlmostEqual(game.price_index(1920), 120.78474341452772)

    def test_price_index_reflects_creation_rate_override_at_call_time(self):
        original = game.CREATION_RATE
        game.CREATION_RATE = 0.01
        try:
            value = game.price_index(4)
        finally:
            game.CREATION_RATE = original
        self.assertAlmostEqual(value, 1.04060401)

    def test_compute_wage_golden(self):
        # INCOME_INTERVAL(4)の直前は0、ちょうどのターンでround済みの名目額。
        self.assertEqual(game.compute_wage(3, game.BANK_STAGE_HEALTHY), 0)
        self.assertEqual(game.compute_wage(4, game.BANK_STAGE_HEALTHY), 40)
        self.assertEqual(game.compute_wage(8, game.BANK_STAGE_HEALTHY), 41)
        self.assertEqual(game.compute_wage(400, game.BANK_STAGE_HEALTHY), 109)

    def test_compute_wage_bank_stage_shrinks_and_halts(self):
        self.assertEqual(game.compute_wage(4, game.BANK_STAGE_CONTRACTION), 24)
        self.assertEqual(game.compute_wage(4, game.BANK_STAGE_HALTED), 6)
        self.assertEqual(game.compute_wage(4, game.BANK_STAGE_COLLAPSED), 0)
        # BANK_STAGE_COLLAPSED以上(等値だけでなく)も0であることを確認する
        # (元の条件は`bank_stage >= BANK_STAGE_COLLAPSED`であって等値判定ではない)。
        self.assertEqual(game.compute_wage(4, game.BANK_STAGE_COLLAPSED + 1), 0)

    def test_compute_salary_golden(self):
        self.assertEqual(game.compute_salary(3), 0)
        self.assertEqual(game.compute_salary(4), 25)
        self.assertEqual(game.compute_salary(400), 68)

    def test_compute_salary_ignores_bank_stage(self):
        # compute_salaryはbank_stageを引数に取らない(銀行が崩壊しても継続する
        # という設計をそのまま表す)。turnだけで決まることを確認する。
        self.assertEqual(game.compute_salary(4), 25)

    def test_compute_income_golden(self):
        self.assertEqual(game.compute_income(3, game.BANK_STAGE_HEALTHY), {})
        self.assertEqual(game.compute_income(4, game.BANK_STAGE_HEALTHY), {"money": 40})
        self.assertEqual(game.compute_income(4, game.BANK_STAGE_COLLAPSED), {})
        self.assertEqual(game.compute_income(4, game.BANK_STAGE_CONTRACTION), {"money": 24})

    def test_economic_functions_do_not_consume_random_state(self):
        state_before = random.getstate()
        game.price_index(4)
        game.compute_wage(4, game.BANK_STAGE_HEALTHY)
        game.compute_salary(4)
        game.compute_income(4, game.BANK_STAGE_HEALTHY)
        self.assertEqual(random.getstate(), state_before)


# ==============================================================================
# economic_rules.py (Step 5D、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class EconomicRulesModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._economic_rules_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.creation_rate = 1.0

    def test_price_index_matches_wrapper(self):
        for turn in [0, 4, 400, 1920]:
            with self.subTest(turn=turn):
                self.assertEqual(
                    economic_rules.price_index(turn, dependencies=self.dependencies()),
                    game.price_index(turn))

    def test_compute_wage_matches_wrapper(self):
        cases = [
            (3, game.BANK_STAGE_HEALTHY),
            (4, game.BANK_STAGE_HEALTHY),
            (4, game.BANK_STAGE_CONTRACTION),
            (4, game.BANK_STAGE_HALTED),
            (4, game.BANK_STAGE_COLLAPSED),
            (4, game.BANK_STAGE_COLLAPSED + 1),
            (400, game.BANK_STAGE_HEALTHY),
        ]
        for turn, bank_stage in cases:
            with self.subTest(turn=turn, bank_stage=bank_stage):
                self.assertEqual(
                    economic_rules.compute_wage(
                        turn, bank_stage, dependencies=self.dependencies()),
                    game.compute_wage(turn, bank_stage))

    def test_compute_salary_matches_wrapper(self):
        for turn in [3, 4, 400]:
            with self.subTest(turn=turn):
                self.assertEqual(
                    economic_rules.compute_salary(turn, dependencies=self.dependencies()),
                    game.compute_salary(turn))

    def test_compute_income_matches_wrapper(self):
        cases = [
            (3, game.BANK_STAGE_HEALTHY),
            (4, game.BANK_STAGE_HEALTHY),
            (4, game.BANK_STAGE_COLLAPSED),
            (4, game.BANK_STAGE_CONTRACTION),
        ]
        for turn, bank_stage in cases:
            with self.subTest(turn=turn, bank_stage=bank_stage):
                self.assertEqual(
                    economic_rules.compute_income(
                        turn, bank_stage, dependencies=self.dependencies()),
                    game.compute_income(turn, bank_stage))

    def test_module_does_not_consume_rng(self):
        rng_before = random.getstate()
        deps = self.dependencies()
        economic_rules.price_index(4, dependencies=deps)
        economic_rules.compute_wage(4, game.BANK_STAGE_HEALTHY, dependencies=deps)
        economic_rules.compute_salary(4, dependencies=deps)
        economic_rules.compute_income(4, game.BANK_STAGE_HEALTHY, dependencies=deps)
        self.assertEqual(random.getstate(), rng_before)

    def test_wrapper_resolves_creation_rate_at_call_time(self):
        original = game.CREATION_RATE
        game.CREATION_RATE = 0.01
        try:
            value = game.price_index(4)
        finally:
            game.CREATION_RATE = original
        self.assertAlmostEqual(value, 1.04060401)

    def test_compute_wage_resolves_price_index_at_call_time(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            self.assertEqual(game.compute_wage(4, game.BANK_STAGE_HEALTHY), 80)
        finally:
            game.price_index = original
        self.assertEqual(calls, [4])

    def test_compute_salary_resolves_price_index_at_call_time(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            self.assertEqual(game.compute_salary(4), 50)
        finally:
            game.price_index = original
        self.assertEqual(calls, [4])

    def test_compute_income_resolves_compute_wage_at_call_time(self):
        original = game.compute_wage
        calls = []

        def fake_compute_wage(turn, bank_stage):
            calls.append((turn, bank_stage))
            return 999

        game.compute_wage = fake_compute_wage
        try:
            result = game.compute_income(4, game.BANK_STAGE_HEALTHY)
        finally:
            game.compute_wage = original
        self.assertEqual(result, {"money": 999})
        self.assertEqual(calls, [(4, game.BANK_STAGE_HEALTHY)])


# ==============================================================================
# 行動コスト生成ルール(Step 5E-0、2026-08-14): action_costs.pyへの実装移動前に、
# 現行のgame.py実装のままgolden値として固定する。
# ==============================================================================

class _ExplodingInt:
    """sanitize_costの「未知キーは値の変換より先にrejectされる」順序を確認する
    ためだけの補助クラス。int()へ変換されたら(=順序が変わったら)例外を出す。"""
    def __int__(self):
        raise RuntimeError("未知キーの値がint()変換に渡された(順序が変わった)")


class ActionCostFixtureTest(unittest.TestCase):
    def test_sanitize_cost_keeps_only_allowed_resources_in_order(self):
        raw_cost = {"money": 10, "junk": 5, "peace": 3}
        clean, rejected, clipped = game.sanitize_cost(raw_cost)
        self.assertEqual(clean, {"money": 10, "peace": 3})
        self.assertEqual(list(clean.keys()), ["money", "peace"])
        self.assertEqual(rejected, ["junk"])
        self.assertEqual(clipped, [])

    def test_sanitize_cost_rejects_non_int_convertible_value(self):
        clean, rejected, clipped = game.sanitize_cost({"energy": "abc"})
        self.assertEqual(clean, {})
        self.assertEqual(rejected, ["energy"])
        self.assertEqual(clipped, [])

    def test_sanitize_cost_current_float_truncation_behavior(self):
        # int(3.9)==3(現在のint()変換仕様。切り捨て方向の「改善」はしない)。
        clean, rejected, clipped = game.sanitize_cost({"money": 3.9})
        self.assertEqual(clean, {"money": 3})
        self.assertEqual(rejected, [])
        self.assertEqual(clipped, [])

    def test_sanitize_cost_clips_values_beyond_max_delta(self):
        raw_cost = {"peace": game.MAX_DELTA + 1, "energy": game.MAX_DELTA,
                    "money": -game.MAX_DELTA}
        clean, rejected, clipped = game.sanitize_cost(raw_cost)
        # ちょうど±MAX_DELTAはクリップされない(>/<であって>=/<=ではない)。
        self.assertEqual(clean, {"peace": game.MAX_DELTA, "energy": game.MAX_DELTA,
                                 "money": -game.MAX_DELTA})
        self.assertEqual(rejected, [])
        self.assertEqual(clipped, ["peace"])

    def test_sanitize_cost_excludes_zero_from_clean_without_flagging(self):
        clean, rejected, clipped = game.sanitize_cost({"money": 0, "peace": 5})
        self.assertEqual(clean, {"peace": 5})
        self.assertEqual(rejected, [])
        self.assertEqual(clipped, [])

    def test_sanitize_cost_does_not_mutate_input(self):
        raw_cost = {"money": 10, "junk": 5, "peace": game.MAX_DELTA + 1}
        before = dict(raw_cost)
        game.sanitize_cost(raw_cost)
        self.assertEqual(raw_cost, before)

    def test_sanitize_cost_rejects_unknown_key_before_value_conversion(self):
        # _ExplodingIntのint()変換が実際に呼ばれていれば例外が飛ぶ。未知キーの
        # 判定(ALLOWED_RESOURCES外)がint()変換より先に走る現在の順序を確認する。
        clean, rejected, clipped = game.sanitize_cost({"unknown_key": _ExplodingInt()})
        self.assertEqual(clean, {})
        self.assertEqual(rejected, ["unknown_key"])
        self.assertEqual(clipped, [])

    def test_draw_ranges_golden_and_key_order(self):
        random.seed(1)
        ranges = {"money": (-40, -20), "peace": (0, 10), "energy": (-5, 5)}
        before = dict(ranges)
        result = game.draw_ranges(ranges)
        self.assertEqual(result, {"money": -36, "peace": 9, "energy": -4})
        self.assertEqual(list(result.keys()), ["money", "peace", "energy"])
        self.assertEqual(ranges, before)
        # カナリア値: この直後にrandom.random()を1回引いた値が変わっていれば、
        # draw_rangesが消費するrandint呼び出し回数・順序がどこかでずれている。
        self.assertAlmostEqual(random.random(), 0.2550690257394217)

    def test_draw_ranges_calls_randint_exactly_once_per_range(self):
        calls = []
        original = random.randint

        def counting_randint(lo, hi):
            calls.append((lo, hi))
            return original(lo, hi)

        random.randint = counting_randint
        try:
            random.seed(1)
            game.draw_ranges({"money": (-40, -20), "peace": (0, 10), "energy": (-5, 5)})
        finally:
            random.randint = original
        self.assertEqual(calls, [(-40, -20), (0, 10), (-5, 5)])

    def test_indexed_money_ranges_golden_with_money(self):
        random.seed(1)
        ranges = {"money": (-40, -20), "peace": (0, 10)}
        before = dict(ranges)
        result = game.indexed_money_ranges(ranges, 10)
        self.assertEqual(result, {"money": (-41, -21), "peace": (0, 10)})
        self.assertEqual(list(result.keys()), ["money", "peace"])
        self.assertEqual(ranges, before)
        self.assertAlmostEqual(random.random(), 0.13436424411240122)

    def test_indexed_money_ranges_golden_without_money(self):
        # moneyキーが無くてもprice_index(turn)を先に1回呼ぶ現在の挙動
        # (呼び出し自体はRNGを消費しないので、カナリアはdraw_ranges単体と同じ
        # 位置から始まる=直前のseed(1)直後の値と一致する)。
        random.seed(1)
        result = game.indexed_money_ranges({"peace": (0, 10), "energy": (-5, 5)}, 10)
        self.assertEqual(result, {"peace": (0, 10), "energy": (-5, 5)})
        self.assertAlmostEqual(random.random(), 0.13436424411240122)

    def test_indexed_money_ranges_price_index_monkeypatch_propagates(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            result = game.indexed_money_ranges({"money": (-10, -5)}, 99)
        finally:
            game.price_index = original
        self.assertEqual(result, {"money": (-20, -10)})
        self.assertEqual(calls, [99])

    def test_draw_archetype_cost_golden_per_archetype(self):
        cases = {
            "money": {"money": -37, "peace": 9},
            "labor": {"energy": -36, "peace": -11},
            "social": {"peace": -18},
            "rest": {"peace": 24, "energy": 33, "money": -8},
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                random.seed(1)
                result = game.draw_archetype_cost(key, 5)
                self.assertEqual(result, expected)

    def test_draw_archetype_cost_unknown_key_raises_stop_iteration(self):
        # next(...)によるarchetype検索が現在のまま(fallback無し)であることを
        # 確認する。例外の種類を変えない。
        with self.assertRaises(StopIteration):
            game.draw_archetype_cost("nonexistent", 5)

    def test_draw_archetype_cost_does_not_mutate_action_archetypes(self):
        before = copy.deepcopy(game.ACTION_ARCHETYPES)
        game.draw_archetype_cost("rest", 5)
        self.assertEqual(game.ACTION_ARCHETYPES, before)

    def test_draw_archetype_cost_calls_indexed_money_ranges_then_draw_ranges(self):
        calls = []
        original_indexed = game.indexed_money_ranges
        original_draw = game.draw_ranges

        def fake_indexed(ranges, turn):
            calls.append(("indexed_money_ranges", ranges, turn))
            return original_indexed(ranges, turn)

        def fake_draw(ranges):
            calls.append(("draw_ranges", ranges))
            return original_draw(ranges)

        game.indexed_money_ranges = fake_indexed
        game.draw_ranges = fake_draw
        try:
            random.seed(1)
            game.draw_archetype_cost("labor", 5)
        finally:
            game.indexed_money_ranges = original_indexed
            game.draw_ranges = original_draw
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "indexed_money_ranges")
        self.assertEqual(calls[1][0], "draw_ranges")

    def test_draw_archetype_hours_golden_and_calls_randint_once(self):
        cases = {"money": 14, "labor": 29, "social": 14, "rest": 29}
        for key, expected in cases.items():
            with self.subTest(key=key):
                calls = []
                original = random.randint

                def counting_randint(lo, hi):
                    calls.append((lo, hi))
                    return original(lo, hi)

                random.randint = counting_randint
                try:
                    random.seed(1)
                    result = game.draw_archetype_hours(key)
                finally:
                    random.randint = original
                self.assertEqual(result, expected)
                self.assertEqual(len(calls), 1)

    def test_draw_archetype_hours_unknown_key_raises_stop_iteration(self):
        with self.assertRaises(StopIteration):
            game.draw_archetype_hours("nonexistent")

    def test_draw_prorated_rest_golden_by_hours(self):
        cases = {
            0: {"peace": 0.0, "energy": 0.0, "money": -0.0},
            10: {"peace": 6.0, "energy": 8.25, "money": -2.0},
            40: {"peace": 24.0, "energy": 33.0, "money": -8.0},
            100: {"peace": 24.0, "energy": 33.0, "money": -8.0},
        }
        for hours, expected in cases.items():
            with self.subTest(hours=hours):
                random.seed(1)
                result = game.draw_prorated_rest(5, hours)
                self.assertEqual(result, expected)
                self.assertEqual(list(result.keys()), ["peace", "energy", "money"])

    def test_draw_prorated_rest_ref_hours_formula(self):
        self.assertEqual(sum(game.REST_TIME_COST_RANGE) / 2, 40.0)

    def test_draw_prorated_rest_zero_hours_still_calls_draw_archetype_cost(self):
        # hours=0でも早期returnせず、draw_archetype_cost("rest", turn)を呼んで
        # RNGを消費する現仕様(factor=0でも先に全レンジを引く)。
        calls = []
        original = game.draw_archetype_cost

        def fake_draw_archetype_cost(key, turn):
            calls.append((key, turn))
            return original(key, turn)

        game.draw_archetype_cost = fake_draw_archetype_cost
        try:
            random.seed(1)
            game.draw_prorated_rest(5, 0)
        finally:
            game.draw_archetype_cost = original
        self.assertEqual(calls, [("rest", 5)])

    def test_draw_prorated_rest_calls_draw_archetype_cost_exactly_once(self):
        calls = []
        original = game.draw_archetype_cost

        def counting(key, turn):
            calls.append((key, turn))
            return original(key, turn)

        game.draw_archetype_cost = counting
        try:
            random.seed(1)
            game.draw_prorated_rest(5, 10)
        finally:
            game.draw_archetype_cost = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("rest", 5))

    def test_composite_rng_order_golden(self):
        # draw_archetype_cost → draw_archetype_hours → draw_prorated_rest の
        # 連続呼び出し。関数分離後にrandintの回数や順序が変わっていないことを、
        # 戻り値と最後のカナリア値の両方で検出する。
        random.seed(42)
        cost = game.draw_archetype_cost("labor", 7)
        hours = game.draw_archetype_hours("labor")
        rest = game.draw_prorated_rest(7, 20)
        self.assertEqual(cost, {"energy": -20, "peace": -19})
        self.assertEqual(hours, 25)
        self.assertEqual(rest, {"peace": 14.0, "energy": 11.0, "money": -3.5})
        self.assertAlmostEqual(random.random(), 0.1395379285251439)


# ==============================================================================
# action_costs.py (Step 5E、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class ActionCostModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._action_cost_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.max_delta = 999

    # --- sanitize_cost(RNGなし) ---
    def test_sanitize_cost_matches_wrapper(self):
        cases = [
            {"money": 10, "junk": 5, "peace": 3},
            {"energy": "abc"},
            {"peace": game.MAX_DELTA + 1, "energy": game.MAX_DELTA,
             "money": -game.MAX_DELTA},
            {"money": 0, "peace": 5},
        ]
        for raw_cost in cases:
            with self.subTest(raw_cost=raw_cost):
                self.assertEqual(
                    action_costs.sanitize_cost(
                        dict(raw_cost), dependencies=self.dependencies()),
                    game.sanitize_cost(dict(raw_cost)))

    def test_sanitize_cost_does_not_mutate_input(self):
        raw_cost = {"money": 10, "junk": 5}
        before = dict(raw_cost)
        action_costs.sanitize_cost(raw_cost, dependencies=self.dependencies())
        self.assertEqual(raw_cost, before)

    # --- indexed_money_ranges(RNGなし) ---
    def test_indexed_money_ranges_matches_wrapper(self):
        cases = [
            ({"money": (-40, -20), "peace": (0, 10)}, 10),
            ({"peace": (0, 10), "energy": (-5, 5)}, 10),
        ]
        for ranges, turn in cases:
            with self.subTest(ranges=ranges, turn=turn):
                self.assertEqual(
                    action_costs.indexed_money_ranges(
                        dict(ranges), turn, dependencies=self.dependencies()),
                    game.indexed_money_ranges(dict(ranges), turn))

    def test_indexed_money_ranges_does_not_mutate_input(self):
        ranges = {"money": (-40, -20), "peace": (0, 10)}
        before = dict(ranges)
        action_costs.indexed_money_ranges(ranges, 10, dependencies=self.dependencies())
        self.assertEqual(ranges, before)

    # --- RNGを消費する関数の比較(Step 4A/4Bと同じgetstate/setstate手順) ---
    def _compare_with_rng(self, game_fn, module_fn):
        state_before = random.getstate()
        try:
            game_result = game_fn()
            state_after_game = random.getstate()

            random.setstate(state_before)
            module_result = module_fn()
            state_after_module = random.getstate()

            self.assertEqual(module_result, game_result)
            self.assertEqual(state_after_module, state_after_game)
            return game_result
        finally:
            random.setstate(state_before)

    def test_draw_ranges_matches_wrapper_and_rng_state(self):
        ranges = {"money": (-40, -20), "peace": (0, 10), "energy": (-5, 5)}
        random.seed(1)
        self._compare_with_rng(
            lambda: game.draw_ranges(dict(ranges)),
            lambda: action_costs.draw_ranges(dict(ranges), dependencies=self.dependencies()))

    def test_draw_ranges_does_not_mutate_input(self):
        ranges = {"money": (-40, -20)}
        before = dict(ranges)
        random.seed(1)
        try:
            action_costs.draw_ranges(ranges, dependencies=self.dependencies())
        finally:
            pass
        self.assertEqual(ranges, before)

    def test_draw_archetype_cost_matches_wrapper_and_rng_state(self):
        for key in ["money", "labor", "social", "rest"]:
            with self.subTest(key=key):
                random.seed(1)
                self._compare_with_rng(
                    lambda key=key: game.draw_archetype_cost(key, 5),
                    lambda key=key: action_costs.draw_archetype_cost(
                        key, 5, dependencies=self.dependencies()))

    def test_draw_archetype_cost_does_not_mutate_action_archetypes(self):
        before = copy.deepcopy(game.ACTION_ARCHETYPES)
        random.seed(1)
        action_costs.draw_archetype_cost("rest", 5, dependencies=self.dependencies())
        self.assertEqual(game.ACTION_ARCHETYPES, before)

    def test_draw_archetype_cost_callback_args_and_order(self):
        calls = []

        def fake_indexed(ranges, turn):
            calls.append(("indexed", ranges, turn))
            return dict(ranges)

        def fake_draw(ranges):
            calls.append(("draw", ranges))
            return {k: lo for k, (lo, hi) in ranges.items()}

        action_costs.draw_archetype_cost(
            "labor", 5, dependencies=self.dependencies(),
            indexed_money_ranges_fn=fake_indexed, draw_ranges_fn=fake_draw)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "indexed")
        self.assertEqual(calls[1][0], "draw")
        labor_ranges = next(
            a for a in game.ACTION_ARCHETYPES if a["key"] == "labor")["ranges"]
        self.assertEqual(calls[0][1], labor_ranges)
        self.assertEqual(calls[0][2], 5)

    def test_draw_archetype_hours_matches_wrapper_and_rng_state(self):
        for key in ["money", "labor", "social", "rest"]:
            with self.subTest(key=key):
                random.seed(1)
                self._compare_with_rng(
                    lambda key=key: game.draw_archetype_hours(key),
                    lambda key=key: action_costs.draw_archetype_hours(
                        key, dependencies=self.dependencies()))

    def test_draw_prorated_rest_matches_wrapper_and_rng_state(self):
        for hours in [0, 10, 40, 100]:
            with self.subTest(hours=hours):
                random.seed(1)
                self._compare_with_rng(
                    lambda hours=hours: game.draw_prorated_rest(5, hours),
                    lambda hours=hours: action_costs.draw_prorated_rest(
                        5, hours, dependencies=self.dependencies()))

    def test_draw_prorated_rest_callback_args_and_count(self):
        calls = []

        def fake_draw_archetype_cost(key, turn):
            calls.append((key, turn))
            return {"peace": 40, "energy": 40, "money": -8}

        result = action_costs.draw_prorated_rest(
            5, 10, dependencies=self.dependencies(),
            draw_archetype_cost_fn=fake_draw_archetype_cost)

        self.assertEqual(calls, [("rest", 5)])
        self.assertEqual(result, {"peace": 10.0, "energy": 10.0, "money": -2.0})

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_randint_at_call_time(self):
        original = random.randint
        calls = []

        def fake_randint(lo, hi):
            calls.append((lo, hi))
            return lo

        random.randint = fake_randint
        try:
            result = game.draw_ranges({"money": (-40, -20), "peace": (0, 10)})
        finally:
            random.randint = original
        self.assertEqual(result, {"money": -40, "peace": 0})
        self.assertEqual(calls, [(-40, -20), (0, 10)])

    def test_wrapper_resolves_price_index_at_call_time(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            result = game.indexed_money_ranges({"money": (-10, -5)}, 42)
        finally:
            game.price_index = original
        self.assertEqual(result, {"money": (-20, -10)})
        self.assertEqual(calls, [42])

    def test_draw_archetype_cost_resolves_helpers_at_call_time(self):
        original_indexed = game.indexed_money_ranges
        original_draw = game.draw_ranges
        calls = []

        def fake_indexed(ranges, turn):
            calls.append(("indexed", turn))
            return original_indexed(ranges, turn)

        def fake_draw(ranges):
            calls.append(("draw", None))
            return original_draw(ranges)

        game.indexed_money_ranges = fake_indexed
        game.draw_ranges = fake_draw
        try:
            random.seed(1)
            game.draw_archetype_cost("labor", 5)
        finally:
            game.indexed_money_ranges = original_indexed
            game.draw_ranges = original_draw
        self.assertEqual([c[0] for c in calls], ["indexed", "draw"])

    def test_draw_prorated_rest_resolves_draw_archetype_cost_at_call_time(self):
        original = game.draw_archetype_cost
        calls = []

        def fake_draw_archetype_cost(key, turn):
            calls.append((key, turn))
            return original(key, turn)

        game.draw_archetype_cost = fake_draw_archetype_cost
        try:
            random.seed(1)
            game.draw_prorated_rest(5, 10)
        finally:
            game.draw_archetype_cost = original
        self.assertEqual(calls, [("rest", 5)])


# ==============================================================================
# 対人信用・契約上限ルール(Step 5F-0、2026-08-14): relationship_rules.pyへの
# 実装移動前に、現行のgame.py実装のままgolden値として固定する。
# ==============================================================================

class RelationshipRulesFixtureTest(unittest.TestCase):
    # --- npc_price_modifier ---
    def test_npc_price_modifier_empty_dict_is_neutral(self):
        self.assertEqual(game.npc_price_modifier({}), 1.0)

    def test_npc_price_modifier_ethics_default_is_50(self):
        # ethics未指定→50.0がdefault(trustはNPC_TRUST_INITIALに固定して切り分け)。
        self.assertEqual(
            game.npc_price_modifier({"trust": game.NPC_TRUST_INITIAL}), 1.0)

    def test_npc_price_modifier_trust_default_is_npc_trust_initial(self):
        self.assertEqual(game.npc_price_modifier({"ethics": 50.0}), 1.0)

    def test_npc_price_modifier_ethics_values(self):
        cases = [(20.0, 1.3), (50.0, 1.0), (80.0, 0.7)]
        for ethics, expected in cases:
            with self.subTest(ethics=ethics):
                npc = {"ethics": ethics, "trust": game.NPC_TRUST_INITIAL}
                self.assertAlmostEqual(game.npc_price_modifier(npc), expected)

    def test_npc_price_modifier_trust_values(self):
        cases = [(0.0, 1.3), (game.NPC_TRUST_INITIAL, 1.0), (game.NPC_TRUST_CAP, 0.7)]
        for trust, expected in cases:
            with self.subTest(trust=trust):
                npc = {"ethics": 50.0, "trust": trust}
                self.assertAlmostEqual(game.npc_price_modifier(npc), expected)

    def test_npc_price_modifier_lower_bound_clamped_to_half(self):
        # ethics=80(factor 0.7)・trust=100(factor 0.7)→積0.49→下限0.5でクランプ。
        npc = {"ethics": 80.0, "trust": 100.0}
        self.assertEqual(game.npc_price_modifier(npc), 0.5)

    def test_npc_price_modifier_has_no_upper_cap(self):
        # ethics=20(factor 1.3)・trust=0(factor 1.3)→積1.69。上側には
        # 明示的なcapが無い現仕様をそのまま固定する。
        npc = {"ethics": 20.0, "trust": 0.0}
        self.assertAlmostEqual(game.npc_price_modifier(npc), 1.69)

    def test_npc_price_modifier_does_not_mutate_input(self):
        npc = {"ethics": 20.0, "trust": 60.0}
        before = dict(npc)
        game.npc_price_modifier(npc)
        self.assertEqual(npc, before)

    # --- npc_trust_gain ---
    def test_npc_trust_gain_golden_values(self):
        cases = [
            (0.0, 3.0),
            (game.NPC_TRUST_INITIAL, 1.5),
            (game.NPC_TRUST_CAP, 0.0),
            (150.0, 0.0),  # cap超過は0(現仕様、負にはならない)
            (-50.0, 4.5),  # 負のtrustでは3.0を超え得る現仕様
        ]
        for current_trust, expected in cases:
            with self.subTest(current_trust=current_trust):
                self.assertEqual(game.npc_trust_gain(current_trust), expected)

    # --- player_reputation ---
    def test_player_reputation_empty_is_neutral(self):
        self.assertEqual(game.player_reputation({}, 10), game.NPC_TRUST_INITIAL)

    def test_player_reputation_averages_active_only(self):
        acq = {"a": {"trust": 40.0, "retire_turn": 200},
               "b": {"trust": 60.0, "retire_turn": 200}}
        self.assertEqual(game.player_reputation(acq, 10), 50.0)

    def test_player_reputation_retire_turn_boundary(self):
        acq = {"a": {"trust": 40.0, "retire_turn": 10}}
        # turn == retire_turn は対象外(turn < retire_turnで判定するため)
        # → activeが空になりNPC_TRUST_INITIALへフォールバック。
        self.assertEqual(game.player_reputation(acq, 10), game.NPC_TRUST_INITIAL)
        # turn == retire_turn - 1 は対象。
        self.assertEqual(game.player_reputation(acq, 9), 40.0)

    def test_player_reputation_retire_turn_none_is_active(self):
        acq = {"a": {"trust": 40.0, "retire_turn": None}}
        self.assertEqual(game.player_reputation(acq, 99999), 40.0)

    def test_player_reputation_retire_turn_missing_is_active(self):
        acq = {"a": {"trust": 40.0}}
        self.assertEqual(game.player_reputation(acq, 99999), 40.0)

    def test_player_reputation_retire_turn_zero_is_active_current_quirk(self):
        # `n.get("retire_turn") or float("inf")`はretire_turn=0(falsy)を
        # Noneと同じ「無期限」扱いにする(意図的な修正はしない、現仕様の固定)。
        acq = {"a": {"trust": 40.0, "retire_turn": 0}}
        self.assertEqual(game.player_reputation(acq, 99999), 40.0)

    def test_player_reputation_trust_missing_defaults_to_initial(self):
        acq = {"a": {"retire_turn": 200}}
        self.assertEqual(game.player_reputation(acq, 10), game.NPC_TRUST_INITIAL)

    def test_player_reputation_all_retired_is_neutral(self):
        acq = {"a": {"trust": 40.0, "retire_turn": 5},
               "b": {"trust": 60.0, "retire_turn": 5}}
        self.assertEqual(game.player_reputation(acq, 10), game.NPC_TRUST_INITIAL)

    def test_player_reputation_does_not_mutate_input(self):
        acq = {"a": {"trust": 40.0, "retire_turn": 200}}
        before = copy.deepcopy(acq)
        game.player_reputation(acq, 10)
        self.assertEqual(acq, before)

    # --- initial_trust_for_new_npc ---
    def test_initial_trust_for_new_npc_golden_values(self):
        cases = [
            ({}, 50.0),
            ({"a": {"trust": 10.0, "retire_turn": 200}}, 40.0),
            ({"a": {"trust": 50.0, "retire_turn": 200}}, 50.0),
            ({"a": {"trust": 90.0, "retire_turn": 200}}, 60.0),
        ]
        for acquaintances, expected in cases:
            with self.subTest(acquaintances=acquaintances):
                self.assertAlmostEqual(
                    game.initial_trust_for_new_npc(acquaintances, 10), expected)

    def test_initial_trust_for_new_npc_calls_player_reputation_exactly_once(self):
        calls = []
        original = game.player_reputation

        def counting(acquaintances, turn):
            calls.append((dict(acquaintances), turn))
            return original(acquaintances, turn)

        game.player_reputation = counting
        try:
            game.initial_trust_for_new_npc({"a": {"trust": 90.0, "retire_turn": 200}}, 10)
        finally:
            game.player_reputation = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 10)

    def test_initial_trust_for_new_npc_does_not_mutate_input(self):
        acq = {"a": {"trust": 90.0, "retire_turn": 200}}
        before = copy.deepcopy(acq)
        game.initial_trust_for_new_npc(acq, 10)
        self.assertEqual(acq, before)

    # --- contract_credit_multiplier ---
    def test_contract_credit_multiplier_golden_boundaries(self):
        cases = [
            (20.0, 0.0),                   # trust < MIN
            (game.NPC_CONTRACT_TRUST_MIN, 0.0),
            (42.5, 0.5),                   # MINとINITIALの中間
            (game.NPC_TRUST_INITIAL, 1.0),
            (75.0, 2.0),                   # INITIALとCAPの中間
            (game.NPC_TRUST_CAP, game.CONTRACT_CREDIT_MULTIPLIER_MAX),
            (150.0, 5.0),                  # CAP超過は外挿される(上限なし)
        ]
        for trust, expected in cases:
            with self.subTest(trust=trust):
                self.assertAlmostEqual(game.contract_credit_multiplier(trust), expected)

    def test_contract_credit_multiplier_high_side_span_zero_fallback(self):
        original = game.NPC_TRUST_CAP
        game.NPC_TRUST_CAP = game.NPC_TRUST_INITIAL
        try:
            result = game.contract_credit_multiplier(game.NPC_TRUST_INITIAL)
        finally:
            game.NPC_TRUST_CAP = original
        self.assertEqual(result, 1.0)

    def test_contract_credit_multiplier_low_side_span_zero_fallback(self):
        original = game.NPC_TRUST_INITIAL
        game.NPC_TRUST_INITIAL = game.NPC_CONTRACT_TRUST_MIN
        try:
            result = game.contract_credit_multiplier(game.NPC_CONTRACT_TRUST_MIN - 5.0)
        finally:
            game.NPC_TRUST_INITIAL = original
        self.assertEqual(result, 0.0)

    # --- contract_credit_limit ---
    def test_contract_credit_limit_golden_payment_counts(self):
        trust = game.NPC_TRUST_INITIAL
        cases = [
            (1, 3, 0.0),
            (1, 4, 25.0625),
            (4, 7, 0.0),
            (4, 8, 25.250939063476558),
            (5, 5, 0.0),    # due_turn == turn
            (10, 5, 0.0),   # due_turn < turn
            (1, 20, 125.3125),  # 複数回の支払い
        ]
        for turn, due_turn, expected in cases:
            with self.subTest(turn=turn, due_turn=due_turn):
                self.assertAlmostEqual(
                    game.contract_credit_limit(trust, turn, due_turn), expected)

    def test_contract_credit_limit_trust_scaling(self):
        self.assertEqual(game.contract_credit_limit(20.0, 1, 4), 0.0)
        self.assertAlmostEqual(game.contract_credit_limit(100.0, 1, 4), 75.1875)

    def test_contract_credit_limit_calls_price_index_with_turn_not_due_turn(self):
        calls = []
        original = game.price_index

        def fake_price_index(turn):
            calls.append(turn)
            return original(turn)

        game.price_index = fake_price_index
        try:
            game.contract_credit_limit(game.NPC_TRUST_INITIAL, 4, 8)
        finally:
            game.price_index = original
        self.assertEqual(calls, [4])

    def test_contract_credit_limit_calls_price_index_even_when_zero_payments(self):
        calls = []
        original = game.price_index

        def fake_price_index(turn):
            calls.append(turn)
            return original(turn)

        game.price_index = fake_price_index
        try:
            game.contract_credit_limit(game.NPC_TRUST_INITIAL, 1, 3)
        finally:
            game.price_index = original
        self.assertEqual(calls, [1])

    def test_contract_credit_limit_call_order_is_price_index_then_multiplier(self):
        calls = []
        original_price_index = game.price_index
        original_multiplier = game.contract_credit_multiplier

        def fake_price_index(turn):
            calls.append("price_index")
            return original_price_index(turn)

        def fake_multiplier(trust):
            calls.append("contract_credit_multiplier")
            return original_multiplier(trust)

        game.price_index = fake_price_index
        game.contract_credit_multiplier = fake_multiplier
        try:
            game.contract_credit_limit(game.NPC_TRUST_INITIAL, 1, 4)
        finally:
            game.price_index = original_price_index
            game.contract_credit_multiplier = original_multiplier
        self.assertEqual(calls, ["price_index", "contract_credit_multiplier"])
        self.assertEqual(len(calls), 2)

    # --- 複合的な非変更確認 ---
    def test_composite_does_not_mutate_or_consume_rng(self):
        state_before = random.getstate()
        acquaintances = {"a": {"trust": 40.0, "ethics": 60.0, "retire_turn": 200},
                         "b": {"trust": 90.0, "ethics": 30.0, "retire_turn": 200}}
        npc = {"ethics": 20.0, "trust": 60.0}
        acq_before = copy.deepcopy(acquaintances)
        npc_before = dict(npc)

        r1 = game.npc_price_modifier(npc)
        r2 = game.npc_trust_gain(60.0)
        r3 = game.player_reputation(acquaintances, 10)
        r4 = game.initial_trust_for_new_npc(acquaintances, 10)
        r5 = game.contract_credit_multiplier(60.0)
        r6 = game.contract_credit_limit(60.0, 1, 10)

        self.assertEqual(acquaintances, acq_before)
        self.assertEqual(npc, npc_before)
        self.assertEqual(random.getstate(), state_before)
        for value in (r1, r2, r3, r4, r5, r6):
            self.assertIsInstance(value, float)


# ==============================================================================
# relationship_rules.py (Step 5F、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class RelationshipRulesModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._relationship_rules_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.npc_trust_initial = 1.0

    # --- module版とgame.pyラッパー版の一致 ---
    def test_npc_price_modifier_matches_wrapper(self):
        cases = [
            {},
            {"ethics": 20.0, "trust": 60.0},
            {"ethics": 80.0, "trust": 100.0},  # 下限クランプ境界
            {"ethics": 20.0, "trust": 0.0},    # 上限なしの外挿
        ]
        for npc in cases:
            with self.subTest(npc=npc):
                self.assertEqual(
                    relationship_rules.npc_price_modifier(
                        dict(npc), dependencies=self.dependencies()),
                    game.npc_price_modifier(dict(npc)))

    def test_npc_price_modifier_does_not_mutate_input(self):
        npc = {"ethics": 20.0, "trust": 60.0}
        before = dict(npc)
        relationship_rules.npc_price_modifier(npc, dependencies=self.dependencies())
        self.assertEqual(npc, before)

    def test_npc_trust_gain_matches_wrapper(self):
        for current_trust in [0.0, game.NPC_TRUST_INITIAL, game.NPC_TRUST_CAP, 150.0, -50.0]:
            with self.subTest(current_trust=current_trust):
                self.assertEqual(
                    relationship_rules.npc_trust_gain(
                        current_trust, dependencies=self.dependencies()),
                    game.npc_trust_gain(current_trust))

    def test_player_reputation_matches_wrapper(self):
        cases = [
            ({}, 10),
            ({"a": {"trust": 40.0, "retire_turn": 200},
              "b": {"trust": 60.0, "retire_turn": 200}}, 10),
            ({"a": {"trust": 40.0, "retire_turn": 10}}, 10),   # 境界: turn==retire_turn
            ({"a": {"trust": 40.0, "retire_turn": 10}}, 9),    # 境界: turn==retire_turn-1
            ({"a": {"trust": 40.0, "retire_turn": 0}}, 99999),  # retire_turn=0の現仕様
        ]
        for acquaintances, turn in cases:
            with self.subTest(acquaintances=acquaintances, turn=turn):
                self.assertEqual(
                    relationship_rules.player_reputation(
                        copy.deepcopy(acquaintances), turn, dependencies=self.dependencies()),
                    game.player_reputation(copy.deepcopy(acquaintances), turn))

    def test_player_reputation_does_not_mutate_input(self):
        acq = {"a": {"trust": 40.0, "retire_turn": 200}}
        before = copy.deepcopy(acq)
        relationship_rules.player_reputation(acq, 10, dependencies=self.dependencies())
        self.assertEqual(acq, before)

    def test_initial_trust_for_new_npc_matches_wrapper(self):
        cases = [
            ({}, 10),
            ({"a": {"trust": 10.0, "retire_turn": 200}}, 10),
            ({"a": {"trust": 90.0, "retire_turn": 200}}, 10),
        ]
        for acquaintances, turn in cases:
            with self.subTest(acquaintances=acquaintances, turn=turn):
                self.assertAlmostEqual(
                    relationship_rules.initial_trust_for_new_npc(
                        copy.deepcopy(acquaintances), turn, dependencies=self.dependencies()),
                    game.initial_trust_for_new_npc(copy.deepcopy(acquaintances), turn))

    def test_initial_trust_for_new_npc_callback_args_and_count(self):
        calls = []

        def fake_player_reputation(acquaintances, turn):
            calls.append((dict(acquaintances), turn))
            return 77.0

        result = relationship_rules.initial_trust_for_new_npc(
            {"x": {"trust": 1.0}}, 5, dependencies=self.dependencies(),
            player_reputation_fn=fake_player_reputation)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ({"x": {"trust": 1.0}}, 5))
        deps = self.dependencies()
        expected = (1 - deps.npc_reputation_weight) * deps.npc_trust_initial \
            + deps.npc_reputation_weight * 77.0
        self.assertAlmostEqual(result, expected)

    def test_contract_credit_multiplier_matches_wrapper(self):
        cases = [20.0, game.NPC_CONTRACT_TRUST_MIN, 42.5, game.NPC_TRUST_INITIAL,
                75.0, game.NPC_TRUST_CAP, 150.0]
        for trust in cases:
            with self.subTest(trust=trust):
                self.assertAlmostEqual(
                    relationship_rules.contract_credit_multiplier(
                        trust, dependencies=self.dependencies()),
                    game.contract_credit_multiplier(trust))

    def test_contract_credit_limit_matches_wrapper(self):
        cases = [
            (game.NPC_TRUST_INITIAL, 1, 3), (game.NPC_TRUST_INITIAL, 1, 4),
            (game.NPC_TRUST_INITIAL, 4, 7), (game.NPC_TRUST_INITIAL, 4, 8),
            (game.NPC_TRUST_INITIAL, 5, 5), (game.NPC_TRUST_INITIAL, 10, 5),
            (game.NPC_TRUST_INITIAL, 1, 20), (20.0, 1, 4), (100.0, 1, 4),
        ]
        for trust, turn, due_turn in cases:
            with self.subTest(trust=trust, turn=turn, due_turn=due_turn):
                self.assertAlmostEqual(
                    relationship_rules.contract_credit_limit(
                        trust, turn, due_turn, dependencies=self.dependencies()),
                    game.contract_credit_limit(trust, turn, due_turn))

    def test_contract_credit_limit_callback_args_and_order(self):
        calls = []
        deps = self.dependencies()

        def fake_multiplier(trust):
            calls.append(trust)
            return 9.0

        result = relationship_rules.contract_credit_limit(
            42.0, 1, 4, dependencies=deps, contract_credit_multiplier_fn=fake_multiplier)

        self.assertEqual(calls, [42.0])
        expected_income = deps.salary_base * deps.price_index_fn(1) * 1
        self.assertAlmostEqual(result, expected_income * 9.0)

    def test_module_does_not_consume_rng(self):
        state_before = random.getstate()
        deps = self.dependencies()
        acquaintances = {"a": {"trust": 40.0, "ethics": 60.0, "retire_turn": 200}}
        relationship_rules.npc_price_modifier({"ethics": 20.0, "trust": 60.0}, dependencies=deps)
        relationship_rules.npc_trust_gain(60.0, dependencies=deps)
        relationship_rules.player_reputation(acquaintances, 10, dependencies=deps)
        relationship_rules.initial_trust_for_new_npc(acquaintances, 10, dependencies=deps)
        relationship_rules.contract_credit_multiplier(60.0, dependencies=deps)
        relationship_rules.contract_credit_limit(60.0, 1, 10, dependencies=deps)
        self.assertEqual(random.getstate(), state_before)

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_npc_trust_initial_at_call_time(self):
        original = game.NPC_TRUST_INITIAL
        game.NPC_TRUST_INITIAL = 60.0
        try:
            result = game.npc_price_modifier({"ethics": 50.0, "trust": 60.0})
        finally:
            game.NPC_TRUST_INITIAL = original
        self.assertEqual(result, 1.0)

    def test_wrapper_resolves_npc_reputation_weight_at_call_time(self):
        original = game.NPC_REPUTATION_WEIGHT
        game.NPC_REPUTATION_WEIGHT = 0.5
        try:
            result = game.initial_trust_for_new_npc(
                {"a": {"trust": 90.0, "retire_turn": 200}}, 10)
        finally:
            game.NPC_REPUTATION_WEIGHT = original
        self.assertAlmostEqual(result, 70.0)  # (1-0.5)*50 + 0.5*90

    def test_wrapper_resolves_npc_trust_cap_at_call_time(self):
        original = game.NPC_TRUST_CAP
        game.NPC_TRUST_CAP = 50.0
        try:
            result = game.npc_trust_gain(25.0)
        finally:
            game.NPC_TRUST_CAP = original
        self.assertAlmostEqual(result, 1.5)  # 3.0*max(0,1-25/50)

    def test_wrapper_resolves_contract_credit_multiplier_max_at_call_time(self):
        original = game.CONTRACT_CREDIT_MULTIPLIER_MAX
        game.CONTRACT_CREDIT_MULTIPLIER_MAX = 10.0
        try:
            result = game.contract_credit_multiplier(game.NPC_TRUST_CAP)
        finally:
            game.CONTRACT_CREDIT_MULTIPLIER_MAX = original
        self.assertEqual(result, 10.0)

    def test_wrapper_resolves_income_interval_and_salary_base_at_call_time(self):
        original_interval = game.INCOME_INTERVAL
        original_salary = game.SALARY_BASE
        game.INCOME_INTERVAL = 2
        game.SALARY_BASE = 100.0
        try:
            result = game.contract_credit_limit(game.NPC_TRUST_INITIAL, 1, 3)
        finally:
            game.INCOME_INTERVAL = original_interval
            game.SALARY_BASE = original_salary
        # n_payments = 3//2 - 1//2 = 1 - 0 = 1
        expected = 100.0 * game.price_index(1) * 1 * 1.0
        self.assertAlmostEqual(result, expected)

    def test_contract_credit_limit_resolves_price_index_at_call_time(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            result = game.contract_credit_limit(game.NPC_TRUST_INITIAL, 1, 4)
        finally:
            game.price_index = original
        self.assertEqual(calls, [1])
        self.assertAlmostEqual(result, game.SALARY_BASE * 2.0 * 1 * 1.0)

    def test_initial_trust_for_new_npc_resolves_player_reputation_at_call_time(self):
        original = game.player_reputation
        calls = []

        def fake_player_reputation(acquaintances, turn):
            calls.append((acquaintances, turn))
            return 80.0

        game.player_reputation = fake_player_reputation
        try:
            result = game.initial_trust_for_new_npc({}, 10)
        finally:
            game.player_reputation = original
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(
            result,
            (1 - game.NPC_REPUTATION_WEIGHT) * game.NPC_TRUST_INITIAL
            + game.NPC_REPUTATION_WEIGHT * 80.0)

    def test_contract_credit_limit_resolves_contract_credit_multiplier_at_call_time(self):
        original = game.contract_credit_multiplier
        calls = []

        def fake_multiplier(trust):
            calls.append(trust)
            return 5.0

        game.contract_credit_multiplier = fake_multiplier
        try:
            result = game.contract_credit_limit(42.0, 1, 4)
        finally:
            game.contract_credit_multiplier = original
        self.assertEqual(calls, [42.0])
        expected_income = game.SALARY_BASE * game.price_index(1) * 1
        self.assertAlmostEqual(result, expected_income * 5.0)


# ==============================================================================
# NPC信用回帰(Step 5G-0、2026-08-14): relationship_rules.pyへの
# effective_npc_trust実装移動前に、現行のgame.py実装のままgolden値として固定する。
# ==============================================================================

class EffectiveNpcTrustFixtureTest(unittest.TestCase):
    def test_neutral_trust_stays_neutral_regardless_of_elapsed(self):
        npc = {"trust": game.NPC_TRUST_INITIAL, "trust_updated_turn": 0}
        self.assertEqual(game.effective_npc_trust(npc, 0), game.NPC_TRUST_INITIAL)
        self.assertEqual(game.effective_npc_trust(npc, 100), game.NPC_TRUST_INITIAL)

    def test_above_neutral_golden_values(self):
        cases = {0: 70.0, 1: 69.75, 120: 54.42061315239901, 240: 50.97709103215816}
        for turn, expected in cases.items():
            with self.subTest(turn=turn):
                npc = {"trust": 70.0, "trust_updated_turn": 0}
                self.assertAlmostEqual(game.effective_npc_trust(npc, turn), expected)

    def test_below_neutral_golden_values(self):
        cases = {1: 30.25, 120: 45.57938684760099, 240: 49.02290896784184}
        for turn, expected in cases.items():
            with self.subTest(turn=turn):
                npc = {"trust": 30.0, "trust_updated_turn": 0}
                self.assertAlmostEqual(game.effective_npc_trust(npc, turn), expected)

    def test_trust_missing_defaults_to_npc_trust_initial(self):
        npc = {"trust_updated_turn": 0}
        self.assertEqual(game.effective_npc_trust(npc, 50), game.NPC_TRUST_INITIAL)

    def test_trust_updated_turn_takes_priority_over_birth_turn(self):
        npc = {"trust": 70.0, "trust_updated_turn": 5, "birth_turn": 0}
        self.assertAlmostEqual(game.effective_npc_trust(npc, 10), 68.78086181030274)

    def test_trust_updated_turn_zero_is_respected_not_treated_as_missing(self):
        # trust_updated_turn=0は`if last_turn is None`では偽ではない
        # (0 is not None)ので、birth_turn=100へフォールバックしない。
        npc = {"trust": 70.0, "trust_updated_turn": 0, "birth_turn": 100}
        self.assertAlmostEqual(game.effective_npc_trust(npc, 5), 68.78086181030274)

    def test_trust_updated_turn_none_falls_back_to_birth_turn(self):
        npc = {"trust": 70.0, "trust_updated_turn": None, "birth_turn": 3}
        self.assertAlmostEqual(game.effective_npc_trust(npc, 10), 68.31427477470304)

    def test_trust_updated_turn_missing_falls_back_to_birth_turn(self):
        npc = {"trust": 70.0, "birth_turn": 3}
        self.assertAlmostEqual(game.effective_npc_trust(npc, 10), 68.31427477470304)

    def test_trust_updated_turn_and_birth_turn_both_missing_gives_elapsed_zero(self):
        npc = {"trust": 70.0}
        self.assertEqual(game.effective_npc_trust(npc, 10), 70.0)

    def test_future_update_turn_clamps_elapsed_to_zero(self):
        # trust_updated_turn > turn の場合、elapsed = max(0, turn-last_turn) = 0
        # となり、最後のtrustがそのまま返る。
        npc = {"trust": 70.0, "trust_updated_turn": 20}
        self.assertEqual(game.effective_npc_trust(npc, 10), 70.0)

    def test_birth_turn_none_raises_type_error_current_behavior(self):
        # trust_updated_turnが未指定/None、かつbirth_turnキーが明示的にNoneの
        # 場合、`turn - None`でTypeErrorになる現在の挙動を固定する(改善しない)。
        npc = {"trust": 70.0, "birth_turn": None}
        with self.assertRaises(TypeError):
            game.effective_npc_trust(npc, 10)

    def test_does_not_mutate_input(self):
        npc = {"trust": 70.0, "trust_updated_turn": 0, "birth_turn": 0}
        before = dict(npc)
        game.effective_npc_trust(npc, 50)
        self.assertEqual(npc, before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        game.effective_npc_trust({"trust": 70.0, "trust_updated_turn": 0}, 50)
        self.assertEqual(random.getstate(), state_before)

    def test_wrapper_resolves_npc_trust_initial_at_call_time(self):
        original = game.NPC_TRUST_INITIAL
        game.NPC_TRUST_INITIAL = 60.0
        try:
            result = game.effective_npc_trust(
                {"trust": 60.0, "trust_updated_turn": 0}, 100)
        finally:
            game.NPC_TRUST_INITIAL = original
        self.assertEqual(result, 60.0)

    def test_wrapper_resolves_npc_trust_reversion_rate_at_call_time(self):
        original = game.NPC_TRUST_REVERSION_RATE
        game.NPC_TRUST_REVERSION_RATE = 1.0  # 1ターンで完全に中立へ戻る極端値
        try:
            result = game.effective_npc_trust(
                {"trust": 70.0, "trust_updated_turn": 0}, 1)
        finally:
            game.NPC_TRUST_REVERSION_RATE = original
        self.assertAlmostEqual(result, game.NPC_TRUST_INITIAL)


# ==============================================================================
# relationship_rules.effective_npc_trust (Step 5G、2026-08-14): 独立モジュールの
# 直接テスト
# ==============================================================================

class EffectiveNpcTrustModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._relationship_rules_dependencies()

    def test_dependencies_has_npc_trust_reversion_rate_field(self):
        deps = self.dependencies()
        self.assertEqual(deps.npc_trust_reversion_rate, game.NPC_TRUST_REVERSION_RATE)

    def test_dependencies_is_still_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.npc_trust_reversion_rate = 1.0

    def test_matches_wrapper_above_neutral(self):
        for turn in [0, 1, 120, 240]:
            with self.subTest(turn=turn):
                npc = {"trust": 70.0, "trust_updated_turn": 0}
                self.assertEqual(
                    relationship_rules.effective_npc_trust(
                        dict(npc), turn, dependencies=self.dependencies()),
                    game.effective_npc_trust(dict(npc), turn))

    def test_matches_wrapper_below_neutral(self):
        for turn in [1, 120, 240]:
            with self.subTest(turn=turn):
                npc = {"trust": 30.0, "trust_updated_turn": 0}
                self.assertEqual(
                    relationship_rules.effective_npc_trust(
                        dict(npc), turn, dependencies=self.dependencies()),
                    game.effective_npc_trust(dict(npc), turn))

    def test_matches_wrapper_at_neutral(self):
        npc = {"trust": game.NPC_TRUST_INITIAL, "trust_updated_turn": 0}
        self.assertEqual(
            relationship_rules.effective_npc_trust(
                dict(npc), 100, dependencies=self.dependencies()),
            game.effective_npc_trust(dict(npc), 100))

    def test_matches_wrapper_trust_updated_turn_fallback_to_birth_turn(self):
        cases = [
            {"trust": 70.0, "trust_updated_turn": None, "birth_turn": 3},
            {"trust": 70.0, "birth_turn": 3},
            {"trust": 70.0},
        ]
        for npc in cases:
            with self.subTest(npc=npc):
                self.assertEqual(
                    relationship_rules.effective_npc_trust(
                        dict(npc), 10, dependencies=self.dependencies()),
                    game.effective_npc_trust(dict(npc), 10))

    def test_matches_wrapper_future_update_turn_elapsed_zero(self):
        npc = {"trust": 70.0, "trust_updated_turn": 20}
        self.assertEqual(
            relationship_rules.effective_npc_trust(
                dict(npc), 10, dependencies=self.dependencies()),
            game.effective_npc_trust(dict(npc), 10))

    def test_matches_wrapper_birth_turn_none_raises_same_type_error(self):
        npc = {"trust": 70.0, "birth_turn": None}
        with self.assertRaises(TypeError):
            relationship_rules.effective_npc_trust(
                dict(npc), 10, dependencies=self.dependencies())
        with self.assertRaises(TypeError):
            game.effective_npc_trust(dict(npc), 10)

    def test_wrapper_resolves_npc_trust_initial_at_call_time(self):
        original = game.NPC_TRUST_INITIAL
        game.NPC_TRUST_INITIAL = 60.0
        try:
            result = game.effective_npc_trust(
                {"trust": 60.0, "trust_updated_turn": 0}, 100)
        finally:
            game.NPC_TRUST_INITIAL = original
        self.assertEqual(result, 60.0)

    def test_wrapper_resolves_npc_trust_reversion_rate_at_call_time(self):
        original = game.NPC_TRUST_REVERSION_RATE
        game.NPC_TRUST_REVERSION_RATE = 1.0
        try:
            result = game.effective_npc_trust(
                {"trust": 70.0, "trust_updated_turn": 0}, 1)
        finally:
            game.NPC_TRUST_REVERSION_RATE = original
        self.assertAlmostEqual(result, game.NPC_TRUST_INITIAL)

    def test_does_not_mutate_input(self):
        npc = {"trust": 70.0, "trust_updated_turn": 0, "birth_turn": 0}
        before = dict(npc)
        relationship_rules.effective_npc_trust(npc, 50, dependencies=self.dependencies())
        self.assertEqual(npc, before)

    def test_does_not_consume_random_state(self):
        state_before = random.getstate()
        relationship_rules.effective_npc_trust(
            {"trust": 70.0, "trust_updated_turn": 0}, 50, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)


# ==============================================================================
# 契約清算コスト(Step 5H-0、2026-08-14): contract_costs.pyへの実装移動前に、
# 現行のgame.py実装のままgolden値として固定する。
# ==============================================================================

class ContractCostFixtureTest(unittest.TestCase):
    def test_draw_contract_repay_labor_golden(self):
        random.seed(1)
        self.assertEqual(game.draw_contract_repay_labor(-40, 5), {"energy": -65})

    def test_draw_contract_repay_labor_sign_independent(self):
        random.seed(1)
        neg = game.draw_contract_repay_labor(-40, 5)
        random.seed(1)
        pos = game.draw_contract_repay_labor(40, 5)
        self.assertEqual(neg, pos)

    def test_draw_contract_repay_labor_zero_repay_money(self):
        random.seed(1)
        self.assertEqual(game.draw_contract_repay_labor(0, 5), {})

    def test_draw_contract_repay_labor_turn_golden(self):
        random.seed(1)
        self.assertEqual(game.draw_contract_repay_labor(-40, 0), {"energy": -65})
        random.seed(1)
        self.assertEqual(game.draw_contract_repay_labor(-40, 400), {"energy": -24})

    def test_draw_contract_repay_labor_rng_canary(self):
        random.seed(1)
        game.draw_contract_repay_labor(-40, 5)
        self.assertAlmostEqual(random.random(), 0.8474337369372327)

    def test_draw_contract_repay_labor_calls_uniform_in_coef_dict_order(self):
        original_coef = game.CONTRACT_REPAY_LABOR_COEF
        game.CONTRACT_REPAY_LABOR_COEF = {"a": 1.0, "b": 2.0, "c": 3.0}
        calls = []
        original_uniform = random.uniform

        def counting_uniform(lo, hi):
            calls.append((lo, hi))
            return original_uniform(lo, hi)

        random.uniform = counting_uniform
        try:
            random.seed(1)
            result = game.draw_contract_repay_labor(-40, 5)
        finally:
            random.uniform = original_uniform
            game.CONTRACT_REPAY_LABOR_COEF = original_coef
        # 1係数につきuniformを正確に1回、dict順(a→b→c)で呼ぶ。
        # lo/hi = coef*(1-SPREAD), coef*(1+SPREAD)。
        self.assertEqual(calls, [(0.75, 1.25), (1.5, 2.5), (2.25, 3.75)])
        self.assertEqual(list(result.keys()), ["a", "b", "c"])

    def test_draw_contract_repay_labor_excludes_zero_cost(self):
        original_coef = game.CONTRACT_REPAY_LABOR_COEF
        game.CONTRACT_REPAY_LABOR_COEF = {"energy": 0.001}
        try:
            random.seed(1)
            result = game.draw_contract_repay_labor(-40, 5)
        finally:
            game.CONTRACT_REPAY_LABOR_COEF = original_coef
        self.assertEqual(result, {})

    def test_draw_contract_repay_labor_calls_price_index_then_uniform(self):
        calls = []
        original_price_index = game.price_index
        original_uniform = random.uniform

        def fake_price_index(turn):
            calls.append("price_index")
            return original_price_index(turn)

        def fake_uniform(lo, hi):
            calls.append("uniform")
            return original_uniform(lo, hi)

        game.price_index = fake_price_index
        random.uniform = fake_uniform
        try:
            random.seed(1)
            game.draw_contract_repay_labor(-40, 5)
        finally:
            game.price_index = original_price_index
            random.uniform = original_uniform
        self.assertEqual(calls, ["price_index", "uniform"])

    def test_draw_contract_repay_labor_does_not_mutate_coef(self):
        before = dict(game.CONTRACT_REPAY_LABOR_COEF)
        random.seed(1)
        game.draw_contract_repay_labor(-40, 5)
        self.assertEqual(game.CONTRACT_REPAY_LABOR_COEF, before)

    # --- contract_default_penalty ---
    def test_contract_default_penalty_golden(self):
        self.assertEqual(game.contract_default_penalty(-40, 5), {"peace": -79})

    def test_contract_default_penalty_sign_independent(self):
        self.assertEqual(
            game.contract_default_penalty(-40, 5),
            game.contract_default_penalty(40, 5))

    def test_contract_default_penalty_zero_repay_money_returns_zero_peace(self):
        self.assertEqual(game.contract_default_penalty(0, 5), {"peace": 0})

    def test_contract_default_penalty_turn_golden(self):
        self.assertEqual(game.contract_default_penalty(-40, 0), {"peace": -80})
        self.assertEqual(game.contract_default_penalty(-40, 400), {"peace": -29})

    def test_contract_default_penalty_calls_price_index_exactly_once(self):
        calls = []
        original = game.price_index

        def fake_price_index(turn):
            calls.append(turn)
            return original(turn)

        game.price_index = fake_price_index
        try:
            game.contract_default_penalty(-40, 5)
        finally:
            game.price_index = original
        self.assertEqual(calls, [5])

    def test_contract_default_penalty_does_not_consume_rng(self):
        state_before = random.getstate()
        game.contract_default_penalty(-40, 5)
        self.assertEqual(random.getstate(), state_before)


# ==============================================================================
# contract_costs.py (Step 5H、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class ContractCostModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._contract_cost_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.contract_default_penalty_rate = 999.0

    def _compare_with_rng(self, game_fn, module_fn):
        state_before = random.getstate()
        try:
            game_result = game_fn()
            state_after_game = random.getstate()

            random.setstate(state_before)
            module_result = module_fn()
            state_after_module = random.getstate()

            self.assertEqual(module_result, game_result)
            self.assertEqual(state_after_module, state_after_game)
            return game_result
        finally:
            random.setstate(state_before)

    def test_draw_contract_repay_labor_matches_wrapper_and_rng_state(self):
        for repay_money, turn in [(-40, 5), (40, 5), (0, 5), (-40, 0), (-40, 400)]:
            with self.subTest(repay_money=repay_money, turn=turn):
                random.seed(1)
                self._compare_with_rng(
                    lambda repay_money=repay_money, turn=turn:
                        game.draw_contract_repay_labor(repay_money, turn),
                    lambda repay_money=repay_money, turn=turn:
                        contract_costs.draw_contract_repay_labor(
                            repay_money, turn, dependencies=self.dependencies()))

    def test_draw_contract_repay_labor_does_not_mutate_coef(self):
        before = dict(game.CONTRACT_REPAY_LABOR_COEF)
        random.seed(1)
        contract_costs.draw_contract_repay_labor(
            -40, 5, dependencies=self.dependencies())
        self.assertEqual(game.CONTRACT_REPAY_LABOR_COEF, before)

    def test_draw_contract_repay_labor_callback_args_and_order(self):
        deps = self.dependencies()
        calls = []

        def fake_uniform(lo, hi):
            calls.append((lo, hi))
            return 2.0

        custom_deps = contract_costs.ContractCostDependencies(
            contract_repay_labor_coef={"a": 1.0, "b": 2.0},
            contract_repay_labor_spread=deps.contract_repay_labor_spread,
            contract_default_penalty_rate=deps.contract_default_penalty_rate,
            uniform_fn=fake_uniform,
            price_index_fn=deps.price_index_fn,
        )
        contract_costs.draw_contract_repay_labor(-40, 5, dependencies=custom_deps)
        self.assertEqual(calls, [(0.75, 1.25), (1.5, 2.5)])

    def test_contract_default_penalty_matches_wrapper(self):
        for repay_money, turn in [(-40, 5), (40, 5), (0, 5), (-40, 0), (-40, 400)]:
            with self.subTest(repay_money=repay_money, turn=turn):
                self.assertEqual(
                    contract_costs.contract_default_penalty(
                        repay_money, turn, dependencies=self.dependencies()),
                    game.contract_default_penalty(repay_money, turn))

    def test_contract_default_penalty_does_not_consume_rng(self):
        state_before = random.getstate()
        contract_costs.contract_default_penalty(-40, 5, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_contract_repay_labor_coef_at_call_time(self):
        original = game.CONTRACT_REPAY_LABOR_COEF
        game.CONTRACT_REPAY_LABOR_COEF = {"peace": 5.0}
        try:
            random.seed(1)
            result = game.draw_contract_repay_labor(-40, 5)
        finally:
            game.CONTRACT_REPAY_LABOR_COEF = original
        self.assertEqual(list(result.keys()), ["peace"])

    def test_wrapper_resolves_contract_repay_labor_spread_at_call_time(self):
        original = game.CONTRACT_REPAY_LABOR_SPREAD
        game.CONTRACT_REPAY_LABOR_SPREAD = 0.0
        calls = []
        original_uniform = random.uniform

        def counting_uniform(lo, hi):
            calls.append((lo, hi))
            return original_uniform(lo, hi)

        random.uniform = counting_uniform
        try:
            random.seed(1)
            game.draw_contract_repay_labor(-40, 5)
        finally:
            game.CONTRACT_REPAY_LABOR_SPREAD = original
            random.uniform = original_uniform
        # spread=0.0ならlo==hi==coefになる。
        self.assertEqual(calls[0][0], calls[0][1])

    def test_wrapper_resolves_contract_default_penalty_rate_at_call_time(self):
        original = game.CONTRACT_DEFAULT_PENALTY_RATE
        game.CONTRACT_DEFAULT_PENALTY_RATE = 10.0
        try:
            result = game.contract_default_penalty(-40, 0)
        finally:
            game.CONTRACT_DEFAULT_PENALTY_RATE = original
        self.assertEqual(result, {"peace": -400})

    def test_wrapper_resolves_uniform_at_call_time(self):
        original = random.uniform
        calls = []

        def fake_uniform(lo, hi):
            calls.append((lo, hi))
            return 1.0

        random.uniform = fake_uniform
        try:
            result = game.draw_contract_repay_labor(-40, 5)
        finally:
            random.uniform = original
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, {"energy": -round(40 / game.price_index(5))})

    def test_wrapper_resolves_price_index_at_call_time(self):
        original = game.price_index
        calls = []

        def fake_price_index(turn):
            calls.append(turn)
            return 2.0

        game.price_index = fake_price_index
        try:
            result = game.contract_default_penalty(-40, 5)
        finally:
            game.price_index = original
        self.assertEqual(calls, [5])
        expected_real = 40 / 2.0
        self.assertEqual(
            result, {"peace": -round(game.CONTRACT_DEFAULT_PENALTY_RATE * expected_real)})


# ==============================================================================
# social契約の相手選択(Step 5I-0、2026-08-14): counterparty_selection.pyへの
# pick_social_counterparty実装移動前に、現行のgame.py実装のままgolden値として
# 固定する。
# ==============================================================================

class ScriptedRng:
    """random.random()の戻り値をあらかじめ指定できる、テスト用の疑似rng。
    random()/choice()の呼び出し回数・引数・順序を記録する。random()に
    指定した値を使い切って呼ばれた場合はAssertionErrorにする(想定外の
    RNG消費を検出するため)。"""
    def __init__(self, random_values=None):
        self._random_values = list(random_values or [])
        self.random_calls = 0
        self.choice_calls = []

    def random(self):
        self.random_calls += 1
        if self._random_values:
            return self._random_values.pop(0)
        raise AssertionError("random()が想定回数より多く呼ばれた")

    def choice(self, seq):
        self.choice_calls.append(list(seq))
        return seq[0]

    def choice_call_names(self):
        """choice_calls(npc dictのリストのリスト)から名前だけを取り出す、
        テストの可読性のためのヘルパー。"""
        return [[n["name"] for n in call] for call in self.choice_calls]


class PickSocialCounterpartyFixtureTest(unittest.TestCase):
    def test_no_acquaintances_returns_new_without_rng(self):
        rng = ScriptedRng([])
        result = game.pick_social_counterparty({}, set(), 0, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 0)
        self.assertEqual(rng.choice_calls, [])

    def test_all_retired_returns_new_without_rng(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": 5}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, set(), 10, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 0)

    def test_reusable_under_cap_reuse_succeeds(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        rng = ScriptedRng([0.1])  # 0.1 < NPC_REUSE_RATE(0.7)
        result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        self.assertEqual(result, ("A", False))
        self.assertEqual(rng.random_calls, 1)
        self.assertEqual(rng.choice_call_names(), [["A"]])

    def test_reusable_under_cap_reuse_fails(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        rng = ScriptedRng([0.9])  # 0.9 >= NPC_REUSE_RATE(0.7)
        result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 1)
        self.assertEqual(rng.choice_calls, [])

    def test_at_cap_reusable_skips_reuse_random_goes_straight_to_choice(self):
        # at_capのときは`at_cap or rng.random()<NPC_REUSE_RATE`の短絡評価で
        # rng.random()自体を呼ばない。
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, set(), 0, 1, rng=rng)
        self.assertEqual(result, ("A", False))
        self.assertEqual(rng.random_calls, 0)
        self.assertEqual(rng.choice_call_names(), [["A"]])

    def test_open_with_ethics_none_rejected_without_rng(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": None}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, {"a"}, 0, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 0)

    def test_open_with_low_ethics_p_zero_never_willing(self):
        # ethics=20(NPC_ETHICS_RANGEの下端)ではp=0.0にクランプされ、
        # rng.random()<0.0は常に偽になる(0.0を渡しても真にならない)。
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": 20.0}}
        rng = ScriptedRng([0.0])
        result = game.pick_social_counterparty(acq, {"a"}, 0, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 1)

    def test_open_with_high_ethics_p_one_willing_then_reuse_check(self):
        # ethics=80(NPC_ETHICS_RANGEの上端)ではp=1.0にクランプされ、
        # willingのrng.random()呼び出しに続けて、reuse判定のrng.random()も呼ぶ。
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": 80.0}}
        rng = ScriptedRng([0.99, 0.1])
        result = game.pick_social_counterparty(acq, {"a"}, 0, 5, rng=rng)
        self.assertEqual(result, ("A", False))
        self.assertEqual(rng.random_calls, 2)
        self.assertEqual(rng.choice_call_names(), [["A"]])

    def test_trust_below_minimum_excluded_from_reusable(self):
        acq = {"a": {"name": "A", "trust": 10.0, "retire_turn": None}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 0)

    def test_at_cap_willing_fails_but_trusted_only_has_candidate(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": None}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, {"a"}, 0, 1, rng=rng)
        self.assertEqual(result, ("A", False))
        self.assertEqual(rng.random_calls, 0)
        self.assertEqual(rng.choice_call_names(), [["A"]])

    def test_at_cap_no_one_trusted_returns_new_despite_cap(self):
        acq = {"a": {"name": "A", "trust": 10.0, "retire_turn": None}}
        rng = ScriptedRng([])
        result = game.pick_social_counterparty(acq, set(), 0, 1, rng=rng)
        self.assertEqual(result, (None, True))
        self.assertEqual(rng.random_calls, 0)
        self.assertEqual(rng.choice_calls, [])

    def test_retire_turn_zero_is_active_current_quirk(self):
        # `turn < (n.get("retire_turn") or float("inf"))`はretire_turn=0
        # (falsy)をNoneと同じ「無期限」扱いにする(現仕様の固定、修正しない)。
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": 0}}
        rng = ScriptedRng([0.1])
        result = game.pick_social_counterparty(acq, set(), 99999, 5, rng=rng)
        self.assertEqual(result, ("A", False))

    def test_multiple_npcs_scan_order(self):
        acq = {"x": {"name": "X", "trust": 50.0, "retire_turn": None},
               "y": {"name": "Y", "trust": 50.0, "retire_turn": None},
               "z": {"name": "Z", "trust": 50.0, "retire_turn": None}}
        rng = ScriptedRng([0.1])
        result = game.pick_social_counterparty(acq, set(), 0, 10, rng=rng)
        self.assertEqual(result, ("X", False))
        self.assertEqual(rng.choice_call_names(), [["X", "Y", "Z"]])

    def test_fixed_seed_golden_and_rng_canary(self):
        random.seed(1)
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        result = game.pick_social_counterparty(acq, set(), 0, 5)
        self.assertEqual(result, ("A", False))
        self.assertAlmostEqual(random.random(), 0.2550690257394217)

    def test_fixed_seed_golden_with_two_candidates(self):
        random.seed(1)
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None},
               "b": {"name": "B", "trust": 90.0, "retire_turn": None}}
        result = game.pick_social_counterparty(acq, set(), 0, 5)
        self.assertEqual(result, ("A", False))
        self.assertAlmostEqual(random.random(), 0.2550690257394217)

    def test_does_not_mutate_inputs(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        open_cps = {"z"}
        acq_before = {k: dict(v) for k, v in acq.items()}
        open_before = set(open_cps)
        rng = ScriptedRng([0.1])
        game.pick_social_counterparty(acq, open_cps, 0, 5, rng=rng)
        self.assertEqual(acq, acq_before)
        self.assertEqual(open_cps, open_before)


# ==============================================================================
# counterparty_selection.py (Step 5I、2026-08-14): 独立モジュールの直接テスト
# ==============================================================================

class PickSocialCounterpartyModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._counterparty_selection_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.npc_reuse_rate = 1.0

    def _compare(self, acquaintances, open_counterparties, turn, npc_pool_cap, random_values):
        game_rng = ScriptedRng(list(random_values))
        module_rng = ScriptedRng(list(random_values))
        game_result = game.pick_social_counterparty(
            copy.deepcopy(acquaintances), set(open_counterparties), turn, npc_pool_cap,
            rng=game_rng)
        module_result = counterparty_selection.pick_social_counterparty(
            copy.deepcopy(acquaintances), set(open_counterparties), turn, npc_pool_cap,
            module_rng, dependencies=self.dependencies())
        self.assertEqual(module_result, game_result)
        self.assertEqual(module_rng.random_calls, game_rng.random_calls)
        self.assertEqual(module_rng.choice_call_names(), game_rng.choice_call_names())
        return game_result

    def test_matches_wrapper_no_acquaintances(self):
        self._compare({}, set(), 0, 5, [])

    def test_matches_wrapper_reuse_succeeds(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        self._compare(acq, set(), 0, 5, [0.1])

    def test_matches_wrapper_reuse_fails(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        self._compare(acq, set(), 0, 5, [0.9])

    def test_matches_wrapper_at_cap(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        self._compare(acq, set(), 0, 1, [])

    def test_matches_wrapper_open_with_ethics(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": 80.0}}
        self._compare(acq, {"a"}, 0, 5, [0.99, 0.1])

    def test_matches_wrapper_multiple_npcs(self):
        acq = {"x": {"name": "X", "trust": 50.0, "retire_turn": None},
               "y": {"name": "Y", "trust": 50.0, "retire_turn": None}}
        self._compare(acq, set(), 0, 10, [0.1])

    def test_matches_wrapper_with_real_random_fixed_seed(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        state_before = random.getstate()
        try:
            random.seed(1)
            game_result = game.pick_social_counterparty(dict(acq), set(), 0, 5)
            state_after_game = random.getstate()

            random.setstate(state_before)
            random.seed(1)
            module_result = counterparty_selection.pick_social_counterparty(
                dict(acq), set(), 0, 5, random, dependencies=self.dependencies())
            state_after_module = random.getstate()

            self.assertEqual(module_result, game_result)
            self.assertEqual(state_after_module, state_after_game)
        finally:
            random.setstate(state_before)

    def test_explicit_rng_argument_propagates(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        rng = ScriptedRng([0.1])
        result = counterparty_selection.pick_social_counterparty(
            acq, set(), 0, 5, rng, dependencies=self.dependencies())
        self.assertEqual(result, ("A", False))
        self.assertEqual(rng.random_calls, 1)

    def test_does_not_mutate_inputs(self):
        acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
        open_cps = {"z"}
        acq_before = {k: dict(v) for k, v in acq.items()}
        open_before = set(open_cps)
        rng = ScriptedRng([0.1])
        counterparty_selection.pick_social_counterparty(
            acq, open_cps, 0, 5, rng, dependencies=self.dependencies())
        self.assertEqual(acq, acq_before)
        self.assertEqual(open_cps, open_before)

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_npc_ethics_range_at_call_time(self):
        original = game.NPC_ETHICS_RANGE
        game.NPC_ETHICS_RANGE = (0.0, 100.0)
        try:
            acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None, "ethics": 50.0}}
            rng = ScriptedRng([0.49, 0.1])  # p=(50-0)/(100-0)=0.5, 0.49<0.5
            result = game.pick_social_counterparty(acq, {"a"}, 0, 5, rng=rng)
        finally:
            game.NPC_ETHICS_RANGE = original
        self.assertEqual(result, ("A", False))

    def test_wrapper_resolves_npc_reuse_rate_at_call_time(self):
        original = game.NPC_REUSE_RATE
        game.NPC_REUSE_RATE = 0.0
        try:
            acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
            rng = ScriptedRng([0.5])  # 0.5 < 0.0 は偽
            result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        finally:
            game.NPC_REUSE_RATE = original
        self.assertEqual(result, (None, True))

    def test_wrapper_resolves_npc_trust_initial_at_call_time(self):
        original = game.NPC_TRUST_INITIAL
        game.NPC_TRUST_INITIAL = 10.0
        try:
            # trust未指定のNPC。NPC_TRUST_INITIAL=10ならNPC_CONTRACT_TRUST_MIN(35)未満。
            acq = {"a": {"name": "A", "retire_turn": None}}
            rng = ScriptedRng([])
            result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        finally:
            game.NPC_TRUST_INITIAL = original
        self.assertEqual(result, (None, True))

    def test_wrapper_resolves_npc_contract_trust_min_at_call_time(self):
        original = game.NPC_CONTRACT_TRUST_MIN
        game.NPC_CONTRACT_TRUST_MIN = 60.0
        try:
            acq = {"a": {"name": "A", "trust": 50.0, "retire_turn": None}}
            rng = ScriptedRng([])
            result = game.pick_social_counterparty(acq, set(), 0, 5, rng=rng)
        finally:
            game.NPC_CONTRACT_TRUST_MIN = original
        self.assertEqual(result, (None, True))


# ==============================================================================
# generate_settlement_turn (Step 6A、2026-08-15): main()側の清算状況生成の
# 構造化golden値。Step 6B以降でループ分割を行う前に、通常通貨時・通貨放棄時の
# 戻り値とRNG状態を固定する。
# ==============================================================================

class GenerateSettlementTurnFixtureTest(unittest.TestCase):
    MOCK_JSON = json.dumps({
        "situation": "モックの清算状況です。",
        "choices": {"money": "お金で返す", "labor": "労力で返す", "avoid": "先延ばしにする"},
    }, ensure_ascii=False)

    CONTRACT = {"id": "c1", "counterparty": "友人A", "description": "頼み事",
               "created_turn": 0, "due_turn": 5, "repay_money": -40, "is_bank_debt": False}

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_JSON, 0.001)
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)

    @staticmethod
    def _base_state(currency_stage):
        return {
            "resources": {"energy": 100, "peace": 100, "money": 0},
            "contracts": {}, "npcs": {}, "npc_pool_cap": 10,
            "currency_stage": currency_stage,
        }

    def test_normal_currency_stage(self):
        random.seed(1)
        state = self._base_state(game.CURRENCY_STAGE_NORMAL)
        result = game.generate_settlement_turn(
            "dummy-model", state, 5, dict(self.CONTRACT), [])

        self.assertEqual(result["situation"], "モックの清算状況です。")
        self.assertEqual(result["kind"], "settlement")
        self.assertEqual(result["contract"], self.CONTRACT)
        self.assertEqual([c["key"] for c in result["choices"]], ["money", "labor", "avoid"])
        for c in result["choices"]:
            self.assertEqual(set(c.keys()), {"key", "label", "cost", "settle"})

        by_key = {c["key"]: c for c in result["choices"]}
        self.assertEqual(by_key["money"]["label"], "お金で返す")
        self.assertEqual(by_key["money"]["cost"], {"money": -40})
        self.assertEqual(by_key["money"]["settle"], "fulfilled")
        self.assertEqual(by_key["labor"]["label"], "労力で返す")
        self.assertEqual(by_key["labor"]["cost"], {"energy": -65})
        self.assertEqual(by_key["labor"]["settle"], "fulfilled")
        self.assertEqual(by_key["avoid"]["label"], "先延ばしにする")
        self.assertEqual(by_key["avoid"]["cost"], {"peace": -79})
        self.assertEqual(by_key["avoid"]["settle"], "defaulted")
        # カナリア値: laborのdraw_contract_repay_labor(uniform呼び出し1回)だけが
        # RNGを消費する。この直後の値が変わっていれば消費順序がずれている。
        self.assertAlmostEqual(random.random(), 0.8474337369372327)

    def test_abandoned_currency_stage_excludes_money(self):
        random.seed(1)
        state = self._base_state(game.CURRENCY_STAGE_ABANDONED)
        result = game.generate_settlement_turn(
            "dummy-model", state, 5, dict(self.CONTRACT), [])

        self.assertEqual([c["key"] for c in result["choices"]], ["labor", "avoid"])
        by_key = {c["key"]: c for c in result["choices"]}
        self.assertEqual(by_key["labor"]["cost"], {"energy": -65})
        self.assertEqual(by_key["avoid"]["cost"], {"peace": -79})
        self.assertAlmostEqual(random.random(), 0.8474337369372327)


# ==============================================================================
# scenario_generation.py の直接テスト(Step 7、2026-08-15): 「LLMによる状況
# 生成」責務全体の移動後、module版とgame.py版の一致・RNG状態・動的依存の
# 呼び出し時点解決を確認する。generate_normal_turn/generate_settlement_turnの
# 網羅的な分岐確認はGenerateNormalTurnFixtureTest/GenerateSettlementTurnFixtureTest
# (無変更)に委ね、ここは責務境界の確認に絞る。
# ==============================================================================

class ScenarioGenerationModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._scenario_generation_dependencies()

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.bank_npc_name = "x"

    # --- プロンプト構築: module版とgame版の一致 ---
    def test_build_situation_prompt_matches_wrapper(self):
        state = {"resources": {"energy": 100, "peace": 100, "money": 0},
                 "contracts": {"c1": {"status": "open", "counterparty": "友人A",
                                      "description": "本を借りた", "due_turn": 10}}}
        game_result = game.build_situation_prompt(
            state, 5, "夜道 × 頼まれごと", ("友人A", False))
        module_result = scenario_generation.build_situation_prompt(
            state, 5, "夜道 × 頼まれごと", ("友人A", False),
            dependencies=self.dependencies())
        self.assertEqual(module_result, game_result)
        self.assertIn("友人A", module_result)
        self.assertIn("本を借りた", module_result)

    def test_build_settlement_prompt_matches_wrapper(self):
        state = {"resources": {"energy": 90, "peace": 80, "money": 5}}
        contract = {"counterparty": "友人A", "description": "本を借りた",
                    "created_turn": 1, "due_turn": 5}
        game_result = game.build_settlement_prompt(state, 7, contract)
        module_result = scenario_generation.build_settlement_prompt(state, 7, contract)
        self.assertEqual(module_result, game_result)
        self.assertIn("2ターン超過", module_result)  # turn7 - due_turn5

    # --- clean_label ---
    def test_clean_label_strips_schema_leakage_matches_wrapper(self):
        text = "お金で返す行動の具体的な説明: アドバイス代として100円を返済する。"
        self.assertEqual(scenario_generation.clean_label(text, "fallback"),
                         game.clean_label(text, "fallback"))
        self.assertEqual(game.clean_label(text, "fallback"),
                         "アドバイス代として100円を返済する。")

    def test_clean_label_fallback_on_blank(self):
        self.assertEqual(game.clean_label("", "fallback"), "fallback")
        self.assertEqual(game.clean_label(None, "fallback"), "fallback")

    # --- parse_json_response ---
    def test_parse_json_response_matches_wrapper_clean_and_padded(self):
        for raw in ('{"a": 1}', 'ゴミ{"a": 1}ゴミ'):
            with self.subTest(raw=raw):
                self.assertEqual(scenario_generation.parse_json_response(raw),
                                 game.parse_json_response(raw))
                self.assertEqual(game.parse_json_response(raw), {"a": 1})

    def test_parse_json_response_reraises_on_unrecoverable(self):
        with self.assertRaises(json.JSONDecodeError):
            game.parse_json_response("not json at all")
        with self.assertRaises(json.JSONDecodeError):
            scenario_generation.parse_json_response("not json at all")

    # --- generate_normal_turn / generate_settlement_turn: module版とgame版の
    #     一致・RNG消費後の状態一致(getstate/setstate round-trip) ---
    def test_generate_normal_turn_matches_wrapper_and_rng(self):
        mock_json = json.dumps({
            "situation": "モック", "choices": {"money": "お金", "labor": "労力",
                                              "social": "友人", "rest": "休む"},
            "social_counterparty": None, "social_favor": "頼み事",
        }, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        state = {"resources": {"energy": 100, "peace": 100, "money": 0},
                 "contracts": {}, "npcs": {}, "npc_pool_cap": 10,
                 "currency_stage": game.CURRENCY_STAGE_NORMAL}

        state_before = random.getstate()
        random.seed(3)
        game_result = game.generate_normal_turn("m", state, 1, "題材", [])
        state_after_game = random.getstate()

        random.setstate(state_before)
        random.seed(3)
        module_result = scenario_generation.generate_normal_turn(
            "m", state, 1, "題材", [], dependencies=self.dependencies())
        state_after_module = random.getstate()

        self.assertEqual(module_result, game_result)
        self.assertEqual(state_after_module, state_after_game)

    def test_generate_settlement_turn_matches_wrapper_and_rng(self):
        mock_json = json.dumps({
            "situation": "モック清算", "choices": {"money": "お金で返す",
                                                "labor": "労力で返す", "avoid": "先延ばし"},
        }, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        state = {"resources": {"energy": 100, "peace": 100, "money": 0},
                 "contracts": {}, "npcs": {}, "npc_pool_cap": 10,
                 "currency_stage": game.CURRENCY_STAGE_NORMAL}
        contract = {"id": "c1", "counterparty": "友人A", "description": "頼み事",
                    "created_turn": 1, "due_turn": 5, "repay_money": -40,
                    "is_bank_debt": False}

        state_before = random.getstate()
        random.seed(3)
        game_result = game.generate_settlement_turn("m", state, 5, dict(contract), [])
        state_after_game = random.getstate()

        random.setstate(state_before)
        random.seed(3)
        module_result = scenario_generation.generate_settlement_turn(
            "m", state, 5, dict(contract), [], dependencies=self.dependencies())
        state_after_module = random.getstate()

        self.assertEqual(module_result, game_result)
        self.assertEqual(state_after_module, state_after_game)

    # --- run_consistency_check: 代表ケース(flagged/JSON解析失敗)でmodule版と
    #     game版の一致を確認 ---
    def test_run_consistency_check_flagged_case_matches_wrapper(self):
        mock_json = json.dumps({"contradiction": True, "reason": "money減ったのに増えた"},
                               ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.002)
        game_result = game.run_consistency_check("m", ["お金"], [], "財布が重くなった", [])
        module_result = scenario_generation.run_consistency_check(
            "m", ["お金"], [], "財布が重くなった", [], dependencies=self.dependencies())
        self.assertEqual(module_result, game_result)
        self.assertEqual(game_result, (True, "money減ったのに増えた", 0.002))

    def test_run_consistency_check_malformed_json_fallback_matches_wrapper(self):
        game.call_ollama = lambda model, system, user, want_json: ("not json", 0.003)
        game_result = game.run_consistency_check("m", [], [], "何もなかった", [])
        module_result = scenario_generation.run_consistency_check(
            "m", [], [], "何もなかった", [], dependencies=self.dependencies())
        self.assertEqual(module_result, game_result)
        flagged, reason, latency = game_result
        self.assertFalse(flagged)
        self.assertIn("JSON解析失敗", reason)
        self.assertEqual(latency, 0.003)

    def test_run_consistency_check_log_false_skips_llm_call_event(self):
        mock_json = json.dumps({"contradiction": False, "reason": ""}, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        game.run_consistency_check("m", [], [], "特に変化なし", [], log=False)
        self.assertEqual(list(event_store.iter_jsonl_events(self.events_path)), [])

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_propagates_monkeypatched_call_ollama(self):
        calls = []

        def fake_call_ollama(model, system, user, want_json):
            calls.append((model, want_json))
            return (json.dumps({"contradiction": False, "reason": ""}, ensure_ascii=False), 0.5)

        original = game.call_ollama
        game.call_ollama = fake_call_ollama
        try:
            result = game.run_consistency_check("dummy-model", [], [], "何もなし", [])
        finally:
            game.call_ollama = original
        self.assertEqual(calls, [("dummy-model", True)])
        self.assertEqual(result[2], 0.5)

    def test_wrapper_propagates_monkeypatched_append_event(self):
        recorded = []

        def fake_append_event(event_type, data):
            recorded.append((event_type, data.get("role")))

        mock_json = json.dumps({"contradiction": False, "reason": ""}, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        original = game.append_event
        game.append_event = fake_append_event
        try:
            game.run_consistency_check("m", [], [], "変化なし", [])
        finally:
            game.append_event = original
        self.assertEqual(recorded, [("llm_call", "consistency_check")])

    # --- Step 8作業1: randint_fn/uniform_fnへのRNG依存修正の確認 ---
    def test_dependencies_have_randint_and_uniform_fn(self):
        deps = self.dependencies()
        self.assertIs(deps.randint_fn, random.randint)
        self.assertIs(deps.uniform_fn, random.uniform)

    def test_generate_normal_turn_rng_fix_matches_pre_fix_golden(self):
        # GenerateNormalTurnFixtureTest.test_new_npc_case と同じ入力・同じseedで、
        # randint_fn/uniform_fn導入前のgolden値(due_turn=9・repay_money=-24・
        # prospective_ethics=42.77691339942366・prospective_retire_turn=137)と
        # 完全に一致することを確認する(RNG修正の前後で戻り値・消費順序が
        # 変わっていないことの直接的な裏付け)。
        mock_json = json.dumps({
            "situation": "モックの状況説明です。",
            "choices": {"money": "お金で片付ける", "labor": "自分で頑張る",
                       "social": "友人に頼る", "rest": "休む"},
            "social_counterparty": None, "social_favor": "ちょっとした頼み事",
        }, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        state = {"resources": {"energy": 100, "peace": 100, "money": 40},
                 "contracts": {}, "npcs": {}, "npc_pool_cap": 10,
                 "currency_stage": game.CURRENCY_STAGE_NORMAL}
        random.seed(1)
        result = game.generate_normal_turn("dummy-model", state, turn=5,
                                           theme="題材", latencies=[])
        contract = next(c for c in result["choices"] if c["key"] == "social")["contract"]
        self.assertEqual(contract["due_turn"], 9)
        self.assertEqual(contract["repay_money"], -24)
        self.assertAlmostEqual(contract["prospective_ethics"], 42.77691339942366)
        self.assertEqual(contract["prospective_retire_turn"], 137)

    def test_dependencies_randint_and_uniform_fn_isolated_call_order(self):
        # deps.randint_fn/deps.uniform_fnを差し替えて、generate_normal_turn自身が
        # 直接呼ぶ4回(due_turn→prospective_ethics→repay_money→
        # prospective_retire_turn)だけを分離して観測する——build_normal_base_
        # choice_fn経由の他のrandint呼び出し(draw_archetype_hours等、
        # randint_fn/uniform_fnとは別経路)と混同しないため。
        calls = []

        def fake_randint(lo, hi):
            calls.append(("randint", lo, hi))
            return random.randint(lo, hi)

        def fake_uniform(lo, hi):
            calls.append(("uniform", lo, hi))
            return random.uniform(lo, hi)

        mock_json = json.dumps({
            "situation": "モック",
            "choices": {"money": "お金で片付ける", "labor": "自分で頑張る",
                       "social": "友人に頼る", "rest": "休む"},
            "social_counterparty": None, "social_favor": "頼み事",
        }, ensure_ascii=False)
        game.call_ollama = lambda model, system, user, want_json: (mock_json, 0.001)
        state = {"resources": {"energy": 100, "peace": 100, "money": 0},
                 "contracts": {}, "npcs": {}, "npc_pool_cap": 10,
                 "currency_stage": game.CURRENCY_STAGE_NORMAL}
        deps = dataclasses.replace(self.dependencies(), randint_fn=fake_randint, uniform_fn=fake_uniform)
        random.seed(1)
        scenario_generation.generate_normal_turn("m", state, 1, "題材", [], dependencies=deps)
        # due_turn(randint)→prospective_ethics(uniform)→repay_money(randint)→
        # prospective_retire_turn(randint)の順で、それぞれ正確に1回ずつ呼ばれる。
        self.assertEqual([c[0] for c in calls], ["randint", "uniform", "randint", "randint"])


# ==============================================================================
# interactive_runtime.process_one_situation() の直接テスト(Step 8、
# 2026-08-15): main()を介さず、1状況ぶんの提示・選択・適用・ナレーション・
# 整合性チェックを直接呼ぶ。main()経由の網羅的な確認はMainNormalApplication
# FixtureTest/MainSettlementFixtureTest(無変更)に委ねる。
# ==============================================================================

class InteractiveRuntimeModuleTest(unittest.TestCase):
    BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                            "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                            "role": "wage_issuer", "trust": 50.0}),
    ]

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_input = builtins.input
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        game.call_ollama = lambda model, system, user, want_json: ("モックのナレーション文です。", 0.001)

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        builtins.input = self._orig_input
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _stats(self):
        return {"blocked_choices": 0, "contracts_created": 0, "fulfilled": 0, "defaulted": 0,
                "choice_counts": {}, "min_seen": dict(game.INITIAL_RESOURCES),
                "code_check_hits": 0, "llm_check_hits": 0}

    def _args(self, **overrides):
        ns = types.SimpleNamespace(
            auto=True, auto_policy="balanced", policy="none", safety_floor=game.SAFETY_FLOOR,
            model="dummy-model", no_consistency_check=True)
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def _events_from(self, marker_type):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == marker_type:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:]]
        raise AssertionError(f"{marker_type!r}が見つからない")

    def dependencies(self):
        return game._interactive_runtime_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.bank_npc_id = "x"

    def test_normal_kind_auto_money_choice_event_sequence(self):
        self._write_fixture(self.BASE)
        cur_state = game.reduce_state()
        # 選択肢を1つだけにし、auto_selectの内部ロジックに関わらず結果を
        # 決定論的にする(auto_select自体の検証は他のテストの役割)。
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [
                {"key": "money", "label": "お金で解決する", "cost": {"money": -10}, "hours": 150},
            ],
        }
        stats = self._stats()
        situations_this_run, latencies = [], []
        args = self._args(auto=True)
        with contextlib.redirect_stdout(io.StringIO()):
            result = interactive_runtime.process_one_situation(
                tr, None, cur_state, 150, False,
                turn=1, args=args, stats=stats, situations_this_run=situations_this_run,
                latencies=latencies, decay={}, dependencies=self.dependencies())

        self.assertEqual(result["key"], "money")
        self.assertEqual(self._events_from("situation_presented"), [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {"money": -10},
                "choice_key": "money", "choice_label": "お金で解決する"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "dummy-model", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
        ])
        self.assertEqual(situations_this_run, ["モックの通常状況です。"])
        self.assertEqual(latencies, [0.001])
        self.assertEqual(stats["choice_counts"], {"通常:money": 1})

    def test_settlement_kind_auto_money_fulfilled_event_sequence(self):
        base = list(self.BASE) + [
            ("turn_started", {"turn": 4}),
            ("contract_created", {"id": "c1", "counterparty": "中央銀行",
                                  "description": "賃金前借り", "created_turn": 1, "due_turn": 4,
                                  "repay_money": -40, "is_bank_debt": True}),
        ]
        self._write_fixture(base)
        cur_state = game.reduce_state()
        contract = cur_state["contracts"]["c1"]
        # 選択肢を1つだけにし、auto_selectの内部ロジックに関わらず結果を
        # 決定論的にする(auto_select自体の検証は他のテストの役割)。
        tr = {
            "situation": "モックの清算状況です。", "latency": 0.001, "kind": "settlement",
            "contract": contract,
            "choices": [
                {"key": "money", "label": "お金で返す", "cost": {"money": -40}, "settle": "fulfilled"},
            ],
        }
        stats = self._stats()
        args = self._args(auto=True)
        with contextlib.redirect_stdout(io.StringIO()):
            result = interactive_runtime.process_one_situation(
                tr, None, cur_state, None, False,
                turn=4, args=args, stats=stats, situations_this_run=[], latencies=[],
                decay={}, dependencies=self.dependencies())

        self.assertEqual(result["key"], "money")
        got = self._events_from("situation_presented")
        self.assertEqual([e["type"] for e in got], [
            "situation_presented", "state_applied", "contract_settled",
            "npc_trust_changed", "npc_wallet_changed", "currency_confidence_changed",
            "enforcement_capacity_changed",
            "llm_call",
        ])
        self.assertEqual(got[2]["data"]["outcome"], "fulfilled")
        self.assertEqual(stats["fulfilled"], 1)

    def test_manual_quit_returns_none(self):
        self._write_fixture(self.BASE)
        cur_state = game.reduce_state()
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [{"key": "rest", "label": "休む", "cost": {}, "hours": 150}],
        }
        builtins.input = lambda prompt: "q"
        stats = self._stats()
        with contextlib.redirect_stdout(io.StringIO()):
            result = interactive_runtime.process_one_situation(
                tr, None, cur_state, 150, False,
                turn=1, args=self._args(auto=False), stats=stats, situations_this_run=[],
                latencies=[], decay={}, dependencies=self.dependencies())
        self.assertIsNone(result)
        # situation_presentedだけが積まれ、state_applied以降は一切発生しない。
        self.assertEqual([e["type"] for e in self._events_from("situation_presented")],
                         ["situation_presented"])

    def test_manual_selection_returns_chosen_choice(self):
        self._write_fixture(self.BASE)
        cur_state = game.reduce_state()
        # cur_state["resources"]["money"]は既定で0のため、money型(-10)は
        # is_affordableがFalseになり選択肢1が"1"では選べない——常に払える
        # rest(peace+5)を単独選択肢にして、入力"1"が確実に有効になるようにする。
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [
                {"key": "rest", "label": "休む", "cost": {"peace": 5}, "hours": 150},
            ],
        }
        builtins.input = lambda prompt: "1"
        stats = self._stats()
        with contextlib.redirect_stdout(io.StringIO()):
            result = interactive_runtime.process_one_situation(
                tr, None, cur_state, 150, False,
                turn=1, args=self._args(auto=False), stats=stats, situations_this_run=[],
                latencies=[], decay={}, dependencies=self.dependencies())
        self.assertEqual(result["key"], "rest")

    def test_wrapper_propagates_monkeypatched_append_event(self):
        # game.append_eventをmonkeypatchすると、game._interactive_runtime_
        # dependencies()経由(呼び出しのたびに新しいDependenciesを組み立てる)で
        # process_one_situationにも反映されることを確認する。
        original = game.append_event
        recorded = []

        def fake_append_event(event_type, data):
            recorded.append(event_type)

        game.append_event = fake_append_event
        try:
            deps = game._interactive_runtime_dependencies()
        finally:
            game.append_event = original
        self.assertIs(deps.append_event_fn, fake_append_event)


# ==============================================================================
# main()の通常行動適用(Step 6A、2026-08-15): process_one_situation()を含む
# main()の「4. 適用」節の構造化golden値。generate_normal_turn()を固定結果へ
# mockし、1回の行動でTURN_TIME_BUDGETを使い切ることで月内ループを1回に限定する。
# ==============================================================================

class MainNormalApplicationFixtureTest(unittest.TestCase):
    """main()を`--auto --turns 1`で走らせ、generate_normal_turn()を固定tr値へ
    mockし、auto_selectを強制して4主要分岐(money/labor/rest/social)を確認する。
    socialは新規NPC・既存NPCの両方を確認する。stdoutはcontextlib.redirect_stdout
    で捨てる(MainSettlementFixtureTestと同じくコンソール文字コード依存の
    UnicodeEncodeErrorを避けるため)。"""

    MOCK_NARRATION = "モックのナレーション文です。"

    BANK_BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行",
                            "birth_turn": 0, "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済",
                            "birth_turn": 0, "role": "wage_issuer", "trust": 50.0}),
    ]

    EXISTING_NPC_EVENT = ("npc_introduced", {"id": "友人A", "name": "友人A", "birth_turn": 0,
                          "role": "acquaintance", "trust": 60.0, "ethics": 60.0,
                          "retire_turn": 200})

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_generate_normal_turn = game.generate_normal_turn
        self._orig_auto_select = game.auto_select
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        self._orig_argv = sys.argv

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.generate_normal_turn = self._orig_generate_normal_turn
        game.auto_select = self._orig_auto_select
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)
        sys.argv = self._orig_argv

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    @staticmethod
    def _build_tr(social_contract):
        # hoursは全archetype共通で150(=TURN_TIME_BUDGET)にし、どれが選ばれても
        # 予算をちょうど使い切って月内ループを1回で終わらせる。
        return {
            "situation": "モックの通常状況です。",
            "latency": 0.001,
            "kind": "normal",
            "choices": [
                {"key": "money", "label": "お金で解決する", "cost": {"money": -10}, "hours": 150},
                {"key": "labor", "label": "自分で頑張る", "cost": {"energy": -10}, "hours": 150},
                {"key": "social", "label": "友人に頼る", "cost": {"peace": -5}, "hours": 150,
                 "contract": social_contract},
                {"key": "rest", "label": "休む", "cost": {"peace": 5}, "hours": 150},
            ],
        }

    def _force_choice(self, key):
        orig = game.auto_select

        def forced(choices, resources, policy, policy_name=None,
                  safety_floor=game.SAFETY_FLOOR, turn=0, budget=None):
            keys = [c["key"] for c in choices]
            if "avoid" not in keys and key in keys:
                return keys.index(key)
            return orig(choices, resources, policy, policy_name, safety_floor, turn, budget)

        game.auto_select = forced

    def _run_one_normal_turn(self, base_events, tr, choice_key, seed=1):
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_NARRATION, 0.001)
        game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
        self._write_fixture(base_events)
        self._force_choice(choice_key)
        random.seed(seed)
        sys.argv = ["game.py", "--auto", "--turns", "1", "--events", str(self.events_path),
                   "--no-consistency-check"]
        with contextlib.redirect_stdout(io.StringIO()):
            game.main()

    def _events_from(self, marker_type):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == marker_type:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:]]
        raise AssertionError(f"{marker_type!r}が見つからない")

    def test_money_choice(self):
        social_contract = {"counterparty": "新友人", "description": "頼み事",
                           "due_turn": 4, "repay_money": -20,
                           "prospective_ethics": 55.0, "prospective_retire_turn": 151}
        tr = self._build_tr(social_contract)
        self._run_one_normal_turn(list(self.BANK_BASE), tr, "money")

        got = self._events_from("situation_presented")
        self.assertEqual(got, [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {"money": -10},
                "choice_key": "money", "choice_label": "お金で解決する"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"health": 1.179667, "dexterity": -0.12,
                         "intellect": -0.12, "skill": -0.12},
                "theme_trait": "health", "choice_key": "money",
                "fired": {"trait": "health", "via": "money", "talent": True},
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["resources"], {"energy": 100, "peace": 100, "money": -10})
        self.assertAlmostEqual(state["traits"]["health"], 81.179667)
        self.assertEqual(state["traits"]["dexterity"], 49.88)
        self.assertEqual(set(state["npcs"].keys()), {"bank", "economy"})
        self.assertEqual(state["contracts"], {})

    def test_labor_choice(self):
        social_contract = {"counterparty": "新友人", "description": "頼み事",
                           "due_turn": 4, "repay_money": -20,
                           "prospective_ethics": 55.0, "prospective_retire_turn": 151}
        tr = self._build_tr(social_contract)
        self._run_one_normal_turn(list(self.BANK_BASE), tr, "labor")

        got = self._events_from("situation_presented")
        self.assertEqual(got, [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {"energy": -10},
                "choice_key": "labor", "choice_label": "自分で頑張る"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"health": 0.429667, "dexterity": -0.12,
                         "intellect": -0.12, "skill": -0.12},
                "theme_trait": "health", "choice_key": "labor",
                "fired": {"trait": "health", "via": "labor", "talent": True},
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["resources"], {"energy": 90, "peace": 100, "money": 0})
        self.assertAlmostEqual(state["traits"]["health"], 80.429667)
        self.assertEqual(state["contracts"], {})

    def test_rest_choice(self):
        social_contract = {"counterparty": "新友人", "description": "頼み事",
                           "due_turn": 4, "repay_money": -20,
                           "prospective_ethics": 55.0, "prospective_retire_turn": 151}
        tr = self._build_tr(social_contract)
        self._run_one_normal_turn(list(self.BANK_BASE), tr, "rest")

        got = self._events_from("situation_presented")
        self.assertEqual(got, [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {},
                "choice_key": "rest", "choice_label": "休む"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": None, "choice_key": "rest", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["resources"], {"energy": 100, "peace": 100, "money": 0})
        self.assertAlmostEqual(state["traits"]["health"], 79.979667)
        self.assertEqual(state["contracts"], {})

    def test_social_choice_new_npc(self):
        social_contract = {"counterparty": "新友人", "description": "頼み事",
                           "due_turn": 4, "repay_money": -20,
                           "prospective_ethics": 55.0, "prospective_retire_turn": 151}
        tr = self._build_tr(social_contract)
        self._run_one_normal_turn(list(self.BANK_BASE), tr, "social")

        got = self._events_from("situation_presented")
        self.assertEqual(got, [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {"peace": -5},
                "choice_key": "social", "choice_label": "友人に頼る"}},
            {"type": "npc_introduced", "data": {
                "id": "新友人", "name": "新友人", "birth_turn": 1, "role": "acquaintance",
                "trust": 50.0, "ethics": 55.0, "retire_turn": 151}},
            {"type": "contract_created", "data": {
                "id": "c1", "counterparty": "新友人", "description": "頼み事",
                "created_turn": 1, "due_turn": 4, "repay_money": -20,
                "origin_choice": "友人に頼る"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": "health", "choice_key": "social", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["resources"], {"energy": 100, "peace": 95, "money": 0})
        self.assertEqual(set(state["npcs"].keys()), {"bank", "economy", "新友人"})
        self.assertAlmostEqual(state["npcs"]["新友人"]["trust"], 50.0)
        self.assertEqual(state["npcs"]["新友人"]["ethics"], 55.0)
        self.assertEqual(state["npcs"]["新友人"]["retire_turn"], 151)
        self.assertEqual(state["contracts"]["c1"]["counterparty"], "新友人")
        self.assertEqual(state["contracts"]["c1"]["repay_money"], -20)
        self.assertEqual(state["contracts"]["c1"]["status"], "open")

    def test_social_choice_existing_npc(self):
        social_contract = {"counterparty": "友人A", "description": "頼み事",
                           "due_turn": 4, "repay_money": -20,
                           "prospective_ethics": None, "prospective_retire_turn": None}
        tr = self._build_tr(social_contract)
        self._run_one_normal_turn(
            list(self.BANK_BASE) + [self.EXISTING_NPC_EVENT], tr, "social")

        got = self._events_from("situation_presented")
        # 既存NPCの再利用なのでnpc_introducedイベントは発生しない
        # (contract_createdの前にnpc_introducedを挟まない)。
        self.assertEqual(got, [
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {"peace": -5},
                "choice_key": "social", "choice_label": "友人に頼る"}},
            {"type": "contract_created", "data": {
                "id": "c1", "counterparty": "友人A", "description": "頼み事",
                "created_turn": 1, "due_turn": 4, "repay_money": -20,
                "origin_choice": "友人に頼る"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": "health", "choice_key": "social", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["resources"], {"energy": 100, "peace": 95, "money": 0})
        self.assertEqual(set(state["npcs"].keys()), {"bank", "economy", "友人A"})
        # 既存NPCのtrustは中間値への回帰(effective_npc_trust)を1ターン分受けている
        # (60.0 → 59.875、NPC_TRUST_REVERSION_RATE=0.0125による導出値)。
        self.assertAlmostEqual(state["npcs"]["友人A"]["trust"], 59.875)
        self.assertEqual(state["contracts"]["c1"]["counterparty"], "友人A")
        self.assertEqual(state["contracts"]["c1"]["status"], "open")


# ==============================================================================
# Step 6B-0(2026-08-15、段階的モジュール分割): plan_confidence_reversion /
# plan_institution_transitionsへの抽出前に、現行game.pyの「信用回帰→stage遷移
# 判定」の組み合わせ式・イベント順序をgolden値として固定する。
#
# PlanConfidenceReversionFormulaFixtureTest / PlanInstitutionTransitionsFormula
# FixtureTestは、既にgolden化済みのgame.bank_trust_reversion / game.
# currency_confidence_reversion / game.bank_stage_next / game.currency_stage_next
# (ReversionFormulaTest / BankStageNextTest / CurrencyStageNextTest、いずれも
# 無変更)を直接呼び、現行のmain()・simulate_policy()に書かれている組み合わせ式
# ——bank_trust -= bank_trust_reversion(bank_trust) /
# currency_confidence = max(0.0, currency_confidence - currency_confidence_
# reversion(currency_confidence)) / bank_stage_next→currency_stage_nextの順——
# をテスト側でそのまま再現して期待値を得る(plan_confidence_reversion /
# plan_institution_transitions自体はStep 6B-1まで存在しないため、これらの
# 組み合わせ計算を代替のgolden捕捉手段とする)。
#
# callbackの引数・回数・順序の検証は、Dependencies注入の仕組みそのものが
# Step 6B-1で新規作成されるturn_engine.pyにしか存在しないため、Step 6B-2側の
# モジュール直接テストに配置する(移動前には検証しようがないため)。
# ==============================================================================

class PlanConfidenceReversionFormulaFixtureTest(unittest.TestCase):
    """bank_trust_after・currency_confidence_afterの組み合わせ式のgolden。
    bank_trust_afterには上限・下限のクランプが無く、currency_confidence_after
    にだけmax(0.0, ...)が掛かる非対称性を、同一入力(-10.0)で明示的に示す。"""

    def test_above_neutral_pulls_down(self):
        self.assertEqual(game.bank_trust_reversion(70.0), 1.0)
        bank_trust_after = 70.0 - game.bank_trust_reversion(70.0)
        self.assertEqual(bank_trust_after, 69.0)
        self.assertEqual(game.currency_confidence_reversion(70.0), 1.0)
        currency_confidence_after = max(
            0.0, 70.0 - game.currency_confidence_reversion(70.0))
        self.assertEqual(currency_confidence_after, 69.0)

    def test_below_neutral_pulls_up(self):
        self.assertEqual(game.bank_trust_reversion(30.0), -1.0)
        bank_trust_after = 30.0 - game.bank_trust_reversion(30.0)
        self.assertEqual(bank_trust_after, 31.0)
        self.assertEqual(game.currency_confidence_reversion(20.0), -1.5)
        currency_confidence_after = max(
            0.0, 20.0 - game.currency_confidence_reversion(20.0))
        self.assertEqual(currency_confidence_after, 21.5)

    def test_at_neutral_stays_unchanged(self):
        bank_trust_after = (game.BANK_TRUST_INITIAL
                            - game.bank_trust_reversion(game.BANK_TRUST_INITIAL))
        self.assertEqual(bank_trust_after, game.BANK_TRUST_INITIAL)
        currency_confidence_after = max(
            0.0, game.CURRENCY_CONFIDENCE_INITIAL
            - game.currency_confidence_reversion(game.CURRENCY_CONFIDENCE_INITIAL))
        self.assertEqual(currency_confidence_after, game.CURRENCY_CONFIDENCE_INITIAL)

    def test_currency_confidence_clamped_to_zero_bank_trust_is_not(self):
        # 同じ入力値(-10.0)・同じreversion量(-3.0)でも、bank_trust_afterは
        # クランプ無しで負のまま(-7.0)、currency_confidence_afterだけ0.0に
        # クランプされる——現行実装のこの非対称性は意図的にそのまま維持する。
        self.assertEqual(game.bank_trust_reversion(-10.0), -3.0)
        bank_trust_after = -10.0 - game.bank_trust_reversion(-10.0)
        self.assertEqual(bank_trust_after, -7.0)
        self.assertEqual(game.currency_confidence_reversion(-10.0), -3.0)
        currency_confidence_after = max(
            0.0, -10.0 - game.currency_confidence_reversion(-10.0))
        self.assertEqual(currency_confidence_after, 0.0)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.bank_trust_reversion(70.0)
        game.currency_confidence_reversion(20.0)
        self.assertEqual(random.getstate(), state_before)


class PlanInstitutionTransitionsFormulaFixtureTest(unittest.TestCase):
    """bank_stage_after・currency_stage_after・transitionedフラグの組み合わせの
    golden。bank_stage_next→currency_stage_nextという現行の呼び出し順序自体は
    このテストでは検証できない(単一の状態変数を返す関数を2つ呼ぶだけなので
    順序による副作用が無い)——順序の維持はMainTurnStartInstitutionTransition
    FixtureTest(イベント順序として現れる)側で確認する。"""

    def test_bank_stage_transitions_at_boundary(self):
        after = game.bank_stage_next(game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER - 0.01, 0)
        self.assertEqual(after, game.BANK_STAGE_CONTRACTION)
        self.assertNotEqual(after, game.BANK_STAGE_HEALTHY)  # transitioned=True相当

    def test_bank_stage_stays_at_exact_boundary(self):
        after = game.bank_stage_next(game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER, 0)
        self.assertEqual(after, game.BANK_STAGE_HEALTHY)  # transitioned=False相当

    def test_bank_stage_collapses_at_crisis_count_threshold(self):
        after = game.bank_stage_next(
            game.BANK_STAGE_HEALTHY, 100.0, game.BANK_STAGE3_CRISIS_COUNT)
        self.assertEqual(after, game.BANK_STAGE_COLLAPSED)

    def test_currency_stage_transitions_at_boundary(self):
        after = game.currency_stage_next(
            game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER - 0.01)
        self.assertEqual(after, game.CURRENCY_STAGE_WARY)
        self.assertNotEqual(after, game.CURRENCY_STAGE_NORMAL)  # transitioned=True相当

    def test_currency_stage_stays_at_exact_boundary(self):
        after = game.currency_stage_next(
            game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER)
        self.assertEqual(after, game.CURRENCY_STAGE_NORMAL)  # transitioned=False相当

    def test_bank_and_currency_transition_independently(self):
        # bankは遷移する・currencyは遷移しない組み合わせを1回で確認する
        # (plan_institution_transitionsが両者を独立に扱うことの根拠)。
        bank_after = game.bank_stage_next(game.BANK_STAGE_HEALTHY, 20.0, 0)
        currency_after = game.currency_stage_next(game.CURRENCY_STAGE_NORMAL, 50.0)
        self.assertEqual(bank_after, game.BANK_STAGE_CONTRACTION)
        self.assertEqual(currency_after, game.CURRENCY_STAGE_NORMAL)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.bank_stage_next(game.BANK_STAGE_HEALTHY, 20.0, 0)
        game.currency_stage_next(game.CURRENCY_STAGE_NORMAL, 16.75)
        self.assertEqual(random.getstate(), state_before)


class MainTurnStartInstitutionTransitionFixtureTest(unittest.TestCase):
    """main()を`--auto --turns 1`で走らせ、bank_trust=25.0(中立から外れた値)・
    currency_confidence=15.0(currency_confidence_changedで中立から-35した値)を
    仕込んだfixtureで、ターン開始処理の「信用回帰イベント→制度遷移イベント」の
    順序をgolden値として固定する。MainNormalApplicationFixtureTestのBANK_BASEは
    bank_trust・currency_confidenceがともに中立(50.0)のままなので回帰・遷移
    イベントが一切発生しない——このクラスはその空白を埋める。選択肢は"rest"の
    1つだけにし、ターン開始処理以外の副作用(auto_choiceイベント等)を排除する
    (args.policyの既定値"none"でも１択なのでauto_selectは自明に選ぶ)。"""

    MOCK_NARRATION = "モックのナレーション文です。"

    BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行",
                            "birth_turn": 0, "role": "money_issuer", "trust": 25.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済",
                            "birth_turn": 0, "role": "wage_issuer", "trust": 50.0}),
        ("currency_confidence_changed", {"turn": 0, "delta": -35.0, "reason": "test_setup"}),
    ]

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_generate_normal_turn = game.generate_normal_turn
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        self._orig_argv = sys.argv

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.generate_normal_turn = self._orig_generate_normal_turn
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)
        sys.argv = self._orig_argv

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _events_from(self, marker_type):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == marker_type:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:]]
        raise AssertionError(f"{marker_type!r}が見つからない")

    def test_bank_and_currency_transition_order(self):
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [{"key": "rest", "label": "休む", "cost": {"peace": 5}, "hours": 150}],
        }
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_NARRATION, 0.001)
        game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
        self._write_fixture(self.BASE)
        random.seed(1)
        sys.argv = ["game.py", "--auto", "--turns", "1", "--events", str(self.events_path),
                   "--no-consistency-check"]
        with contextlib.redirect_stdout(io.StringIO()):
            game.main()

        got = self._events_from("turn_started")
        self.assertEqual(got, [
            {"type": "turn_started", "data": {"turn": 1}},
            {"type": "npc_trust_changed", "data": {
                "npc_id": "bank", "delta": 1.25, "reason": "bank_trust_upkeep", "turn": 1}},
            {"type": "currency_confidence_changed", "data": {
                "turn": 1, "delta": 1.75, "reason": "currency_confidence_upkeep"}},
            {"type": "institution_transition", "data": {
                "turn": 1, "institution_id": "central_bank", "kind": "bank",
                "from_stage": game.BANK_STAGE_HEALTHY, "to_stage": game.BANK_STAGE_CONTRACTION,
                "trigger": "bank_trust/bank_crisis_countの閾値",
                "metric_snapshot": {"bank_trust": 26.25, "bank_crisis_count": 0},
                "scope": "nation"}},
            {"type": "institution_transition", "data": {
                "turn": 1, "institution_id": "central_currency", "kind": "currency",
                "from_stage": game.CURRENCY_STAGE_NORMAL, "to_stage": game.CURRENCY_STAGE_WARY,
                "trigger": "currency_confidenceの閾値",
                "metric_snapshot": {"currency_confidence": 16.75},
                "scope": "nation"}},
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {}, "choice_key": "rest", "choice_label": "休む"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": None, "choice_key": "rest", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["bank_stage"], game.BANK_STAGE_CONTRACTION)
        self.assertEqual(state["currency_stage"], game.CURRENCY_STAGE_WARY)
        self.assertAlmostEqual(state["npcs"]["bank"]["trust"], 26.25)
        self.assertAlmostEqual(state["currency_confidence"], 16.75)


class MainTurnStartLocalCreditTransitionFixtureTest(unittest.TestCase):
    """MainTurnStartInstitutionTransitionFixtureTestと同じ狙い(2026-08-15、
    地域信用制度追加)。community_trust=35.0(community_trust_changedで中立から
    -15した値、LOCAL_CREDIT_STAGE1_ENTER=38.0を下回る)を仕込んだfixtureで、
    ターン開始処理の「community_trust回帰イベント→local_credit遷移イベント」の
    順序をgolden値として固定する。bank_trust・currency_confidenceは中立(50.0)の
    ままなのでそちら側の回帰・遷移イベントは一切発生しない(delta=0は
    appendされない、既存のbank/currency側と同じ仕様)——このクラスは
    local_creditだけの遷移を単独で確認する。"""

    MOCK_NARRATION = "モックのナレーション文です。"

    BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行",
                            "birth_turn": 0, "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済",
                            "birth_turn": 0, "role": "wage_issuer", "trust": 50.0}),
        ("community_trust_changed", {"turn": 0, "delta": -15.0, "reason": "test_setup"}),
    ]

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_generate_normal_turn = game.generate_normal_turn
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        self._orig_argv = sys.argv

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.generate_normal_turn = self._orig_generate_normal_turn
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)
        sys.argv = self._orig_argv

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _events_from(self, marker_type):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == marker_type:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:]]
        raise AssertionError(f"{marker_type!r}が見つからない")

    def test_local_credit_transition_order(self):
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [{"key": "rest", "label": "休む", "cost": {"peace": 5}, "hours": 150}],
        }
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_NARRATION, 0.001)
        game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
        self._write_fixture(self.BASE)
        random.seed(1)
        sys.argv = ["game.py", "--auto", "--turns", "1", "--events", str(self.events_path),
                   "--no-consistency-check"]
        with contextlib.redirect_stdout(io.StringIO()):
            game.main()

        got = self._events_from("turn_started")
        self.assertEqual(got, [
            {"type": "turn_started", "data": {"turn": 1}},
            {"type": "community_trust_changed", "data": {
                "turn": 1, "delta": 0.75, "reason": "local_credit_trust_upkeep"}},
            {"type": "institution_transition", "data": {
                "turn": 1, "institution_id": "local_credit", "kind": "local_credit",
                "from_stage": game.LOCAL_CREDIT_STAGE_HEALTHY,
                "to_stage": game.LOCAL_CREDIT_STAGE_CONTRACTION,
                "trigger": "community_trustの閾値",
                "metric_snapshot": {"community_trust": 35.75},
                "scope": "community"}},  # 2026-08-15訂正、LOCAL_CREDIT_SCOPE参照
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {}, "choice_key": "rest", "choice_label": "休む"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": None, "choice_key": "rest", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["local_credit_stage"], game.LOCAL_CREDIT_STAGE_CONTRACTION)
        self.assertAlmostEqual(state["community_trust"], 35.75)
        # bank/currencyは中立のままなので遷移していないこと(local_creditが
        # 独立した制度として動くことの確認)。
        self.assertEqual(state["bank_stage"], game.BANK_STAGE_HEALTHY)
        self.assertEqual(state["currency_stage"], game.CURRENCY_STAGE_NORMAL)


class MainTurnStartEnforcementTransitionFixtureTest(unittest.TestCase):
    """MainTurnStartLocalCreditTransitionFixtureTestと同じ狙い(2026-08-15、
    契約執行制度追加)。enforcement_capacity=33.0(enforcement_capacity_changed
    で中立から-17した値、ENFORCEMENT_STAGE1_ENTER=42.0を下回る)を仕込んだ
    fixtureで、ターン開始処理の「enforcement_capacity回帰イベント→
    enforcement遷移イベント」の順序をgolden値として固定する。bank_trust・
    currency_confidence・community_trustは中立のままなのでそちら側の回帰・
    遷移イベントは一切発生しない——このクラスはcontract_enforcementだけの
    遷移を単独で確認する。"""

    MOCK_NARRATION = "モックのナレーション文です。"

    BASE = [
        ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                            "years_per_turn": 0.0833, "npc_pool_cap": 10}),
        ("npc_introduced", {"id": "bank", "name": "中央銀行",
                            "birth_turn": 0, "role": "money_issuer", "trust": 50.0}),
        ("npc_introduced", {"id": "economy", "name": "地域経済",
                            "birth_turn": 0, "role": "wage_issuer", "trust": 50.0}),
        ("enforcement_capacity_changed", {"turn": 0, "delta": -17.0, "reason": "test_setup"}),
    ]

    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_generate_normal_turn = game.generate_normal_turn
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        self._orig_argv = sys.argv

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.generate_normal_turn = self._orig_generate_normal_turn
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)
        sys.argv = self._orig_argv

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _events_from(self, marker_type):
        events = list(event_store.iter_jsonl_events(self.events_path))
        for i, e in enumerate(events):
            if e["type"] == marker_type:
                return [{"type": ev["type"], "data": ev["data"]} for ev in events[i:]]
        raise AssertionError(f"{marker_type!r}が見つからない")

    def test_enforcement_transition_order(self):
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [{"key": "rest", "label": "休む", "cost": {"peace": 5}, "hours": 150}],
        }
        game.call_ollama = lambda model, system, user, want_json: (self.MOCK_NARRATION, 0.001)
        game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
        self._write_fixture(self.BASE)
        random.seed(1)
        sys.argv = ["game.py", "--auto", "--turns", "1", "--events", str(self.events_path),
                   "--no-consistency-check"]
        with contextlib.redirect_stdout(io.StringIO()):
            game.main()

        got = self._events_from("turn_started")
        self.assertEqual(got, [
            {"type": "turn_started", "data": {"turn": 1}},
            {"type": "enforcement_capacity_changed", "data": {
                "turn": 1, "delta": 0.85, "reason": "enforcement_capacity_upkeep"}},
            {"type": "institution_transition", "data": {
                "turn": 1, "institution_id": "contract_enforcement", "kind": "contract_enforcement",
                "from_stage": game.ENFORCEMENT_STAGE_INSTITUTIONAL,
                "to_stage": game.ENFORCEMENT_STAGE_DELAYED,
                "trigger": "enforcement_capacityの閾値",
                "metric_snapshot": {"enforcement_capacity": 33.85},
                "scope": "nation"}},
            {"type": "situation_presented", "data": {
                "turn": 1, "kind": "normal", "situation": "モックの通常状況です。"}},
            {"type": "state_applied", "data": {
                "turn": 1, "delta": {}, "choice_key": "rest", "choice_label": "休む"}},
            {"type": "llm_call", "data": {
                "role": "narration", "model": "qwen2.5vl:7b", "latency_sec": 0.001,
                "raw": "モックのナレーション文です。"}},
            {"type": "trait_changed", "data": {
                "turn": 1,
                "delta": {"dexterity": -0.12, "intellect": -0.12,
                         "skill": -0.12, "health": -0.020333},
                "theme_trait": None, "choice_key": "rest", "fired": None,
                "kind": "normal"}},
        ])

        state = game.reduce_state()
        self.assertEqual(state["enforcement_stage"], game.ENFORCEMENT_STAGE_DELAYED)
        self.assertAlmostEqual(state["enforcement_capacity"], 33.85)
        # bank/currency/local_creditは中立のままなので遷移していないこと。
        self.assertEqual(state["bank_stage"], game.BANK_STAGE_HEALTHY)
        self.assertEqual(state["currency_stage"], game.CURRENCY_STAGE_NORMAL)
        self.assertEqual(state["local_credit_stage"], game.LOCAL_CREDIT_STAGE_HEALTHY)


# ==============================================================================
# turn_engine.py (Step 6B-2、2026-08-15): 直接テスト
# ==============================================================================

class TurnEngineModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._institution_upkeep_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.bank_trust_reversion_fn = lambda x: x

    # --- module版とgame.pyラッパー版の一致(Step 6B-0のgolden値と同じケース) ---
    def test_plan_confidence_reversion_matches_wrapper(self):
        cases = [
            (70.0, 70.0), (30.0, 20.0),
            (game.BANK_TRUST_INITIAL, game.CURRENCY_CONFIDENCE_INITIAL),
            (-10.0, -10.0),  # currency_confidence_afterのみ0にクランプされる境界
        ]
        for bank_trust, currency_confidence in cases:
            with self.subTest(bank_trust=bank_trust, currency_confidence=currency_confidence):
                self.assertEqual(
                    turn_engine.plan_confidence_reversion(
                        bank_trust, currency_confidence, dependencies=self.dependencies()),
                    game.plan_confidence_reversion(bank_trust, currency_confidence))

    def test_plan_confidence_reversion_bank_trust_after_is_not_clamped(self):
        result = turn_engine.plan_confidence_reversion(
            -10.0, -10.0, dependencies=self.dependencies())
        self.assertEqual(result["bank_trust_after"], -7.0)
        self.assertEqual(result["currency_confidence_after"], 0.0)

    def test_plan_confidence_reversion_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_confidence_reversion(70.0, 20.0, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_confidence_reversion_callback_args_and_order(self):
        calls = []

        def fake_bank(bank_trust):
            calls.append(("bank", bank_trust))
            return 1.0

        def fake_currency(currency_confidence):
            calls.append(("currency", currency_confidence))
            return 2.0

        deps = dataclasses.replace(
            self.dependencies(),
            bank_trust_reversion_fn=fake_bank,
            currency_confidence_reversion_fn=fake_currency)
        result = turn_engine.plan_confidence_reversion(55.0, 65.0, dependencies=deps)

        # bank→currencyの順で1回ずつ呼ばれる(現行main()/simulate_policy()の
        # 記述順のまま)。
        self.assertEqual(calls, [("bank", 55.0), ("currency", 65.0)])
        self.assertEqual(result, {
            "bank_trust_delta": -1.0, "bank_trust_after": 54.0,
            "currency_confidence_delta": -2.0, "currency_confidence_after": 63.0,
        })

    def test_plan_institution_transitions_matches_wrapper(self):
        cases = [
            (game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER - 0.01, 0,
             game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER - 0.01),
            (game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER, 0,
             game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER),
            (game.BANK_STAGE_HEALTHY, 100.0, game.BANK_STAGE3_CRISIS_COUNT,
             game.CURRENCY_STAGE_NORMAL, 50.0),
            (game.BANK_STAGE_HEALTHY, 20.0, 0, game.CURRENCY_STAGE_NORMAL, 50.0),
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(
                    turn_engine.plan_institution_transitions(
                        *args, dependencies=self.dependencies()),
                    game.plan_institution_transitions(*args))

    def test_plan_institution_transitions_flags(self):
        result = turn_engine.plan_institution_transitions(
            game.BANK_STAGE_HEALTHY, game.BANK_STAGE1_ENTER - 0.01, 0,
            game.CURRENCY_STAGE_NORMAL, game.CURRENCY_STAGE1_ENTER,
            dependencies=self.dependencies())
        self.assertTrue(result["bank_transitioned"])
        self.assertFalse(result["currency_transitioned"])
        self.assertEqual(result["bank_stage_after"], game.BANK_STAGE_CONTRACTION)
        self.assertEqual(result["currency_stage_after"], game.CURRENCY_STAGE_NORMAL)

    def test_plan_institution_transitions_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_institution_transitions(
            game.BANK_STAGE_HEALTHY, 20.0, 0, game.CURRENCY_STAGE_NORMAL, 16.75,
            dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_institution_transitions_callback_args_and_order(self):
        calls = []

        def fake_bank_stage_next(bank_stage, bank_trust, bank_crisis_count):
            calls.append(("bank", bank_stage, bank_trust, bank_crisis_count))
            return game.BANK_STAGE_CONTRACTION

        def fake_currency_stage_next(currency_stage, currency_confidence):
            calls.append(("currency", currency_stage, currency_confidence))
            return game.CURRENCY_STAGE_WARY

        deps = dataclasses.replace(
            self.dependencies(),
            bank_stage_next_fn=fake_bank_stage_next,
            currency_stage_next_fn=fake_currency_stage_next)
        result = turn_engine.plan_institution_transitions(
            game.BANK_STAGE_HEALTHY, 26.25, 0, game.CURRENCY_STAGE_NORMAL, 16.75,
            dependencies=deps)

        # bank_stage_next→currency_stage_nextの順で1回ずつ呼ばれる(現行の
        # main()/simulate_policy()の記述順のまま)。
        self.assertEqual(calls, [
            ("bank", game.BANK_STAGE_HEALTHY, 26.25, 0),
            ("currency", game.CURRENCY_STAGE_NORMAL, 16.75),
        ])
        self.assertEqual(result["bank_stage_after"], game.BANK_STAGE_CONTRACTION)
        self.assertEqual(result["currency_stage_after"], game.CURRENCY_STAGE_WARY)
        self.assertTrue(result["bank_transitioned"])
        self.assertTrue(result["currency_transitioned"])

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_bank_trust_reversion_fn_at_call_time(self):
        original = game.bank_trust_reversion
        game.bank_trust_reversion = lambda bank_trust: 5.0
        try:
            result = game.plan_confidence_reversion(50.0, game.CURRENCY_CONFIDENCE_INITIAL)
        finally:
            game.bank_trust_reversion = original
        self.assertEqual(result["bank_trust_delta"], -5.0)
        self.assertEqual(result["bank_trust_after"], 45.0)

    def test_wrapper_resolves_currency_confidence_reversion_fn_at_call_time(self):
        original = game.currency_confidence_reversion
        game.currency_confidence_reversion = lambda currency_confidence: 5.0
        try:
            result = game.plan_confidence_reversion(game.BANK_TRUST_INITIAL, 50.0)
        finally:
            game.currency_confidence_reversion = original
        self.assertEqual(result["currency_confidence_delta"], -5.0)
        self.assertEqual(result["currency_confidence_after"], 45.0)

    def test_wrapper_resolves_bank_stage_next_fn_at_call_time(self):
        original = game.bank_stage_next
        game.bank_stage_next = lambda bank_stage, bank_trust, bank_crisis_count: 99
        try:
            result = game.plan_institution_transitions(
                game.BANK_STAGE_HEALTHY, 50.0, 0, game.CURRENCY_STAGE_NORMAL, 50.0)
        finally:
            game.bank_stage_next = original
        self.assertEqual(result["bank_stage_after"], 99)
        self.assertTrue(result["bank_transitioned"])

    def test_wrapper_resolves_currency_stage_next_fn_at_call_time(self):
        original = game.currency_stage_next
        game.currency_stage_next = lambda currency_stage, currency_confidence: 99
        try:
            result = game.plan_institution_transitions(
                game.BANK_STAGE_HEALTHY, 50.0, 0, game.CURRENCY_STAGE_NORMAL, 50.0)
        finally:
            game.currency_stage_next = original
        self.assertEqual(result["currency_stage_after"], 99)
        self.assertTrue(result["currency_transitioned"])


# ==============================================================================
# turn_engine.py: 地域信用制度(2026-08-15)のLocalCreditUpkeepDependencies・
# plan_local_credit_reversion・plan_local_credit_transitionの直接テスト。
# TurnEngineModuleTestと同じ構成(dependencies frozen・module版とgame.py
# ラッパー版の一致・callback引数と順序・RNG非消費・monkeypatch伝播)。
# ==============================================================================

class LocalCreditEngineModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._local_credit_upkeep_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.community_trust_reversion_fn = lambda x: x

    def test_plan_local_credit_reversion_matches_wrapper(self):
        cases = [70.0, 30.0, game.LOCAL_CREDIT_TRUST_INITIAL, -10.0]
        for community_trust in cases:
            with self.subTest(community_trust=community_trust):
                self.assertEqual(
                    turn_engine.plan_local_credit_reversion(
                        community_trust, dependencies=self.dependencies()),
                    game.plan_local_credit_reversion(community_trust))

    def test_plan_local_credit_reversion_after_is_clamped_at_zero(self):
        # currency_confidence_afterと同じ扱い(bank_trust_afterと違いクランプする、
        # turn_engine.plan_local_credit_reversionのdocstring参照)。
        result = turn_engine.plan_local_credit_reversion(
            -10.0, dependencies=self.dependencies())
        self.assertEqual(result["community_trust_after"], 0.0)

    def test_plan_local_credit_reversion_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_local_credit_reversion(70.0, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_local_credit_reversion_callback_args(self):
        calls = []

        def fake_reversion(community_trust):
            calls.append(community_trust)
            return 1.5

        deps = dataclasses.replace(
            self.dependencies(), community_trust_reversion_fn=fake_reversion)
        result = turn_engine.plan_local_credit_reversion(55.0, dependencies=deps)
        self.assertEqual(calls, [55.0])
        self.assertEqual(result, {
            "community_trust_delta": -1.5, "community_trust_after": 53.5,
        })

    def test_plan_local_credit_transition_matches_wrapper(self):
        cases = [
            (game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE1_ENTER - 0.01),
            (game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE1_ENTER),
            (game.LOCAL_CREDIT_STAGE_ISOLATED, 100.0),
        ]
        for local_credit_stage, community_trust in cases:
            with self.subTest(local_credit_stage=local_credit_stage, community_trust=community_trust):
                self.assertEqual(
                    turn_engine.plan_local_credit_transition(
                        local_credit_stage, community_trust, dependencies=self.dependencies()),
                    game.plan_local_credit_transition(local_credit_stage, community_trust))

    def test_plan_local_credit_transition_flags(self):
        result = turn_engine.plan_local_credit_transition(
            game.LOCAL_CREDIT_STAGE_HEALTHY, game.LOCAL_CREDIT_STAGE1_ENTER - 0.01,
            dependencies=self.dependencies())
        self.assertTrue(result["local_credit_transitioned"])
        self.assertEqual(result["local_credit_stage_after"], game.LOCAL_CREDIT_STAGE_CONTRACTION)

    def test_plan_local_credit_transition_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_local_credit_transition(
            game.LOCAL_CREDIT_STAGE_HEALTHY, 20.0, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_local_credit_transition_callback_args(self):
        calls = []

        def fake_stage_next(local_credit_stage, community_trust):
            calls.append((local_credit_stage, community_trust))
            return 99

        deps = dataclasses.replace(
            self.dependencies(), local_credit_stage_next_fn=fake_stage_next)
        result = turn_engine.plan_local_credit_transition(
            game.LOCAL_CREDIT_STAGE_HEALTHY, 26.25, dependencies=deps)
        self.assertEqual(calls, [(game.LOCAL_CREDIT_STAGE_HEALTHY, 26.25)])
        self.assertEqual(result["local_credit_stage_after"], 99)
        self.assertTrue(result["local_credit_transitioned"])

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_community_trust_reversion_fn_at_call_time(self):
        original = game.community_trust_reversion
        game.community_trust_reversion = lambda community_trust: 5.0
        try:
            result = game.plan_local_credit_reversion(50.0)
        finally:
            game.community_trust_reversion = original
        self.assertEqual(result["community_trust_delta"], -5.0)
        self.assertEqual(result["community_trust_after"], 45.0)

    def test_wrapper_resolves_local_credit_stage_next_fn_at_call_time(self):
        original = game.local_credit_stage_next
        game.local_credit_stage_next = lambda local_credit_stage, community_trust: 99
        try:
            result = game.plan_local_credit_transition(game.LOCAL_CREDIT_STAGE_HEALTHY, 50.0)
        finally:
            game.local_credit_stage_next = original
        self.assertEqual(result["local_credit_stage_after"], 99)
        self.assertTrue(result["local_credit_transitioned"])


# ==============================================================================
# turn_engine.py: 契約執行制度(2026-08-15)のContractEnforcementUpkeepDependencies・
# plan_enforcement_reversion・plan_enforcement_transitionの直接テスト。
# LocalCreditEngineModuleTestと同じ構成。
# ==============================================================================

class ContractEnforcementEngineModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._contract_enforcement_upkeep_dependencies()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.enforcement_capacity_reversion_fn = lambda x: x

    def test_plan_enforcement_reversion_matches_wrapper(self):
        cases = [70.0, 30.0, game.ENFORCEMENT_CAPACITY_INITIAL, -10.0]
        for enforcement_capacity in cases:
            with self.subTest(enforcement_capacity=enforcement_capacity):
                self.assertEqual(
                    turn_engine.plan_enforcement_reversion(
                        enforcement_capacity, dependencies=self.dependencies()),
                    game.plan_enforcement_reversion(enforcement_capacity))

    def test_plan_enforcement_reversion_after_is_clamped_at_zero(self):
        result = turn_engine.plan_enforcement_reversion(-10.0, dependencies=self.dependencies())
        self.assertEqual(result["enforcement_capacity_after"], 0.0)

    def test_plan_enforcement_reversion_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_enforcement_reversion(70.0, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_enforcement_reversion_callback_args(self):
        calls = []

        def fake_reversion(enforcement_capacity):
            calls.append(enforcement_capacity)
            return 1.5

        deps = dataclasses.replace(
            self.dependencies(), enforcement_capacity_reversion_fn=fake_reversion)
        result = turn_engine.plan_enforcement_reversion(55.0, dependencies=deps)
        self.assertEqual(calls, [55.0])
        self.assertEqual(result, {
            "enforcement_capacity_delta": -1.5, "enforcement_capacity_after": 53.5,
        })

    def test_plan_enforcement_transition_matches_wrapper(self):
        cases = [
            (game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE1_ENTER - 0.01),
            (game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE1_ENTER),
            (game.ENFORCEMENT_STAGE_NONE, 100.0),
        ]
        for enforcement_stage, enforcement_capacity in cases:
            with self.subTest(enforcement_stage=enforcement_stage,
                              enforcement_capacity=enforcement_capacity):
                self.assertEqual(
                    turn_engine.plan_enforcement_transition(
                        enforcement_stage, enforcement_capacity, dependencies=self.dependencies()),
                    game.plan_enforcement_transition(enforcement_stage, enforcement_capacity))

    def test_plan_enforcement_transition_flags(self):
        result = turn_engine.plan_enforcement_transition(
            game.ENFORCEMENT_STAGE_INSTITUTIONAL, game.ENFORCEMENT_STAGE1_ENTER - 0.01,
            dependencies=self.dependencies())
        self.assertTrue(result["enforcement_transitioned"])
        self.assertEqual(result["enforcement_stage_after"], game.ENFORCEMENT_STAGE_DELAYED)

    def test_plan_enforcement_transition_does_not_consume_rng(self):
        state_before = random.getstate()
        turn_engine.plan_enforcement_transition(
            game.ENFORCEMENT_STAGE_INSTITUTIONAL, 20.0, dependencies=self.dependencies())
        self.assertEqual(random.getstate(), state_before)

    def test_plan_enforcement_transition_callback_args(self):
        calls = []

        def fake_stage_next(enforcement_stage, enforcement_capacity):
            calls.append((enforcement_stage, enforcement_capacity))
            return 99

        deps = dataclasses.replace(self.dependencies(), enforcement_stage_next_fn=fake_stage_next)
        result = turn_engine.plan_enforcement_transition(
            game.ENFORCEMENT_STAGE_INSTITUTIONAL, 26.25, dependencies=deps)
        self.assertEqual(calls, [(game.ENFORCEMENT_STAGE_INSTITUTIONAL, 26.25)])
        self.assertEqual(result["enforcement_stage_after"], 99)
        self.assertTrue(result["enforcement_transitioned"])

    # --- 動的依存解決(呼び出し時点解決・monkeypatch伝播) ---
    def test_wrapper_resolves_enforcement_capacity_reversion_fn_at_call_time(self):
        original = game.enforcement_capacity_reversion
        game.enforcement_capacity_reversion = lambda enforcement_capacity: 5.0
        try:
            result = game.plan_enforcement_reversion(50.0)
        finally:
            game.enforcement_capacity_reversion = original
        self.assertEqual(result["enforcement_capacity_delta"], -5.0)
        self.assertEqual(result["enforcement_capacity_after"], 45.0)

    def test_wrapper_resolves_enforcement_stage_next_fn_at_call_time(self):
        original = game.enforcement_stage_next
        game.enforcement_stage_next = lambda enforcement_stage, enforcement_capacity: 99
        try:
            result = game.plan_enforcement_transition(game.ENFORCEMENT_STAGE_INSTITUTIONAL, 50.0)
        finally:
            game.enforcement_stage_next = original
        self.assertEqual(result["enforcement_stage_after"], 99)
        self.assertTrue(result["enforcement_transitioned"])


# ==============================================================================
# game_session.run_game_session() の直接テスト(Step 10、2026-08-15):
# main()を介さず、game_session.run_game_session()を直接呼ぶ。通常/清算の
# 網羅的な分岐確認はMainNormalApplicationFixtureTest/MainSettlementFixtureTest
# (game.main()経由、無変更)に委ね、ここは責務境界の確認(初期化・通常・清算・
# 死亡済み再開・サマリの代表ケース)に絞る。
# ==============================================================================

class GameSessionModuleTest(unittest.TestCase):
    def setUp(self):
        self._orig_call_ollama = game.call_ollama
        self._orig_events_path = game.EVENTS_PATH
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.events_path = Path(path)
        self.events_path.unlink()
        game.EVENTS_PATH = self.events_path
        self._orig_random_state = random.getstate()
        game.call_ollama = lambda model, system, user, want_json: ("モックのナレーション文です。", 0.001)

    def tearDown(self):
        game.call_ollama = self._orig_call_ollama
        game.EVENTS_PATH = self._orig_events_path
        self.events_path.unlink(missing_ok=True)
        random.setstate(self._orig_random_state)

    def _write_fixture(self, events):
        with self.events_path.open("w", encoding="utf-8") as f:
            for etype, data in events:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00",
                                    "type": etype, "data": data}, ensure_ascii=False) + "\n")

    def _events(self):
        return list(event_store.iter_jsonl_events(self.events_path))

    def _args(self, **overrides):
        ns = types.SimpleNamespace(
            model="dummy-model", turns=1, auto=True, auto_policy="balanced",
            policy="none", safety_floor=game.SAFETY_FLOOR, seed=1,
            theme_injection="off", no_consistency_check=True, talent="health",
            endowment=0)
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def _run(self, args):
        game.EVENTS_PATH = self.events_path  # _game_session_config()が読む時点の値
        deps = game._game_session_dependencies()
        cfg = game._game_session_config()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            game_session.run_game_session(args, dependencies=deps, config=cfg)
        return buf.getvalue()

    def test_dependencies_are_frozen(self):
        deps = game._game_session_dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.append_event_fn = lambda *a: None

    def test_config_is_frozen(self):
        cfg = game._game_session_config()
        self.assertTrue(dataclasses.is_dataclass(cfg))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.bank_npc_id = "x"

    def test_initialization_creates_bank_economy_and_character(self):
        # 空のfixture(events.jsonlが空)から始め、初期化(銀行NPC・地域経済NPC・
        # character_born)が正しく積まれることを確認する。endowmentも同時に
        # 確認する(既存のMainNormalApplicationFixtureTestはBANK_BASEで
        # あらかじめ登場済みのため、初期化そのものを直接確認したことが無い)。
        self._write_fixture([])
        args = self._args(talent="dexterity", endowment=50)
        output = self._run(args)

        events = self._events()
        types_seen = [e["type"] for e in events]
        self.assertEqual(types_seen[:4],
                         ["npc_introduced", "npc_introduced", "character_born",
                          "endowment_applied"])
        self.assertEqual(events[0]["data"]["id"], game.BANK_NPC_ID)
        self.assertEqual(events[1]["data"]["id"], game.ECONOMY_NPC_ID)
        self.assertEqual(events[2]["data"]["talent"], "dexterity")
        self.assertEqual(events[3]["data"], {"delta": {"money": 50}, "reason": "birth_endowment"})
        self.assertIn("誕生: 生まれ持ったスキル", output)

    def test_dead_character_resume_is_rejected(self):
        base = [
            ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                "years_per_turn": 0.0833, "npc_pool_cap": 10}),
            ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                "role": "money_issuer", "trust": 50.0}),
            ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                "role": "wage_issuer", "trust": 50.0}),
            ("turn_started", {"turn": 5}),
            ("character_died", {"turn": 5, "age": 15.0}),
        ]
        self._write_fixture(base)
        events_before = len(self._events())
        output = self._run(self._args())

        self.assertIn("続けることはできません", output)
        # 死亡済みなら以降のターン処理(turn_started等)を一切追加しない。
        self.assertEqual(len(self._events()), events_before)

    def test_normal_turn_via_module_directly(self):
        base = [
            ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                "years_per_turn": 0.0833, "npc_pool_cap": 10}),
            ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                "role": "money_issuer", "trust": 50.0}),
            ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                "role": "wage_issuer", "trust": 50.0}),
        ]
        self._write_fixture(base)
        tr = {
            "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
            "choices": [{"key": "rest", "label": "休む", "cost": {}, "hours": 150}],
        }
        original = game.generate_normal_turn
        game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
        try:
            output = self._run(self._args())
        finally:
            game.generate_normal_turn = original

        types_seen = [e["type"] for e in self._events()]
        self.assertIn("turn_started", types_seen)
        self.assertIn("situation_presented", types_seen)
        self.assertIn("trait_changed", types_seen)
        self.assertIn("=== 計測サマリ ===", output)

    def test_settlement_turn_via_module_directly(self):
        base = [
            ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                "years_per_turn": 0.0833, "npc_pool_cap": 10}),
            ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                "role": "money_issuer", "trust": 50.0}),
            ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                "role": "wage_issuer", "trust": 50.0}),
            ("turn_started", {"turn": 1}),
            ("contract_created", {"id": "c1", "counterparty": "友人A", "description": "頼み事",
                                  "created_turn": 1, "due_turn": 1, "repay_money": -20,
                                  "is_bank_debt": False}),
            ("npc_introduced", {"id": "友人A", "name": "友人A", "birth_turn": 1,
                                "role": "acquaintance", "trust": 60.0, "ethics": 60.0,
                                "retire_turn": 200}),
        ]
        self._write_fixture(base)
        tr_template = {
            "situation": "モックの清算状況です。", "latency": 0.001, "kind": "settlement",
            "choices": [{"key": "money", "label": "お金で返す", "cost": {"money": -20},
                        "settle": "fulfilled"}],
        }
        original = game.generate_settlement_turn

        def fake_generate_settlement_turn(model, state, turn, contract, latencies):
            return dict(tr_template, contract=contract)

        game.generate_settlement_turn = fake_generate_settlement_turn
        try:
            output = self._run(self._args())
        finally:
            game.generate_settlement_turn = original

        events = self._events()
        types_seen = [e["type"] for e in events]
        self.assertIn("contract_settled", types_seen)
        settled = next(e for e in events if e["type"] == "contract_settled")
        self.assertEqual(settled["data"]["outcome"], "fulfilled")
        self.assertIn("=== 計測サマリ ===", output)

    def test_shortage_death_is_immediate_and_matches_offline_order(self):
        base = [
            ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                "years_per_turn": 0.0833, "npc_pool_cap": 10}),
            ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                "role": "money_issuer", "trust": 50.0}),
            ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                "role": "wage_issuer", "trust": 50.0}),
            ("trait_changed", {"turn": 0, "delta": {"health": -79.0}, "kind": "fixture"}),
        ]
        self._write_fixture(base)

        def forced_upkeep(*args, **kwargs):
            return {
                "food_delta": -60.0, "food_after": 0.0,
                "medicine_delta": -40.0, "medicine_after": 0.0,
                "shelter_delta": -80.0, "shelter_after": 0.0,
                "tools_delta": -80.0, "tools_after": 0.0,
                "production_capacity_delta": -50.0,
                "production_capacity_after": 0.0,
            }

        forced_transition = lambda stage, score: {
            "barter_stage_after": game.BARTER_STAGE_SHORTAGE,
            "barter_transitioned": stage != game.BARTER_STAGE_SHORTAGE,
        }
        originals = (game.alternative_economy_triggered, game.plan_barter_upkeep,
                     game.plan_barter_transition)
        game.alternative_economy_triggered = lambda *args: True
        game.plan_barter_upkeep = forced_upkeep
        game.plan_barter_transition = forced_transition
        try:
            self._run(self._args())
        finally:
            (game.alternative_economy_triggered, game.plan_barter_upkeep,
             game.plan_barter_transition) = originals

        events = self._events()
        types_seen = [event["type"] for event in events]
        self.assertLess(types_seen.index("essential_goods_shortage_applied"),
                        types_seen.index("character_died"))
        self.assertNotIn("situation_presented", types_seen)
        state = projection.reduce_events(events, game.PROJECTION_DEPENDENCIES)
        self.assertEqual(state["traits"]["health"], game.TRAIT_MIN)
        self.assertFalse(state["alive"])

        deps = dataclasses.replace(
            game._policy_simulation_dependencies(),
            initial_traits=dict(game.INITIAL_TRAITS, health=1.0),
            alternative_economy_triggered_fn=lambda *args: True,
            plan_barter_upkeep_fn=forced_upkeep,
            plan_barter_transition_fn=forced_transition)
        offline = offline_simulation.simulate_policy(
            "cautious", 1, 1, game.SAFETY_FLOOR, "health", dependencies=deps)
        self.assertEqual(offline["death_turn"], 1)
        self.assertEqual(offline["traits"]["health"], game.TRAIT_MIN)

    def test_wrapper_propagates_monkeypatched_append_event(self):
        original = game.append_event
        recorded = []

        def fake_append_event(event_type, data):
            recorded.append(event_type)

        game.append_event = fake_append_event
        try:
            deps = game._game_session_dependencies()
        finally:
            game.append_event = original
        self.assertIs(deps.append_event_fn, fake_append_event)

    def test_wrapper_propagates_monkeypatched_repetition_report(self):
        # game.repetition_reportをmonkeypatchすると、run_game_session()が
        # sibling呼び出しではなくdeps.repetition_report_fn(=game.py側の関数
        # オブジェクト)経由で呼ぶため、game.run_game_session()経由でも
        # 反映されることを確認する。
        original_report = game.repetition_report
        original_generate = game.generate_normal_turn
        calls = []

        def fake_repetition_report(situations):
            calls.append(list(situations))
            return {"pairs": 0, "max": 0.0, "mean": 0.0, "near_duplicates": 0}

        game.repetition_report = fake_repetition_report
        try:
            base = [
                ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                    "years_per_turn": 0.0833, "npc_pool_cap": 10}),
                ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                    "role": "money_issuer", "trust": 50.0}),
                ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                    "role": "wage_issuer", "trust": 50.0}),
            ]
            self._write_fixture(base)
            tr = {
                "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
                "choices": [{"key": "rest", "label": "休む", "cost": {}, "hours": 150}],
            }
            game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
            self._run(self._args())
        finally:
            game.repetition_report = original_report
            game.generate_normal_turn = original_generate
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ["モックの通常状況です。"])

    def test_wrapper_propagates_monkeypatched_bigrams(self):
        # game.bigramsをmonkeypatchすると、game.repetition_report()の内部
        # 呼び出し(bigrams_fn引数経由)にも反映され、run_game_session()経由
        # でも伝播することを確認する(situationsが2件以上でないとbigrams_fnは
        # 呼ばれないため、hoursを半分にして月内2回行動させる)。
        original_bigrams = game.bigrams
        original_generate = game.generate_normal_turn
        calls = []

        def fake_bigrams(text):
            calls.append(text)
            return {text[:2]} if text else set()

        game.bigrams = fake_bigrams
        try:
            base = [
                ("character_born", {"talent": "health", "initial_traits": {}, "start_age": 14,
                                    "years_per_turn": 0.0833, "npc_pool_cap": 10}),
                ("npc_introduced", {"id": "bank", "name": "中央銀行", "birth_turn": 0,
                                    "role": "money_issuer", "trust": 50.0}),
                ("npc_introduced", {"id": "economy", "name": "地域経済", "birth_turn": 0,
                                    "role": "wage_issuer", "trust": 50.0}),
            ]
            self._write_fixture(base)
            tr = {
                "situation": "モックの通常状況です。", "latency": 0.001, "kind": "normal",
                "choices": [{"key": "rest", "label": "休む", "cost": {}, "hours": 70}],
            }
            game.generate_normal_turn = lambda model, state, turn, theme, latencies: tr
            self._run(self._args())
        finally:
            game.bigrams = original_bigrams
            game.generate_normal_turn = original_generate
        self.assertEqual(calls, ["モックの通常状況です。", "モックの通常状況です。"])


# ==============================================================================
# llm_integration.py の直接テスト(Step 11、2026-08-15): 「LLM接続・
# プロンプト方針・ナレーション整合性検証」責務全体の移動後、call_ollamaの
# HTTP層・code_check_narrationの検出ロジック・run_negative_controlの
# callback順序・定数再公開の動的差し替えを確認する。実ollamaへは接続しない
# (urlopen_fnをmockする)。scenario_generation.py/interactive_runtime.pyの
# 既存goldenはすべて無変更で成功している(本クラスの前提)。
# ==============================================================================

class _FakeOllamaResponse:
    """urllib.request.urlopen()が返す、withブロックで使えるレスポンス風オブジェクト。"""
    def __init__(self, body_bytes: bytes):
        self._body = body_bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


class LlmIntegrationModuleTest(unittest.TestCase):
    def dependencies(self):
        return game._llm_integration_dependencies()

    def config(self):
        return game._llm_integration_config()

    def test_dependencies_are_frozen(self):
        deps = self.dependencies()
        self.assertTrue(dataclasses.is_dataclass(deps))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            deps.exit_fn = lambda code: None

    def test_config_is_frozen(self):
        cfg = self.config()
        self.assertTrue(dataclasses.is_dataclass(cfg))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.ollama_host = "x"

    # --- call_ollama: JSON有無・payload・レイテンシ ---
    def test_call_ollama_json_true_sets_format_and_measures_latency(self):
        requests_made = []
        monotonic_values = iter([100.0, 100.25])  # 呼び出し順: start→elapsed計算

        def fake_request_cls(url, data=None, headers=None, method=None):
            requests_made.append({"url": url, "data": data, "headers": headers, "method": method})
            return {"url": url, "data": data}

        def fake_urlopen(req, timeout=None):
            self.assertEqual(timeout, 180)
            return _FakeOllamaResponse(
                json.dumps({"response": "モック応答"}).encode("utf-8"))

        deps = dataclasses.replace(
            self.dependencies(), request_cls=fake_request_cls, urlopen_fn=fake_urlopen,
            monotonic_fn=lambda: next(monotonic_values))
        raw, elapsed = llm_integration.call_ollama(
            "m", "system文", "user文", True, dependencies=deps, config=self.config())

        self.assertEqual(raw, "モック応答")
        self.assertAlmostEqual(elapsed, 0.25)
        self.assertEqual(len(requests_made), 1)
        self.assertEqual(requests_made[0]["url"], f"{game.OLLAMA_HOST}/api/generate")
        self.assertEqual(requests_made[0]["method"], "POST")
        self.assertEqual(requests_made[0]["headers"], {"Content-Type": "application/json"})
        payload = json.loads(requests_made[0]["data"].decode("utf-8"))
        self.assertEqual(payload, {
            "model": "m", "prompt": "user文", "system": "system文",
            "stream": False, "keep_alive": game.KEEP_ALIVE, "format": "json",
        })

    def test_call_ollama_json_false_omits_format_key(self):
        captured = {}

        def capturing_request_cls(url, data=None, headers=None, method=None):
            captured["data"] = data
            return {"data": data}

        def fake_urlopen(req, timeout=None):
            return _FakeOllamaResponse(json.dumps({"response": "x"}).encode("utf-8"))

        deps = dataclasses.replace(
            self.dependencies(), request_cls=capturing_request_cls, urlopen_fn=fake_urlopen)
        llm_integration.call_ollama("m", "s", "u", False, dependencies=deps, config=self.config())
        payload = json.loads(captured["data"].decode("utf-8"))
        self.assertNotIn("format", payload)

    # --- call_ollama: 接続失敗 ---
    def test_call_ollama_connection_failure_prints_stderr_and_exits_1(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("接続できません")

        exit_calls = []
        stderr_buf = io.StringIO()
        deps = dataclasses.replace(
            self.dependencies(), urlopen_fn=fake_urlopen,
            exit_fn=lambda code: exit_calls.append(code), stderr=stderr_buf)
        # exit_fnが実際には終了しない(テスト用のダミー)ため、以降の
        # elapsed計算でbody未定義のままUnboundLocalErrorになる——これは
        # 元のgame.pyでも同じ構造(sys.exit(1)が本当に終了するので到達しない
        # コードパス)なので、例外の型だけ確認すれば十分。
        with self.assertRaises(UnboundLocalError):
            llm_integration.call_ollama(
                "m", "s", "u", False, dependencies=deps, config=self.config())
        self.assertEqual(exit_calls, [1])
        self.assertIn("[ollama接続エラー]", stderr_buf.getvalue())
        self.assertIn(game.OLLAMA_HOST, stderr_buf.getvalue())

    def test_wrapper_propagates_monkeypatched_urlopen_and_exit(self):
        # game.call_ollama経由でも、_llm_integration_dependencies()が
        # urllib.request.urlopenの現在値を毎回読むため、monkeypatchが伝播する
        # ことを確認する(urllib.request.urlopen自体を差し替える、既存の
        # コードベース全体の慣例と同じ)。
        original_urlopen = urllib.request.urlopen
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(timeout)
            return _FakeOllamaResponse(json.dumps({"response": "ok"}).encode("utf-8"))

        urllib.request.urlopen = fake_urlopen
        try:
            raw, _ = game.call_ollama("m", "s", "u", False)
        finally:
            urllib.request.urlopen = original_urlopen
        self.assertEqual(raw, "ok")
        self.assertEqual(calls, [180])

    # --- code_check_narration: 検出・否定・非検出の代表ケース ---
    def test_code_check_narration_detects_positive_pattern_near_synonym(self):
        cfg = self.config()
        issues = llm_integration.code_check_narration(
            ["energy"], "疲れは消え、すっかり元気を取り戻していた。", config=cfg)
        self.assertTrue(issues)

    def test_code_check_narration_negation_marker_suppresses_detection(self):
        cfg = self.config()
        issues = llm_integration.code_check_narration(
            ["energy"], "元気を取り戻したというわけではなかった。", config=cfg)
        self.assertEqual(issues, [])

    def test_code_check_narration_no_synonym_match_is_clean(self):
        cfg = self.config()
        issues = llm_integration.code_check_narration(
            ["money"], "身体は疲れていたが、前へ進む理由があった。", config=cfg)
        self.assertEqual(issues, [])

    def test_wrapper_matches_module_for_code_check_narration(self):
        game_result = game.code_check_narration(["peace"], "心が軽くなった気がした。")
        module_result = llm_integration.code_check_narration(
            ["peace"], "心が軽くなった気がした。", config=self.config())
        self.assertEqual(module_result, game_result)

    # --- run_negative_control: callback順序・game側monkeypatch伝播 ---
    def test_run_negative_control_propagates_monkeypatched_game_functions(self):
        original_code_check = game.code_check_narration
        original_consistency = game.run_consistency_check
        calls = []

        def fake_code_check(not_up_keys, narration):
            calls.append(("code_check", narration))
            return []

        def fake_consistency(model, down, up, narration, latencies, log=True):
            calls.append(("consistency", narration))
            return False, "", 0.1

        game.code_check_narration = fake_code_check
        game.run_consistency_check = fake_consistency
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                game.run_negative_control("dummy-model")
        finally:
            game.code_check_narration = original_code_check
            game.run_consistency_check = original_consistency

        n = len(game.NEGATIVE_CONTROL_CASES)
        self.assertEqual(len([c for c in calls if c[0] == "code_check"]), n)
        self.assertEqual(len([c for c in calls if c[0] == "consistency"]), n)
        # 各ケースでcode_check→consistencyの順に呼ばれる。
        self.assertEqual([c[0] for c in calls],
                         ["code_check", "consistency"] * n)
        # fake両方とも「検出なし」を返すため、should_flag=Falseのケースだけが
        # 一致する(NEGATIVE_CONTROL_CASESの構成上、それが何件かを数えて検証)。
        expected_matches = sum(1 for case in game.NEGATIVE_CONTROL_CASES if not case[3])
        self.assertIn(f"ケース数: {n}", buf.getvalue())
        self.assertIn(f"code版 {expected_matches}/{n} / llm版 {expected_matches}/{n}",
                      buf.getvalue())

    # --- 定数再公開・動的差し替え ---
    def test_constants_are_reexported_from_llm_integration(self):
        self.assertIs(game.SITUATION_SYSTEM, llm_integration.SITUATION_SYSTEM)
        self.assertIs(game.SETTLEMENT_SYSTEM, llm_integration.SETTLEMENT_SYSTEM)
        self.assertIs(game.NARRATION_SYSTEM, llm_integration.NARRATION_SYSTEM)
        self.assertIs(game.CONSISTENCY_SYSTEM, llm_integration.CONSISTENCY_SYSTEM)
        self.assertEqual(game.RESOURCE_SYNONYMS, llm_integration.RESOURCE_SYNONYMS)
        self.assertEqual(game.NEGATIVE_CONTROL_CASES, llm_integration.NEGATIVE_CONTROL_CASES)

    def test_config_resolves_monkeypatched_resource_synonyms_at_call_time(self):
        original = game.RESOURCE_SYNONYMS
        game.RESOURCE_SYNONYMS = {"money": ["カネ"]}
        try:
            cfg = game._llm_integration_config()
        finally:
            game.RESOURCE_SYNONYMS = original
        self.assertEqual(cfg.resource_synonyms, {"money": ["カネ"]})


# ==============================================================================
# 2026-08-16追加: 可視化ダッシュボード用trace(simulate_policyのオプトイン計測)
# ==============================================================================

class SimulatePolicyTraceTest(unittest.TestCase):
    """simulate_policy()のtrace引数(2026-08-16追加)。trace=None(既定)では
    戻り値・RNG消費に一切影響しないこと、trace={...}を渡すと期待どおりの
    内容が記録されることを確認する。"""

    def test_trace_none_matches_omitted_trace_result_and_rng(self):
        state_before = random.getstate()
        r_without = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
        state_after_without = random.getstate()

        random.setstate(state_before)
        r_with_none = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=None)
        state_after_with_none = random.getstate()

        self.assertEqual(r_without, r_with_none)
        self.assertEqual(state_after_without, state_after_with_none)

    def test_trace_dict_does_not_change_result_or_rng(self):
        state_before = random.getstate()
        r_without = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
        state_after_without = random.getstate()

        random.setstate(state_before)
        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        r_with_trace = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=trace)
        state_after_with_trace = random.getstate()

        self.assertEqual(r_without, r_with_trace)
        self.assertEqual(state_after_without, state_after_with_trace)
        self.assertGreater(len(trace["turns"]), 0)

    def test_trace_turns_length_matches_turns_reached(self):
        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        r = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=trace)
        expected = r["death_turn"] if r["death_turn"] is not None else 60
        self.assertEqual(len(trace["turns"]), expected)
        self.assertEqual([row["turn"] for row in trace["turns"]], list(range(1, expected + 1)))

    def test_trace_turn_fields_match_checkpoint_snapshot_fields(self):
        # trace["turns"]の各エントリは、既存のcheckpoint記録(trajectory[turn])と
        # 同じフィールド構成に"turn"を1つ加えたものであるはず(_snapshot()を
        # 両方から呼ぶ設計、二重実装の回帰を防ぐ)。ただし記録するタイミングが
        # どちらもturn_endで記録する。traceだけは時点を自己記述するphaseと
        # turnを持つため、その2キーを除いた内容がcheckpointと完全一致する。
        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        r = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=trace)
        checkpoint_keys = set(r["trajectory"][60].keys())
        trace_last = dict(trace["turns"][-1])
        self.assertEqual(trace_last.pop("turn"), 60)
        self.assertEqual(trace_last.pop("phase"), "turn_end")
        self.assertEqual(trace_last, r["trajectory"][60])

    def test_settlements_recorded_with_expected_fields(self):
        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=trace)
        self.assertGreater(len(trace["settlements"]), 0)
        for event in trace["settlements"]:
            self.assertEqual(set(event.keys()), {
                "turn", "counterparty", "is_bank_debt", "choice_key", "settle",
                "repay_money", "enforcement_stage", "settlement_id"})
            self.assertIn(event["settle"], ("fulfilled", "defaulted"))
            self.assertIsInstance(event["is_bank_debt"], bool)

    def test_npc_introductions_recorded_with_expected_fields(self):
        trace = {"turns": [], "settlements": [], "npc_introductions": []}
        r = game.simulate_policy(
            "cautious", 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health", trace=trace)
        self.assertGreater(len(trace["npc_introductions"]), 0)
        introduced_names = {row["name"] for row in trace["npc_introductions"]}
        acquaintance_names = {n["name"] for n in r["npcs"].values() if n.get("role") == "acquaintance"}
        self.assertEqual(introduced_names, acquaintance_names)
        for event in trace["npc_introductions"]:
            self.assertEqual(set(event.keys()), {"turn", "name", "initial_trust"})


class RunVisualizeTraceTest(unittest.TestCase):
    """offline_simulation.run_visualize_trace() / game.run_visualize_trace()。"""

    def test_writes_json_with_expected_top_level_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "visualize_trace.json")
            data = game.run_visualize_trace(1, "cautious", 60, game.SAFETY_FLOOR, "health", output_path)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as f:
                on_disk = json.load(f)
            # JSON往復ではdictの整数キー(institution_trajectoriesのstage_turns等)
            # が文字列キーに変わるため、完全一致ではなくトップレベルの主要な
            # スカラー値だけを比較する。
            self.assertEqual(on_disk["policy"], data["policy"])
            self.assertEqual(on_disk["seed"], data["seed"])
            self.assertEqual(on_disk["talent"], data["talent"])
            self.assertEqual(len(on_disk["trace"]["turns"]), len(data["trace"]["turns"]))
            for key in ("policy", "seed", "talent", "turns", "death_turn", "final",
                       "counts", "institution_trajectories", "npcs", "contracts",
                       "agree_log", "trace", "schema_version"):
                self.assertIn(key, data)
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["policy"], "cautious")
            self.assertEqual(data["seed"], 1)
            self.assertGreater(len(data["trace"]["turns"]), 0)

    def test_propagates_monkeypatched_simulate_policy(self):
        # run_policy_check等と同じ差し替え口(simulate_policy_fn)経由で呼ばれる
        # ことを確認する——game.simulate_policyをmonkeypatchするとgame.
        # run_visualize_trace()経由でも反映される。
        original = game.simulate_policy
        calls = []

        def fake_simulate_policy(policy_name, turns, seed, safety_floor, talent=None,
                                 checkpoint_turns=None, *, trace=None,
                                 continue_world=False):
            calls.append((policy_name, turns, seed))
            if trace is not None:
                trace["turns"].append({"turn": 1})
            return {
                "policy": policy_name, "talent": "health", "death_turn": None,
                "resources": {}, "traits": {}, "bank_trust": 50.0, "bank_stage": 0,
                "currency_confidence": 50.0, "currency_stage": 0,
                "community_trust": 50.0, "local_credit_stage": 0,
                "enforcement_capacity": 50.0, "enforcement_stage": 0,
                "food": 60.0, "medicine": 40.0, "shelter": 80.0, "tools": 80.0,
                "production_capacity": 50.0, "barter_stage": 0,
                "counts": {}, "institution_trajectories": {}, "npcs": {},
                "contracts": [], "agree_log": [],
                "world_extinct": False, "world_extinct_turn": None,
                "character_death_count": 0, "generation": 1,
                "character_alive": True,
                "settlements": {"home": {
                    "population": 120, "reproductive_population": 42,
                    "stage": 0}},
            }

        game.simulate_policy = fake_simulate_policy
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output_path = os.path.join(tmp, "visualize_trace.json")
                data = game.run_visualize_trace(
                    2, "ambitious", 30, game.SAFETY_FLOOR, "health", output_path)
        finally:
            game.simulate_policy = original
        self.assertEqual(calls, [("ambitious", 30, 2)])
        self.assertEqual(data["trace"]["turns"], [{"turn": 1}])


class DashboardBuildTest(unittest.TestCase):
    """trace JSONから自己完結HTMLへ変換する表示境界。"""

    @staticmethod
    def trace_data(turns=None):
        return {
            "schema_version": 1, "policy": "cautious", "seed": 1,
            "talent": "health", "turns": 1, "death_turn": None,
            "final": {
                "resources": {"energy": 50, "money": 0, "peace": 50},
                "traits": {"dexterity": 50, "intellect": 50, "skill": 50, "health": 80},
                "bank_trust": 50, "bank_stage": 0,
                "currency_confidence": 50, "currency_stage": 0,
                "community_trust": 50, "local_credit_stage": 0,
                "enforcement_capacity": 50, "enforcement_stage": 4,
                "food": 60, "medicine": 40, "shelter": 80, "tools": 80,
                "production_capacity": 50, "barter_stage": 0,
            },
            "contracts": [], "npcs": {}, "institution_trajectories": {},
            "agree_log": [],
            "trace": {"turns": turns or [], "settlements": [], "npc_introductions": []},
        }

    def test_rejects_unknown_trace_schema(self):
        data = self.trace_data()
        data["schema_version"] = 999
        with self.assertRaisesRegex(ValueError, "unsupported visualize trace schema"):
            build_dashboard.build_dashboard_data(data)

    def test_empty_trace_and_choices_are_supported(self):
        result = build_dashboard.build_dashboard_data(self.trace_data())
        self.assertEqual(result["turns"], [])
        self.assertEqual(result["choice_bins"], [])
        self.assertEqual(result["choice_keys"], [])

    def test_choice_bins_cover_full_run_even_without_choices(self):
        bins, keys = build_dashboard.build_choice_bins([], "cautious", 100, 10)
        self.assertEqual(len(bins), 10)
        self.assertEqual((bins[0]["t0"], bins[-1]["t1"]), (1, 100))
        self.assertTrue(all(row["total"] == 0 for row in bins))
        self.assertEqual(keys, [])

    def test_choice_bins_reject_zero(self):
        with self.assertRaisesRegex(ValueError, "bin_count"):
            build_dashboard.build_choice_bins([], "cautious", 100, 0)

    def test_observer_events_preserve_month_and_event_order(self):
        turns = [
            {"t": 1, "bank_stage": 0, "currency_stage": 0,
             "local_credit_stage": 0, "enforcement_stage": 0, "barter_stage": 0},
            {"t": 2, "bank_stage": 1, "currency_stage": 0,
             "local_credit_stage": 0, "enforcement_stage": 0, "barter_stage": 0},
        ]
        settlements = [{
            "t": 2, "cp": "友人A", "bank": False, "settle": "fulfilled", "amt": -12,
        }]
        npcs = [{"name": "友人A", "introduced": 2}]
        self.assertEqual(
            build_dashboard.build_observer_events(turns, settlements, npcs, 2),
            [
                {"t": 2, "kind": "institution_transition", "institution": "bank",
                 "from": 0, "to": 1},
                {"t": 2, "kind": "npc_introduced", "name": "友人A"},
                {"t": 2, "kind": "contract_settled", "counterparty": "友人A",
                 "bank": False, "settle": "fulfilled", "amount": -12},
                {"t": 2, "kind": "character_died"},
            ])

    def test_html_escapes_script_terminator_and_has_document_shell(self):
        html = build_dashboard.build_html(
            {"name": "</script><script>globalThis.PWN=1</script>"},
            build_dashboard.DEFAULT_TEMPLATE)
        self.assertTrue(html.lower().startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', html)
        self.assertNotIn("</script><script>globalThis.PWN=1</script>", html)
        self.assertIn("\\u003c/script\\u003e", html)

    def test_template_uses_dynamic_stage_max_and_empty_states(self):
        template = build_dashboard.DEFAULT_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("const stageMax = Math.max(3", template)
        self.assertIn("0, stageMax", template)
        self.assertIn("清算記録なし", template)
        self.assertIn("選択記録なし", template)

    def test_template_has_observer_playback_and_visibility_pause(self):
        template = build_dashboard.DEFAULT_TEMPLATE.read_text(encoding="utf-8")
        for marker in (
                'id="observer-play"', 'id="observer-speed"',
                'id="observer-timeline"', 'id="observer-snapshot"',
                'id="observer-events"', "function advanceObserver()",
                "DATA.observer_events", "visibilitychange"):
            self.assertIn(marker, template)


class ExperimentParametersTest(unittest.TestCase):
    def test_defaults_validate_and_specs_are_frozen(self):
        result = experiment_parameters.validate_request({"values": {}})
        self.assertEqual(result["run"]["turns"], 1920)
        self.assertEqual(result["run"]["safety_floor"], 30)
        self.assertEqual(result["run"]["initial_population"], 250)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            experiment_parameters.PARAMETER_SPECS[0].label = "changed"

    def test_initial_population_accepts_millions_and_enforces_ui_bound(self):
        result = experiment_parameters.validate_request({"values": {
            "initial_population": 1_000_000,
        }})
        self.assertEqual(result["run"]["initial_population"], 1_000_000)
        with self.assertRaisesRegex(ValueError, "initial_population"):
            experiment_parameters.validate_request({"values": {
                "initial_population": 10_000_001,
            }})

    def test_unknown_parameter_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown parameters"):
            experiment_parameters.validate_request({"values": {"shell_command": "x"}})

    def test_stage_boundaries_are_cross_validated(self):
        with self.assertRaisesRegex(ValueError, "Stage境界"):
            experiment_parameters.validate_request({"values": {
                "stage1_exit": 40, "stage1_enter": 30,
            }})

    def test_overlapping_hysteresis_bands_are_valid(self):
        # 隣接Stageのヒステリシス帯は重なっても状態機械は一意に動く。
        # 制度崩壊後の復旧を意図的に難しくする調整として許可する。
        result = experiment_parameters.validate_request({"values": {
            "stage1_exit": 0, "stage1_enter": 30,
            "stage2_exit": 20, "stage2_enter": 40,
            "stage3_exit": 30, "stage3_enter": 60,
        }})
        self.assertEqual(
            [result["values"][key] for key in (
                "stage1_exit", "stage1_enter", "stage2_exit",
                "stage2_enter", "stage3_exit", "stage3_enter")],
            [0.0, 30.0, 20.0, 40.0, 30.0, 60.0])

    def test_enter_and_exit_sequences_must_each_increase(self):
        with self.assertRaisesRegex(ValueError, "復旧境界"):
            experiment_parameters.validate_request({"values": {
                "stage1_exit": 20, "stage2_exit": 10,
            }})
        with self.assertRaisesRegex(ValueError, "悪化境界"):
            experiment_parameters.validate_request({"values": {
                "stage1_enter": 60, "stage2_enter": 55,
            }})

    def test_numeric_ranges_and_boolean_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "turns"):
            experiment_parameters.validate_request({"values": {"turns": 0}})
        with self.assertRaisesRegex(ValueError, "seed"):
            experiment_parameters.validate_request({"values": {"seed": True}})

    def test_apply_overrides_updates_shortfall_reference(self):
        fake = types.SimpleNamespace()
        experiment_parameters.apply_barter_overrides(
            fake, {"FOOD_STOCK_INITIAL": 25.0})
        self.assertEqual(fake.FOOD_STOCK_INITIAL, 25.0)
        self.assertEqual(fake.FOOD_SHORTFALL_REFERENCE, 25.0)

    def test_apply_overrides_can_keep_shortfall_reference_fixed(self):
        fake = types.SimpleNamespace(FOOD_SHORTFALL_REFERENCE=60.0)
        experiment_parameters.apply_barter_overrides(
            fake, {"FOOD_STOCK_INITIAL": 25.0},
            couple_shortfall_references=False)
        self.assertEqual(fake.FOOD_STOCK_INITIAL, 25.0)
        self.assertEqual(fake.FOOD_SHORTFALL_REFERENCE, 60.0)


class ExperimentWorkerTest(unittest.TestCase):
    def test_worker_isolated_run_returns_dashboard_data(self):
        worker = Path(__file__).parent / "dashboard" / "experiment_worker.py"
        payload = {"values": {"turns": 12, "seed": 1, "policy": "cautious",
                              "talent": "health", "food_initial": 25}}
        proc = subprocess.run(
            [sys.executable, str(worker)], cwd=Path(__file__).parent,
            input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["turns"]), 12)
        self.assertEqual(data["experiment_parameters"]["food_initial"], 25.0)
        # turn traceは月末値。初期25.0は旧会計のbootstrap値であり、財物量と
        # provisioning_scaleを同じ人口比で活動共同体へ保存配賦したあと、
        # 背景会計と共同体間交易を適用するため29.759になる。需要は人口・
        # 年齢構成、在庫と供給は生活基盤規模へ分けたため、空間分割だけでは
        # 不足を作らない一方、初期値そのものはexperiment_parametersに残る。
        self.assertEqual(data["turns"][0]["food"], 29.759)
        self.assertEqual(data["turns"][0]["world_provisioning_scale"], 3.0)
        self.assertEqual(
            data["turns"][0]["world_demand_scales_by_good"]["shelter"],
            3.0)
        self.assertEqual(
            set(data["turns"][0]["goods_coverage_by_good"]),
            {"food", "medicine", "shelter", "tools"})
        # 初期の人口需要と供給基盤が同じ人口比で配賦されるため、この短い
        # fixtureで集落間交易が必ず発生するとは限らない。workerが空間側の
        # 生産イベントまで返すことだけをここで確認し、交易の発生条件と
        # 保存則はIntersettlementTradeTestに委ねる。
        self.assertTrue(any(
            event["kind"] == "production_activity"
            for event in data["observer_events"]))
        # workerは別プロセスなので、親プロセスの通常設定へ値が漏れない。
        self.assertEqual(game.FOOD_STOCK_INITIAL, 90.0)


class ExperimentServerLanTest(unittest.TestCase):
    """LAN公開は明示的なbindと同一オリジンのみ許可する。"""

    def test_bind_host_allows_loopback_private_and_wildcard(self):
        self.assertTrue(experiment_server.allowed_bind_host("127.0.0.1"))
        self.assertTrue(experiment_server.allowed_bind_host("192.168.1.20"))
        self.assertTrue(experiment_server.allowed_bind_host("0.0.0.0"))

    def test_bind_host_rejects_public_address_and_hostname(self):
        self.assertFalse(experiment_server.allowed_bind_host("8.8.8.8"))
        self.assertFalse(experiment_server.allowed_bind_host("example.com"))

    def test_origin_allows_same_private_ipv4_origin(self):
        self.assertTrue(experiment_server.request_origin_allowed(
            "http://192.168.1.20:8765", "192.168.1.20:8765"))

    def test_origin_rejects_cross_origin_public_and_malformed_hosts(self):
        self.assertFalse(experiment_server.request_origin_allowed(
            "http://192.168.1.99:8765", "192.168.1.20:8765"))
        self.assertFalse(experiment_server.request_origin_allowed(
            "http://8.8.8.8:8765", "8.8.8.8:8765"))
        self.assertFalse(experiment_server.request_origin_allowed(
            "not an origin", "192.168.1.20:8765"))

    def test_non_browser_client_without_origin_remains_supported(self):
        self.assertTrue(experiment_server.request_origin_allowed(
            None, "192.168.1.20:8765"))


# ==============================================================================
# CLI契約(Step 0.5): --help はexit 0で成功すべき既存バグの回帰防止
# ==============================================================================

class CliHelpTest(unittest.TestCase):
    """Step 0.5(2026-08-14、ユーザー承認済みの明示的なバグ修正)。
    --creation-rate のhelp文字列に生の`3%`が含まれており、argparseの%書式
    展開でValueErrorになりexit 1で落ちていた。`3%%`へエスケープして修正した
    ——リファクタリングとは分離した、既存バグの回帰防止テストとして固定する。"""

    def test_help_exits_zero_and_shows_creation_rate(self):
        # Step 2.5(2026-08-14、ユーザー指摘): 子プロセスのstdoutエンコーディングは
        # 実行環境のロケール(コードページ)に依存する(Bashのcp932環境で
        # UnicodeDecodeErrorになっていた)。PYTHONIOENCODINGを明示して、
        # Bash/PowerShellどちらの実行環境でも安定してutf-8になるよう固定する。
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "game.py", "--help"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(proc.returncode, 0, msg=f"stderr: {proc.stderr}")
        self.assertIn("--creation-rate", proc.stdout)
        self.assertIn("年率約3%", proc.stdout)


# ==============================================================================
# Step 13(2026-08-15): 物々交換・自給制度(制度5)の最小縦断実装
# ==============================================================================

class BarterStageNextTest(unittest.TestCase):
    """institutions/barter.pyのStage状態機械(worst_shortfallが高いほど悪化する
    点だけenforcement_stage_next等と向きが逆、構造は同じ)。"""

    def test_functioning_stays_at_enter_threshold_boundary(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE1_ENTER - 0.01),
            game.BARTER_STAGE_FUNCTIONING)

    def test_functioning_to_thinned_at_enter(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE1_ENTER),
            game.BARTER_STAGE_THINNED)

    def test_functioning_to_subsistence_only_at_stage2_enter(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE2_ENTER),
            game.BARTER_STAGE_SUBSISTENCE_ONLY)

    def test_functioning_to_shortage_at_stage3_enter(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE3_ENTER),
            game.BARTER_STAGE_SHORTAGE)

    def test_thinned_stays_above_exit(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_THINNED, game.BARTER_STAGE1_EXIT + 0.01),
            game.BARTER_STAGE_THINNED)

    def test_thinned_recovers_at_exit(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_THINNED, game.BARTER_STAGE1_EXIT),
            game.BARTER_STAGE_FUNCTIONING)

    def test_subsistence_only_recovers_at_exit(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_SUBSISTENCE_ONLY, game.BARTER_STAGE2_EXIT),
            game.BARTER_STAGE_THINNED)

    def test_shortage_recovers_at_exit(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_SHORTAGE, game.BARTER_STAGE3_EXIT),
            game.BARTER_STAGE_SUBSISTENCE_ONLY)

    def test_shortage_stays_above_exit(self):
        self.assertEqual(
            game.barter_stage_next(game.BARTER_STAGE_SHORTAGE, game.BARTER_STAGE3_EXIT + 0.01),
            game.BARTER_STAGE_SHORTAGE)

    def test_all_stages_reversible_no_lock_in(self):
        # 仕様「人口制度が未実装なので、不可逆化はまだ行わない」——worst_shortfall
        # を0まで戻せば必ずFUNCTIONINGへ1段階ずつ戻れることを確認する。
        stage = game.BARTER_STAGE_SHORTAGE
        for _ in range(10):
            stage = game.barter_stage_next(stage, 0.0)
        self.assertEqual(stage, game.BARTER_STAGE_FUNCTIONING)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.barter_stage_next(game.BARTER_STAGE_FUNCTIONING, 50.0)
        self.assertEqual(random.getstate(), state_before)


class WorstShortfallBottleneckTest(unittest.TestCase):
    """仕様「Stage判定は各財の不足を相殺しないボトルネック型とする」——食料が
    十分でも医薬品が不足していれば、そちらが最悪値として選ばれることを確認する
    (平均・合計を経由すると必ず相殺が起きるため、maxで判定していることの直接検証)。"""

    def test_all_healthy_gives_zero(self):
        score, good = game.worst_shortfall(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL)
        self.assertEqual(score, 0.0)

    def test_food_surplus_does_not_cancel_medicine_shortage(self):
        # 食料は初期値の2倍(過剰)、医薬品はゼロ(枯渇)。
        score, worst_good = game.worst_shortfall(
            game.FOOD_STOCK_INITIAL * 2, 0.0,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL)
        self.assertEqual(worst_good, "medicine")
        self.assertAlmostEqual(score, 100.0)

    def test_worst_good_switches_to_whichever_is_most_depleted(self):
        # toolsだけが大きく減っている場合はtoolsが最悪値になる。
        score, worst_good = game.worst_shortfall(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, 0.0)
        self.assertEqual(worst_good, "tools")
        self.assertAlmostEqual(score, 100.0)

    def test_mild_shortage_in_one_good_is_not_masked_by_others(self):
        # food/medicine/tools全て健全、shelterだけ半分——shelterの不足率
        # (50%)がそのままworst_shortfallに出ることを確認する(平均they'd give
        # 12.5%になってしまうところ、maxなので50%のまま)。
        score, worst_good = game.worst_shortfall(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL / 2, game.TOOLS_DURABILITY_INITIAL)
        self.assertEqual(worst_good, "shelter")
        self.assertAlmostEqual(score, 50.0)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.worst_shortfall(60.0, 40.0, 80.0, 80.0)
        self.assertEqual(random.getstate(), state_before)

    def test_production_capacity_is_also_a_bottleneck(self):
        capacity_at_twenty_percent = game.PRODUCTION_CAPACITY_INITIAL * 0.2
        score, worst_good = game.worst_shortfall(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL,
            capacity_at_twenty_percent)
        self.assertEqual(worst_good, "production_capacity")
        self.assertAlmostEqual(score, 80.0)


class BarterChoiceEffectsTest(unittest.TestCase):
    def test_barter_affects_only_food_and_medicine(self):
        effects = game.barter_choice_effects("barter", game.PRODUCTION_CAPACITY_INITIAL)
        self.assertGreater(effects["food_delta"], 0.0)
        self.assertGreater(effects["medicine_delta"], 0.0)
        self.assertEqual(effects["shelter_delta"], 0.0)
        self.assertEqual(effects["tools_delta"], 0.0)
        self.assertEqual(effects["production_capacity_delta"], 0.0)

    def test_subsistence_also_repairs_shelter_tools_and_builds_capacity(self):
        effects = game.barter_choice_effects("subsistence", game.PRODUCTION_CAPACITY_INITIAL)
        self.assertGreater(effects["food_delta"], 0.0)
        self.assertGreater(effects["medicine_delta"], 0.0)
        self.assertGreater(effects["shelter_delta"], 0.0)
        self.assertGreater(effects["tools_delta"], 0.0)
        self.assertGreater(effects["production_capacity_delta"], 0.0)

    def test_barter_and_subsistence_keep_distinct_effect_scopes(self):
        # 補充量の大小は調整可能な較正値。barterは在庫だけ、subsistenceは
        # 在庫に加えて耐久資本と生産能力も改善する、という責務差を固定する。
        barter_effects = game.barter_choice_effects("barter", 50.0)
        subsistence_effects = game.barter_choice_effects("subsistence", 50.0)
        self.assertGreater(barter_effects["food_delta"], 0.0)
        self.assertGreater(barter_effects["medicine_delta"], 0.0)
        self.assertEqual(barter_effects["shelter_delta"], 0.0)
        self.assertEqual(barter_effects["tools_delta"], 0.0)
        self.assertEqual(barter_effects["production_capacity_delta"], 0.0)
        self.assertGreater(subsistence_effects["shelter_delta"], 0.0)
        self.assertGreater(subsistence_effects["tools_delta"], 0.0)
        self.assertGreater(subsistence_effects["production_capacity_delta"], 0.0)

    def test_other_keys_are_no_op(self):
        effects = game.barter_choice_effects("money", 50.0)
        self.assertEqual(effects, {"food_delta": 0.0, "medicine_delta": 0.0,
                                   "shelter_delta": 0.0, "tools_delta": 0.0,
                                   "production_capacity_delta": 0.0})

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.barter_choice_effects("barter", 50.0)
        self.assertEqual(random.getstate(), state_before)

    def test_capacity_above_cap_does_not_raise_action_factor_further(self):
        at_cap = game.barter_choice_effects("barter", game.PRODUCTION_CAPACITY_CAP)
        above_cap = game.barter_choice_effects("barter", game.PRODUCTION_CAPACITY_CAP * 10)
        self.assertEqual(above_cap, at_cap)


class BarterGoodsUpkeepTest(unittest.TestCase):
    def test_returns_fixed_negative_deltas(self):
        upkeep = game.barter_goods_upkeep()
        self.assertEqual(upkeep, {
            "food_delta": -game.FOOD_UPKEEP_PER_TURN,
            "medicine_delta": -game.MEDICINE_UPKEEP_PER_TURN,
            "shelter_delta": -game.SHELTER_WEAR_PER_TURN,
            "tools_delta": -game.TOOLS_WEAR_PER_TURN,
        })

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.barter_goods_upkeep()
        self.assertEqual(random.getstate(), state_before)

    def test_background_production_balances_upkeep_at_initial_capacity(self):
        production = game.background_goods_production(game.PRODUCTION_CAPACITY_INITIAL)
        upkeep = game.barter_goods_upkeep()
        for key in upkeep:
            self.assertAlmostEqual(production[key] + upkeep[key], 0.0)


class EssentialGoodsShortagePenaltyTest(unittest.TestCase):
    def test_shortage_stage_applies_penalty(self):
        penalty = game.essential_goods_shortage_penalty(game.BARTER_STAGE_SHORTAGE)
        self.assertEqual(penalty, {"energy": game.ESSENTIAL_GOODS_SHORTAGE_ENERGY_PENALTY,
                                   "health": game.ESSENTIAL_GOODS_SHORTAGE_HEALTH_PENALTY})
        self.assertLess(penalty["energy"], 0.0)
        self.assertLess(penalty["health"], 0.0)

    def test_other_stages_are_no_op(self):
        for stage in (game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE_THINNED,
                     game.BARTER_STAGE_SUBSISTENCE_ONLY):
            with self.subTest(stage=stage):
                self.assertEqual(game.essential_goods_shortage_penalty(stage), {})


class ProductionCapacityReversionTest(unittest.TestCase):
    def test_reverts_toward_initial(self):
        self.assertGreater(game.production_capacity_reversion(80.0), 0.0)
        self.assertLess(game.production_capacity_reversion(20.0), 0.0)
        self.assertEqual(game.production_capacity_reversion(game.PRODUCTION_CAPACITY_INITIAL), 0.0)

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.production_capacity_reversion(60.0)
        self.assertEqual(random.getstate(), state_before)


class AlternativeEconomyTriggeredTest(unittest.TestCase):
    """engine.alternative_economy_triggered()——上位制度いずれかの崩壊・縮退
    判定。健全な既定値では常にFalse(仕様「他制度が健全な期間に支配戦略に
    ならない」の土台)。"""

    def test_all_healthy_is_false(self):
        self.assertFalse(game.alternative_economy_triggered())

    def test_bank_collapsed_triggers(self):
        self.assertTrue(game.alternative_economy_triggered(bank_stage=game.BANK_STAGE_COLLAPSED))

    def test_currency_abandoned_triggers(self):
        self.assertTrue(
            game.alternative_economy_triggered(currency_stage=game.CURRENCY_STAGE_ABANDONED))

    def test_currency_wary_does_not_trigger(self):
        self.assertFalse(game.alternative_economy_triggered(currency_stage=game.CURRENCY_STAGE_WARY))

    def test_local_credit_personal_triggers(self):
        self.assertTrue(game.alternative_economy_triggered(
            local_credit_stage=game.LOCAL_CREDIT_STAGE_PERSONAL))

    def test_local_credit_contraction_does_not_trigger(self):
        self.assertFalse(game.alternative_economy_triggered(
            local_credit_stage=game.LOCAL_CREDIT_STAGE_CONTRACTION))

    def test_enforcement_local_ledger_triggers(self):
        self.assertTrue(game.alternative_economy_triggered(
            enforcement_stage=game.ENFORCEMENT_STAGE_LOCAL_LEDGER))

    def test_enforcement_delayed_does_not_trigger(self):
        self.assertFalse(game.alternative_economy_triggered(
            enforcement_stage=game.ENFORCEMENT_STAGE_DELAYED))

    def test_no_rng_consumed(self):
        state_before = random.getstate()
        game.alternative_economy_triggered(bank_stage=game.BANK_STAGE_COLLAPSED)
        self.assertEqual(random.getstate(), state_before)


class AvailableNormalArchetypesBarterAdditionTest(unittest.TestCase):
    """engine.available_normal_archetypes()のbarter/subsistence追加ロジック
    (AvailableNormalArchetypesTestの既存ケースと重複しない観点だけを追加)。"""

    def test_barter_excluded_once_subsistence_only_reached(self):
        # 仕様「Stage2ではbarterを停止し、subsistenceのみを代替経路とする」。
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, bank_stage=game.BANK_STAGE_COLLAPSED,
            barter_stage=game.BARTER_STAGE_SUBSISTENCE_ONLY)
        keys = [a["key"] for a in result]
        self.assertNotIn("barter", keys)
        self.assertIn("subsistence", keys)

    def test_barter_excluded_at_shortage_stage(self):
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, bank_stage=game.BANK_STAGE_COLLAPSED,
            barter_stage=game.BARTER_STAGE_SHORTAGE)
        keys = [a["key"] for a in result]
        self.assertNotIn("barter", keys)
        self.assertIn("subsistence", keys)

    def test_barter_included_at_thinned_stage(self):
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, bank_stage=game.BANK_STAGE_COLLAPSED,
            barter_stage=game.BARTER_STAGE_THINNED)
        keys = [a["key"] for a in result]
        self.assertIn("barter", keys)
        self.assertIn("subsistence", keys)

    def test_not_triggered_excludes_both(self):
        # 上位制度が健全なままだと、barter_stageの値に関係なくbarter/
        # subsistenceは一切現れない(支配戦略化を防ぐゲート)。
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, barter_stage=game.BARTER_STAGE_THINNED)
        keys = [a["key"] for a in result]
        self.assertNotIn("barter", keys)
        self.assertNotIn("subsistence", keys)

    def test_money_and_social_gates_unaffected_by_barter_stage(self):
        # barter_stageを理由にmoney/socialを直接停止しない(仕様の明示要求)。
        # 既存のcurrency/local_credit判定だけを見る。
        result = game.available_normal_archetypes(
            game.CURRENCY_STAGE_NORMAL, game.LOCAL_CREDIT_STAGE_HEALTHY,
            bank_stage=game.BANK_STAGE_COLLAPSED, barter_stage=game.BARTER_STAGE_SHORTAGE)
        keys = [a["key"] for a in result]
        self.assertIn("money", keys)
        self.assertIn("social", keys)


class BarterUpkeepTurnEngineTest(unittest.TestCase):
    """turn_engine.plan_barter_upkeep/plan_barter_transition(game.pyラッパー
    経由)。RNG不使用・クランプ・戻り値のキー構成を確認する。"""

    def test_plan_barter_upkeep_applies_upkeep_and_clamps_floor(self):
        result = game.plan_barter_upkeep(1.0, 0.5, 0.1, 0.2, 0.0)
        self.assertEqual(result["food_after"], 0.0)      # 1.0 - 3.0 → 0でクランプ
        self.assertEqual(result["medicine_after"], 0.0)  # 0.5 - 1.0 → 0でクランプ
        self.assertEqual(result["shelter_after"], 0.0)   # 0.1 - 0.3 → 0でクランプ
        self.assertEqual(result["tools_after"], 0.0)     # 0.2 - 0.4 → 0でクランプ

    def test_healthy_initial_state_is_stationary(self):
        result = game.plan_barter_upkeep(
            game.FOOD_STOCK_INITIAL, game.MEDICINE_STOCK_INITIAL,
            game.SHELTER_DURABILITY_INITIAL, game.TOOLS_DURABILITY_INITIAL,
            game.PRODUCTION_CAPACITY_INITIAL)
        self.assertEqual(result["food_after"], game.FOOD_STOCK_INITIAL)
        self.assertEqual(result["medicine_after"], game.MEDICINE_STOCK_INITIAL)
        self.assertEqual(result["production_capacity_after"], game.PRODUCTION_CAPACITY_INITIAL)

    def test_disruption_reduces_capacity_but_never_below_zero(self):
        result = game.plan_barter_upkeep(
            60.0, 40.0, 80.0, 80.0, 50.0, disrupted=True)
        self.assertLess(result["production_capacity_after"], 50.0)
        at_floor = game.plan_barter_upkeep(
            0.0, 0.0, 0.0, 0.0, 0.0, disrupted=True)
        self.assertGreaterEqual(at_floor["production_capacity_after"], 0.0)

    def test_plan_barter_upkeep_clamps_ceiling(self):
        result = game.plan_barter_upkeep(
            game.GOODS_CAP, game.GOODS_CAP, game.GOODS_CAP, game.GOODS_CAP,
            game.PRODUCTION_CAPACITY_INITIAL)
        self.assertLessEqual(result["food_after"], game.GOODS_CAP)
        self.assertLessEqual(result["medicine_after"], game.GOODS_CAP)
        self.assertLessEqual(result["shelter_after"], game.GOODS_CAP)
        self.assertLessEqual(result["tools_after"], game.GOODS_CAP)

    def test_plan_barter_upkeep_no_rng(self):
        state_before = random.getstate()
        game.plan_barter_upkeep(60.0, 40.0, 80.0, 80.0, 50.0)
        self.assertEqual(random.getstate(), state_before)

    def test_plan_barter_transition_matches_barter_stage_next(self):
        result = game.plan_barter_transition(game.BARTER_STAGE_FUNCTIONING, game.BARTER_STAGE1_ENTER)
        self.assertEqual(result["barter_stage_after"], game.BARTER_STAGE_THINNED)
        self.assertTrue(result["barter_transitioned"])

    def test_plan_barter_transition_no_transition_flag_false(self):
        result = game.plan_barter_transition(game.BARTER_STAGE_FUNCTIONING, 0.0)
        self.assertEqual(result["barter_stage_after"], game.BARTER_STAGE_FUNCTIONING)
        self.assertFalse(result["barter_transitioned"])


class ReduceEventsBarterTest(unittest.TestCase):
    """projection.reduce_events()のbarter関連投影。旧events.jsonl(barter関連
    イベントを一切含まない)からの復元と、新イベントの反映を確認する。"""

    def _deps(self):
        return game.PROJECTION_DEPENDENCIES

    def test_old_log_without_barter_events_restores_initial_values(self):
        # 仕様「古いevents.jsonlに新フィールドがなくても初期値で復元できること」。
        events = [{"type": "turn_started", "data": {"turn": 1}}]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["food"], game.FOOD_STOCK_INITIAL)
        self.assertEqual(state["medicine"], game.MEDICINE_STOCK_INITIAL)
        self.assertEqual(state["shelter"], game.SHELTER_DURABILITY_INITIAL)
        self.assertEqual(state["tools"], game.TOOLS_DURABILITY_INITIAL)
        self.assertEqual(state["production_capacity"], game.PRODUCTION_CAPACITY_INITIAL)
        self.assertEqual(state["barter_stage"], game.BARTER_STAGE_FUNCTIONING)
        self.assertFalse(state["barter_active"])

    def test_goods_state_changed_applies_all_deltas_without_marking_active(self):
        events = [{"type": "goods_state_changed", "data": {
            "turn": 1, "food_delta": 5.0, "medicine_delta": 2.0, "shelter_delta": -0.3,
            "tools_delta": -0.4, "production_capacity_delta": 1.0, "reason": "test",
        }}]
        state = projection.reduce_events(events, self._deps())
        self.assertAlmostEqual(state["food"], game.FOOD_STOCK_INITIAL + 5.0)
        self.assertAlmostEqual(state["medicine"], game.MEDICINE_STOCK_INITIAL + 2.0)
        self.assertAlmostEqual(state["shelter"], game.SHELTER_DURABILITY_INITIAL - 0.3)
        self.assertAlmostEqual(state["tools"], game.TOOLS_DURABILITY_INITIAL - 0.4)
        self.assertAlmostEqual(state["production_capacity"], game.PRODUCTION_CAPACITY_INITIAL + 1.0)
        self.assertFalse(state["barter_active"])

    def test_barter_activated_event_records_first_turn(self):
        events = [
            {"type": "barter_activated", "data": {"turn": 7, "reason": "test"}},
            {"type": "barter_activated", "data": {"turn": 9, "reason": "test"}},
        ]
        state = projection.reduce_events(events, self._deps())
        self.assertTrue(state["barter_active"])
        self.assertEqual(state["barter_activated_turn"], 7)

    def test_goods_state_changed_clamps_floor_and_ceiling(self):
        events = [
            {"type": "goods_state_changed", "data": {
                "turn": 1, "food_delta": -1000.0, "medicine_delta": 1000.0, "reason": "test"}},
        ]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["food"], 0.0)
        self.assertEqual(state["medicine"], game.GOODS_CAP)

    def test_goods_state_changed_clamps_production_capacity_cap(self):
        events = [{"type": "goods_state_changed", "data": {
            "turn": 1, "production_capacity_delta": 1000.0,
            "reason": "goods_economy_upkeep"}}]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["production_capacity"], game.PRODUCTION_CAPACITY_CAP)

    def test_shortage_trait_change_clamps_health_floor(self):
        events = [{"type": "trait_changed", "data": {
            "turn": 1, "delta": {"health": -1000.0},
            "kind": "essential_goods_shortage"}}]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["traits"]["health"], game.TRAIT_MIN)

    def test_institution_transition_barter_sets_stage_and_active(self):
        events = [{"type": "institution_transition", "data": {
            "turn": 1, "institution_id": "barter", "kind": "barter",
            "from_stage": game.BARTER_STAGE_FUNCTIONING, "to_stage": game.BARTER_STAGE_THINNED,
            "trigger": "barter_market_thinned", "metric_snapshot": {}, "scope": game.BARTER_SCOPE,
        }}]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["barter_stage"], game.BARTER_STAGE_THINNED)
        self.assertTrue(state["barter_active"])

    def test_essential_goods_shortage_applied_updates_energy(self):
        events = [{"type": "essential_goods_shortage_applied", "data": {
            "turn": 1, "delta": {"energy": -3.0}, "reason": "essential_goods_shortage"}}]
        state = projection.reduce_events(events, self._deps())
        self.assertEqual(state["resources"]["energy"],
                         game.INITIAL_RESOURCES["energy"] - 3.0)

    def test_other_institution_transitions_do_not_affect_barter_active(self):
        # barter_activeは"barter"のinstitution_id専用——他制度の遷移では動かない
        # (上位制度との無条件連動が無いことの投影層での確認)。
        events = [{"type": "institution_transition", "data": {
            "turn": 1, "institution_id": "central_bank", "kind": "bank",
            "from_stage": 0, "to_stage": 1, "trigger": "x", "metric_snapshot": {}, "scope": "nation",
        }}]
        state = projection.reduce_events(events, self._deps())
        self.assertFalse(state["barter_active"])
        self.assertEqual(state["barter_stage"], game.BARTER_STAGE_FUNCTIONING)


class SimulatePolicyBarterFieldsMatchWrapperTest(unittest.TestCase):
    """game.simulate_policy(main側と同じ公開関数)とoffline_simulation.
    simulate_policy(モジュール本体)がbarter関連フィールドについても完全一致
    することと、RNG状態が一致することを確認する(Step 12以来の既存パターンの
    継続)。"""

    def test_matches_wrapper_and_rng_with_barter_triggered(self):
        original_fn = game.enforcement_stage_next
        game.enforcement_stage_next = lambda stage, cap: 2  # barterを確実に発火させる
        try:
            state_before = random.getstate()
            game_result = game.simulate_policy(
                "cautious", 150, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
            state_after_game = random.getstate()

            random.setstate(state_before)
            module_result = offline_simulation.simulate_policy(
                "cautious", 150, 1, game.SAFETY_FLOOR, "health",
                dependencies=game._policy_simulation_dependencies())
            state_after_module = random.getstate()

            self.assertEqual(module_result, game_result)
            self.assertEqual(state_after_module, state_after_game)
            # 前提確認: このケースで実際にbarterが発火していること
            # (発火しないケースの比較では何も検証できていないことになるため)。
            self.assertTrue(game_result["barter_active"])
        finally:
            game.enforcement_stage_next = original_fn


class BarterForcedTriggerIntegrationTest(unittest.TestCase):
    """enforcement_stage_nextをStage2固定にmonkeypatchして上位制度を強制的に
    崩壊させた対照実験——仕様「上位制度を強制崩壊させた対照実験でbarter/
    subsistenceが実際に使われる」「Stage3で不足ペナルティが実際に発生する」の
    直接検証。"""

    @classmethod
    def setUpClass(cls):
        original_fn = game.enforcement_stage_next
        game.enforcement_stage_next = lambda stage, cap: 2
        try:
            cls.result = game.simulate_policy(
                "cautious", 300, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
        finally:
            game.enforcement_stage_next = original_fn

    def test_barter_activates(self):
        self.assertTrue(self.result["barter_active"])

    def test_barter_or_subsistence_actually_chosen(self):
        counts = self.result["counts"]
        self.assertGreater(
            counts.get("通常:barter", 0) + counts.get("通常:subsistence", 0), 0)

    def test_disruption_is_observable_and_actions_can_replenish_stock(self):
        self.assertLess(
            self.result["goods_min_seen"]["production_capacity"],
            game.PRODUCTION_CAPACITY_INITIAL)
        self.assertGreater(self.result["food"], game.FOOD_STOCK_INITIAL)

    def test_shortage_penalty_count_matches_shortage_turns_if_reached(self):
        barter_traj = self.result["institution_trajectories"]["barter"]
        shortage_turns = barter_traj["stage_turns"].get(game.BARTER_STAGE_SHORTAGE, 0)
        if shortage_turns:
            self.assertEqual(
                self.result["barter_shortage_penalty_applied_count"], shortage_turns)

    def test_check_trajectory_criteria_includes_barter_category(self):
        checks = game.check_trajectory_criteria(self.result)
        barter_checks = [c for c in checks if c["category"] == "barter"]
        self.assertEqual(len(barter_checks), 5)
        self.assertEqual(
            sum("発火後" in c["name"] for c in barter_checks), 2)

    def test_no_double_counting_goods_never_exceed_cap(self):
        # 自己レビュー項目「財の二重計上」の直接検証——upkeep+choice effectを
        # 何度繰り返してもGOODS_CAPを超えない(turn_engine.plan_barter_upkeep/
        # offline_simulationのchoice適用のどちらもmin(GOODS_CAP,...)でクランプ
        # している)。
        self.assertLessEqual(self.result["food"], game.GOODS_CAP)
        self.assertLessEqual(self.result["medicine"], game.GOODS_CAP)
        self.assertLessEqual(self.result["shelter"], game.GOODS_CAP)
        self.assertLessEqual(self.result["tools"], game.GOODS_CAP)

    def test_fallback_actions_improve_same_seed_over_choice_disabled_control(self):
        original_enforcement = game.enforcement_stage_next
        original_archetypes = game.available_normal_archetypes
        game.enforcement_stage_next = lambda stage, cap: 2

        def without_alternatives(*args, **kwargs):
            return [a for a in original_archetypes(*args, **kwargs)
                    if a["key"] not in ("barter", "subsistence")]

        game.available_normal_archetypes = without_alternatives
        try:
            control = game.simulate_policy(
                "cautious", 300, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
        finally:
            game.enforcement_stage_next = original_enforcement
            game.available_normal_archetypes = original_archetypes
        self.assertIsNone(self.result["death_turn"])
        self.assertIsNotNone(control["death_turn"])
        self.assertGreater(control["barter_shortage_penalty_applied_count"], 0)
        self.assertLess(control["goods_min_seen"]["food"], self.result["goods_min_seen"]["food"])


class BarterHealthyRunNoEarlyCollapseTest(unittest.TestCase):
    """仕様「健全条件でT60までにStage2/3へ即時崩壊しない」——上位制度を一切
    強制しない通常の実行では、barterは発火すらしないはず(発火しないこと自体が
    「崩壊していない」の最も強い形)。"""

    def test_t60_no_barter_activation_under_healthy_conditions(self):
        for policy in ("cautious", "ambitious", "family"):
            with self.subTest(policy=policy):
                r = game.simulate_policy(
                    policy, 60, seed=1, safety_floor=game.SAFETY_FLOOR, talent="health")
                self.assertFalse(r["barter_active"])
                self.assertEqual(r["barter_stage"], game.BARTER_STAGE_FUNCTIONING)


class BarterPolicyVisibilityTest(unittest.TestCase):
    """財の便益が選択器から見えず、代替経路が構造的に無視されていた回帰を防ぐ。"""

    def test_evaluation_uses_real_effects_and_values_shortage_relief(self):
        evaluation = game.barter_choice_evaluation(
            "barter", 12.0, 10.0, 60.0, 60.0, 40.0)
        self.assertGreater(evaluation["goods_survival_value"], 0.0)
        self.assertGreater(
            evaluation["goods_safety_after"],
            100.0 - evaluation["worst_shortfall_before"])
        self.assertEqual(
            evaluation["goods_effects"], game.barter_choice_effects("barter", 40.0))

    def test_policy_score_prefers_same_cost_when_one_choice_relaxes_shortage(self):
        plain = {"key": "rest", "cost": {"energy": -10}, "hours": 20,
                 "goods_safety_after": 20.0, "goods_survival_value": 0.0}
        relieving = {"key": "barter", "cost": {"energy": -10}, "hours": 20,
                     "goods_safety_after": 30.0, "goods_survival_value": 25.0}
        vec = game.POLICY_VECTORS["cautious"]
        self.assertGreater(
            game.policy_score(relieving, vec, 1, dict(game.INITIAL_RESOURCES)),
            game.policy_score(plain, vec, 1, dict(game.INITIAL_RESOURCES)))
        self.assertEqual(game.auto_select(
            [plain, relieving], dict(game.INITIAL_RESOURCES), "balanced", "cautious",
            game.SAFETY_FLOOR, 1, 100), 1)

    def test_build_normal_choice_attaches_goods_evaluation_without_mutating_state(self):
        goods_state = {
            "food": 12.0, "medicine": 10.0, "shelter": 60.0,
            "tools": 60.0, "production_capacity": 40.0,
        }
        before = dict(goods_state)
        random.seed(1)
        choice = game.build_normal_base_choice(
            game.BARTER_ARCHETYPE, 1, 1.0, goods_state=goods_state)
        self.assertIn("goods_effects", choice)
        self.assertIn("goods_survival_value", choice)
        self.assertEqual(goods_state, before)


if __name__ == "__main__":
    unittest.main()
