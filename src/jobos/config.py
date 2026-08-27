"""YAML configuration for the local JobOS process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import Field, field_validator, model_validator

from jobos.models import StrictModel, validate_web_url


class AITaskSettings(StrictModel):
    """Enable or disable individual AI tasks; every task has a deterministic fallback."""

    structure_resume: bool = True
    analyse_jobs: bool = True
    rewrite_summary: bool = True
    rewrite_bullets: bool = True
    cover_letter: bool = True


class AISettings(StrictModel):
    provider: Literal["ollama", "openai", "anthropic", "gemini", "openrouter"] = "ollama"
    model: str = Field(default="llama3.2", min_length=1, max_length=200)
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = Field(default=0.1, ge=0, le=2)
    timeout_seconds: float = Field(default=90, gt=0, le=600)
    max_input_chars: int = Field(default=6_000, ge=500, le=200_000)
    max_output_tokens: int = Field(default=1_200, ge=64, le=8_192)
    max_bullet_rewrites: int = Field(default=6, ge=0, le=50)
    keep_alive: str = Field(default="10m", max_length=20)
    # Reasoning traces consume the output budget before any JSON is produced,
    # which is why this defaults to off for structured extraction tasks.
    disable_thinking: bool = True
    tasks: AITaskSettings = Field(default_factory=AITaskSettings)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return validate_web_url(value).rstrip("/")

    @property
    def api_key(self) -> str | None:
        variable = f"JOBOS_{self.provider.upper()}_API_KEY"
        return os.environ.get(variable)


class CrawlerSettings(StrictModel):
    concurrency: int = Field(default=5, ge=1, le=20)
    per_company_limit: int = Field(default=200, ge=1, le=2_000)
    timeout_seconds: float = Field(default=30, gt=0, le=180)
    use_browser_fallback: bool = True
    respect_robots_txt: bool = True


class PreferenceSettings(StrictModel):
    preferred_locations: list[str] = Field(default_factory=list, max_length=100)
    remote_only: bool = False
    minimum_match_score: int = Field(default=45, ge=0, le=100)


class CompanySettings(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4_000)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return validate_web_url(value)


class RenderSettings(StrictModel):
    """Resume presentation. Themes are validated by the renderer."""

    theme: str = Field(default="classic", min_length=1, max_length=40)


class AppConfig(StrictModel):
    data_dir: Path = Path(".jobos")
    output_dir: Path = Path("output")
    ai: AISettings = Field(default_factory=AISettings)
    crawler: CrawlerSettings = Field(default_factory=CrawlerSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    preferences: PreferenceSettings = Field(default_factory=PreferenceSettings)
    companies: dict[str, CompanySettings] = Field(default_factory=dict)
    config_path: Path = Field(default=Path("config.yaml"), exclude=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_companies(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_companies = data.get("companies")
        if raw_companies is None:
            # `companies:` with nothing under it is a natural way to say "none yet".
            result = dict(data)
            result["companies"] = {}
            return result
        if not isinstance(raw_companies, dict):
            raise ValueError("`companies` must be a mapping of key to URL or settings")
        normalized: dict[str, dict[str, Any] | CompanySettings] = {}
        for slug, value in raw_companies.items():
            display_name = str(slug).replace("_", " ").replace("-", " ").title()
            if isinstance(value, CompanySettings):
                normalized[str(slug)] = value
            elif isinstance(value, str):
                normalized[str(slug)] = {"name": display_name, "url": value}
            elif isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", display_name)
                if "careers_url" in item and "url" not in item:
                    item["url"] = item.pop("careers_url")
                normalized[str(slug)] = item
            else:
                raise ValueError(f"company {slug!r} must be a URL or mapping")
        result = dict(data)
        result["companies"] = normalized
        return result

    @model_validator(mode="after")
    def resolve_paths(self) -> Self:
        base = self.config_path.parent.resolve()
        if not self.data_dir.is_absolute():
            self.data_dir = (base / self.data_dir).resolve()
        if not self.output_dir.is_absolute():
            self.output_dir = (base / self.output_dir).resolve()
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobos.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate configuration, resolving relative paths beside the YAML file."""
    configured = path or os.environ.get("JOBOS_CONFIG") or "config.yaml"
    config_path = Path(configured).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"JobOS configuration not found at {config_path}. "
            "Copy config.example.yaml to config.yaml first."
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a YAML mapping")
    raw["config_path"] = config_path
    config = AppConfig.model_validate(raw)
    config.ensure_directories()
    return config
