"""YAML configuration for the local JobOS process."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
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
    """What counts as a job that fits you, applied together by `search --preferred`.

    Title include/exclude, location and skills work on any job. The years window only
    constrains jobs that have been analysed (the requirement is extracted during
    analysis); unanalysed jobs are not dropped by it, so a pre-analysis shortlist is
    never empty.
    """

    # A job's location must contain one of these. Matching any entry means both
    # spellings of a city can be listed, so "Bengaluru" and "Bangalore" are covered by
    # one search. Ignored when an explicit location is passed, which has already
    # narrowed the query. Empty = any location.
    preferred_locations: list[str] = Field(default_factory=list, max_length=100)
    remote_only: bool = False
    minimum_match_score: int = Field(default=45, ge=0, le=100)
    # Title must contain at least one of these (case-insensitive). Empty = allow any.
    roles_include: list[str] = Field(default_factory=list, max_length=100)
    # Title must contain none of these (case-insensitive).
    roles_exclude: list[str] = Field(default_factory=list, max_length=100)
    # Tech-stack filters. Unlike the title filters above these read the posting's
    # skills, title, description and experience text, matched on whole tokens, so
    # excluding "java" never excludes "JavaScript" and known aliases fold together
    # ("k8s" counts as kubernetes, "reactjs" as react). Terms outside the known
    # vocabulary work too, matched literally, e.g. "full stack".
    # The job must mention at least one of these. Empty = allow any stack.
    skills_include: list[str] = Field(default_factory=list, max_length=100)
    # The job must mention none of these; exclusion wins over inclusion.
    skills_exclude: list[str] = Field(default_factory=list, max_length=100)
    # Required-years window for analysed jobs. None = no bound on that side.
    min_years: float | None = Field(default=None, ge=0, le=60)
    max_years: float | None = Field(default=None, ge=0, le=60)
    # Optional exact seniority match for analysed jobs. Empty = any.
    seniority: str = Field(default="", max_length=100)
    # Many employers cap how many roles one candidate may apply to. The per-company
    # report uses this to show only that many best-fit slots, so the quota is spent
    # on the strongest matches instead of the first ones seen.
    max_applications_per_company: int = Field(default=3, ge=1, le=50)


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

    @property
    def config_history_dir(self) -> Path:
        """Where timestamped config.yaml backups are kept before each write."""
        return self.data_dir / "config_history"

    @property
    def env_path(self) -> Path:
        """Local .env for API keys, kept in the gitignored data dir, never committed."""
        return self.data_dir / ".env"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


_SLUG_SAFE = re.compile(r"[^a-z0-9]+")
_ADDED_MARKER = "# --- added from the dashboard / add-company ---"


def slugify_company(name: str) -> str:
    """Turn a display name into a config key: lowercase, alphanumeric, underscores."""
    slug = _SLUG_SAFE.sub("_", name.strip().casefold()).strip("_")
    return slug or "company"


def append_company(config_path: Path, slug: str, name: str, url: str) -> None:
    """Add a company under `companies:` by editing the YAML text in place.

    The file is rewritten as text rather than dumped from parsed YAML because a
    round-trip through PyYAML would discard every comment, and this config carries
    substantial commentary (per-board role counts, why boards are excluded) that is
    the main reason it is readable. The new entry is inserted after the last line
    belonging to the `companies:` block.

    Raises ValueError if the file has no `companies:` mapping or the slug is taken.
    """
    validate_web_url(url)
    original = config_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(r"^companies:\s*(#.*)?$", line)), None
    )
    if start is None:
        raise ValueError(
            "could not find a `companies:` block in the config, so the entry was not added"
        )
    if re.search(rf"^\s+{re.escape(slug)}\s*:", original, re.MULTILINE):
        raise ValueError(f"{slug!r} is already in the config")

    # The block runs until the first non-blank line that is not indented. An
    # unindented comment ends it too: it introduces whatever follows rather than
    # belonging to the companies mapping.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith((" ", "\t")):
            end = i
            break

    entry = f"  {slug}: {url}"
    if name != slug.replace("_", " ").title():
        # A custom display name needs the mapping form; a derived one does not, since
        # the loader title-cases the slug to the same thing.
        entry = f"  {slug}:\n    name: {name}\n    url: {url}"

    # Added companies collect under one marker at the end of the block. Appending
    # after the last entry instead would drop, say, an Ashby board into the middle of
    # the Workday section, and appending after the block's trailing prose would make
    # the new line look like part of that comment.
    marker_index = next(
        (i for i in range(start + 1, end) if lines[i].strip() == _ADDED_MARKER), None
    )
    if marker_index is None:
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1  # keep any blank separator below the new section
        lines[insert_at:insert_at] = ["", f"  {_ADDED_MARKER}", entry]
    else:
        insert_at = marker_index + 1
        while insert_at < end and lines[insert_at].startswith((" ", "\t")) and lines[insert_at].strip():
            insert_at += 1
        lines.insert(insert_at, entry)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    load_env_file(config.env_path)
    return config


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (without overriding).

    Only JOBOS_* keys are applied, and a value already present in the real
    environment wins — so an explicit shell export always takes precedence over the
    file. This lets the AI page persist an API key to .jobos/.env and have it take
    effect on the next start, without ever touching config.yaml.
    """
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("JOBOS_") and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        return


def replace_top_level_block(text: str, key: str, new_block: str) -> str:
    """Replace one top-level YAML block (e.g. ``ai:``) in place, preserving the rest.

    The rest of the file — including hand-written comments elsewhere, like the
    companies annotations — is left byte-for-byte untouched. This exists because
    re-dumping the whole document through ``yaml.safe_dump`` would strip every
    comment. ``new_block`` must be the full replacement including the ``key:`` line.

    Raises ValueError if the key's block is not found at the top level.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(rf"^{re.escape(key)}:\s*(#.*)?$", line)),
        None,
    )
    if start is None:
        raise ValueError(f"could not find a top-level `{key}:` block to replace")
    # The block runs until the next line that starts at column 0 and is not blank
    # (i.e. the next top-level key or comment introducing a new section).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line and not line[0].isspace():
            end = i
            break
    new_lines = new_block.rstrip("\n").splitlines()
    rebuilt = lines[:start] + new_lines + lines[end:]
    return "\n".join(rebuilt) + "\n"


def validate_config_text(text: str, config_path: Path) -> AppConfig:
    """Parse and fully validate candidate config YAML without writing anything.

    Used before a UI/API save so a broken edit is rejected with a precise message
    instead of being written and breaking the next startup. Raises ValueError on any
    YAML or schema problem.
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a YAML mapping")
    raw["config_path"] = config_path
    return AppConfig.model_validate(raw)


def write_config_text(config_path: Path, text: str, *, history_dir: Path) -> Path:
    """Validate, back up the previous file, then atomically overwrite the config.

    The order matters: validation first (a bad edit never reaches disk), then a
    timestamped snapshot of the current file into ``history_dir`` (so an agent or a
    user overwrite is always recoverable), then an atomic temp-write-and-rename so a
    crash mid-write cannot leave a half-written config. Returns the backup path, or
    the config path itself when there was no prior file to snapshot.
    """
    validate_config_text(text, config_path)
    backup_path = config_path
    if config_path.exists():
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = history_dir / f"config-{stamp}.yaml"
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    # Atomic replace: write a sibling temp file, then rename over the target.
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config_path)
    return backup_path


def list_config_backups(history_dir: Path, limit: int = 50) -> list[Path]:
    """Return timestamped config snapshots, newest first, for a restore UI."""
    if not history_dir.is_dir():
        return []
    backups = sorted(history_dir.glob("config-*.yaml"), reverse=True)
    return backups[:limit]
