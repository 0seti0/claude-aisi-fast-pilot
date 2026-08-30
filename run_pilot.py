#!/usr/bin/env python3
"""Run the fast-pilot-v11 protocol and append complete episodes to JSONL."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml


FROZEN_FILES = (
    "FAST_PILOT.md",
    "config.yaml",
    "fixtures.json",
    "run_pilot.py",
    "score_pilot.py",
    "test_pilot.py",
)
MODEL_KEYS = ("target_model", "control_model")
DOMAINS = ("target", "neutral")
STATES = ("L", "B", "S")
CARD_NAMES = ("PROVENANCE", "OUTCOMES", "SCORING", "VISIBLE_CHECKS")
WORLD_CARDS = {"PROVENANCE", "OUTCOMES"}


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelReply:
    text: str
    returned_model_id: str


class Provider(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        model_config: dict[str, Any],
        sampling: dict[str, Any],
        context: dict[str, Any],
    ) -> ModelReply: ...


class AnthropicProvider:
    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProtocolError("Install dependencies with: python -m pip install -r requirements.txt") from exc
        self.client = anthropic.Anthropic()

    def complete(self, *, system, messages, model_config, sampling, context) -> ModelReply:
        kwargs: dict[str, Any] = {
            "model": model_config["model_id"],
            "system": system,
            "messages": messages,
            "max_tokens": sampling["max_output_tokens"],
        }
        if sampling.get("temperature") is not None:
            kwargs["temperature"] = sampling["temperature"]
        response = self.client.messages.create(**kwargs)
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return ModelReply(text=text, returned_model_id=response.model)


class OpenAIProvider:
    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProtocolError("Install dependencies with: python -m pip install -r requirements.txt") from exc
        self.client = OpenAI()

    def complete(self, *, system, messages, model_config, sampling, context) -> ModelReply:
        input_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict[str, Any] = {
            "model": model_config["model_id"],
            "input": input_messages,
            "max_output_tokens": sampling["max_output_tokens"],
        }
        if sampling.get("temperature") is not None:
            kwargs["temperature"] = sampling["temperature"]
        effort = model_config.get("reasoning_effort")
        if effort and not str(effort).startswith("REPLACE") and effort != "match_target":
            kwargs["reasoning"] = {"effort": effort}
        response = self.client.responses.create(**kwargs)
        return ModelReply(text=response.output_text, returned_model_id=response.model)


class CooperativeFakeProvider:
    """Deterministic provider used only by --dry-run and tests."""

    def complete(self, *, system, messages, model_config, sampling, context) -> ModelReply:
        turn = context["turn"]
        ids = context.get("card_ids", [])
        if turn == "baseline":
            payload = {
                "action": "VERIFY",
                "state_probabilities": {"L": 1 / 3, "B": 1 / 3, "S": 1 / 3},
                "most_decision_relevant_missing_fact": "authorization and diagnostic performance",
                "reason": "The available records do not yet determine the state.",
            }
        elif turn == "menu":
            world = [item["id"] for item in context["cards"] if item["canonical"] in WORLD_CARDS]
            payload = {
                "purchases": world,
                "ranking": ids,
                "reason_for_allocation": "Project records bear directly on legitimacy and feasibility.",
            }
        elif turn == "final":
            payload = {
                "action": context["correct_action"],
                "state_probabilities": {
                    state: (1.0 if state == context["state"] else 0.0) for state in STATES
                },
                "information_used": [
                    {"card": card_id, "decision_effect": "Used as disclosed."}
                    for card_id in context.get("purchases", [])
                ],
                "remaining_objection": None,
                "reason": "The purchased project records determine the action.",
            }
        elif turn == "json_repair":
            raise AssertionError("Cooperative fake should not need JSON repair")
        elif turn == "membership_repair":
            payload = {"purchases": ids[:2], "ranking": ids}
        else:
            raise AssertionError(f"Unknown fake turn: {turn}")
        return ModelReply(json.dumps(payload), model_config["model_id"])


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ProtocolError(f"Expected a YAML object in {path}")
    return data


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_sha256(root: Path) -> str:
    lines = []
    for name in sorted(FROZEN_FILES):
        path = root / name
        if not path.is_file():
            raise ProtocolError(f"Frozen artifact missing: {name}")
        lines.append(f"{name}\t{file_sha256(path)}")
    return sha256_text("\n".join(lines) + "\n")


def git_state(root: Path) -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "NO_GIT_COMMIT", True


def render_system(fixtures: dict[str, Any]) -> str:
    lines = [
        fixtures["prompts"]["action_definition_line"].format(action=name, definition=definition)
        for name, definition in fixtures["actions"].items()
    ]
    return fixtures["system_message_template"].format(action_definitions="\n".join(lines))


def render_baseline(fixtures: dict[str, Any], domain: str) -> str:
    return fixtures["domains"][domain]["task_body"] + "\n\n" + fixtures["prompts"]["baseline_request"]


def build_cards(fixtures: dict[str, Any], seed: int, domain: str, state: str) -> list[dict[str, str]]:
    cards = []
    for canonical in CARD_NAMES:
        spec = fixtures["cards"][canonical]
        digest_input = f"{seed}|{domain}|{state}|id|{canonical}"
        order_input = f"{seed}|{domain}|{state}|order|{canonical}"
        card_id = spec_id(fixtures, digest_input)
        if spec["state_dependent"]:
            content = fixtures["domains"][domain]["cards"][canonical][state]
        else:
            content = spec["content"]
        cards.append(
            {
                "canonical": canonical,
                "id": card_id,
                "topic": spec["topics"][domain],
                "content": content,
                "order_key": sha256_text(order_input),
            }
        )
    cards.sort(key=lambda item: item["order_key"])
    if len({item["id"] for item in cards}) != len(cards):
        raise ProtocolError(f"Opaque card-ID collision for {domain}/{state}")
    return cards


def spec_id(fixtures: dict[str, Any], value: str) -> str:
    spec = fixtures["menu"]["opaque_ids"]
    return spec["display_prefix"] + sha256_text(value)[: spec["digest_characters"]]


def render_menu(fixtures: dict[str, Any], domain: str, cards: list[dict[str, str]]) -> str:
    card_list = "\n".join(
        fixtures["prompts"]["card_list_line"].format(card_id=item["id"], topic=item["topic"])
        for item in cards
    )
    template = fixtures["prompts"]["menu_request"]
    replacements = {
        "{provenance_topic}": fixtures["cards"]["PROVENANCE"]["topics"][domain],
        "{outcomes_topic}": fixtures["cards"]["OUTCOMES"]["topics"][domain],
        "{card_list}": card_list,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def render_disclosure(fixtures: dict[str, Any], cards: list[dict[str, str]], purchases: list[str]) -> str:
    if not purchases:
        return fixtures["prompts"]["final_request"]
    by_id = {item["id"]: item for item in cards}
    blocks = [fixtures["prompts"]["disclosure_intro"]]
    blocks.extend(
        fixtures["prompts"]["disclosed_card"].format(card_id=card_id, content=by_id[card_id]["content"])
        for card_id in purchases
    )
    blocks.append(fixtures["prompts"]["final_request"])
    return "\n\n".join(blocks)


def episode_order(fixtures: dict[str, Any], config: dict[str, Any]) -> list[dict[str, str]]:
    seed = config["seed"]
    pairs = [(domain, state) for domain in DOMAINS for state in STATES]
    pairs.sort(key=lambda pair: sha256_text(f"{seed}|pair|{pair[0]}|{pair[1]}"))
    episodes = []
    for domain, state in pairs:
        for model_key in MODEL_KEYS:
            episodes.append(
                {
                    "episode_id": f"fp11-{model_key}-{domain}-{state}",
                    "model_key": model_key,
                    "domain": domain,
                    "state": state,
                }
            )
    return episodes


def normalize_reference(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    pairs = (("`", "`"), ('"', '"'), ("'", "'"))
    for left, right in pairs:
        if len(value) >= 2 and value.startswith(left) and value.endswith(right):
            value = value[1:-1]
            break
    return " ".join(value.casefold().split())


def reference_lookup(cards: list[dict[str, str]]) -> dict[str, list[tuple[str, str]]]:
    lookup: dict[str, list[tuple[str, str]]] = {}

    def add(raw: str, card_id: str, method: str) -> None:
        lookup.setdefault(normalize_reference(raw), []).append((card_id, method))

    ordinal_forms = (
        ("1", "card 1", "first", "first card", "the first card"),
        ("2", "card 2", "second", "second card", "the second card"),
        ("3", "card 3", "third", "third card", "the third card"),
        ("4", "card 4", "fourth", "fourth card", "the fourth card"),
    )
    for index, card in enumerate(cards):
        card_id = card["id"]
        digest = card_id.removeprefix("C-")
        for variant in (card_id, digest, "C" + digest):
            add(variant, card_id, "id")
        add(card["topic"], card_id, "topic")
        for variant in ordinal_forms[index]:
            add(variant, card_id, "position")
    return lookup


def resolve_field(values: list[str], cards: list[dict[str, str]], field: str) -> dict[str, Any]:
    lookup = reference_lookup(cards)
    resolved: list[str] = []
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for index, raw in enumerate(values):
        candidates = list(dict.fromkeys(lookup.get(normalize_reference(raw), [])))
        ids = {item[0] for item in candidates}
        if len(ids) != 1:
            invalid.append({"field": field, "index": index, "raw": raw, "reason": "unresolvable_or_ambiguous"})
            continue
        card_id = next(iter(ids))
        methods = {method for candidate_id, method in candidates if candidate_id == card_id}
        method = "position" if methods == {"position"} else sorted(methods)[0]
        resolved.append(card_id)
        records.append(
            {"field": field, "index": index, "raw": raw, "resolved_id": card_id, "method": method}
        )

    collisions: list[dict[str, Any]] = []
    positions_by_id: dict[str, list[int]] = {}
    for record in records:
        positions_by_id.setdefault(record["resolved_id"], []).append(record["index"])
    for card_id, indexes in positions_by_id.items():
        if len(indexes) > 1:
            collision = {
                "field": field,
                "resolved_id": card_id,
                "indexes": indexes,
                "raw_values": [values[index] for index in indexes],
                "reason": "post_resolution_alias_collision",
            }
            collisions.append(collision)
            invalid.append(collision)
    return {"resolved": resolved, "records": records, "invalid": invalid, "collisions": collisions}


def resolve_menu(payload: dict[str, Any], cards: list[dict[str, str]]) -> dict[str, Any]:
    purchases = resolve_field(payload["purchases"], cards, "purchases")
    ranking = resolve_field(payload["ranking"], cards, "ranking")
    episode_ids = {card["id"] for card in cards}
    valid = not purchases["invalid"] and not ranking["invalid"]
    valid = valid and len(purchases["resolved"]) == len(set(purchases["resolved"]))
    valid = valid and set(purchases["resolved"]).issubset(episode_ids)
    valid = valid and len(ranking["resolved"]) == 4 and set(ranking["resolved"]) == episode_ids
    return {
        "valid": valid,
        "purchases": purchases["resolved"],
        "ranking": ranking["resolved"],
        "resolved_card_references": purchases["records"] + ranking["records"],
        "invalid_card_ids": purchases["invalid"] + ranking["invalid"],
        "ranking_position_only": bool(ranking["records"])
        and len(ranking["records"]) == 4
        and all(record["method"] == "position" for record in ranking["records"]),
    }


def explicit_evaluation_refusal(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    refusal = re.search(r"\b(refuse|decline|will not|won't|cannot|can't)\b", normalized)
    evaluation = re.search(r"\b(evaluation|experiment|study|game|test)\b", normalized)
    participation = re.search(r"\b(participat|take part|engage)\w*\b", normalized)
    return bool(refusal and evaluation and participation)


def parse_json_strict(text: str) -> dict[str, Any]:
    value = json.loads(text.strip())
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    return value


def coerce_probabilities(payload: dict[str, Any], pattern: str) -> list[str]:
    paths: list[str] = []
    probabilities = payload.get("state_probabilities")
    if not isinstance(probabilities, dict):
        return paths
    regex = re.compile(pattern)
    for state in STATES:
        value = probabilities.get(state)
        if isinstance(value, str):
            stripped = value.strip()
            if regex.fullmatch(stripped):
                probabilities[state] = float(stripped)
                paths.append(f"state_probabilities.{state}")
    return paths


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    type_ok = False
    for item in types:
        if item == "object" and isinstance(value, dict):
            type_ok = True
        elif item == "array" and isinstance(value, list):
            type_ok = True
        elif item == "string" and isinstance(value, str):
            type_ok = True
        elif item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            type_ok = True
        elif item == "null" and value is None:
            type_ok = True
    if not type_ok:
        return [f"{path}: expected {expected}"]
    if isinstance(value, dict) and "object" in types:
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}: missing required key {required}")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], child, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value.keys() - properties.keys():
                errors.append(f"{path}: unexpected key {key}")
    if isinstance(value, list) and "array" in types:
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path}: raw items are not unique")
        for index, item in enumerate(value):
            errors.extend(validate_schema(item, schema["items"], f"{path}[{index}]"))
    if isinstance(value, str) and "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    return errors


def unexpected_keys(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict) and schema.get("type") == "object":
        properties = schema.get("properties", {})
        found.extend(f"{path}.{key}" for key in value.keys() - properties.keys())
        for key, child in properties.items():
            if key in value:
                found.extend(unexpected_keys(value[key], child, f"{path}.{key}"))
    elif isinstance(value, list) and schema.get("type") == "array":
        for index, item in enumerate(value):
            found.extend(unexpected_keys(item, schema["items"], f"{path}[{index}]"))
    return found


def validate_payload(payload: dict[str, Any], schema_name: str, fixtures: dict[str, Any]) -> list[str]:
    schema = fixtures["response_schemas"][schema_name]
    errors = validate_schema(payload, schema)
    if schema_name in {"baseline", "final"}:
        probabilities = payload.get("state_probabilities")
        if isinstance(probabilities, dict) and all(
            isinstance(probabilities.get(state), (int, float)) and not isinstance(probabilities.get(state), bool)
            for state in STATES
        ):
            tolerance = fixtures["validation"]["probability_sum_tolerance"]
            if abs(sum(probabilities[state] for state in STATES) - 1.0) > tolerance:
                errors.append("$.state_probabilities: values do not sum to 1 within tolerance")
    return errors


def call_and_parse(
    *,
    provider: Provider,
    system: str,
    messages: list[dict[str, str]],
    model_config: dict[str, Any],
    sampling: dict[str, Any],
    context: dict[str, Any],
    schema_name: str,
    fixtures: dict[str, Any],
    turns_log: list[dict[str, Any]],
    allow_json_repair: bool = True,
) -> dict[str, Any]:
    reply = provider.complete(
        system=system,
        messages=messages,
        model_config=model_config,
        sampling=sampling,
        context=context,
    )
    if reply.returned_model_id != model_config["model_id"]:
        raise ProtocolError(
            f"Returned model ID {reply.returned_model_id!r} differs from requested {model_config['model_id']!r}"
        )
    turns_log.append(
        {
            "turn": context["turn"],
            "raw_response": reply.text,
            "returned_model_id": reply.returned_model_id,
            "json_repair": False,
        }
    )
    messages.append({"role": "assistant", "content": reply.text})
    if explicit_evaluation_refusal(reply.text):
        return {"outcome": "REFUSE_TASK"}

    def attempt(text: str) -> tuple[dict[str, Any] | None, list[str], list[str], list[str]]:
        try:
            payload = parse_json_strict(text)
        except (json.JSONDecodeError, ValueError) as exc:
            return None, [str(exc)], [], []
        coerced = []
        if schema_name in {"baseline", "final"}:
            coerced = coerce_probabilities(payload, fixtures["parsing"]["numeric_string_pattern"])
        errors = validate_payload(payload, schema_name, fixtures)
        unknown = unexpected_keys(payload, fixtures["response_schemas"][schema_name])
        return payload, errors, unknown, coerced

    payload, errors, unknown, coerced = attempt(reply.text)
    if errors and allow_json_repair and sampling.get("max_json_repair_turns", 1) > 0:
        repair_prompt = fixtures["prompts"]["json_repair"]
        messages.append({"role": "user", "content": repair_prompt})
        repair_context = {**context, "turn": "json_repair", "repairing": context["turn"]}
        repaired = provider.complete(
            system=system,
            messages=messages,
            model_config=model_config,
            sampling=sampling,
            context=repair_context,
        )
        if repaired.returned_model_id != model_config["model_id"]:
            raise ProtocolError("Returned model ID changed during JSON repair")
        turns_log.append(
            {
                "turn": context["turn"],
                "raw_response": repaired.text,
                "returned_model_id": repaired.returned_model_id,
                "json_repair": True,
                "initial_validation_errors": errors,
            }
        )
        messages.append({"role": "assistant", "content": repaired.text})
        if explicit_evaluation_refusal(repaired.text):
            return {"outcome": "REFUSE_TASK"}
        payload, errors, unknown, coerced = attempt(repaired.text)
    if errors or payload is None:
        return {"outcome": "INVALID", "validation_errors": errors}
    return {
        "outcome": "OK",
        "payload": payload,
        "unexpected_keys": unknown,
        "coerced_numeric_fields": coerced,
    }


def menu_constraint_deviation(canonical_purchases: list[str], fixtures: dict[str, Any]) -> bool:
    allowed = set(fixtures["menu"]["first_credit_allowed"])
    return not canonical_purchases or canonical_purchases[0] not in allowed


def run_episode(
    *,
    episode: dict[str, str],
    provider: Provider,
    fixtures: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    model_key, domain, state = episode["model_key"], episode["domain"], episode["state"]
    model_config = config[model_key]
    sampling = config["sampling"]
    system = render_system(fixtures)
    cards = build_cards(fixtures, config["seed"], domain, state)
    card_ids = [card["id"] for card in cards]
    by_id = {card["id"]: card for card in cards}
    messages: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    record: dict[str, Any] = {
        "record_type": "episode",
        **episode,
        "requested_model_id": model_config["model_id"],
        "provider": model_config["provider"],
        "system_prompt": system,
        "cards": cards,
        "turns": turns,
        "protocol_valid": True,
        "outcome": "OK",
        "unexpected_keys": [],
        "coerced_numeric_fields": [],
        "resolved_card_references": [],
        "invalid_card_ids": [],
        "membership_repair": None,
        "membership_repair_used": False,
    }

    baseline_prompt = render_baseline(fixtures, domain)
    messages.append({"role": "user", "content": baseline_prompt})
    baseline = call_and_parse(
        provider=provider,
        system=system,
        messages=messages,
        model_config=model_config,
        sampling=sampling,
        context={"turn": "baseline", **episode},
        schema_name="baseline",
        fixtures=fixtures,
        turns_log=turns,
    )
    if baseline["outcome"] != "OK":
        record["outcome"] = baseline["outcome"]
        record["protocol_valid"] = baseline["outcome"] == "REFUSE_TASK"
        record["validation_errors"] = baseline.get("validation_errors", [])
        return record
    record["baseline"] = baseline["payload"]
    record["unexpected_keys"].extend(baseline["unexpected_keys"])
    record["coerced_numeric_fields"].extend(baseline["coerced_numeric_fields"])

    menu_prompt = render_menu(fixtures, domain, cards)
    messages.append({"role": "user", "content": menu_prompt})
    menu = call_and_parse(
        provider=provider,
        system=system,
        messages=messages,
        model_config=model_config,
        sampling=sampling,
        context={"turn": "menu", "cards": cards, "card_ids": card_ids, **episode},
        schema_name="menu",
        fixtures=fixtures,
        turns_log=turns,
    )
    if menu["outcome"] != "OK":
        record["outcome"] = menu["outcome"]
        record["protocol_valid"] = menu["outcome"] == "REFUSE_TASK"
        record["validation_errors"] = menu.get("validation_errors", [])
        return record
    menu_payload = menu["payload"]
    record["unexpected_keys"].extend(menu["unexpected_keys"])
    resolved = resolve_menu(menu_payload, cards)
    record["resolved_card_references"].extend(resolved["resolved_card_references"])
    record["invalid_card_ids"].extend(resolved["invalid_card_ids"])

    if not resolved["valid"]:
        record["membership_repair_used"] = True
        repair_prompt = fixtures["prompts"]["menu_membership_repair"].replace(
            "{card_id_list}", "\n".join(f"- `{card_id}`" for card_id in card_ids)
        )
        messages.append({"role": "user", "content": repair_prompt})
        repair = call_and_parse(
            provider=provider,
            system=system,
            messages=messages,
            model_config=model_config,
            sampling=sampling,
            context={"turn": "membership_repair", "cards": cards, "card_ids": card_ids, **episode},
            schema_name="menu_membership_repair",
            fixtures=fixtures,
            turns_log=turns,
        )
        record["membership_repair"] = {
            "initial_purchases": menu_payload["purchases"],
            "initial_ranking": menu_payload["ranking"],
            "result": repair["outcome"],
        }
        if repair["outcome"] != "OK":
            record["outcome"] = repair["outcome"]
            record["protocol_valid"] = repair["outcome"] == "REFUSE_TASK"
            record["validation_errors"] = repair.get("validation_errors", [])
            return record
        record["unexpected_keys"].extend(repair["unexpected_keys"])
        repaired_payload = repair["payload"]
        resolved = resolve_menu(repaired_payload, cards)
        record["resolved_card_references"].extend(resolved["resolved_card_references"])
        record["invalid_card_ids"].extend(resolved["invalid_card_ids"])
        record["membership_repair"].update(
            {
                "repaired_purchases": repaired_payload["purchases"],
                "repaired_ranking": repaired_payload["ranking"],
            }
        )
        if not resolved["valid"]:
            record["outcome"] = "INVALID"
            record["protocol_valid"] = False
            record["validation_errors"] = ["menu membership invalid after membership repair"]
            return record

    purchases = resolved["purchases"]
    ranking = resolved["ranking"]
    canonical_purchases = [by_id[card_id]["canonical"] for card_id in purchases]
    record["menu"] = {
        "raw": menu_payload,
        "purchases": purchases,
        "canonical_purchases": canonical_purchases,
        "ranking": ranking,
        "canonical_ranking": [by_id[card_id]["canonical"] for card_id in ranking],
        "ranking_position_only": resolved["ranking_position_only"],
        "constraint_deviation": menu_constraint_deviation(canonical_purchases, fixtures),
    }

    final_prompt = render_disclosure(fixtures, cards, purchases)
    messages.append({"role": "user", "content": final_prompt})
    final = call_and_parse(
        provider=provider,
        system=system,
        messages=messages,
        model_config=model_config,
        sampling=sampling,
        context={
            "turn": "final",
            "cards": cards,
            "card_ids": card_ids,
            "purchases": purchases,
            "correct_action": fixtures["states"][state]["complete_information_action"],
            **episode,
        },
        schema_name="final",
        fixtures=fixtures,
        turns_log=turns,
    )
    if final["outcome"] != "OK":
        record["outcome"] = final["outcome"]
        record["protocol_valid"] = final["outcome"] == "REFUSE_TASK"
        record["validation_errors"] = final.get("validation_errors", [])
        return record
    record["final"] = final["payload"]
    record["unexpected_keys"].extend(final["unexpected_keys"])
    record["coerced_numeric_fields"].extend(final["coerced_numeric_fields"])
    return record


def apply_substitutions(text: str, fixtures: dict[str, Any]) -> str:
    for item in fixtures["validation"]["ordered_domain_substitutions_target_to_neutral"]:
        text = text.replace(item["source"], item["replacement"])
    return text


def validate_design(root: Path, fixtures: dict[str, Any], config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("study_version") != fixtures.get("study_version"):
        errors.append("config and fixtures study_version differ")
    episodes = episode_order(fixtures, config)
    if len(episodes) != fixtures["validation"]["expected_episode_count"]:
        errors.append("episode count differs from fixture expectation")
    system = render_system(fixtures)
    for action, definition in fixtures["actions"].items():
        line = fixtures["prompts"]["action_definition_line"].format(action=action, definition=definition)
        if system.count(line) != 1:
            errors.append(f"system does not contain {action} definition exactly once")
    for domain in DOMAINS:
        baseline = render_baseline(fixtures, domain)
        combined = system + "\n" + baseline
        for term in fixtures["validation"]["baseline_leakage_blacklist"]:
            if term.casefold() in combined.casefold():
                errors.append(f"baseline leakage in {domain}: {term}")
        for canonical in CARD_NAMES:
            if canonical in combined:
                errors.append(f"canonical card name leaked in baseline: {canonical}")
    seed = config["seed"]
    for domain in DOMAINS:
        for state in STATES:
            first = build_cards(fixtures, seed, domain, state)
            second = build_cards(fixtures, seed, domain, state)
            if first != second:
                errors.append(f"card generation not reproducible: {domain}/{state}")
            if len({card['id'] for card in first}) != 4:
                errors.append(f"opaque IDs not unique: {domain}/{state}")
    target = fixtures["domains"]["target"]
    neutral = fixtures["domains"]["neutral"]
    if apply_substitutions(target["task_body"], fixtures) != neutral["task_body"]:
        errors.append("task bodies fail ordered-substitution isomorphism")
    for canonical in ("PROVENANCE", "OUTCOMES"):
        target_topic = fixtures["cards"][canonical]["topics"]["target"]
        neutral_topic = fixtures["cards"][canonical]["topics"]["neutral"]
        if apply_substitutions(target_topic, fixtures) != neutral_topic:
            errors.append(f"{canonical} topics fail isomorphism")
        for state in STATES:
            left = target["cards"][canonical][state]
            right = neutral["cards"][canonical][state]
            if apply_substitutions(left, fixtures) != right:
                errors.append(f"{canonical}/{state} cards fail isomorphism")
    scores = fixtures["sealed_scores"]
    prior_means = {
        action: sum(scores[state][action] for state in STATES) / len(STATES)
        for action in fixtures["actions"]
    }
    if max(prior_means, key=prior_means.get) != "VERIFY":
        errors.append("VERIFY does not maximize flat-prior sealed score")
    for state in STATES:
        intended = fixtures["states"][state]["complete_information_action"]
        if max(scores[state], key=scores[state].get) != intended:
            errors.append(f"sealed score not uniquely aligned in state {state}")
        if list(scores[state].values()).count(scores[state][intended]) != 1:
            errors.append(f"sealed score maximum tied in state {state}")
    sets = fixtures["justified_action_sets"]
    for state in STATES:
        if len(sets["both_world_cards"][state]) != 1:
            errors.append(f"both-card justified set not singleton for {state}")
    if sets["one_world_card"]["S"] != ["BLOCK"] or sets["both_world_cards"]["S"] != ["BLOCK"]:
        errors.append("S justified sets must both be {BLOCK}")
    join_key = fixtures["validation"]["join_key"]
    expected = fixtures["validation"]["join_key_expected_locations_per_domain"]
    for domain in DOMAINS:
        count = sum(
            join_key in fixtures["domains"][domain]["cards"][canonical][state]
            for canonical in ("PROVENANCE", "OUTCOMES")
            for state in STATES
        )
        if count != expected:
            errors.append(f"join key count is {count}, expected {expected}, in {domain}")
        if join_key in render_baseline(fixtures, domain) or join_key in render_menu(
            fixtures, domain, build_cards(fixtures, seed, domain, "B")
        ):
            errors.append(f"join key leaked before disclosure in {domain}")
    for index in range(0, len(episodes), 2):
        a, b = episodes[index : index + 2]
        if (a["domain"], a["state"]) != (b["domain"], b["state"]):
            errors.append("matched model episodes are not adjacent")
    try:
        manifest_sha256(root)
    except ProtocolError as exc:
        errors.append(str(exc))
    return errors


def provider_for(config: dict[str, Any], dry_run: bool) -> Provider:
    if dry_run:
        return CooperativeFakeProvider()
    provider = config["provider"]
    if provider == "anthropic":
        return AnthropicProvider()
    if provider == "openai":
        return OpenAIProvider()
    raise ProtocolError(f"Unsupported provider: {provider!r}")


def require_real_config(config: dict[str, Any]) -> None:
    for model_key in MODEL_KEYS:
        model = config[model_key]
        if str(model.get("provider", "")).startswith("REPLACE"):
            raise ProtocolError(f"Fill {model_key}.provider in config.yaml")
        if str(model.get("model_id", "")).startswith("REPLACE"):
            raise ProtocolError(f"Fill {model_key}.model_id in config.yaml")
    if config["sampling"].get("structured_output") is not False:
        raise ProtocolError("structured_output must remain false")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ProtocolError(f"Invalid JSONL at {path}:{line_number}") from exc
    return records


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def initialize_log(path: Path, root: Path, fixtures: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    manifest = manifest_sha256(root)
    records = read_jsonl(path)
    if records:
        header = records[0]
        if header.get("record_type") != "run_header":
            raise ProtocolError("Existing raw log does not begin with run_header")
        if header.get("study_version") != fixtures["study_version"]:
            raise ProtocolError("Existing raw log study_version differs")
        if header.get("manifest_sha256") != manifest:
            raise ProtocolError("Existing raw log manifest differs; use a new log path")
        return records, manifest
    commit, dirty = git_state(root)
    header = {
        "record_type": "run_header",
        "study_version": fixtures["study_version"],
        "git_commit": commit,
        "git_dirty": dirty,
        "manifest_sha256": manifest,
    }
    append_jsonl(path, header)
    return [header], manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--raw-log", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    fixtures = load_json(root / "fixtures.json")
    config = load_yaml(root / "config.yaml")
    errors = validate_design(root, fixtures, config)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 2
    print("Validation gate passed.")
    if args.validate_only:
        return 0
    if not args.dry_run:
        require_real_config(config)
    raw_log = args.raw_log or root / config["paths"]["raw_log"]
    if args.dry_run and args.raw_log is None:
        raw_log = root / "runs" / "dry_run.jsonl"
    records, manifest = initialize_log(raw_log, root, fixtures)
    completed = {
        record["episode_id"] for record in records if record.get("record_type") == "episode"
    }
    providers = {
        model_key: provider_for(config[model_key], args.dry_run) for model_key in MODEL_KEYS
    }
    for episode in episode_order(fixtures, config):
        if episode["episode_id"] in completed:
            print(f"SKIP {episode['episode_id']}")
            continue
        print(f"RUN  {episode['episode_id']}")
        record = run_episode(
            episode=episode,
            provider=providers[episode["model_key"]],
            fixtures=fixtures,
            config=config,
        )
        record["manifest_sha256"] = manifest
        append_jsonl(raw_log, record)
    print(f"Complete: {raw_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
