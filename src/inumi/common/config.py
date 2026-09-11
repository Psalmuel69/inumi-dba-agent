"""Process-wide configuration (spec §33).

Every service imports `get_settings()` rather than reading `os.environ`
directly, so there is exactly one place that knows how configuration is
sourced. No secret ever has a literal default here beyond obviously-fake
placeholders — production values always come from the environment /
secrets manager, never from source.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Order in which a provider is auto-selected when the operator hasn't forced
# one via LLM_PROVIDER — first provider that has an API key configured wins.
LLM_PROVIDER_PREFERENCE: tuple[str, ...] = ("anthropic", "openai", "gemini", "deepseek")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    inumi_env: str = "development"
    log_level: str = "INFO"

    control_db_url: str = "sqlite+aiosqlite:///./inumi_dev.db"
    redis_url: str = "redis://localhost:6379/0"

    secrets_provider: str = "local_dev"
    vault_addr: str = ""
    vault_token: str = ""
    aws_region: str = ""
    azure_key_vault_url: str = ""
    gcp_project_id: str = ""

    identity_provider: str = "mock"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""

    policy_config_path: str = "./config/policy.yaml"
    servers_config_path: str = "./config/servers.yaml"
    identity_config_path: str = "./config/identity.yaml"
    rate_limit_config_path: str = "./config/rate_limits.yaml"
    # How often the discovery crawler refreshes each server's catalog.
    discovery_refresh_minutes: int = 60
    discovery_max_objects_per_database: int = 5000

    # --- LLM providers (spec §34) -------------------------------------------
    # A provider becomes *selectable* the moment its API key is present. The
    # DBA can then pick a specific model per conversation via `/model`
    # (unless `llm_provider_lock` forces one).
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""

    # "" / "auto" -> auto-pick the first configured provider (see
    #   LLM_PROVIDER_PREFERENCE).
    # "mock"      -> the deterministic offline planner (tests / no keys).
    # "<name>"    -> force that provider AND disable per-conversation switching.
    llm_provider: str = ""
    # Default model for the resolved provider ("" -> the provider's own
    # default; see agent.llm.registry).
    llm_model: str = ""
    # Whether the `/model` command lets a DBA switch provider/model mid-chat.
    allow_user_model_selection: bool = True

    service_jwt_secret: str = "dev-only-insecure-secret-change-me"
    service_jwt_issuer: str = "inumi-internal"

    gateway_base_url: str = "http://localhost:8001"
    gateway_port: int = 8001

    execution_base_url: str = "http://localhost:8002"
    execution_port: int = 8002
    execution_service_token_audience: str = "inumi-execution"

    agent_base_url: str = "http://localhost:8000"
    agent_port: int = 8000

    channels_port: int = 8003
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    teams_app_id: str = ""
    teams_app_password: str = ""

    enable_readonly_sql_tool: bool = False
    enable_execute_sql_tool: bool = False
    enable_restore_database_tool: bool = False
    enable_create_database_tool: bool = False
    enable_drop_database_tool: bool = False
    enable_truncate_table_tool: bool = False
    enable_bulk_delete_tool: bool = False

    # ---------------------------------------------------------------------- #

    def is_production(self) -> bool:
        return self.inumi_env == "production"

    def _llm_api_keys(self) -> dict[str, str]:
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
            "deepseek": self.deepseek_api_key,
        }

    def configured_llm_providers(self) -> list[str]:
        """Providers that have an API key set, in preference order."""
        keys = self._llm_api_keys()
        return [p for p in LLM_PROVIDER_PREFERENCE if keys.get(p, "").strip()]

    def llm_selection_locked(self) -> bool:
        return bool(self.llm_provider) and self.llm_provider not in ("auto", "mock")

    def effective_default_llm(self) -> tuple[str, str]:
        """(provider, model) used when a conversation has no explicit choice.

        Returns provider == "mock" when there is nothing real to fall back
        to — the deterministic planner — so callers never have to special-case
        "no keys configured".
        """
        if self.llm_provider == "mock":
            return "mock", "mock-planner"
        if self.llm_selection_locked():
            return self.llm_provider, self.llm_model
        configured = self.configured_llm_providers()
        if configured:
            return configured[0], self.llm_model
        return "mock", "mock-planner"

    def validate_for_production(self) -> None:
        """Fail closed at process startup rather than at request time.

        Every "mock"/"local_dev" mode in this codebase exists to make local
        development and the test suite runnable without real credentials
        (spec §32/§65-67) — none of them are safe to run in production, and
        none of the request-time code silently tightens them back up on its
        own. This is the one place that refuses to even start the process
        if `INUMI_ENV=production` is paired with any of them, rather than
        relying on an operator remembering to flip every flag correctly.
        """
        if not self.is_production():
            return

        violations: list[str] = []

        provider, _model = self.effective_default_llm()
        if provider == "mock":
            violations.append(
                "No real LLM provider is configured — set at least one of "
                "ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY "
                "(and optionally LLM_PROVIDER to force one)."
            )
        elif provider not in self.configured_llm_providers():
            violations.append(
                f"LLM_PROVIDER={provider} but no {provider.upper()}_API_KEY is set."
            )

        if self.identity_provider == "mock":
            violations.append(
                "IDENTITY_PROVIDER=mock — production must use a real enterprise "
                "identity provider (e.g. OIDC), never the config-driven mock directory."
            )
        if self.secrets_provider == "local_dev":
            violations.append(
                "SECRETS_PROVIDER=local_dev — production must use a real secrets "
                "manager (vault | aws_secrets_manager | azure_key_vault | gcp_secret_manager)."
            )
        if self.service_jwt_secret == "dev-only-insecure-secret-change-me":
            violations.append(
                "SERVICE_JWT_SECRET is still the development placeholder — set a "
                "real, unique secret for production."
            )

        if violations:
            raise RuntimeError(
                "Refusing to start with INUMI_ENV=production while running in a "
                "development/mock configuration:\n- " + "\n- ".join(violations)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
