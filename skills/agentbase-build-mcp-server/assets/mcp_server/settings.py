from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK = "127.0.0.1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="MCP_", env_ignore_empty=True
    )

    # Fail closed: with no env at all, MCP_AUTH_MODE=none is refused (prod). Local dev sets
    # MCP_APP_ENV=local in .env (`cp .env.example .env`); the Dockerfile also sets MCP_APP_ENV=prod.
    app_env: Literal["local", "dev", "staging", "prod"] = "prod"
    server_name: str = "__SERVER_NAME__"
    # Public URL of the MCP endpoint (resource identifier, RFC 9728/8707), e.g.
    # https://endpoint-<id>.agentbase-runtime.aiplatform.vngcloud.vn/mcp
    resource_url: str = "http://localhost:8080/mcp"
    # Bind address. Empty ⇒ 127.0.0.1 when auth_mode=none (unauthenticated server never listens on
    # the network), else 0.0.0.0 (AgentBase Runtime). Override only for local Docker (`make docker-run`).
    host: str | None = None
    port: int = 8080  # AgentBase Runtime expects 8080

    # api_key (Gateway outbound API Key) | jwt (OAuth 2LO/3LO, inbound forward) | none (local only)
    auth_mode: Literal["api_key", "jwt", "none"] = "none"
    dev_user: str = "dev-user"  # auth_mode=none only: identity returned by current_user()
    api_key_sha256: list[str] = Field(default_factory=list)
    # Scopes granted to API-key callers (on top of required_scopes), e.g. ["catalog.read"]
    api_key_scopes: list[str] = Field(default_factory=list)
    issuer: str = ""  # OAuth authorization server (iss) — required for jwt
    jwks_url: str = ""
    audience: str = ""  # required for jwt: API identifier at the IdP (ideally = resource_url)
    user_claim: str = "sub"
    jwt_algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    required_scopes: list[str] = Field(default_factory=list)  # applied to every request

    # Data & internal system used by the tools (see store.py, backend.py)
    db_path: str = "mcp.db"  # local SQLite file; not shared across replicas
    backend_url: str = ""  # base URL of the internal system, e.g. https://catalog.internal/api
    # Service credential for the internal system (runtime env / secret store, never committed)
    backend_token: str = ""
    backend_timeout_s: float = 10.0

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.auth_mode == "none" and self.app_env != "local":
            raise ValueError(
                f"MCP_AUTH_MODE=none is only allowed when MCP_APP_ENV=local (got {self.app_env!r}):"
                " set MCP_AUTH_MODE=api_key|jwt, or for local dev `cp .env.example .env`"
            )
        if self.auth_mode == "api_key" and not self.api_key_sha256:
            raise ValueError(
                "MCP_AUTH_MODE=api_key requires MCP_API_KEY_SHA256 (JSON list of SHA-256 hex)"
            )
        if self.auth_mode == "jwt" and not (self.jwks_url and self.issuer and self.audience):
            raise ValueError(
                "MCP_AUTH_MODE=jwt requires MCP_JWKS_URL, MCP_ISSUER and MCP_AUDIENCE"
                " (tokens minted for another API must be rejected — RFC 8707)"
            )
        if self.host is None:
            self.host = LOOPBACK if self.auth_mode == "none" else "0.0.0.0"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
