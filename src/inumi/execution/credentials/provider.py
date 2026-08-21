"""CredentialProvider abstraction (spec §19).

The Execution Service is the only component that ever holds a real database
credential, and even it never receives one directly from configuration in
production — it asks a `CredentialProvider`, which is backed by a real
secrets manager. Credentials are fetched just-in-time per execution and are
never logged, cached in the Gateway/Agent, or returned in any tool result
(the redaction filter in `inumi.common.observability` is a defense-in-depth
backstop, not the primary control — the primary control is that credentials
simply never leave this module and the connection layer built on it).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import yaml
from pydantic import BaseModel, SecretStr

from inumi.common.models.failures import FailureCode, InumiError


class DatabaseCredentials(BaseModel):
    host: str
    port: int
    username: str
    password: SecretStr
    database: str
    options: dict = {}

    def __repr__(self) -> str:  # never leak the secret via logs/repr
        return f"DatabaseCredentials(host={self.host!r}, username={self.username!r}, password=***)"

    __str__ = __repr__


class CredentialProvider(ABC):
    @abstractmethod
    async def get_credentials(self, database_id: str) -> DatabaseCredentials: ...


class LocalDevCredentialProvider(CredentialProvider):
    """Development-only provider. Reads `config/dev_credentials.yaml`, which
    contains only fake, non-routable local credentials for the mock
    execution mode — never used in production (see `SECRETS_PROVIDER` env
    var and the Vault/AWS/Azure/GCP adapters below, which fail closed until
    properly configured)."""

    def __init__(self, config_path: str | Path):
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        self._entries = raw.get("credentials", {})

    async def get_credentials(self, database_id: str) -> DatabaseCredentials:
        entry = self._entries.get(database_id)
        if entry is None:
            raise InumiError(
                FailureCode.DEPENDENCY_UNAVAILABLE,
                f"No development credentials configured for '{database_id}'.",
            )
        return DatabaseCredentials(**entry)


class _UnconfiguredSecretsManagerProvider(CredentialProvider):
    """Base for real secrets-manager adapters. Fails closed with a clear,
    actionable error until the corresponding SDK/client is wired up with real
    connection details — it never silently falls back to a permissive or
    mock credential (spec §63: privileged dependencies fail closed)."""

    name = "unconfigured"

    async def get_credentials(self, database_id: str) -> DatabaseCredentials:
        raise InumiError(
            FailureCode.DEPENDENCY_UNAVAILABLE,
            f"The '{self.name}' secrets manager integration is not configured in this "
            "deployment. Refusing to execute rather than falling back to a less secure "
            "credential source.",
        )


class VaultCredentialProvider(_UnconfiguredSecretsManagerProvider):
    """HashiCorp Vault-backed provider. Production implementation reads
    `VAULT_ADDR`/`VAULT_TOKEN` (or a Kubernetes/AppRole auth method) and
    fetches a short-lived dynamic database credential per `database_id` from
    Vault's database secrets engine."""

    name = "vault"

    def __init__(self, vault_addr: str = "", vault_token: str = ""):
        self._addr = vault_addr
        self._token = vault_token


class AWSSecretsManagerCredentialProvider(_UnconfiguredSecretsManagerProvider):
    name = "aws_secrets_manager"

    def __init__(self, region: str = ""):
        self._region = region


class AzureKeyVaultCredentialProvider(_UnconfiguredSecretsManagerProvider):
    name = "azure_key_vault"

    def __init__(self, vault_url: str = ""):
        self._vault_url = vault_url


class GCPSecretManagerCredentialProvider(_UnconfiguredSecretsManagerProvider):
    name = "gcp_secret_manager"

    def __init__(self, project_id: str = ""):
        self._project_id = project_id


def build_credential_provider(settings) -> CredentialProvider:
    provider = settings.secrets_provider
    if provider == "local_dev":
        return LocalDevCredentialProvider("config/dev_credentials.yaml")
    if provider == "vault":
        return VaultCredentialProvider(settings.vault_addr, settings.vault_token)
    if provider == "aws_secrets_manager":
        return AWSSecretsManagerCredentialProvider(settings.aws_region)
    if provider == "azure_key_vault":
        return AzureKeyVaultCredentialProvider(settings.azure_key_vault_url)
    if provider == "gcp_secret_manager":
        return GCPSecretManagerCredentialProvider(settings.gcp_project_id)
    raise InumiError(
        FailureCode.DEPENDENCY_UNAVAILABLE, f"Unknown secrets provider '{provider}'."
    )
