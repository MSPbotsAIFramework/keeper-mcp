from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Non-credential configuration only.

    Credentials are never read from environment variables or config files —
    they arrive per request in the X-Keeper-Config header (see middleware.py).
    `extra="ignore"` is required so the runtime environment can inject
    unrelated variables without crashing the service (SOP §1.2).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080

    # Name of the HTTP header carrying the tenant's bound KSM configuration
    # (base64-encoded JSON produced once by redeeming a One-Time Access Token
    # outside this service). Not a secret value itself — just the header name.
    keeper_config_header: str = "X-Keeper-Config"


def get_settings() -> Settings:
    return Settings()
