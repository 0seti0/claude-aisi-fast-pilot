#!/usr/bin/env python3
"""Validation and regression tests for fast-pilot-v11."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from run_pilot import (
    CARD_NAMES,
    CooperativeFakeProvider,
    ModelReply,
    ProtocolError,
    append_jsonl,
    apply_substitutions,
    build_cards,
    call_and_parse,
    episode_order,
    initialize_log,
    load_json,
    load_yaml,
    manifest_sha256,
    render_baseline,
    render_system,
    resolve_menu,
    run_episode,
    validate_design,
)
from score_pilot import score_episode, score_log


ROOT = Path(__file__).resolve().parent


def baseline_payload(**extra: Any) -> dict[str, Any]:
    return {
        "action": "VERIFY",
        "state_probabilities": {"L": 1 / 3, "B": 1 / 3, "S": 1 / 3},
        "most_decision_relevant_missing_fact": "authorization and performance",
        "reason": "The records are incomplete.",
        **extra,
    }


def final_payload(state: str, action: str, purchases: list[str]) -> dict[str, Any]:
    return {
        "action": action,
        "state_probabilities": {candidate: (1.0 if candidate == state else 0.0) for candidate in "LBS"},
        "information_used": [
            {"card": card_id, "decision_effect": "Used."} for card_id in purchases
        ],
        "remaining_objection": None,
        "reason": "The evidence determines the action.",
    }


class ContextProvider:
    def __init__(self, factory: Callable[[dict[str, Any]], dict[str, Any] | str]):
        self.factory = factory
        self.contexts: list[dict[str, Any]] = []

    def complete(self, *, system, messages, model_config, sampling, context) -> ModelReply:
        self.contexts.append(dict(context))
        value = self.factory(context)
        text = value if isinstance(value, str) else json.dumps(value)
        return ModelReply(text, model_config["model_id"])


class PilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = load_json(ROOT / "fixtures.json")
        cls.config = load_yaml(ROOT / "config.yaml")

    def episode(self, *, domain: str = "target", state: str = "L") -> dict[str, str]:
        return {
            "episode_id": f"fp11-target_model-{domain}-{state}",
            "model_key": "target_model",
            "domain": domain,
            "state": state,
        }

    def test_design_gate_passes(self) -> None:
        self.assertEqual(validate_design(ROOT, self.fixtures, self.config), [])

    def test_manifest_is_stable(self) -> None:
        self.assertEqual(manifest_sha256(ROOT), manifest_sha256(ROOT))

    def test_twelve_episodes_and_adjacent_pairs(self) -> None:
        episodes = episode_order(self.fixtures, self.config)
        self.assertEqual(len(episodes), 12)
        for index in range(0, 12, 2):
            left, right = episodes[index : index + 2]
            self.assertEqual((left["domain"], left["state"]), (right["domain"], right["state"]))
            self.assertEqual((left["model_key"], right["model_key"]), ("target_model", "control_model"))

    def test_baselines_are_state_independent(self) -> None:
        for domain in ("target", "neutral"):
            rendered = {render_baseline(self.fixtures, domain) for _state in "LBS"}
            self.assertEqual(len(rendered), 1)

    def test_action_definitions_each_render_once(self) -> None:
        system = render_system(self.fixtures)
        for action, definition in self.fixtures["actions"].items():
            line = self.fixtures["prompts"]["action_definition_line"].format(
                action=action, definition=definition
            )
            self.assertEqual(system.count(line), 1)

    def test_full_domain_isomorphism(self) -> None:
        target = self.fixtures["domains"]["target"]
        neutral = self.fixtures["domains"]["neutral"]
        self.assertEqual(apply_substitutions(target["task_body"], self.fixtures), neutral["task_body"])
        for canonical in ("PROVENANCE", "OUTCOMES"):
            self.assertEqual(
                apply_substitutions(
                    self.fixtures["cards"][canonical]["topics"]["target"], self.fixtures
                ),
                self.fixtures["cards"][canonical]["topics"]["neutral"],
            )
            for state in "LBS":
                self.assertEqual(
                    apply_substitutions(target["cards"][canonical][state], self.fixtures),
                    neutral["cards"][canonical][state],
                )

    def test_card_ids_are_unique_and_reproducible(self) -> None:
        for domain in ("target", "neutral"):
            for state in "LBS":
                first = build_cards(self.fixtures, self.config["seed"], domain, state)
                second = build_cards(self.fixtures, self.config["seed"], domain, state)
                self.assertEqual(first, second)
                self.assertEqual(len({card["id"] for card in first}), 4)

    def test_score_table_properties(self) -> None:
        scores = self.fixtures["sealed_scores"]
        means = {
            action: sum(scores[state][action] for state in "LBS") / 3
            for action in self.fixtures["actions"]
        }
        self.assertEqual(max(means, key=means.get), "VERIFY")
        for state, action in (("L", "FOLLOW"), ("B", "REPAIR"), ("S", "BLOCK")):
            self.assertEqual(max(scores[state], key=scores[state].get), action)

    def test_reference_variants_resolve(self) -> None:
        cards = build_cards(self.fixtures, self.config["seed"], "target", "L")
        ids = [card["id"] for card in cards]
        payload = {
            "purchases": [ids[0].lower(), ids[1].removeprefix("C-")],
            "ranking": ["first", cards[1]["topic"], "3", ids[3]],
        }
        resolved = resolve_menu(payload, cards)
        self.assertTrue(resolved["valid"])
        self.assertEqual(resolved["purchases"], ids[:2])
        self.assertEqual(resolved["ranking"], ids)

    def test_canonical_names_do_not_resolve(self) -> None:
        cards = build_cards(self.fixtures, self.config["seed"], "target", "L")
        payload = {"purchases": ["PROVENANCE"], "ranking": [card["id"] for card in cards]}
        resolved = resolve_menu(payload, cards)
        self.assertFalse(resolved["valid"])
        self.assertEqual(resolved["invalid_card_ids"][0]["reason"], "unresolvable_or_ambiguous")

    def test_purchase_alias_collision_is_invalid(self) -> None:
        cards = build_cards(self.fixtures, self.config["seed"], "target", "L")
        ids = [card["id"] for card in cards]
        payload = {
            "purchases": [ids[0], ids[0].removeprefix("C-")],
            "ranking": ids,
        }
        resolved = resolve_menu(payload, cards)
        self.assertFalse(resolved["valid"])
        self.assertTrue(
            any(item["reason"] == "post_resolution_alias_collision" for item in resolved["invalid_card_ids"])
        )

    def test_ranking_alias_collision_is_invalid(self) -> None:
        cards = build_cards(self.fixtures, self.config["seed"], "target", "L")
        ids = [card["id"] for card in cards]
        payload = {
            "purchases": [ids[0]],
            "ranking": [ids[0], ids[0].removeprefix("C-"), cards[1]["topic"], "third"],
        }
        resolved = resolve_menu(payload, cards)
        self.assertFalse(resolved["valid"])
        self.assertTrue(
            any(item["field"] == "ranking" for item in resolved["invalid_card_ids"])
        )

    def test_position_only_ranking_flag(self) -> None:
        cards = build_cards(self.fixtures, self.config["seed"], "target", "L")
        payload = {"purchases": ["first"], "ranking": ["1", "2", "3", "4"]}
        resolved = resolve_menu(payload, cards)
        self.assertTrue(resolved["valid"])
        self.assertTrue(resolved["ranking_position_only"])

    def test_alias_collision_uses_membership_repair(self) -> None:
        repaired: dict[str, list[str]] = {}

        def factory(context: dict[str, Any]) -> dict[str, Any]:
            if context["turn"] == "baseline":
                return baseline_payload()
            ids = context.get("card_ids", [])
            if context["turn"] == "menu":
                repaired["ids"] = ids
                return {
                    "purchases": [ids[0], ids[0].removeprefix("C-")],
                    "ranking": ids,
                    "reason_for_allocation": "Two records.",
                }
            if context["turn"] == "membership_repair":
                return {"purchases": ids[:2], "ranking": ids}
            if context["turn"] == "final":
                return final_payload(context["state"], context["correct_action"], context["purchases"])
            raise AssertionError(context)

        provider = ContextProvider(factory)
        record = run_episode(
            episode=self.episode(), provider=provider, fixtures=self.fixtures, config=self.config
        )
        self.assertTrue(record["protocol_valid"])
        self.assertTrue(record["membership_repair_used"])
        self.assertEqual(record["menu"]["purchases"], repaired["ids"][:2])
        self.assertIn("post_resolution_alias_collision", json.dumps(record["invalid_card_ids"]))

    def test_first_credit_deviation_is_honored_without_repair(self) -> None:
        selected: dict[str, str] = {}

        def factory(context: dict[str, Any]) -> dict[str, Any]:
            if context["turn"] == "baseline":
                return baseline_payload()
            if context["turn"] == "menu":
                evaluator = next(
                    card for card in context["cards"] if card["canonical"] == "SCORING"
                )
                selected["id"] = evaluator["id"]
                return {
                    "purchases": [evaluator["id"]],
                    "ranking": context["card_ids"],
                    "reason_for_allocation": "Evaluator information preferred.",
                }
            if context["turn"] == "final":
                return final_payload(context["state"], "VERIFY", context["purchases"])
            raise AssertionError(context)

        provider = ContextProvider(factory)
        record = run_episode(
            episode=self.episode(), provider=provider, fixtures=self.fixtures, config=self.config
        )
        self.assertFalse(record["membership_repair_used"])
        self.assertTrue(record["menu"]["constraint_deviation"])
        self.assertEqual(record["menu"]["purchases"], [selected["id"]])
        self.assertNotIn("membership_repair", [item["turn"] for item in record["turns"]])

    def test_empty_purchase_is_not_assessable(self) -> None:
        def factory(context: dict[str, Any]) -> dict[str, Any]:
            if context["turn"] == "baseline":
                return baseline_payload()
            if context["turn"] == "menu":
                return {
                    "purchases": [],
                    "ranking": context["card_ids"],
                    "reason_for_allocation": "No purchase.",
                }
            if context["turn"] == "final":
                return final_payload(context["state"], "VERIFY", [])
            raise AssertionError(context)

        record = run_episode(
            episode=self.episode(),
            provider=ContextProvider(factory),
            fixtures=self.fixtures,
            config=self.config,
        )
        self.assertFalse(record["membership_repair_used"])
        self.assertTrue(record["menu"]["constraint_deviation"])
        row = score_episode(record, self.fixtures)
        self.assertEqual(row["world_card_count"], 0)
        self.assertEqual(row["assessability"], "NOT_ASSESSABLE")

    def test_literal_duplicate_routes_to_json_repair(self) -> None:
        def factory(context: dict[str, Any]) -> dict[str, Any]:
            if context["turn"] == "baseline":
                return baseline_payload()
            ids = context.get("card_ids", [])
            if context["turn"] == "menu":
                return {
                    "purchases": [ids[0], ids[0]],
                    "ranking": ids,
                    "reason_for_allocation": "Typo.",
                }
            if context["turn"] == "json_repair" and context["repairing"] == "menu":
                return {
                    "purchases": ids[:2],
                    "ranking": ids,
                    "reason_for_allocation": "Same intended allocation, corrected JSON.",
                }
            if context["turn"] == "final":
                return final_payload(context["state"], context["correct_action"], context["purchases"])
            raise AssertionError(context)

        provider = ContextProvider(factory)
        record = run_episode(
            episode=self.episode(), provider=provider, fixtures=self.fixtures, config=self.config
        )
        self.assertFalse(record["membership_repair_used"])
        self.assertTrue(any(turn["json_repair"] for turn in record["turns"]))

    def test_numeric_strings_are_stripped_and_coerced(self) -> None:
        class OneReply:
            def complete(self, *, system, messages, model_config, sampling, context):
                value = baseline_payload()
                value["state_probabilities"] = {"L": " 0.33", "B": "0.33 ", "S": "0.34"}
                return ModelReply(json.dumps(value), model_config["model_id"])

        messages = [{"role": "user", "content": "test"}]
        result = call_and_parse(
            provider=OneReply(),
            system="test",
            messages=messages,
            model_config=self.config["target_model"],
            sampling=self.config["sampling"],
            context={"turn": "baseline"},
            schema_name="baseline",
            fixtures=self.fixtures,
            turns_log=[],
        )
        self.assertEqual(result["outcome"], "OK")
        self.assertEqual(len(result["coerced_numeric_fields"]), 3)

    def test_unexpected_keys_are_logged_without_repair(self) -> None:
        class OneReply:
            def complete(self, *, system, messages, model_config, sampling, context):
                return ModelReply(json.dumps(baseline_payload(notes="extra")), model_config["model_id"])

        turns: list[dict[str, Any]] = []
        result = call_and_parse(
            provider=OneReply(),
            system="test",
            messages=[{"role": "user", "content": "test"}],
            model_config=self.config["target_model"],
            sampling=self.config["sampling"],
            context={"turn": "baseline"},
            schema_name="baseline",
            fixtures=self.fixtures,
            turns_log=turns,
        )
        self.assertEqual(result["outcome"], "OK")
        self.assertEqual(result["unexpected_keys"], ["$.notes"])
        self.assertFalse(any(turn["json_repair"] for turn in turns))

    def test_refusal_is_valid_and_not_repaired(self) -> None:
        class Refuser:
            def complete(self, *, system, messages, model_config, sampling, context):
                return ModelReply(
                    "I decline to participate in this evaluation.", model_config["model_id"]
                )

        record = run_episode(
            episode=self.episode(), provider=Refuser(), fixtures=self.fixtures, config=self.config
        )
        self.assertEqual(record["outcome"], "REFUSE_TASK")
        self.assertTrue(record["protocol_valid"])
        self.assertEqual(len(record["turns"]), 1)

    def test_model_id_mismatch_stops(self) -> None:
        class WrongModel:
            def complete(self, *, system, messages, model_config, sampling, context):
                return ModelReply(json.dumps(baseline_payload()), "different-model")

        with self.assertRaises(ProtocolError):
            run_episode(
                episode=self.episode(),
                provider=WrongModel(),
                fixtures=self.fixtures,
                config=self.config,
            )

    def test_resumption_keeps_header_and_skips_completed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.jsonl"
            records, manifest = initialize_log(path, ROOT, self.fixtures)
            self.assertEqual(records[0]["record_type"], "run_header")
            append_jsonl(path, {"record_type": "episode", "episode_id": "done"})
            records_again, manifest_again = initialize_log(path, ROOT, self.fixtures)
            self.assertEqual(manifest, manifest_again)
            self.assertEqual(records_again[0]["record_type"], "run_header")
            self.assertEqual(records_again[1]["episode_id"], "done")

    def test_fake_run_scores_reproducibly(self) -> None:
        records = []
        for episode in episode_order(self.fixtures, self.config):
            records.append(
                run_episode(
                    episode=episode,
                    provider=CooperativeFakeProvider(),
                    fixtures=self.fixtures,
                    config=self.config,
                )
            )
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            raw = temporary_path / "raw.jsonl"
            append_jsonl(
                raw,
                {
                    "record_type": "run_header",
                    "study_version": self.fixtures["study_version"],
                    "git_commit": "test",
                    "git_dirty": True,
                    "manifest_sha256": "test",
                },
            )
            for record in records:
                append_jsonl(raw, record)
            first = temporary_path / "analysis1"
            second = temporary_path / "analysis2"
            rows1 = score_log(raw, self.fixtures, first)
            rows2 = score_log(raw, self.fixtures, second)
            self.assertEqual(rows1, rows2)
            self.assertEqual(len(rows1), 12)
            for name in ("run_level.csv", "cell_table.csv", "cross_model.csv", "purchase_table.csv", "notes.md"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
            with (first / "cross_model.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 6)


if __name__ == "__main__":
    unittest.main()
