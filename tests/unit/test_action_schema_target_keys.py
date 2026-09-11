"""Pins the exact keys the LLM must use inside a propose_tool_call's
`target` dict against what DatabaseTarget actually accepts.

Reproduces a live finding: the real Gemini model proposed
database.update_statistics with an empty `target` (no schema/object at
all), because _FLAT_ACTION_SCHEMA's target field had no guidance on what
keys belong there — every schema/object-scoped write tool would fail the
same way regardless of how the request was phrased. DatabaseTarget uses
`extra="forbid"` with aliases (schema_name -> "schema", object_name ->
"object") and no populate_by_name, so "schema_name"/"object_name" in the
JSON itself are rejected outright — only "schema"/"object" work."""

from __future__ import annotations

from inumi.agent.llm.base import _ACTION_SYSTEM, _FLAT_ACTION_SCHEMA
from inumi.common.models.target import DatabaseTarget


def test_database_target_only_accepts_the_short_alias_keys():
    # Confirms the assumption the prompt fix relies on: "schema_name" is
    # rejected (extra="forbid", no populate_by_name), "schema" is accepted.
    DatabaseTarget.model_validate(
        {"environment": "development", "schema": "Person", "object": "Person"}
    )
    try:
        DatabaseTarget.model_validate(
            {"environment": "development", "schema_name": "Person", "object_name": "Person"}
        )
    except Exception:
        pass
    else:
        raise AssertionError(
            "DatabaseTarget unexpectedly accepted schema_name/object_name — "
            "if this now passes, the prompt guidance below needs updating to match."
        )


def test_action_schema_target_description_names_the_real_keys():
    target_schema = _FLAT_ACTION_SCHEMA["properties"]["target"]
    description = target_schema["description"]
    assert "schema" in description
    assert "object" in description
    # The wrong (rejected-by-DatabaseTarget) names must never appear as
    # guidance, or the model will confidently produce an invalid target.
    assert "schema_name" not in description
    assert "object_name" not in description


def test_action_system_prompt_has_a_schema_object_scoped_example():
    assert '"schema": "Person"' in _ACTION_SYSTEM
    assert '"object": "Person"' in _ACTION_SYSTEM
    assert "database.update_statistics" in _ACTION_SYSTEM
