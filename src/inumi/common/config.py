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
    inventory_config_path: str = "./config/inventory.yaml"
    identity_config_path: str = "./config/identity.yaml"
    rate_limit_config_path: str = "./config/rate_limits.yaml"

    llm_provider: str = "mock"
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"

    service_jwt_secret: str = "dev-only-insecure-secret-change-me"
    service_jwt_issuer: str = "inumi-internal"

    gateway_base_url: str = "http://localhost:8001"
    gateway_port: int = 8001

    execution_base_url: str = "http://localhost:8002"
    execution_port: int = 8002
    execution_service_token_audience: str = "inumi-execution"
    # "mock" (default, no real DB required) or "real" (uses live connections
    # via CredentialProvider + pyodbc/psycopg — requires the `db-drivers` extra).
    execution_mode: str = "mock"

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

    def is_production(self) -> bool:
        return self.inumi_env == "production"

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
        if self.llm_provider == "mock":
            violations.append(
                "LLM_PROVIDER=mock — production must use a real provider "
                "(e.g. LLM_PROVIDER=anthropic with ANTHROPIC_API_KEY set)."
            )
        if self.execution_mode == "mock":
            violations.append(
                "EXECUTION_MODE=mock — production must use EXECUTION_MODE=real "
                "with the `db-drivers` extra installed."
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
