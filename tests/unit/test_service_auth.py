"""Service-to-service token issue/verify (spec §31). No live HTTP here —
this is the primitive every internal call (Agent -> Gateway, Gateway ->
Execution Service) trusts to prove which service is calling."""

from __future__ import annotations

import pytest

from inumi.common.models.failures import FailureCode, InumiError
from inumi.common.service_auth import ServiceTokenIssuer, ServiceTokenVerifier


def test_issue_then_verify_round_trips_the_service_identity():
    issuer = ServiceTokenIssuer("shared-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal")

    token = issuer.issue(service_name="agent", audience="inumi-gateway")
    identity = verifier.verify(token, expected_audience="inumi-gateway")

    assert identity.service_name == "agent"
    assert identity.audience == "inumi-gateway"


def test_expired_token_is_rejected(monkeypatch):
    issuer = ServiceTokenIssuer("shared-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal", max_age_seconds=60)

    now = [1_000_000.0]
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: now[0])
    token = issuer.issue(service_name="agent", audience="inumi-gateway")

    now[0] += 61
    with pytest.raises(InumiError) as exc_info:
        verifier.verify(token, expected_audience="inumi-gateway")
    assert exc_info.value.code == FailureCode.AUTHENTICATION_FAILED
    assert "expired" in str(exc_info.value)


def test_a_token_still_within_its_max_age_is_accepted(monkeypatch):
    issuer = ServiceTokenIssuer("shared-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal", max_age_seconds=60)

    now = [1_000_000.0]
    monkeypatch.setattr("itsdangerous.timed.time.time", lambda: now[0])
    token = issuer.issue(service_name="agent", audience="inumi-gateway")

    now[0] += 59
    identity = verifier.verify(token, expected_audience="inumi-gateway")
    assert identity.service_name == "agent"


def test_a_tampered_token_is_rejected():
    issuer = ServiceTokenIssuer("shared-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal")

    token = issuer.issue(service_name="agent", audience="inumi-gateway")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(InumiError) as exc_info:
        verifier.verify(tampered, expected_audience="inumi-gateway")
    assert exc_info.value.code == FailureCode.AUTHENTICATION_FAILED
    assert "signature is invalid" in str(exc_info.value)


def test_a_token_signed_with_a_different_secret_is_rejected():
    issuer = ServiceTokenIssuer("attacker-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal")

    token = issuer.issue(service_name="agent", audience="inumi-gateway")

    with pytest.raises(InumiError) as exc_info:
        verifier.verify(token, expected_audience="inumi-gateway")
    assert exc_info.value.code == FailureCode.AUTHENTICATION_FAILED


def test_a_token_issued_for_a_different_audience_is_rejected():
    """The audience is folded into the signing salt (see the module
    docstring), so a token minted for one audience fails signature
    verification outright against another — it never reaches the payload's
    own `aud` field. Same outcome either way: the caller only sees
    AUTHENTICATION_FAILED, never which check tripped."""
    issuer = ServiceTokenIssuer("shared-secret", "inumi-internal")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal")

    token = issuer.issue(service_name="agent", audience="inumi-gateway")

    with pytest.raises(InumiError) as exc_info:
        verifier.verify(token, expected_audience="inumi-execution")
    assert exc_info.value.code == FailureCode.AUTHENTICATION_FAILED


def test_a_token_from_a_different_issuer_is_rejected_despite_a_valid_signature():
    """The one case a shared-secret + shared-audience attacker could still
    pull off: minting a structurally valid, correctly-signed token that
    just claims a different `iss`. The signature alone can't catch this —
    it only proves *a* holder of the secret signed it, not which one — so
    `verify()` must check `iss` itself against a token that otherwise
    verifies cleanly."""
    forged_issuer = ServiceTokenIssuer("shared-secret", "some-other-system")
    verifier = ServiceTokenVerifier("shared-secret", "inumi-internal")

    token = forged_issuer.issue(service_name="agent", audience="inumi-gateway")

    with pytest.raises(InumiError) as exc_info:
        verifier.verify(token, expected_audience="inumi-gateway")
    assert exc_info.value.code == FailureCode.AUTHENTICATION_FAILED
    assert "issuer/audience mismatch" in str(exc_info.value)
