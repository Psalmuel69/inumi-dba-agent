"""Playbook library correctness: every step must name a real, currently
registered read-only tool (never a write, never an invented id — a typo
here would silently degrade to "no tool ever runs" or, worse, "the wrong
tool runs"), and the keyword matcher must pick the intended playbook (and
only that one) for representative real-world phrasings, while leaving
ordinary non-matching problem text to the existing freeform path (None)."""

from __future__ import annotations

from inumi.agent.playbooks.library import PLAYBOOKS, get_playbook, match_playbook
from inumi.common.config import Settings
from inumi.gateway.domain.tool_catalog import build_tool_catalog

# The real, currently registered read-only tool ids (spec §8) — a playbook
# step naming anything outside this set (or a write tool) could never
# actually run, or worse, could silently propose a write with no DBA in the
# loop to review the exact call first.
_REAL_READ_TOOL_IDS = {
    t.tool_id
    for t in build_tool_catalog(Settings(_env_file=None))
    if not t.data_modification
}


def test_every_playbook_step_names_a_real_read_only_tool():
    assert _REAL_READ_TOOL_IDS, "sanity check: the real catalog must be non-empty"
    for playbook in PLAYBOOKS:
        assert playbook.steps, f"{playbook.playbook_id} has no steps"
        for step in playbook.steps:
            assert step.tool_id in _REAL_READ_TOOL_IDS, (
                f"{playbook.playbook_id} step names {step.tool_id!r}, which is not a "
                "real, currently-registered read-only tool"
            )


def test_every_playbook_id_is_unique():
    ids = [p.playbook_id for p in PLAYBOOKS]
    assert len(ids) == len(set(ids))


def test_get_playbook_round_trips_every_registered_id():
    for playbook in PLAYBOOKS:
        assert get_playbook(playbook.playbook_id) is playbook


def test_get_playbook_returns_none_for_an_unknown_or_missing_id():
    assert get_playbook(None) is None
    assert get_playbook("not-a-real-playbook") is None


def test_matcher_picks_the_intended_playbook_for_real_world_phrasings():
    cases = {
        "Why is CoreBanking so slow today?": "slow_queries",
        "queries are timing out on production": "slow_queries",
        "CPU usage on sqlserver-prod-01 is spiking": "high_cpu",
        "we're seeing high memory usage on the replica": "high_memory",
        "sessions are blocked and nothing is completing": "blocking",
        "we just hit a deadlock in CoreBanking": "deadlocks",
        "getting connection refused, too many connections": "connections",
        "replication lag on the standby is growing": "replication",
        "did last night's backup job fail?": "backups",
        "the transaction log is full on CoreBanking": "storage",
        "seeing a lot of errors in the error log": "errors",
        "can you do a health check on CoreBanking": "general_health",
        "can you tune this instance for us?": "configuration_review",
        "we'd like a configuration review of postgres-local": "configuration_review",
        "can you recommend settings for this server": "configuration_review",
        "are our settings okay on the prod instance": "configuration_review",
    }
    for text, expected_id in cases.items():
        playbook = match_playbook(text)
        assert playbook is not None, f"expected a match for {text!r}"
        assert playbook.playbook_id == expected_id, (
            f"{text!r} matched {playbook.playbook_id!r}, expected {expected_id!r}"
        )


def test_deadlock_matches_before_the_broader_blocking_playbook():
    """'deadlock' contains the substring 'lock' — a naive substring matcher
    would hit the blocking playbook's 'lock'-ish triggers first if it came
    first in the list, or word-boundary matching could still misfire if
    implemented carelessly. Pins the intended, more-specific match."""
    playbook = match_playbook("we had a deadlock on the orders table")
    assert playbook is not None
    assert playbook.playbook_id == "deadlocks"


def test_word_boundary_matching_does_not_false_positive_on_substrings():
    # "login" must not trigger the connections playbook via a naive "log"
    # substring match, and "backlog" must not trigger the backups playbook
    # via a naive "back" substring match.
    assert match_playbook("users are failing to login to the app") is None
    assert match_playbook("there's a backlog of jobs to process") is None


def test_unmatched_problem_text_falls_back_to_freeform():
    assert match_playbook("please list the tables in CoreBanking") is None
    assert match_playbook("") is None
    assert match_playbook("   ") is None


def test_no_playbook_step_ever_proposes_a_write_tool():
    write_tool_ids = {
        t.tool_id for t in build_tool_catalog(Settings(_env_file=None)) if t.data_modification
    }
    for playbook in PLAYBOOKS:
        for step in playbook.steps:
            assert step.tool_id not in write_tool_ids


# --- configuration_review (new playbook, added after comparing against ---
# --- Xata's shipped playbook prompts — see library.py's PLAYBOOKS tuple) ---


def test_configuration_review_playbook_uses_only_configuration_and_health():
    playbook = get_playbook("configuration_review")
    assert playbook is not None
    assert [step.tool_id for step in playbook.steps] == [
        "database.get_configuration",
        "database.get_health",
    ]


def test_configuration_review_matches_tune_and_tuning_but_not_unrelated_words():
    # "tune" and "tuning" are both registered triggers, but neither is a
    # substring false-positive off something like "attune" or "fortune".
    assert match_playbook("can you tune this instance for us?") is not None
    assert match_playbook("can you tune this instance for us?").playbook_id == "configuration_review"
    assert match_playbook("we need some tuning done on postgres-local") is not None
    assert match_playbook("we need some tuning done on postgres-local").playbook_id == (
        "configuration_review"
    )
    assert match_playbook("the orchestra needs to attune its instruments") is None
    assert match_playbook("we lost a fortune on that deal") is None


def test_configuration_review_does_not_shadow_or_get_shadowed_by_connections_or_general_health():
    # "configuration review"/"tune"/"tuning" must not accidentally match the
    # connections playbook's "connection"/"configuration"-adjacent wording,
    # and existing connections/general-health phrasing must not accidentally
    # route into configuration_review.
    assert match_playbook("too many connections to the database").playbook_id == "connections"
    assert match_playbook("can you do a health check on CoreBanking").playbook_id == "general_health"
    assert match_playbook("can you recommend settings for this server").playbook_id == (
        "configuration_review"
    )
    assert match_playbook("we'd like a configuration review of postgres-local").playbook_id == (
        "configuration_review"
    )


def test_slow_queries_and_high_cpu_guidance_warns_against_introspection_queries():
    slow_queries = get_playbook("slow_queries")
    high_cpu = get_playbook("high_cpu")
    for playbook in (slow_queries, high_cpu):
        assert playbook is not None
        guidance = playbook.conclusion_guidance
        assert "pg_catalog" in guidance
        assert "information_schema" in guidance
        assert "performance_schema" in guidance
        assert "INFORMATION_SCHEMA" in guidance or "sys.*" in guidance


def test_connections_guidance_frames_a_healthy_count_as_no_alarm():
    connections = get_playbook("connections")
    assert connections is not None
    guidance = connections.conclusion_guidance
    assert "false alarm" in guidance
    assert "comfortable headroom" in guidance or "well under" in guidance


def test_configuration_review_guidance_scopes_itself_honestly_and_names_engine_params():
    playbook = get_playbook("configuration_review")
    assert playbook is not None
    guidance = playbook.conclusion_guidance
    description = playbook.description
    # Must not overclaim true capacity-based sizing (no instance-class /
    # hardware data exists anywhere in Inumi's server registry).
    assert "capacity" in guidance
    assert "hardware" in guidance or "instance-class" in guidance
    # Must name real, engine-specific parameters, adapted per engine.
    assert "shared_buffers" in guidance
    assert "max_connections" in guidance
    assert "max degree of parallelism" in guidance
    assert "innodb_buffer_pool_size" in guidance
    # The scoping limitation must also be stated in the description, not just
    # buried in the conclusion guidance.
    assert "instance-class" in description or "hardware" in description
