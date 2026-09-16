"""Alert-triggered investigation (`agent.alert_trigger`) — the event-driven
sibling of `agent.scheduled_report`'s clock-driven digest. Tested the same
way that file's tests are: `AlertTriggerRunner.handle_alert` called directly
with a fake orchestrator/publisher, no HTTP, no webhook signature, no LLM.
See `test_scheduled_digest_never_writes.py` for the read-only/never-executes
guarantee itself, which both unattended entry points share via
`orchestrator._continue_investigation`.
"""

from __future__ import annotations

import pytest

from inumi.agent.alert_trigger import (
    AlertPayload,
    AlertTriggerRunner,
    build_problem_statement,
    resolve_server,
)
from inumi.agent.orchestrator import ScheduledSummary
from inumi.common.config import Settings

# --------------------------------------------------------------- resolve_server ---

_SERVERS = [
    {"id": "sqlserver-dev-02", "environment": "development", "aliases": ["dev-sql-02", "winsql"]},
    {"id": "postgres-dev-02", "environment": "development", "aliases": ["dev-postgres-02", "winpg"]},
    {"id": "sqlserver-uat-01", "environment": "uat", "aliases": ["uat-sql"]},
]


def test_resolve_server_matches_the_exact_id():
    assert resolve_server("postgres-dev-02", _SERVERS)["id"] == "postgres-dev-02"


def test_resolve_server_matches_an_alias():
    assert resolve_server("winsql", _SERVERS)["id"] == "sqlserver-dev-02"


def test_resolve_server_matches_case_and_punctuation_insensitively():
    assert resolve_server("WinPG", _SERVERS)["id"] == "postgres-dev-02"
    assert resolve_server("sql-server-dev-02", _SERVERS)["id"] == "sqlserver-dev-02"


def test_resolve_server_returns_none_for_no_match():
    """Fails closed, never guesses — nobody is present to correct a wrong
    match the way a DBA would notice mid-conversation."""
    assert resolve_server("does-not-exist", _SERVERS) is None


def test_resolve_server_returns_none_for_an_ambiguous_match():
    servers = [
        {"id": "sqlserver-dev-01", "aliases": ["shared-alias"]},
        {"id": "sqlserver-dev-02", "aliases": ["shared-alias"]},
    ]
    assert resolve_server("shared-alias", servers) is None


# ----------------------------------------------------------- build_problem_statement ---


def test_problem_statement_includes_alert_details():
    alert = AlertPayload(
        server="winpg",
        metric="replication_lag_seconds",
        current_value="340",
        threshold="120",
        severity="critical",
        source="datadog",
        message="Replica lag exceeded threshold for 10 minutes",
    )
    text = build_problem_statement(alert)
    assert "replication_lag_seconds" in text
    assert "340" in text and "120" in text
    assert "critical" in text
    assert "datadog" in text
    assert "Replica lag exceeded" in text


def test_problem_statement_never_directs_an_action_or_asks_a_question():
    """Layer zero of the read-only guarantee — mirrors
    `orchestrator._SCHEDULED_SUMMARY_PROBLEM`'s own contract."""
    text = build_problem_statement(AlertPayload(server="winpg"))
    assert "nothing you propose will be executed" in text
    assert "Do not ask a clarifying question" in text


def test_problem_statement_degrades_gracefully_with_no_detail_at_all():
    text = build_problem_statement(AlertPayload(server="winpg"))
    assert "{server_id}" in text  # still a valid format template
    assert "no further detail" in text


# ------------------------------------------------------------- AlertTriggerRunner ---


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, *, channel_id: str, text: str) -> None:
        self.published.append((channel_id, text))


class _FakeOrchestrator:
    def __init__(self, *, servers, summary=None, raises=None, registry_error=None):
        self._servers = servers
        self._summary = summary
        self._raises = raises
        self._registry_error = registry_error
        self.calls: list[dict] = []

    async def list_registered_servers(self):
        if self._registry_error is not None:
            raise self._registry_error
        return self._servers

    async def run_triggered_investigation(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._summary


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _ok_summary(**overrides) -> ScheduledSummary:
    defaults = dict(
        server_id="postgres-dev-02",
        environment="development",
        investigation_id="inv_1",
        status="ok",
        text="Replication lag is within normal bounds now; the earlier spike self-resolved.",
    )
    defaults.update(overrides)
    return ScheduledSummary(**defaults)


@pytest.mark.asyncio
async def test_happy_path_resolves_investigates_and_publishes():
    servers = [{"id": "postgres-dev-02", "environment": "development", "aliases": ["winpg"]}]
    orchestrator = _FakeOrchestrator(servers=servers, summary=_ok_summary())
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(
            alert_webhook_slack_channel="C_ALERTS",
            alert_webhook_identity_channel="dev",
            alert_webhook_identity_account="dba_l2@example.com",
        ),
        publisher=publisher,
    )

    outcome = await runner.handle_alert(AlertPayload(server="winpg", metric="replication_lag_seconds"))

    assert outcome.ok
    assert orchestrator.calls[0]["server_id"] == "postgres-dev-02"
    assert orchestrator.calls[0]["environment"] == "development"
    assert orchestrator.calls[0]["channel"] == "dev"
    assert orchestrator.calls[0]["channel_account_id"] == "dba_l2@example.com"
    assert len(publisher.published) == 1
    channel_id, text = publisher.published[0]
    assert channel_id == "C_ALERTS"
    assert "postgres-dev-02" in text
    assert "Replication lag is within normal bounds" in text


@pytest.mark.asyncio
async def test_unknown_server_never_reaches_the_orchestrator():
    """No wasted LLM call, and no way for a garbage/attacker-controlled
    `server` field to reach the investigation pipeline at all."""
    orchestrator = _FakeOrchestrator(servers=[{"id": "postgres-dev-02", "environment": "development"}])
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(alert_webhook_slack_channel="C_ALERTS"),
        publisher=publisher,
    )

    outcome = await runner.handle_alert(AlertPayload(server="totally-unregistered"))

    assert not outcome.ok
    assert "does not match" in outcome.error
    assert orchestrator.calls == []
    assert publisher.published == []


@pytest.mark.asyncio
async def test_disabled_without_a_channel_touches_nothing():
    orchestrator = _FakeOrchestrator(servers=[{"id": "postgres-dev-02", "environment": "development"}])
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator, settings=_settings(), publisher=publisher
    )

    outcome = await runner.handle_alert(AlertPayload(server="postgres-dev-02"))

    assert not outcome.ok
    assert orchestrator.calls == []
    assert publisher.published == []


@pytest.mark.asyncio
async def test_unreachable_server_registry_reports_plainly_without_raising():
    orchestrator = _FakeOrchestrator(servers=[], registry_error=RuntimeError("gateway down"))
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(alert_webhook_slack_channel="C_ALERTS"),
        publisher=publisher,
    )

    outcome = await runner.handle_alert(AlertPayload(server="postgres-dev-02"))

    assert not outcome.ok
    assert "gateway down" not in outcome.error  # no-raw-error invariant
    assert publisher.published == []


@pytest.mark.asyncio
async def test_a_publish_failure_never_escapes():
    class _BrokenPublisher:
        async def publish(self, *, channel_id: str, text: str) -> None:
            raise RuntimeError("slack is down")

    servers = [{"id": "postgres-dev-02", "environment": "development"}]
    orchestrator = _FakeOrchestrator(servers=servers, summary=_ok_summary())
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(alert_webhook_slack_channel="C_ALERTS"),
        publisher=_BrokenPublisher(),
    )

    outcome = await runner.handle_alert(AlertPayload(server="postgres-dev-02"))  # must not raise

    assert not outcome.ok


@pytest.mark.asyncio
async def test_a_dropped_write_proposal_is_visible_in_the_posted_text():
    """Mirrors the digest's own `_server_block`: the model wanting to act
    and being structurally stopped is information worth surfacing, not an
    implementation detail to hide."""
    servers = [{"id": "postgres-dev-02", "environment": "development"}]
    summary = _ok_summary(dropped_proposals=("database.kill_session",))
    orchestrator = _FakeOrchestrator(servers=servers, summary=summary)
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(alert_webhook_slack_channel="C_ALERTS"),
        publisher=publisher,
    )

    await runner.handle_alert(AlertPayload(server="postgres-dev-02"))

    _, text = publisher.published[0]
    assert "database.kill_session" in text
    assert "never executes an action" in text


@pytest.mark.asyncio
async def test_an_incomplete_investigation_is_reported_not_printed_as_clean():
    servers = [{"id": "postgres-dev-02", "environment": "development"}]
    summary = _ok_summary(status="error", error="no diagnostic call succeeded")
    orchestrator = _FakeOrchestrator(servers=servers, summary=summary)
    publisher = _RecordingPublisher()
    runner = AlertTriggerRunner(
        orchestrator=orchestrator,
        settings=_settings(alert_webhook_slack_channel="C_ALERTS"),
        publisher=publisher,
    )

    outcome = await runner.handle_alert(AlertPayload(server="postgres-dev-02"))

    assert outcome.ok  # delivery itself succeeded
    _, text = publisher.published[0]
    assert "Could not complete the investigation" in text
    assert "no diagnostic call succeeded" in text
