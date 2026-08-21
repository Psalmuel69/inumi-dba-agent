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


@lru_cache
def get_settings() -> Settings:
    return Settings()
