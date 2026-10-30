"""Configuration, with separate upstream and inbound credentials."""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Configuration failure whose message contains no supplied values."""


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise ValueError("Expected an HTTP(S) URL without credentials, query or fragment")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    jumpserver_url: str = ""
    swagger_url: str = ""
    api_token: SecretStr = SecretStr("")
    auth_scheme: str = "Bearer"
    api_key: SecretStr = SecretStr("")
    jms_org_id: str = ""
    server_host: str = "127.0.0.1"
    server_port: int = Field(default=8099, ge=1, le=65535)
    base_path: str = "/sse"
    allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*"
    allowed_origins: str = "http://127.0.0.1:*,http://localhost:*,http://[::1]:*"
    allow_unauthenticated: bool = False
    tls_verify: bool = True
    ca_bundle: str = ""
    trust_env: bool = False
    request_timeout: float = Field(default=30, gt=0, le=300)
    max_concurrency: int = Field(default=8, ge=1, le=64)
    max_schema_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    max_response_bytes: int = Field(default=1024 * 1024, ge=1024)
    read_only: bool = True
    include_paths: str = "/api/v1/assets/*"
    include_operations: str = ""
    exclude_operations: str = ""
    include_tags: str = ""
    max_tools: int = Field(default=100, ge=1, le=500)
    log_level: str = "INFO"

    def allowed_hosts_list(self) -> list[str]:
        return self.expand_ports(self.allowed_hosts)

    def allowed_origins_list(self) -> list[str]:
        return self.expand_ports(self.allowed_origins)

    @staticmethod
    def expand_ports(value: str) -> list[str]:
        values = [v.strip() for v in value.split(",") if v.strip()]
        return list(dict.fromkeys([*values, *(v[:-2] for v in values if v.endswith(":*"))]))

    @field_validator("jumpserver_url", "swagger_url")
    @classmethod
    def url_valid(cls, value: str) -> str:
        return validate_url(value) if value else value

    @field_validator("log_level")
    @classmethod
    def log_level_valid(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("log_level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return value

    @field_validator("base_path")
    @classmethod
    def base_path_valid(cls, value: str) -> str:
        value = value.rstrip("/")
        if value in {"", "/mcp", "/healthz", "/readyz"} or not value.startswith("/"):
            raise ValueError("base_path must be an absolute path distinct from reserved routes")
        if any(c in value for c in "?#{}%") or ".." in value or "//" in value:
            raise ValueError("Invalid SSE base_path")
        return value

    def authorization(self) -> str:
        token = self.api_token.get_secret_value().strip()
        if token.startswith(('"', "'")) or token.endswith(('"', "'")):
            raise ConfigurationError(
                "api_token contains quote characters; remove quotes when using Docker --env-file"
            )
        if not token or "\r" in token or "\n" in token:
            raise ConfigurationError("api_token must contain a non-empty upstream credential")
        parts = token.split()
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
            return f"{parts[0].title()} {parts[1]}"
        if len(parts) != 1 or self.auth_scheme.lower() not in {"bearer", "token"}:
            raise ConfigurationError(
                "api_token must use Bearer/Token plus one token, or a bare token with auth_scheme"
            )
        return f"{self.auth_scheme.title()} {token}"

    def validate_runtime(self) -> None:
        if not self.jumpserver_url:
            raise ConfigurationError("jumpserver_url is required")
        if urlsplit(self.jumpserver_url).path.rstrip("/").endswith("/api/v1"):
            raise ConfigurationError("jumpserver_url must be the host root, without /api/v1")
        if self.swagger_url:
            root, swagger = urlsplit(self.jumpserver_url), urlsplit(self.swagger_url)
            if (root.scheme, root.netloc) != (swagger.scheme, swagger.netloc):
                raise ConfigurationError("swagger_url must use the same origin as jumpserver_url")
        self.authorization()
        if not self.api_key.get_secret_value():
            if not self.allow_unauthenticated or self.server_host not in {"127.0.0.1", "::1"}:
                raise ConfigurationError(
                    "api_key is required; unauthenticated mode is loopback-only"
                )
        key = self.api_key.get_secret_value()
        if key.startswith(('"', "'")) or key.endswith(('"', "'")):
            raise ConfigurationError(
                "api_key contains quote characters; remove quotes when using Docker --env-file"
            )
        if any(c in self.api_key.get_secret_value() for c in " \t\r\n"):
            raise ConfigurationError("api_key must be a token without whitespace")
