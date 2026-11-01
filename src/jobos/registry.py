"""A lightweight, durable company registry kept beside config.yaml.

config.yaml stays the human-edited source of truth for *which* boards get crawled —
it keeps its hand-written comments and per-board annotations. The registry is a
machine-maintained index of *what JobOS learned* about each board: ATS type,
validation status, last-validated / last-crawled dates, and India / relevant role
counts. Keeping it separate means:

* config.yaml is never rewritten by a round-trip that would strip its comments,
* adding the registry is fully backward-compatible (no file simply means an empty
  registry, and older configs load unchanged), and
* coverage can be reported and dead boards flagged without re-validating every run.

It is stored as JSON at ``<data_dir>/company_registry.json`` and written atomically.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from jobos.classification import MAJOR_EMPLOYERS
from jobos.models import (
    BoardStatus,
    BoardValidation,
    CategoryCoverage,
    CompanyCategory,
    CoverageReport,
    RegistryEntry,
    utc_now,
)

REGISTRY_FILENAME = "company_registry.json"
_SCHEMA_VERSION = 1


class CompanyRegistry:
    """Load, update and persist the sidecar registry."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, RegistryEntry] = {}

    @classmethod
    def load(cls, data_dir: Path) -> CompanyRegistry:
        registry = cls(data_dir / REGISTRY_FILENAME)
        if not registry.path.exists():
            return registry
        try:
            raw = json.loads(registry.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt registry is non-fatal: it is a derived index, so start fresh
            # rather than blocking a crawl. The next validate/crawl rebuilds it.
            return registry
        for item in raw.get("entries", []):
            try:
                entry = RegistryEntry.model_validate(item)
            except Exception:
                continue
            registry.entries[entry.slug] = entry
        return registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "updated_at": utc_now().isoformat(),
            "entries": [entry.model_dump(mode="json") for entry in self.entries.values()],
        }
        # Atomic write: a crash mid-write must not truncate the registry.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record_validation(self, result: BoardValidation) -> RegistryEntry:
        """Upsert an entry from a validation result, preserving any crawl date."""
        existing = self.entries.get(result.slug)
        entry = RegistryEntry(
            slug=result.slug,
            name=result.name,
            url=result.url,
            ats_type=result.ats_type,
            category=result.category,
            is_gcc=result.is_gcc,
            status=result.status,
            india_role_count=result.india_role_count,
            relevant_role_count=result.relevant_role_count,
            last_validated_at=result.validated_at,
            last_crawled_at=existing.last_crawled_at if existing else None,
            detail=result.detail,
        )
        self.entries[result.slug] = entry
        return entry

    def mark_crawled(self, slug: str) -> None:
        entry = self.entries.get(slug)
        if entry is not None:
            entry.last_crawled_at = utc_now()

    def remove(self, slug: str) -> bool:
        return self.entries.pop(slug, None) is not None

    def supported(self) -> list[RegistryEntry]:
        """Entries on a known ATS adapter, regardless of validation outcome."""
        return [entry for entry in self.entries.values() if entry.ats_type]

    def unsupported(self) -> list[RegistryEntry]:
        """Boards flagged for manual review because no JSON adapter handles them."""
        return [
            entry
            for entry in self.entries.values()
            if entry.status is BoardStatus.UNSUPPORTED
        ]

    def valid(self) -> list[RegistryEntry]:
        return [entry for entry in self.entries.values() if entry.status is BoardStatus.VALID]

    def coverage(self, configured_slugs: set[str]) -> CoverageReport:
        """Summarise coverage for the before/after comparison, broken out by category."""
        valid = self.valid()
        by_category: list[CategoryCoverage] = []
        for category in CompanyCategory:
            members = [entry for entry in valid if entry.category is category]
            if not members:
                continue
            by_category.append(
                CategoryCoverage(
                    category=category,
                    boards=len(members),
                    india_roles=sum(entry.india_role_count for entry in members),
                    relevant_roles=sum(entry.relevant_role_count for entry in members),
                )
            )
        by_category.sort(key=lambda item: item.relevant_roles, reverse=True)
        return CoverageReport(
            configured_boards=len(configured_slugs),
            validated_boards=len(valid),
            supported_boards=len(self.supported()),
            unsupported_boards=len(self.unsupported()),
            india_roles=sum(entry.india_role_count for entry in valid),
            relevant_roles=sum(entry.relevant_role_count for entry in valid),
            flagged_for_review=sorted(entry.name for entry in self.unsupported()),
            by_category=by_category,
            unsupported_major_employers=self._missing_major_employers(valid),
            gcc_boards=sum(1 for entry in valid if entry.is_gcc),
        )

    @staticmethod
    def _missing_major_employers(valid: list[RegistryEntry]) -> list[str]:
        """Well-known employers not yet represented among validated boards.

        A "are the obvious names covered?" checklist (requirement 9), matched on a
        name substring so "HCLTech" counts for "HCL". Purely a report; it never drives
        crawling.
        """
        have = " ".join(entry.name.casefold() for entry in valid)
        missing: list[str] = []
        for names in MAJOR_EMPLOYERS.values():
            for name in names:
                token = name.casefold().split()[0]
                if token not in have:
                    missing.append(name)
        return missing
