"""The alert standard: schema cache, folder classification and clone validation.

Classification mirrors the compliance engine's evaluate_rule and resolve_field_value
(migration-compliance/src/evaluator/engine.py, src/adapter/mapper.py), so the folder
view agrees with the per-rule verdicts of /api/rules/load.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.compliance.client import ComplianceClient

OPERATOR_FIELD = "Appchi-$operator"
OPERATOR_LABEL_PREFIX = "Appchi - "

# The standard schema as the compliance service returns it.
StandardSchema = Mapping[str, Any]

# What String.prototype.trim() removes.
_JS_WHITESPACE = "\t\n\v\f\r " + "".join(
    map(
        chr,
        (0x00A0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF),
    )
)


def _js_trim(value: str) -> str:
    return value.strip(_JS_WHITESPACE)


class StandardSchemaCache:
    """The standard schema, fetched once per process."""

    def __init__(self, compliance: ComplianceClient) -> None:
        self._compliance = compliance
        self._schema: StandardSchema | None = None

    async def get(self) -> StandardSchema:
        if self._schema:
            return self._schema
        response = await self._compliance.request("GET", "/standard/schema")
        if not response.is_success:
            raise RuntimeError(f"Failed to load standard schema: {response.status_code}")
        self._schema = response.json()
        return self._schema


def _operator_label(labels: Mapping[str, str]) -> str | None:
    return next((key for key in labels if key.startswith(OPERATOR_LABEL_PREFIX)), None)


def _get_case_insensitive(mapping: Mapping[str, str], key: str) -> str | None:
    if key in mapping:
        return mapping[key]
    key_lower = key.lower()
    return next((value for name, value in mapping.items() if name.lower() == key_lower), None)


def resolve_field_value(
    field: Mapping[str, Any], labels: Mapping[str, str], annotations: Mapping[str, str]
) -> str | None:
    name = field["name"]
    if field.get("location") == "label":
        if name == OPERATOR_FIELD:
            return _operator_label(labels)
        if name == "Send-Alert":
            return labels.get("Send-Alert")
        if name == "component":
            # The engine's `get("component") or get("object")`.
            return labels.get("component") or labels.get("object")
        return _get_case_insensitive(labels, name)
    if field.get("location") == "annotation":
        return annotations.get(name)
    return None


def classify_rule_against_schema(
    schema: StandardSchema, labels: Mapping[str, str], annotations: Mapping[str, str]
) -> list[str]:
    """Names of the fields a rule fails; empty means the rule is v2.

    Skips only the notification-policy membership check, which runs in /api/rules/load.
    """
    failed: list[str] = []
    for field in schema["fields"]:
        name = field["name"]
        observed = resolve_field_value(field, labels, annotations)
        # The engine treats only "" and None as missing, without trimming.
        if observed is None or observed == "":
            if field.get("required"):
                failed.append(name)
            continue

        constraints = field.get("constraints") or {}

        exact_value = constraints.get("exact_value")
        if exact_value is not None:
            # Case-insensitive on purpose (the engine is exact): Grafana has returned
            # this label as "TRUE"/"True", and the seeder must still recognise those.
            if observed.lower() != str(exact_value).lower():
                failed.append(name)
            continue

        if name == OPERATOR_FIELD:
            prefix = constraints.get("starts_with")
            # As the engine: the right prefix, and the label itself set to exactly "true".
            if (
                not observed.startswith(OPERATOR_LABEL_PREFIX if prefix is None else prefix)
                or labels.get(observed) != "true"
            ):
                failed.append(name)
            continue

        min_length = constraints.get("min_length")
        if min_length is not None and len(observed) < min_length:
            failed.append(name)
            continue
        max_length = constraints.get("max_length")
        if max_length is not None and len(observed) > max_length:
            failed.append(name)
            continue

        allowed_values = constraints.get("allowed_values")
        if allowed_values is not None and observed not in allowed_values:
            failed.append(name)
    return failed


def validate_clone_payload(
    schema: StandardSchema, labels: Mapping[str, str], annotations: Mapping[str, str]
) -> list[str]:
    """Required fields missing from a clone or create payload, as messages."""
    missing: list[str] = []
    for field in schema["fields"]:
        if not field.get("required"):
            continue
        name = field["name"]
        if name == OPERATOR_FIELD:
            operator_key = _operator_label(labels)
            if operator_key is None:
                missing.append(name)
            elif _js_trim(labels[operator_key]) != "true":
                missing.append(f"{name} (label '{operator_key}' must have the value 'true')")
            continue
        raw = labels.get(name) if field.get("location") == "label" else annotations.get(name)
        if raw is None or _js_trim(raw) == "":
            missing.append(name)
    return missing
