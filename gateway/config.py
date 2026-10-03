import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Project(StrictModel):
    key_env: str
    models: list[str]
    daily_limit: int = Field(default=200, ge=1)
    max_output_tokens: int = Field(default=3500, ge=1, le=100000)


class Deployment(StrictModel):
    provider: Literal["demo", "compatible"]
    base_url: str = ""
    key_env: str = ""
    model: str
    account: str
    quota_group: str
    capabilities: list[str] = Field(default_factory=lambda: ["text"])
    billing_error_codes: list[str] = Field(default_factory=list)
    rejected_image_error_codes: list[str] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)
    demo_failure: Literal["", "billing", "unavailable"] = ""
    currency: str = "CNY"
    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    image_price: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_endpoint(self):
        if self.provider == "compatible":
            if not self.model or self.model.startswith("<") or any(ord(c) < 32 or ord(c) > 126 for c in self.model):
                raise ValueError("replace the model placeholder with an actual ASCII model ID")
            url = urlsplit(self.base_url)
            local = url.hostname in {"localhost", "127.0.0.1", "::1"}
            if not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url must be an origin/path without credentials or query")
            if url.scheme != "https" and not (url.scheme == "http" and local):
                raise ValueError("upstream HTTPS required except for localhost tests")
            if not self.key_env:
                raise ValueError("key_env required")
        reserved = {"model", "messages", "prompt", "max_tokens", "stream", "n", "response_format", "image"}
        if self.defaults.keys() & reserved:
            raise ValueError("deployment defaults cannot override protected request fields")
        return self


class Route(StrictModel):
    kind: Literal["chat", "image"] = "chat"
    candidates: list[str] = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=3)
    attempt_timeout: float = Field(default=50, gt=0, le=240)
    deadline: float = Field(default=120, gt=0, le=480)


class Settings(StrictModel):
    mode: Literal["demo", "live"] = "demo"
    text_concurrency: int = Field(default=200, ge=1, le=1000)
    image_concurrency: int = Field(default=10, ge=1, le=100)
    # Accepted for old config files only; text requests no longer queue.
    text_queue_timeout: float = Field(default=0, ge=0, le=30)
    image_queue_limit: int = Field(default=6, ge=0, le=100)
    image_queue_timeout: float = Field(default=60, gt=0, le=480)
    retry_base_seconds: float = Field(default=1, ge=0, le=10)
    cooldown_seconds: float = Field(default=60, gt=0)
    projects: dict[str, Project]
    deployments: dict[str, Deployment]
    routes: dict[str, Route]

    @model_validator(mode="after")
    def validate_references(self):
        if not self.projects or not self.routes:
            raise ValueError("at least one project and route required")
        for alias, route in self.routes.items():
            for candidate in route.candidates:
                if candidate not in self.deployments:
                    raise ValueError(f"unknown deployment: {candidate}")
                dep = self.deployments[candidate]
                if (dep.provider == "demo") != (self.mode == "demo"):
                    raise ValueError("demo and live deployments cannot be mixed")
                capability = "image" if route.kind == "image" else "text"
                if capability not in dep.capabilities:
                    raise ValueError(f"{alias}: candidate lacks {capability}")
        for project in self.projects.values():
            if not set(project.models) <= self.routes.keys():
                raise ValueError("project references unknown model alias")
        return self


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    return Settings.model_validate_json((path or ROOT / "config.json").read_text(encoding="utf-8-sig"))


def load_secrets(settings: Settings) -> dict[str, str]:
    names = {"GATEWAY_ADMIN_KEY"} | {p.key_env for p in settings.projects.values()}
    names |= {d.key_env for d in settings.deployments.values() if d.provider != "demo"}
    result = {name: os.getenv(name, "") for name in names}
    missing = [name for name, value in result.items() if not value or value.startswith("<")]
    if missing:
        raise ValueError("Missing environment variables: " + ", ".join(sorted(missing)))
    access = [result["GATEWAY_ADMIN_KEY"], *(result[p.key_env] for p in settings.projects.values())]
    if len(set(access)) != len(access) or any(len(x) < 24 for x in access):
        raise ValueError("admin/project keys must be distinct random tokens, at least 24 characters")
    return result
