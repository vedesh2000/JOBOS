"""Load candidate company boards for validation, from the seed or a user file.

Discovery here means *importing candidate board URLs*, never minting slugs. Two
sources are supported:

* the packaged seed (``data/discovery_seed.yaml``) — real board slugs harvested from
  public open ATS directories and pre-filtered for India roles, and
* any user-supplied YAML/text file of ``slug: url`` pairs or bare board URLs, so a
  board found in a public directory can be dropped in and validated.

Nothing loaded here is trusted: every candidate is re-validated live by
``BoardValidator`` before it can be adopted into config.yaml.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from jobos.config import CompanySettings, slugify_company
from jobos.crawler import derive_company_name
from jobos.models import validate_web_url

SEED_RESOURCE = "discovery_seed.yaml"
ENTERPRISE_SEED_RESOURCE = "discovery_seed_enterprise.yaml"
ORACLE_SEED_RESOURCE = "discovery_seed_oracle.yaml"

# Named seed sets the CLI can select. "all" is the union, so one run spans startups,
# enterprises and GCC/Oracle boards side by side (requirement 8).
SEED_SETS: dict[str, tuple[str, ...]] = {
    "main": (SEED_RESOURCE,),
    "enterprise": (ENTERPRISE_SEED_RESOURCE,),
    "oracle": (ORACLE_SEED_RESOURCE,),
    "gcc": (ORACLE_SEED_RESOURCE, ENTERPRISE_SEED_RESOURCE),
    "all": (SEED_RESOURCE, ENTERPRISE_SEED_RESOURCE, ORACLE_SEED_RESOURCE),
}


def _available_resources(resources_for: tuple[str, ...]) -> tuple[str, ...]:
    """Drop seed resources that are not packaged yet, so a missing optional seed
    (e.g. the Oracle one before it is generated) does not break discovery."""
    present: list[str] = []
    for resource in resources_for:
        try:
            if resources.files("jobos.data").joinpath(resource).is_file():
                present.append(resource)
        except (FileNotFoundError, ModuleNotFoundError):
            continue
    return tuple(present)


def _candidate(slug: str | None, url: str) -> CompanySettings:
    url = validate_web_url(url.strip())
    # When the caller gives an explicit slug (the seed files and `slug: url` mappings
    # do), trust it for the display name: it is the clean employer key, and for
    # Workday the URL path is a site name ("careers", "External") rather than the
    # employer, so deriving from the URL would mangle it ("Lbg", "Svb"). A bare-URL
    # list has no slug, so fall back to deriving the employer from the URL.
    if slug:
        name = slug.replace("_", " ").replace("-", " ").strip().title()
    else:
        name = derive_company_name(url)
    return CompanySettings(name=name, url=url, enabled=True)


def _parse_mapping(raw: dict[str, object]) -> list[CompanySettings]:
    candidates = raw.get("candidates", raw)
    out: list[CompanySettings] = []
    if isinstance(candidates, dict):
        for slug, value in candidates.items():
            if isinstance(value, str):
                out.append(_candidate(str(slug), value))
            elif isinstance(value, dict) and "url" in value:
                out.append(_candidate(str(slug), str(value["url"])))
    elif isinstance(candidates, list):
        for value in candidates:
            if isinstance(value, str):
                out.append(_candidate(None, value))
            elif isinstance(value, dict) and "url" in value:
                out.append(_candidate(value.get("slug"), str(value["url"])))
    return out


def _load_resource(resource: str) -> list[CompanySettings]:
    text = resources.files("jobos.data").joinpath(resource).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return _parse_mapping(raw if isinstance(raw, dict) else {})


def load_seed_candidates(seed_set: str = "all") -> list[CompanySettings]:
    """Packaged candidate boards for a named seed set (``main``/``enterprise``/``all``).

    Defaults to ``all`` so a discovery run considers startups and enterprises together
    rather than one segment at a time. Duplicate slugs across seeds are collapsed,
    keeping the first occurrence.
    """
    resources_for = _available_resources(SEED_SETS.get(seed_set, SEED_SETS["all"]))
    seen: set[str] = set()
    out: list[CompanySettings] = []
    for resource in resources_for:
        for company in _load_resource(resource):
            slug = slugify_company(company.name)
            if slug in seen:
                continue
            seen.add(slug)
            out.append(company)
    return out


def load_candidates_file(path: Path) -> list[CompanySettings]:
    """Candidate boards from a user file: YAML mapping/list, or one URL per line."""
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        raw = None
    if isinstance(raw, (dict, list)):
        parsed = _parse_mapping(raw if isinstance(raw, dict) else {"candidates": raw})
        if parsed:
            return parsed
    # Fall back to plain "one URL per line" so a quick paste works too.
    out: list[CompanySettings] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("http://", "https://")):
            out.append(_candidate(None, line))
    return out


def dedupe_against(
    candidates: list[CompanySettings], existing_slugs: set[str]
) -> list[CompanySettings]:
    """Drop candidates whose slug is already configured, keeping first of each slug."""
    seen: set[str] = set()
    result: list[CompanySettings] = []
    for company in candidates:
        slug = slugify_company(company.name)
        if slug in existing_slugs or slug in seen:
            continue
        seen.add(slug)
        result.append(company)
    return result
