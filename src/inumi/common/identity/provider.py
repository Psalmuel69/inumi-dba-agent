"""IdentityProvider abstraction (spec §3).

`IdentityProvider` is the *only* legitimate source of a `VerifiedIdentity`.
Channel adapters call `resolve_by_external_account` with the raw account id
from Slack/Teams; the Agent, Gateway, and Policy Engine never see — and
never trust — a raw channel username again after that point.

This module intentionally contains no HTTP calls to any real IdP. The
production implementation (OIDC/AAD) lives in each service's infrastructure
layer and satisfies this same interface; only the interface and the
development/test mock live here, in the shared package, since every service
needs to be able to construct one.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from inumi.common.models.identity import DBARole, VerifiedIdentity


class IdentityProvider(ABC):
    """Resolves a channel account into a verified enterprise identity.

    Implementations MUST NOT accept a display name, a free-text username, or
    any claim asserted by the LLM as input — only a channel-verified account
    identifier (e.g. a Slack user id from a signature-verified webhook, or an
    AAD object id from a verified Teams bot token).
    """

    @abstractmethod
    async def resolve_by_external_account(
        self, channel: str, external_account_id: str
    ) -> VerifiedIdentity | None:
        """Return the verified identity for this channel account, or None."""

    @abstractmethod
    async def refresh(self, subject_id: str) -> VerifiedIdentity | None:
        """Re-resolve an identity by subject id (used to re-check group
        membership / MFA state at authorization time, rather than trusting a
        cached identity indefinitely)."""


class _DirectoryEntry:
    __slots__ = ("subject_id", "email", "display_name", "channel_accounts", "groups", "mfa")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.subject_id: str = raw["subject_id"]
        self.email: str = raw["email"]
        self.display_name: str = raw["display_name"]
        self.channel_accounts: dict[str, str] = raw.get("channel_accounts", {})
        self.groups: list[str] = raw.get("groups", [])
        self.mfa: bool = raw.get("mfa_satisfied", False)


class MockIdentityProvider(IdentityProvider):
    """Config-driven identity provider for local development and tests.

    Loads `config/identity.yaml`, builds group -> role mappings, and resolves
    channel accounts against a fictitious directory declared in that same
    file. Never used in production — production wires a real OIDC provider
    satisfying the same `IdentityProvider` interface instead.
    """

    def __init__(self, config_path: str | Path):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        identity_cfg = raw["identity"]
        self._dba_team_groups: set[str] = set(identity_cfg["groups"]["dba_team"])
        self._role_group_map: dict[DBARole, set[str]] = {
            DBARole(role_name): set(role_cfg["groups"])
            for role_name, role_cfg in identity_cfg["roles"].items()
        }
        self._directory: list[_DirectoryEntry] = [
            _DirectoryEntry(entry) for entry in raw.get("mock_directory", [])
        ]

    def _derive_roles(self, groups: list[str]) -> list[DBARole]:
        group_set = set(groups)
        if not (group_set & self._dba_team_groups):
            return []
        return [
            role
            for role, required_groups in self._role_group_map.items()
            if group_set & required_groups
        ]

    def _to_identity(self, entry: _DirectoryEntry) -> VerifiedIdentity:
        return VerifiedIdentity(
            subject_id=entry.subject_id,
            email=entry.email,
            display_name=entry.display_name,
            enterprise_groups=list(entry.groups),
            dba_roles=self._derive_roles(entry.groups),
            mfa_satisfied=entry.mfa,
            authenticated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )

    async def resolve_by_external_account(
        self, channel: str, external_account_id: str
    ) -> VerifiedIdentity | None:
        for entry in self._directory:
            if entry.channel_accounts.get(channel) == external_account_id:
                return self._to_identity(entry)
        return None

    async def refresh(self, subject_id: str) -> VerifiedIdentity | None:
        for entry in self._directory:
            if entry.subject_id == subject_id:
                return self._to_identity(entry)
        return None
