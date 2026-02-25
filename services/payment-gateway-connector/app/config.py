"""Application configuration via environment variables.

All secrets are loaded from Vault at runtime, NEVER from environment variables.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Service configuration loaded from environment variables.

    Only infrastructure/connection settings are stored here.
    Actual secrets (API keys, credentials) are loaded from Vault at runtime.
    """

    vault_addr: str = "http://vault.orbitpay.svc.cluster.local:8200"
    vault_role: str = "payment-gateway-connector"
    vault_secret_path: str = "secret/data/orbitpay/vaultgateway"
    vault_auth_method: str = "kubernetes"
    vault_token: str = ""

    vaultgateway_url: str = (
        "http://vaultgateway-mock.orbitpay.svc.cluster.local:8080"
    )

    credential_check_interval: int = 30
    graceful_shutdown_timeout: int = 30
    service_port: int = 8000
    log_level: str = "INFO"

    api_keys: list[str] = []

    model_config = {
        "env_prefix": "",
        "case_sensitive": False,
    }


settings = Settings()
