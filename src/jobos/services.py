"""Application use cases shared by the web UI and the CLI."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from jobos.agents import (
    ATSAgent,
    Coordinator,
    CoverLetterAgent,
    JobAnalyzerAgent,
    ResumeAgent,
    ResumeStructuringAgent,
    SkillsAgent,
    SummaryAgent,
    TruthGuard,
)
from jobos.ai import AIClient, AIError
from jobos.ats import canonical_skill, extract_known_skills, mentioned_skills
from jobos.config import (
    AppConfig,
    CompanySettings,
    append_company,
    list_config_backups,
    load_config,
    replace_top_level_block,
    slugify_company,
    write_config_text,
)
from jobos.crawler import (
    CRAWL_WAVES,
    CareerCrawler,
    _extract_experience_text,
    derive_company_name,
    detect_ats_type,
    job_fingerprint,
)
from jobos.database import Database
from jobos.discovery import (
    dedupe_against,
    load_candidates_file,
    load_seed_candidates,
)
from jobos.models import (
    ApplicationStatus,
    ApplicationView,
    ATSReportData,
    BoardStatus,
    BoardValidation,
    ChatMessage,
    ChatReply,
    CoverageReport,
    CrawlSummary,
    DailyDigest,
    GccReport,
    HealthItem,
    HealthReport,
    InsightsReport,
    JobAnalysis,
    JobData,
    JobStatus,
    JobView,
    LabelCount,
    RegistryEntry,
    ResumeImportResult,
    ResumeVersionView,
    ValidationSummary,
    utc_now,
    validate_web_url,
)
from jobos.registry import CompanyRegistry
from jobos.renderer import (
    BulletCondenser,
    PackageWriter,
    RenderedPackage,
    RenderError,
    ResumeRenderer,
    open_apply_page,
)
from jobos.resume import (
    ResumeImporter,
    ResumeParseError,
    dump_structured_resume_yaml,
    load_structured_resume,
)
from jobos.validation import BoardValidator

_INSIGHT_CITIES = ("Hyderabad", "Bengaluru", "Bangalore", "Pune", "Chennai", "Mumbai",
                   "Gurugram", "Gurgaon", "Noida", "Delhi", "Remote")


_PARENT_NOISE = re.compile(
    r"\b(india|private|pvt|ltd|limited|inc|llc|corp|corporation|global|services|"
    r"solutions|technology|technologies|group|centre|center|gcc|careers?|"
    r"capability|international|software|systems|labs?)\b",
    re.I,
)


def _parent_key(name: str) -> str:
    """Normalize an employer name to a parent identity for dedup.

    Strips common suffixes/noise ("India", "Private Ltd", "Global Services", etc.) so
    a parent with several India entities collapses to one key. Conservative: if
    stripping leaves nothing, fall back to the casefolded full name.
    """
    stripped = _PARENT_NOISE.sub(" ", name.casefold())
    stripped = re.sub(r"[^a-z0-9]+", " ", stripped).strip()
    return stripped or name.casefold().strip()


def _location_bucket(location: str) -> str:
    """Bucket a free-form location into a city the user cares about, India, or Other."""
    text = (location or "").casefold()
    for city in _INSIGHT_CITIES:
        if city.casefold() in text:
            # Normalise the two spellings of Bengaluru into one bucket.
            return "Bengaluru" if city.casefold() == "bangalore" else city
    if "india" in text:
        return "India (other)"
    return "Other / global"


class ServiceError(RuntimeError):
    """A use case could not complete; the message is safe to show the user."""


@dataclass(slots=True)
class JobPage:
    """One page of search results plus the honest total count across all pages."""

    jobs: list[JobView]
    total: int
    offset: int
    limit: int

    @property
    def has_prev(self) -> bool:
        return self.offset > 0

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.jobs) < self.total

    @property
    def page_number(self) -> int:
        return (self.offset // self.limit) + 1 if self.limit else 1

    @property
    def page_count(self) -> int:
        if self.limit <= 0:
            return 1
        return max(1, (self.total + self.limit - 1) // self.limit)

    @property
    def showing_from(self) -> int:
        return self.offset + 1 if self.jobs else 0

    @property
    def showing_to(self) -> int:
        return self.offset + len(self.jobs)


@dataclass(slots=True)
class WaveResult:
    """Outcome of one wave within a sequential multi-wave crawl."""

    wave: str
    boards: int
    summary: CrawlSummary


@dataclass(slots=True)
class CrawlProgress:
    """Status of the most recent background crawl, for the web UI."""

    running: bool = False
    label: str = ""
    started_at: str = ""
    finished_at: str = ""
    summary: CrawlSummary | None = None
    error: str = ""
    # Multi-wave tracking, so the dashboard can notify as each wave completes. Empty
    # for a plain single crawl. ``completed_waves`` accumulates finished-wave names;
    # the poller compares it to what it last saw to fire exactly one notification.
    current_wave: str = ""
    completed_waves: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OptimizationOutcome:
    application: ApplicationView
    job: JobView
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BatchAnalysisResult:
    """Outcome of analysing several jobs at once."""

    requested: int
    analysed: int
    errors: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class CompanyShortlist:
    """The best-fit roles at one company, bounded by that company's application quota."""

    company: str
    relevant: int
    """How many roles at this company pass the preference filter."""
    applied: int
    """How many have already been marked applied, consuming quota."""
    quota: int
    slots_left: int
    picks: list[JobView] = field(default_factory=list)
    """The strongest matches that fit the remaining quota."""
    overflow: int = 0
    """Relevant roles beyond the quota cut, available on demand."""
    unscored: int = 0
    """Picks still lacking a match score, so ranking is provisional until analysed."""

    @property
    def best_score(self) -> int:
        return max((job.match_score or 0) for job in self.picks) if self.picks else 0

    @property
    def exhausted(self) -> bool:
        return self.slots_left <= 0


@dataclass(slots=True)
class DailyPreparation:
    """One job whose tailored resume package is ready for review at apply time."""

    job: JobView
    application_id: int
    output_dir: str
    score_before: int
    score_after: int


@dataclass(slots=True)
class DailyRunResult:
    """Everything one morning run did, ready to be printed as an action list."""

    crawl: CrawlSummary | None = None
    analysed: int = 0
    shortlists: list[CompanyShortlist] = field(default_factory=list)
    prepared: list[DailyPreparation] = field(default_factory=list)
    already_prepared: list[DailyPreparation] = field(default_factory=list)
    analysis_errors: dict[int, str] = field(default_factory=dict)
    preparation_errors: dict[int, str] = field(default_factory=dict)

    @property
    def ready(self) -> list[DailyPreparation]:
        """Newly prepared plus previously prepared, best match first."""
        combined = self.prepared + self.already_prepared
        return sorted(combined, key=lambda item: -item.score_after)


class JobOSService:
    """Wire the pieces together. Plain constructor injection, no framework."""

    def __init__(self, config: AppConfig, database: Database | None = None) -> None:
        config.ensure_directories()
        self.config = config
        self.database = database or Database(config.database_path)
        self.database.initialize()

        self.ai = AIClient(config.ai)
        tasks = config.ai.tasks
        self.importer = ResumeImporter(
            config,
            self.database,
            ResumeStructuringAgent(self.ai) if tasks.structure_resume else None,
        )
        self.analyzer = JobAnalyzerAgent(self.ai if tasks.analyse_jobs else None)
        self.ats = ATSAgent()
        self.cover_letters = CoverLetterAgent(self.ai if tasks.cover_letter else None)
        self.coordinator = Coordinator(
            skills=SkillsAgent(),
            summary=SummaryAgent(self.ai if tasks.rewrite_summary else None),
            resume=ResumeAgent(self.ai if tasks.rewrite_bullets else None),
            ats=self.ats,
            truth_guard=TruthGuard(),
        )
        self.renderer = ResumeRenderer(config.render.theme)
        self.packages = PackageWriter(config.output_dir, self.renderer)
        self.crawl_progress = CrawlProgress()
        self._crawl_lock = asyncio.Lock()
        # Held so the running crawl is not garbage collected mid-flight.
        self._crawl_task: asyncio.Task[None] | None = None
        # mtime of config.yaml as last loaded, so a long-running server can notice
        # the file changing underneath it (CLI adoption, hand edits) and reload.
        try:
            self._config_mtime_ns = config.config_path.stat().st_mtime_ns
        except OSError:
            self._config_mtime_ns = 0

    # ----- resume -------------------------------------------------------

    async def import_resume(self, filename: str, content: bytes) -> ResumeImportResult:
        return await self.importer.import_bytes(filename, content)

    def master_resume(self) -> ResumeVersionView | None:
        return self.database.get_master_resume()

    def require_master_resume(self) -> ResumeVersionView:
        master = self.master_resume()
        if master is None:
            raise ServiceError(
                "No master resume yet. Upload a PDF, DOCX, or Markdown resume first."
            )
        return master

    def resume_versions(self) -> list[ResumeVersionView]:
        return self.database.list_resume_versions()

    # ----- jobs ---------------------------------------------------------

    async def crawl(
        self,
        company: str | None = None,
        *,
        summary: CrawlSummary | None = None,
        ats_types: set[str] | None = None,
    ) -> CrawlSummary:
        """Crawl configured boards, optionally limited to an ATS "wave".

        ``ats_types`` runs only boards on those ATSs so a crawl can be done in speed
        tiers (fast Greenhouse/Lever/Ashby, then slow Workday). ``None`` crawls all.
        """
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        crawler = CareerCrawler(self.config, self.database)
        result = await crawler.crawl(company, summary=summary, ats_types=ats_types)
        # Keep the database bounded without a manual step: delete postings a board
        # stopped listing (already hidden from search) that have been gone 60+ days.
        # Jobs tied to a prepared application are always kept by prune_closed_jobs.
        result.jobs_pruned = self.prune_jobs(older_than_days=60, commit=True)
        self._record_crawl_dates(company, result, ats_types=ats_types)
        return result

    async def crawl_waves(
        self,
        *,
        on_wave_done: Callable[[WaveResult], None] | None = None,
    ) -> list[WaveResult]:
        """Crawl every board in speed waves, fastest first, one wave after another.

        Runs the fast boards (Greenhouse/Lever/Ashby) to completion first, then medium
        (SmartRecruiters/Oracle), then slow (Workday). Because each company's jobs are
        written as it finishes, the fast wave's jobs are searchable — and applyable —
        the moment that wave completes, while the later waves keep running behind it.
        ``on_wave_done`` fires once per finished wave so a caller can notify the user.

        Sequential, not parallel: two crawls at once would contend on the SQLite
        writer and hammer the same APIs, so the waves share one run and never overlap.
        """
        results: list[WaveResult] = []
        for wave, ats_types in CRAWL_WAVES:
            boards = sum(
                1
                for settings in self.config.companies.values()
                if (detect_ats_type(settings.url) or "generic") in ats_types
            )
            self.crawl_progress.current_wave = wave
            summary = await self.crawl(ats_types=ats_types)
            self.crawl_progress.completed_waves.append(wave)
            result = WaveResult(wave=wave, boards=boards, summary=summary)
            results.append(result)
            if on_wave_done is not None:
                on_wave_done(result)
        self.crawl_progress.current_wave = ""
        return results

    def _record_crawl_dates(
        self,
        company: str | None,
        result: CrawlSummary,
        *,
        ats_types: set[str] | None = None,
    ) -> None:
        """Stamp last_crawled_at on registered boards a successful crawl covered.

        Best-effort and non-fatal: the registry is a derived index, so a write
        failure here must never break a crawl. Only companies that did not error are
        stamped.
        """
        try:
            registry = self._registry()
        except Exception:
            return
        errored = set(result.errors)
        if company is not None:
            crawled = [company]
        else:
            crawled = [
                slug
                for slug, settings in self.config.companies.items()
                if slug not in errored
                # Only stamp boards actually in this wave, so a fast-wave crawl does
                # not falsely mark Workday boards as freshly crawled.
                and (
                    ats_types is None
                    or (detect_ats_type(settings.url) or "generic") in ats_types
                )
            ]
        changed = False
        for slug in crawled:
            if slug in registry.entries and slug not in errored:
                registry.mark_crawled(slug)
                changed = True
        if changed:
            with contextlib.suppress(Exception):
                registry.save()

    async def crawl_url(
        self, url: str, name: str | None = None, *, summary: CrawlSummary | None = None
    ) -> CrawlSummary:
        """Crawl a single career page or board URL that is not in config.

        Uses the same adapters as a configured crawl (Greenhouse/Lever/Ashby/etc. by
        host, generic HTML/JSON-LD otherwise) and stores jobs under a derived employer
        name. SSRF protection applies. The source is not written to config, so a
        re-crawl means pasting the URL again.
        """
        clean = self._validate_crawl_url(url)
        settings = CompanySettings(
            name=(name or "").strip() or derive_company_name(clean), url=clean
        )
        crawler = CareerCrawler(self.config, self.database)
        return await crawler.crawl_settings(settings, summary=summary)

    def add_job_from_text(
        self,
        *,
        title: str,
        company: str,
        description: str,
        url: str = "",
    ) -> JobView:
        """Create a job directly from pasted JD text, bypassing the crawler.

        For postings on custom career portals the crawler cannot read (anything that
        is not a supported ATS board), the user can paste the job description here and
        still analyse, score, and optimise against it. Skills and the years-of-
        experience line are derived from the text with the same deterministic helpers
        the crawler uses, so the pasted job behaves like a crawled one downstream.

        A real apply URL is used when given; otherwise a stable local placeholder keeps
        the job openable in the dashboard without pretending there is a live page.
        """
        title = title.strip()
        company = company.strip()
        description = description.strip()
        if not title:
            raise ServiceError("A job title is required.")
        if not company:
            raise ServiceError("A company name is required.")
        if len(description) < 40:
            raise ServiceError("Paste the full job description (at least a few sentences).")

        clean_url = ""
        if url.strip():
            clean_url = self._validate_crawl_url(url)
        # JobData requires valid web URLs; use the pasted URL, or a stable loopback
        # placeholder that is clearly a local paste rather than a fake employer link.
        fingerprint_src = f"{company}\x1f{title}\x1f{description[:200]}"
        digest = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()[:16]
        apply_url = clean_url or f"https://localhost/jobos/pasted/{digest}"
        job_data = JobData(
            company=company,
            title=title,
            description=description,
            experience=_extract_experience_text(description),
            skills=extract_known_skills(description),
            apply_url=apply_url,
            source_url=clean_url or apply_url,
            external_id=f"pasted-{digest}",
        )
        view, _ = self.database.upsert_job(job_data, job_fingerprint(job_data))
        return view

    def add_company(self, url: str, name: str | None = None) -> str:
        """Persist a company under `companies:` in config.yaml and load it immediately.

        Returns the slug it was stored as. The running process picks the company up
        without a restart, so it can be crawled straight away.
        """
        clean = self._validate_crawl_url(url)
        display = (name or "").strip() or derive_company_name(clean)
        slug = slugify_company(display)
        if slug in self.config.companies:
            raise ServiceError(f"{display} is already configured as {slug!r}.")
        try:
            append_company(self.config.config_path, slug, display, clean)
        except OSError as exc:
            raise ServiceError(f"Could not write to the config file: {exc}") from exc
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        self.config.companies[slug] = CompanySettings(name=display, url=clean)
        return slug

    # ----- board discovery, validation and registry ---------------------

    def _registry(self) -> CompanyRegistry:
        return CompanyRegistry.load(self.config.data_dir)

    def load_candidate_boards(
        self,
        source: Path | None = None,
        *,
        skip_configured: bool = True,
        seed_set: str = "all",
    ) -> list[CompanySettings]:
        """Candidate boards to validate: a packaged seed set, or a user file.

        ``seed_set`` selects which packaged seeds to load (``main`` for the
        startup/SaaS seed, ``enterprise`` for MNC/GCC/IT-services boards, or ``all``).
        Boards already in config.yaml are skipped by default so a run only looks at
        genuinely new coverage.
        """
        candidates = (
            load_candidates_file(source)
            if source is not None
            else load_seed_candidates(seed_set)
        )
        if skip_configured:
            configured = set(self.config.companies.keys())
            candidates = dedupe_against(candidates, configured)
        return candidates

    async def validate_boards(
        self,
        candidates: list[CompanySettings],
        *,
        record: bool = True,
        adopt_incrementally: bool = False,
        on_result: Callable[[int, int, BoardValidation], None] | None = None,
    ) -> ValidationSummary:
        """Validate candidate boards against their ATS API and update the registry.

        HTTP 200 alone is never accepted: each board is crawled through the real
        adapter and judged on whether it returned well-formed postings with India
        roles. Results are written to the sidecar registry so coverage can be
        reported and unsupported portals flagged for manual review. ``on_result`` is
        forwarded to the validator for live per-board progress.

        When ``adopt_incrementally`` is set, each board that passes is written to
        config.yaml and the registry *as it finishes*, not in one batch at the end.
        That makes progress durable — interrupting the run keeps every board already
        adopted — and visible, since config.yaml grows live. The registry is loaded
        once and saved per adoption so a crash cannot lose more than the last board.
        """
        validator = BoardValidator(self.config, self.database)
        registry = self._registry() if (record or adopt_incrementally) else None

        def handle(done: int, total: int, result: BoardValidation) -> None:
            if registry is not None:
                registry.record_validation(result)
                if adopt_incrementally and result.adoptable:
                    self._adopt_one(result)
                # Save per board so an interrupt keeps everything up to here. These
                # are small files; the cost is negligible next to the network calls.
                with contextlib.suppress(OSError):
                    registry.save()
            if on_result is not None:
                on_result(done, total, result)

        summary = await validator.validate_candidates(candidates, on_result=handle)
        if registry is not None and not adopt_incrementally:
            # Non-incremental path already recorded during the callback; just persist
            # the final state once more in case the last save raced a failure.
            with contextlib.suppress(OSError):
                registry.save()
        return summary

    def _adopt_one(self, result: BoardValidation) -> bool:
        """Append a single validated board to config.yaml if not already present.

        Returns True when it was added. Comment-preserving and idempotent, so it is
        safe to call repeatedly and from the per-board validation callback.
        """
        if result.slug in self.config.companies:
            return False
        try:
            append_company(
                self.config.config_path, result.slug, result.name, result.url
            )
        except (ValueError, OSError):
            return False
        self.config.companies[result.slug] = CompanySettings(
            name=result.name, url=result.url
        )
        return True

    def adopt_boards(
        self, results: list[BoardValidation], *, limit: int | None = None
    ) -> list[str]:
        """Write passing boards into config.yaml, newest-first by relevant roles.

        Only boards that validated as adoptable are written, through the existing
        comment-preserving ``append_company`` path. Each is a reversible, additive
        config edit (a prior config.yaml is snapshotted on any save elsewhere, and the
        added block is clearly marked), so adoption can be rolled back. Returns the
        slugs actually added.
        """
        adoptable = sorted(
            (result for result in results if result.adoptable),
            key=lambda item: (item.relevant_role_count, item.india_role_count),
            reverse=True,
        )
        if limit is not None:
            adoptable = adoptable[:limit]
        return [result.slug for result in adoptable if self._adopt_one(result)]

    def coverage_report(self) -> CoverageReport:
        """Before/after coverage snapshot from config.yaml and the registry."""
        registry = self._registry()
        return registry.coverage(set(self.config.companies.keys()))

    def gcc_report(self) -> GccReport:
        """Phase-6 GCC discovery report, measured against the 2,117 benchmark.

        Built from the registry (what discovery learned) and config (what is adopted),
        with GCC employers deduped by a normalized parent identity so one parent with
        several India boards counts once. Nothing here is invented; the benchmark is a
        cited published figure, and coverage is reported honestly against it.
        """
        from collections import Counter

        registry = self._registry()
        gcc_entries = [e for e in registry.entries.values() if e.is_gcc]
        # Dedup by a normalized parent key so "Wells Fargo" and "Wells Fargo India"
        # collapse to one employer (Phase 1 requirement).
        by_parent: dict[str, list[RegistryEntry]] = {}
        for entry in gcc_entries:
            by_parent.setdefault(_parent_key(entry.name), []).append(entry)

        configured = set(self.config.companies)
        valid = [e for e in gcc_entries if e.status is BoardStatus.VALID]
        ats = Counter(e.ats_type or "unknown" for e in gcc_entries)
        failed = Counter(
            e.status.value for e in gcc_entries if e.status is not BoardStatus.VALID
        )
        # GCC employers already in config before any adoption vs the whole set.
        in_jobos = sum(
            1 for parent, entries in by_parent.items()
            if any(e.slug in configured for e in entries)
        )
        return GccReport(
            gcc_candidates_seen=len(gcc_entries),
            unique_gcc_employers=len(by_parent),
            already_in_jobos=in_jobos,
            careers_pages_found=sum(1 for e in gcc_entries if e.url),
            ats_distribution=[
                LabelCount(label=label, count=count) for label, count in ats.most_common()
            ],
            crawlable_boards=len(valid),
            unsupported_boards=sum(
                1 for e in gcc_entries if e.status is BoardStatus.UNSUPPORTED
            ),
            with_active_india_jobs=sum(1 for e in valid if e.india_role_count > 0),
            newly_added=sum(1 for e in valid if e.slug in configured),
            failed_validations=[
                LabelCount(label=label, count=count) for label, count in failed.most_common()
            ],
        )

    def flagged_for_review(self) -> list[RegistryEntry]:
        """Unsupported portals recorded during validation, for manual handling."""
        return self._registry().unsupported()

    @staticmethod
    def _validate_crawl_url(url: str) -> str:
        try:
            return validate_web_url(url.strip())
        except ValueError as exc:
            raise ServiceError(f"That does not look like a web address: {exc}") from exc

    # ----- config file --------------------------------------------------

    def read_config_text(self) -> str:
        """Return the raw config.yaml text for display and editing in the UI."""
        try:
            return self.config.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ServiceError(f"Could not read the config file: {exc}") from exc

    def save_config_text(self, text: str) -> Path:
        """Validate and atomically save edited config.yaml, snapshotting the old one.

        A bad edit is rejected before anything is written, so the running app and the
        next startup are never left with a broken config. The previous file is backed
        up under the data directory first, so any overwrite (yours or an agent's) is
        recoverable. The in-memory company map is refreshed so a crawl right after a
        save sees the new boards without a restart.
        """
        try:
            backup = write_config_text(
                self.config.config_path, text, history_dir=self.config.config_history_dir
            )
        except ValueError as exc:
            raise ServiceError(f"Config not saved: {exc}") from exc
        except OSError as exc:
            raise ServiceError(f"Could not write the config file: {exc}") from exc
        # Refresh the live company map so the change takes effect without a restart.
        refreshed = load_config(self.config.config_path)
        self.config.companies = refreshed.companies
        return backup

    def config_backups(self) -> list[Path]:
        """Timestamped config snapshots, newest first, for the restore list."""
        return list_config_backups(self.config.config_history_dir)

    def reload_config_if_changed(self) -> bool:
        """Re-read config.yaml when the file changed under a long-running server.

        The web app builds one service at startup and reuses it, so edits made to
        config.yaml afterwards — a CLI ``validate-boards --adopt`` run, ``add-company``
        from another process, or a hand edit — are invisible until a restart. Pages
        that display the company list call this first so they reflect the file on
        disk. It is cheap: an mtime check, and a re-parse only when the file actually
        changed. A broken edit is ignored (the last good config stays live) rather
        than crashing the page. Returns True when the company map was refreshed.
        """
        try:
            mtime = self.config.config_path.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._config_mtime_ns:
            return False
        self._config_mtime_ns = mtime
        try:
            refreshed = load_config(self.config.config_path)
        except (ValueError, OSError):
            # A config mid-edit or temporarily invalid: keep the last good one.
            return False
        self.config.companies = refreshed.companies
        self.config.preferences = refreshed.preferences
        return True

    # ----- resume authoring help ----------------------------------------

    def resume_yaml_guide(self) -> tuple[str, str]:
        """Return (full guide markdown, just the conversion prompt) for the UI.

        The guide ships in the repo at docs/resume-yaml-guide.md. The prompt is the
        block between the two horizontal rules (``---``) under "one-time conversion
        prompt", surfaced as plain text so the page can offer a copy button. Both are
        empty strings when the file is missing, so the caller degrades gracefully.
        """
        guide_path = Path(__file__).resolve().parents[2] / "docs" / "resume-yaml-guide.md"
        if not guide_path.is_file():
            return "", ""
        text = guide_path.read_text(encoding="utf-8")
        prompt = ""
        # The prompt is the first block fenced by lines of exactly "---".
        parts = text.split("\n---\n")
        if len(parts) >= 3:
            # parts[1] is the blockquoted prompt; strip the leading "> " markers.
            raw = parts[1].strip()
            prompt = "\n".join(
                line[2:] if line.startswith("> ") else line.removeprefix(">")
                for line in raw.splitlines()
            ).strip()
        return text, prompt

    # ----- AI settings --------------------------------------------------

    PROVIDERS = ("ollama", "openai", "anthropic", "gemini", "openrouter")

    def save_api_key(self, provider: str, key: str) -> None:
        """Persist a provider API key to the local .env (never config.yaml).

        The key goes into ``<data_dir>/.env`` (which is gitignored and owner-readable
        only) as ``JOBOS_<PROVIDER>_API_KEY=...`` and is applied to the running
        process immediately, so it takes effect without a restart. An empty value
        removes the key. The key is never written to config.yaml and never echoed back
        to the UI.
        """
        provider = provider.strip().lower()
        if provider not in self.PROVIDERS or provider == "ollama":
            raise ServiceError(f"{provider!r} does not use an API key.")
        var = f"JOBOS_{provider.upper()}_API_KEY"
        key = key.strip()
        env_path = self.config.env_path
        # Read existing lines, drop any prior entry for this var, re-add if non-empty.
        lines: list[str] = []
        if env_path.exists():
            try:
                lines = [
                    ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                    if not ln.strip().startswith(f"{var}=")
                ]
            except OSError as exc:
                raise ServiceError(f"Could not read the key file: {exc}") from exc
        if key:
            lines.append(f"{var}={key}")
            os.environ[var] = key
        else:
            os.environ.pop(var, None)
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            # Owner read/write only — a secrets file should not be group/world readable.
            os.chmod(env_path, 0o600)
        except OSError as exc:
            raise ServiceError(f"Could not write the key file: {exc}") from exc

    def ai_settings_view(self) -> dict[str, Any]:
        """Current AI configuration plus per-provider API-key presence (never values).

        Keys live in environment variables (JOBOS_<PROVIDER>_API_KEY), not in
        config.yaml, so this only reports whether each one is set — it never reads or
        returns the secret itself.
        """
        ai = self.config.ai
        key_status = {}
        for provider in self.PROVIDERS:
            if provider == "ollama":
                continue
            variable = f"JOBOS_{provider.upper()}_API_KEY"
            key_status[provider] = {
                "variable": variable,
                "present": bool(os.environ.get(variable)),
            }
        return {
            "provider": ai.provider,
            "model": ai.model,
            "base_url": ai.base_url,
            "temperature": ai.temperature,
            "timeout_seconds": ai.timeout_seconds,
            "max_input_chars": ai.max_input_chars,
            "max_output_tokens": ai.max_output_tokens,
            "max_bullet_rewrites": ai.max_bullet_rewrites,
            "keep_alive": ai.keep_alive,
            "disable_thinking": ai.disable_thinking,
            "tasks": ai.tasks.model_dump(),
            "providers": list(self.PROVIDERS),
            "key_status": key_status,
        }

    async def ollama_models(self) -> list[str]:
        """List installed Ollama model tags from the running server, for the dropdown.

        Returns an empty list (not an error) when Ollama is unreachable, so the page
        can fall back to a free-text model field without failing.
        """
        base = self.config.ai.base_url.rstrip("/")
        if "11434" not in base and self.config.ai.provider != "ollama":
            base = "http://127.0.0.1:11434"
        try:
            timeout = httpx.Timeout(5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base}/api/tags")
                response.raise_for_status()
                models = response.json().get("models", [])
        except (httpx.HTTPError, ValueError):
            return []
        names = [str(item.get("name", "")) for item in models if item.get("name")]
        return sorted(names)

    def save_ai_settings(self, updates: dict[str, Any]) -> Path:
        """Update only the ``ai:`` block of config.yaml, validated and backed up.

        The rest of the file (companies, preferences, their comments) is preserved by
        editing the parsed structure and writing it back through the same validated,
        atomic, snapshot-first path used for the full-config editor. API keys are never
        accepted or written here — they stay in environment variables.
        """
        raw = yaml.safe_load(self.config.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ServiceError("config.yaml is not a mapping; fix it in the editor first.")
        ai_block = dict(raw.get("ai") or {})
        tasks = dict(ai_block.get("tasks") or {})

        if "provider" in updates:
            if updates["provider"] not in self.PROVIDERS:
                raise ServiceError(f"Unknown provider {updates['provider']!r}.")
            ai_block["provider"] = updates["provider"]
        for key in ("model", "base_url", "keep_alive"):
            if updates.get(key):
                ai_block[key] = str(updates[key]).strip()
        for key in ("temperature", "timeout_seconds"):
            if key in updates and updates[key] is not None:
                ai_block[key] = float(updates[key])
        for key in ("max_input_chars", "max_output_tokens", "max_bullet_rewrites"):
            if key in updates and updates[key] is not None:
                ai_block[key] = int(updates[key])
        if "disable_thinking" in updates:
            ai_block["disable_thinking"] = bool(updates["disable_thinking"])
        for task in ("structure_resume", "analyse_jobs", "rewrite_summary",
                     "rewrite_bullets", "cover_letter"):
            if task in updates:
                tasks[task] = bool(updates[task])
        if tasks:
            ai_block["tasks"] = tasks

        # Render ONLY the ai: block and splice it back in place, so every comment
        # elsewhere in config.yaml (the companies annotations especially) is kept.
        # Dumping the whole document would strip all comments.
        block_yaml = yaml.safe_dump({"ai": ai_block}, sort_keys=False, allow_unicode=True, width=100)
        current = self.config.config_path.read_text(encoding="utf-8")
        try:
            text = replace_top_level_block(current, "ai", block_yaml)
            backup = write_config_text(
                self.config.config_path, text, history_dir=self.config.config_history_dir
            )
        except ValueError as exc:
            raise ServiceError(f"AI settings not saved: {exc}") from exc
        except OSError as exc:
            raise ServiceError(f"Could not write the config file: {exc}") from exc
        # Refresh in-memory config so the change takes effect without a restart.
        refreshed = load_config(self.config.config_path)
        self.config.ai = refreshed.ai
        self.ai.settings = refreshed.ai
        return backup

    # ----- preferences --------------------------------------------------

    def preferences_view(self) -> dict[str, Any]:
        """Current preferences as plain values for the friendly form."""
        prefs = self.config.preferences
        return {
            "preferred_locations": ", ".join(prefs.preferred_locations),
            "remote_only": prefs.remote_only,
            "minimum_match_score": prefs.minimum_match_score,
            "roles_include": ", ".join(prefs.roles_include),
            "roles_exclude": ", ".join(prefs.roles_exclude),
            "skills_include": ", ".join(prefs.skills_include),
            "skills_exclude": ", ".join(prefs.skills_exclude),
            "min_years": prefs.min_years if prefs.min_years is not None else "",
            "max_years": prefs.max_years if prefs.max_years is not None else "",
            "seniority": prefs.seniority,
            "max_applications_per_company": prefs.max_applications_per_company,
        }

    def save_preferences(self, updates: dict[str, Any]) -> Path:
        """Update only the ``preferences:`` block of config.yaml, validated and backed up.

        Like the AI editor, this splices one top-level block back into the file so the
        companies annotations and every other comment are preserved. Lists are given as
        comma-separated text in the form and normalised here.
        """
        raw = yaml.safe_load(self.config.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ServiceError("config.yaml is not a mapping; fix it in the editor first.")
        block = dict(raw.get("preferences") or {})

        def terms(value: Any) -> list[str]:
            return [part.strip() for part in str(value or "").split(",") if part.strip()]

        for key in ("preferred_locations", "roles_include", "roles_exclude",
                    "skills_include", "skills_exclude"):
            if key in updates:
                block[key] = terms(updates[key])
        if "remote_only" in updates:
            block["remote_only"] = bool(updates["remote_only"])
        if "minimum_match_score" in updates and updates["minimum_match_score"] is not None:
            block["minimum_match_score"] = int(updates["minimum_match_score"])
        if "max_applications_per_company" in updates and updates["max_applications_per_company"] is not None:
            block["max_applications_per_company"] = int(updates["max_applications_per_company"])
        for key in ("min_years", "max_years"):
            if key in updates:
                value = updates[key]
                block[key] = float(value) if value not in (None, "") else None
        if "seniority" in updates:
            block["seniority"] = str(updates["seniority"] or "").strip()
        raw_block = {"preferences": block}

        block_yaml = yaml.safe_dump(raw_block, sort_keys=False, allow_unicode=True, width=100)
        current = self.config.config_path.read_text(encoding="utf-8")
        try:
            text = replace_top_level_block(current, "preferences", block_yaml)
            backup = write_config_text(
                self.config.config_path, text, history_dir=self.config.config_history_dir
            )
        except ValueError as exc:
            raise ServiceError(f"Preferences not saved: {exc}") from exc
        except OSError as exc:
            raise ServiceError(f"Could not write the config file: {exc}") from exc
        refreshed = load_config(self.config.config_path)
        self.config.preferences = refreshed.preferences
        return backup

    # ----- job cleanup --------------------------------------------------

    def closed_job_count(self) -> int:
        """Soft-deleted postings still occupying rows, for the UI badge."""
        return self.database.count_closed_jobs()

    def hard_delete_closed_jobs(self, *, vacuum: bool = False) -> int:
        """Permanently remove all soft-deleted jobs now (the manual UI action)."""
        removed = self.database.hard_delete_closed_jobs()
        if vacuum and removed:
            self.database.vacuum()
        return removed

    async def start_background_crawl(
        self, company: str | None = None, *, ats_types: set[str] | None = None
    ) -> None:
        """Kick off a crawl of configured companies for the web UI, without blocking.

        ``ats_types`` runs only one speed wave (e.g. the fast Greenhouse/Lever/Ashby
        boards), mirroring the CLI ``--ats`` option, so the dashboard can start a quick
        wave without committing to a full multi-minute Workday crawl.
        """
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        if company:
            label = company
        elif ats_types:
            label = f"{', '.join(sorted(ats_types))} boards"
        else:
            label = f"{len(self.config.companies)} configured companies"
        live = CrawlSummary()
        self._launch_background_crawl(
            self.crawl(company, summary=live, ats_types=ats_types), label, live
        )

    async def start_background_url_crawl(self, url: str, name: str | None = None) -> None:
        """Kick off an ad-hoc crawl of a pasted URL for the web UI, without blocking."""
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        clean = self._validate_crawl_url(url)
        label = (name or "").strip() or derive_company_name(clean)
        live = CrawlSummary()
        self._launch_background_crawl(self.crawl_url(clean, name, summary=live), label, live)

    async def start_background_waves(self) -> None:
        """Kick off the fast→medium→slow wave pipeline for the dashboard, non-blocking.

        The shared ``crawl_progress`` carries ``current_wave`` and ``completed_waves``
        so the dashboard poller can raise a browser notification as each wave finishes
        — the fast wave's jobs are ready to apply to while the rest run behind it.
        """
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        live = CrawlSummary()
        self.crawl_progress = CrawlProgress(
            running=True,
            label="all boards in waves (fast → medium → slow)",
            started_at=utc_now().isoformat(timespec="seconds"),
            summary=live,
        )

        async def run() -> None:
            async with self._crawl_lock:
                try:
                    results = await self.crawl_waves()
                    # Surface the combined counts in the final summary line.
                    combined = CrawlSummary()
                    for res in results:
                        combined.jobs_added += res.summary.jobs_added
                        combined.jobs_updated += res.summary.jobs_updated
                        combined.jobs_closed += res.summary.jobs_closed
                        combined.companies_requested += res.summary.companies_requested
                        combined.companies_succeeded += res.summary.companies_succeeded
                        combined.errors.update(res.summary.errors)
                    self.crawl_progress.summary = combined
                except Exception as exc:
                    self.crawl_progress.error = str(exc)
                finally:
                    self.crawl_progress.running = False
                    self.crawl_progress.current_wave = ""
                    self.crawl_progress.finished_at = utc_now().isoformat(timespec="seconds")

        self._crawl_task = asyncio.create_task(run())

    def _launch_background_crawl(
        self, work: Coroutine[Any, Any, CrawlSummary], label: str, live: CrawlSummary
    ) -> None:
        # The live summary is published straight away and mutated as each company
        # finishes, so polling `/api/crawl` shows progress instead of nothing until
        # the whole run completes.
        self.crawl_progress = CrawlProgress(
            running=True,
            label=label,
            started_at=utc_now().isoformat(timespec="seconds"),
            summary=live,
        )
        self._crawl_task = asyncio.create_task(self._run_background_crawl(work))

    async def _run_background_crawl(self, work: Coroutine[Any, Any, CrawlSummary]) -> None:
        async with self._crawl_lock:
            try:
                self.crawl_progress.summary = await work
            except Exception as exc:
                self.crawl_progress.error = str(exc)
            finally:
                self.crawl_progress.running = False
                self.crawl_progress.finished_at = utc_now().isoformat(timespec="seconds")

    def search_jobs(
        self,
        *,
        search: str = "",
        company: str = "",
        location: str = "",
        status: JobStatus | None = None,
        minimum_score: int | None = None,
        max_years: float | None = None,
        min_years: float | None = None,
        seniority: str = "",
        skills_include: list[str] | None = None,
        skills_exclude: list[str] | None = None,
        since_hours: float | None = None,
        posted_days: float | None = None,
        sort: str = "newest",
        limit: int = 200,
        offset: int = 0,
        apply_preferences: bool = False,
    ) -> list[JobView]:
        prefs = self.config.preferences
        # Under preferences the minimum score is applied leniently in Python so a
        # not-yet-analysed job (NULL score) still appears in the shortlist. An
        # explicit minimum_score is applied strictly in SQL as before.
        lenient_score: int | None = None
        if apply_preferences:
            lenient_score = minimum_score if minimum_score is not None else prefs.minimum_match_score
            minimum_score = None
            if max_years is None:
                max_years = prefs.max_years
            if min_years is None:
                min_years = prefs.min_years
            if not seniority:
                seniority = prefs.seniority
            if skills_include is None:
                skills_include = prefs.skills_include
            if skills_exclude is None:
                skills_exclude = prefs.skills_exclude
        wanted_skills = skills_include or []
        unwanted_skills = skills_exclude or []
        # Preference/experience/role/skill filters run in Python, so fetch a wide set
        # and re-apply the limit afterwards. Any filter added here must be listed, or
        # it would only see the first `limit` rows and silently under-return.
        needs_python_filter = (
            max_years is not None
            or min_years is not None
            or bool(seniority)
            or bool(wanted_skills)
            or bool(unwanted_skills)
            or apply_preferences
        )
        db_limit = limit if not needs_python_filter else 5_000
        since = (
            utc_now() - timedelta(hours=since_hours) if since_hours and since_hours > 0 else None
        )
        posted_after = (
            utc_now() - timedelta(days=posted_days) if posted_days and posted_days > 0 else None
        )
        # An explicit `location` is already a narrower request, so the preferred list
        # only applies when the caller did not name a place. It is pushed into SQL
        # because `list_jobs` caps how many rows it returns, and on a corpus this size
        # that cap is reached long before the Python filters below get to run.
        preferred_places = (
            prefs.preferred_locations if apply_preferences and not location else None
        )
        jobs = self.database.list_jobs(
            search=search,
            company=company,
            location=location,
            locations=preferred_places,
            status=status,
            minimum_score=minimum_score,
            since=since,
            posted_after=posted_after,
            sort=sort,
            limit=db_limit,
            offset=offset,
        )
        if apply_preferences:
            if prefs.remote_only:
                jobs = [job for job in jobs if "remote" in job.location.casefold()]
            if prefs.roles_include or prefs.roles_exclude:
                jobs = self._filter_by_title(jobs, prefs.roles_include, prefs.roles_exclude)
            if lenient_score:
                jobs = [
                    job
                    for job in jobs
                    if job.match_score is None or job.match_score >= lenient_score
                ]
        if wanted_skills or unwanted_skills:
            # Reads the posting text, so this works before a job has been analysed.
            jobs = self._filter_by_skills(jobs, wanted_skills, unwanted_skills)
        if max_years is not None or min_years is not None or seniority:
            # Under preferences, unanalysed jobs are kept (requirement unknown until
            # analysis); an explicit flag is strict and drops them.
            jobs = self._filter_by_experience(
                jobs, max_years, min_years, seniority, lenient=apply_preferences
            )
        if needs_python_filter:
            jobs = jobs[:limit]
        return jobs

    def search_jobs_page(
        self, *, limit: int = 500, offset: int = 0, **kwargs: Any
    ) -> JobPage:
        """A page of matching jobs plus the honest total, for the paginated Jobs view.

        The Jobs list is capped per request, so the length of a page is never the true
        match count. This returns both: the page (respecting ``limit``/``offset``) and
        the total number of matches across all pages, so the UI can say "showing X of
        Y" truthfully and offer Next/Prev.

        Computing the total is filter-aware. Preference, skill and experience filters
        run in Python after the DB fetch, so when any of those apply the total is the
        length of the fully filtered set; otherwise the fast SQL ``count_jobs`` is
        used. Either way the number matches exactly what paging through would yield.
        """
        page = self.search_jobs(limit=limit, offset=offset, **kwargs)
        total = self._count_matches(**kwargs)
        return JobPage(jobs=page, total=total, offset=offset, limit=limit)

    def _count_matches(self, **kwargs: Any) -> int:
        """Total matches for a search, accounting for Python-side preference filters."""
        prefs = self.config.preferences
        apply_preferences = bool(kwargs.get("apply_preferences"))
        # These filters run in Python, not SQL, so a bare SQL count would overcount.
        python_filtered = apply_preferences or bool(
            kwargs.get("skills_include")
            or kwargs.get("skills_exclude")
            or kwargs.get("max_years") is not None
            or kwargs.get("min_years") is not None
            or kwargs.get("seniority")
        )
        if python_filtered:
            # Re-run the same pipeline without paging and count the result. Capped at
            # the same 5,000 ceiling list_jobs uses, so the number is consistent with
            # what the user can actually page to.
            filtered = self.search_jobs(limit=5_000, offset=0, **kwargs)
            return len(filtered)
        # Pure-SQL filters: count directly, which is cheap and exact.
        location = kwargs.get("location", "")
        since_hours = kwargs.get("since_hours")
        posted_days = kwargs.get("posted_days")
        preferred_places = (
            prefs.preferred_locations if apply_preferences and not location else None
        )
        return self.database.count_jobs(
            search=kwargs.get("search", ""),
            company=kwargs.get("company", ""),
            location=location,
            locations=preferred_places,
            status=kwargs.get("status"),
            minimum_score=kwargs.get("minimum_score"),
            since=(
                utc_now() - timedelta(hours=since_hours)
                if since_hours and since_hours > 0
                else None
            ),
            posted_after=(
                utc_now() - timedelta(days=posted_days)
                if posted_days and posted_days > 0
                else None
            ),
        )

    @staticmethod
    def _filter_by_title(
        jobs: list[JobView], include: list[str], exclude: list[str]
    ) -> list[JobView]:
        """Keep jobs whose title matches the role include/exclude keyword lists."""
        includes = [term.casefold() for term in include if term.strip()]
        excludes = [term.casefold() for term in exclude if term.strip()]
        result: list[JobView] = []
        for job in jobs:
            title = job.title.casefold()
            if includes and not any(term in title for term in includes):
                continue
            if any(term in title for term in excludes):
                continue
            result.append(job)
        return result

    @staticmethod
    def _filter_by_skills(
        jobs: list[JobView], include: list[str], exclude: list[str]
    ) -> list[JobView]:
        """Keep jobs that mention a wanted part of the stack and none of the unwanted.

        Include is any-of, exclude wins over include, and an empty list means "no
        opinion" — the same semantics as the title filters. Each posting's stored
        skills, title, experience line and description are searched together, so the
        filter is useful before a job has been analysed.

        Matching is token-delimited via :func:`mentioned_skills`, so excluding "java"
        keeps a JavaScript role. Note that exclusion drops a posting that merely
        mentions the term anywhere, which will also drop polyglot postings such as
        "Java or Python"; leave a language out of ``exclude`` and rely on ``include``
        if you would still take those.
        """
        includes = [term for term in include if term.strip()]
        excludes = [term for term in exclude if term.strip()]
        if not includes and not excludes:
            return jobs
        result: list[JobView] = []
        for job in jobs:
            haystack = " ".join(
                [job.title, job.experience, " ".join(job.skills), job.description]
            )
            if excludes and mentioned_skills(excludes, haystack):
                continue
            if includes and not mentioned_skills(includes, haystack):
                continue
            result.append(job)
        return result

    @staticmethod
    def _filter_by_experience(
        jobs: list[JobView],
        max_years: float | None,
        min_years: float | None,
        seniority: str,
        lenient: bool = False,
    ) -> list[JobView]:
        """Filter jobs by required years and seniority from the stored analysis.

        With ``lenient`` (the preferences path), a job that has not been analysed is
        kept, since its requirement is unknown until analysis. Without it (an explicit
        flag), an unanalysed or requirement-less job is dropped.
        """
        wanted_level = seniority.strip().casefold()
        want_years = max_years is not None or min_years is not None
        result: list[JobView] = []
        for job in jobs:
            analysis = job.analysis
            # --- Years filter (works before analysis) --------------------------
            # Prefer the analysed minimum; fall back to years parsed from the
            # crawl-time experience text, so a not-yet-analysed job is still filtered
            # when its posting states a figure.
            if want_years:
                years = job.min_years
                if years is None:
                    # Requirement unknown. Lenient keeps it (so a pre-analysis
                    # shortlist is never empty); strict drops it.
                    if not lenient:
                        continue
                else:
                    if max_years is not None and years > max_years:
                        continue
                    if min_years is not None and years < min_years:
                        continue
            # --- Seniority filter (needs the analysis) -------------------------
            if wanted_level:
                if analysis is None:
                    if not lenient:
                        continue
                elif analysis.seniority.casefold() != wanted_level and not (
                    lenient and not analysis.seniority
                ):
                    continue
            result.append(job)
        return result

    def prune_jobs(self, *, older_than_days: int, commit: bool, vacuum: bool = False) -> int:
        """Report or remove closed postings older than a cutoff to bound the database.

        With ``commit`` false this only counts what is eligible (a dry run); with it
        true the rows are deleted and, if ``vacuum`` is set, the file is compacted.
        Jobs with an application are never touched. Active jobs are never touched.
        """
        cutoff = utc_now() - timedelta(days=max(older_than_days, 0))
        if not commit:
            return self.database.count_prunable_jobs(cutoff)
        removed = self.database.prune_closed_jobs(cutoff)
        if vacuum:
            self.database.vacuum()
        return removed

    def company_shortlists(
        self,
        *,
        search: str = "",
        location: str = "",
        quota: int | None = None,
        since_hours: float | None = None,
        posted_days: float | None = None,
        include_exhausted: bool = False,
    ) -> list[CompanyShortlist]:
        """Rank relevant roles per company, capped by each company's remaining quota.

        Applying to every opening at one employer burns a limited quota on weak fits,
        so relevance is compared *within* each company and only the strongest roles
        that fit the remaining slots are put forward. Jobs already marked applied
        consume quota.
        """
        prefs = self.config.preferences
        effective_quota = quota if quota is not None else prefs.max_applications_per_company
        relevant = self.search_jobs(
            search=search,
            location=location,
            since_hours=since_hours,
            posted_days=posted_days,
            sort="score",
            limit=5_000,
            apply_preferences=True,
        )
        # Applied roles consume quota even when they no longer pass the filter.
        applied_counts: dict[str, int] = {}
        for job in self.database.list_jobs(status=JobStatus.APPLIED, limit=5_000):
            applied_counts[job.company] = applied_counts.get(job.company, 0) + 1

        grouped: dict[str, list[JobView]] = {}
        for job in relevant:
            grouped.setdefault(job.company, []).append(job)

        shortlists: list[CompanyShortlist] = []
        for company, jobs in grouped.items():
            # Highest score first; unscored jobs rank last so analysed matches lead.
            ranked = sorted(
                jobs,
                key=lambda job: (job.match_score is None, -(job.match_score or 0), job.title),
            )
            # Already-applied roles should not occupy a recommendation slot.
            candidates = [job for job in ranked if job.status is not JobStatus.APPLIED]
            applied = applied_counts.get(company, 0)
            slots_left = max(effective_quota - applied, 0)
            picks = candidates[:slots_left]
            shortlists.append(
                CompanyShortlist(
                    company=company,
                    relevant=len(candidates),
                    applied=applied,
                    quota=effective_quota,
                    slots_left=slots_left,
                    picks=picks,
                    overflow=max(len(candidates) - len(picks), 0),
                    unscored=sum(1 for job in picks if job.match_score is None),
                )
            )
        if not include_exhausted:
            shortlists = [item for item in shortlists if item.picks]
        # Strongest opportunity first, then most relevant roles available.
        shortlists.sort(key=lambda item: (-item.best_score, -item.relevant, item.company))
        return shortlists

    async def run_daily(
        self,
        *,
        search: str = "",
        location: str = "",
        since_hours: float | None = None,
        posted_days: float | None = None,
        quota: int | None = None,
        top: int = 10,
        analyse_limit: int = 40,
        do_crawl: bool = True,
        ats_types: set[str] | None = None,
    ) -> DailyRunResult:
        """One morning run: crawl, score what is new, then prepare the best packages.

        Everything up to the human review is automated, and it stops there: JobOS
        never submits an application. Work is idempotent, so a job whose package was
        already generated is reported rather than optimised again.

        ``ats_types`` limits the crawl to one speed wave (e.g. the fast
        Greenhouse/Lever/Ashby boards), so a morning run can finish quickly instead of
        waiting on the slow Workday boards. Scoring/ranking still runs over everything
        already stored, so a fast-wave crawl still surfaces older strong matches.
        """
        # Fail before any slow work if there is nothing to tailor from.
        self.require_master_resume()
        result = DailyRunResult()
        if do_crawl:
            result.crawl = await self.crawl(ats_types=ats_types)

        # Score the relevant-but-unanalysed roles so the ranking below is meaningful.
        candidates = self.search_jobs(
            search=search,
            location=location,
            since_hours=since_hours,
            posted_days=posted_days,
            sort="newest",
            limit=5_000,
            apply_preferences=True,
        )
        unanalysed = [job.id for job in candidates if job.analysis is None][:analyse_limit]
        if unanalysed:
            batch = await self.analyse_jobs(unanalysed)
            result.analysed = batch.analysed
            result.analysis_errors = batch.errors

        result.shortlists = self.company_shortlists(
            search=search,
            location=location,
            quota=quota,
            since_hours=since_hours,
            posted_days=posted_days,
        )
        # Per-company quota is already applied, so taking the strongest picks overall
        # spreads the day's effort across employers instead of exhausting one.
        picks = [job for shortlist in result.shortlists for job in shortlist.picks]
        picks.sort(key=lambda job: -(job.match_score or 0))

        for job in picks[: max(top, 0)]:
            existing = self._existing_package(job)
            if existing is not None:
                result.already_prepared.append(existing)
                continue
            try:
                outcome = await self.optimise_job(job.id)
                approved, package = await self.approve_application(outcome.application.id)
            except (ServiceError, LookupError, AIError, RenderError) as exc:
                result.preparation_errors[job.id] = str(exc)
                continue
            result.prepared.append(
                DailyPreparation(
                    job=job,
                    application_id=approved.id,
                    output_dir=str(package.directory),
                    score_before=approved.ats_before.overall_score,
                    score_after=approved.ats_after.overall_score,
                )
            )
        return result

    def _existing_package(self, job: JobView) -> DailyPreparation | None:
        """Return an already generated package for this job, so work is not repeated."""
        for application in self.database.list_applications(job_id=job.id):
            if application.status is ApplicationStatus.APPROVED and application.output_dir:
                return DailyPreparation(
                    job=job,
                    application_id=application.id,
                    output_dir=application.output_dir,
                    score_before=application.ats_before.overall_score,
                    score_after=application.ats_after.overall_score,
                )
        return None

    def companies_with_jobs(self) -> list[str]:
        return self.database.companies()

    # ----- insights / trends --------------------------------------------

    def insights_report(self) -> InsightsReport:
        """Aggregate market and personal trends from the local DB, config and registry.

        Pure, deterministic aggregation — no AI — so it is fast and reproducible, and
        doubles as the grounding data the Chat coach reads. Skill counts answer "how
        many of my jobs mention Java / Python / React?" using whole-token matching, so
        'java' never matches 'javascript'.
        """
        facts = self.database.active_job_facts()
        total_jobs = len(facts)
        now = utc_now()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        by_company: Counter[str] = Counter()
        by_location: Counter[str] = Counter()
        skill_counts: Counter[str] = Counter()
        scored = 0
        new_7d = new_30d = 0
        score_buckets = {"0-39": 0, "40-59": 0, "60-79": 0, "80-100": 0}
        day_counts: Counter[str] = Counter()

        # The skills the user tracks (from config), plus a few always-on comparators
        # so the trend answers "how many jobs are Java?" even when Java is excluded
        # from the user's own include list.
        always_show = ["python", "java", "javascript", "typescript", "react", "node.js", "aws"]
        tracked_terms = list(
            dict.fromkeys([*self.config.preferences.skills_include, *always_show])
        )
        tracked_counts: Counter[str] = Counter()

        for company, title, location, skills, score, first_seen in facts:
            by_company[company] += 1
            # Location bucketing into the cities the user cares about, else "Other".
            loc = _location_bucket(location)
            by_location[loc] += 1
            # Per-job searchable text for whole-token skill matching.
            text = f"{title} {location} {' '.join(skills)}".casefold()
            for skill in skills:
                skill_counts[canonical_skill(skill) or skill.casefold()] += 1
            for term in tracked_terms:
                if mentioned_skills([term], text):
                    tracked_counts[term] += 1
            if score is not None:
                scored += 1
                bucket = (
                    "0-39"
                    if score < 40
                    else "40-59"
                    if score < 60
                    else "60-79"
                    if score < 80
                    else "80-100"
                )
                score_buckets[bucket] += 1
            if first_seen is not None:
                seen = first_seen if first_seen.tzinfo else first_seen.replace(tzinfo=UTC)
                if seen >= week_ago:
                    new_7d += 1
                if seen >= month_ago:
                    new_30d += 1
                if seen >= now - timedelta(days=14):
                    day_counts[seen.date().isoformat()] += 1

        registry = self._registry()
        validated = len(registry.valid())
        category_counts = Counter(
            {item.category.value: item.boards for item in registry.coverage(
                set(self.config.companies)
            ).by_category}
        )

        def top(counter: Counter[str], n: int) -> list[LabelCount]:
            total = sum(counter.values()) or 1
            return [
                LabelCount(label=label, count=count, pct=round(100 * count / total, 1))
                for label, count in counter.most_common(n)
            ]

        # 14-day timeline, oldest first, zero-filled so gaps show as gaps.
        timeline = []
        for i in range(13, -1, -1):
            day = (now - timedelta(days=i)).date().isoformat()
            timeline.append(LabelCount(label=day, count=day_counts.get(day, 0)))

        tracked = [
            LabelCount(
                label=term,
                count=tracked_counts.get(term, 0),
                pct=round(100 * tracked_counts.get(term, 0) / (total_jobs or 1), 1),
            )
            for term in tracked_terms
        ]
        tracked.sort(key=lambda item: item.count, reverse=True)

        return InsightsReport(
            total_companies=len(self.config.companies),
            validated_companies=validated,
            total_jobs=total_jobs,
            scored_jobs=scored,
            new_jobs_7d=new_7d,
            new_jobs_30d=new_30d,
            by_category=[
                LabelCount(label=label, count=count)
                for label, count in category_counts.most_common()
            ],
            by_location=top(by_location, 8),
            top_companies=top(by_company, 12),
            top_skills=top(skill_counts, 15),
            tracked_skills=tracked,
            new_jobs_timeline=timeline,
            score_buckets=[
                LabelCount(label=label, count=count) for label, count in score_buckets.items()
            ],
        )

    # ----- insights Q&A (stateless, grounded in the trends) -------------

    # Clickable starters shown under the Insights "Ask about your data" box.
    INSIGHTS_SUGGESTIONS = (
        "What are the most in-demand skills in my matches?",
        "How many jobs mention Java versus Python?",
        "Which locations have the most openings for me?",
        "Which company categories should I focus on?",
        "What stands out about my job market this week?",
    )

    async def ask_insights(self, question: str) -> ChatReply:
        """Answer one question about the Insights data. Stateless and grounded.

        Unlike the Coach, this keeps no memory — it is a focused "interrogate my
        trends" box, so each question is answered fresh against the current numbers.
        It is fed only the aggregated insights snapshot and told to use nothing else;
        with no AI provider it returns the deterministic headline instead of failing.
        """
        question = question.strip()
        if not question:
            raise ServiceError("Type a question about your data.")
        if len(question) > 2_000:
            raise ServiceError("Keep the question under 2,000 characters.")
        insights = self.insights_report()
        snapshot = {
            "total_companies": insights.total_companies,
            "validated_companies": insights.validated_companies,
            "total_jobs": insights.total_jobs,
            "new_jobs_7d": insights.new_jobs_7d,
            "new_jobs_30d": insights.new_jobs_30d,
            "scored_jobs": insights.scored_jobs,
            "tech_stack_counts": [
                {"skill": s.label, "jobs": s.count} for s in insights.tracked_skills
            ],
            "top_skills": [{"skill": s.label, "jobs": s.count} for s in insights.top_skills],
            "jobs_by_location": [
                {"place": s.label, "jobs": s.count} for s in insights.by_location
            ],
            "companies_by_category": [
                {"category": s.label, "boards": s.count} for s in insights.by_category
            ],
            "top_companies": [
                {"company": s.label, "jobs": s.count} for s in insights.top_companies
            ],
            "score_buckets": [{"range": s.label, "jobs": s.count} for s in insights.score_buckets],
        }
        system = (
            "You are JobOS Insights, a precise data analyst for one job seeker. Answer "
            "ONLY from the JSON trends snapshot below, which is their real local job "
            "data. Quote the actual numbers. If the snapshot cannot answer the "
            "question, say so plainly — never invent companies, counts, or skills. Be "
            "concise and concrete.\n\n"
            f"TRENDS_SNAPSHOT (JSON, trusted facts):\n{json.dumps(snapshot, ensure_ascii=False)}"
        )
        try:
            reply = await self.ai.chat(
                system=system, messages=[{"role": "user", "content": question}]
            )
            if reply.strip():
                return ChatReply(reply=reply.strip(), used_ai=True)
        except AIError:
            pass
        fallback = self.daily_digest().headline + (
            "\n(AI is unavailable — configure a provider on the AI page to ask free-form "
            "questions. These figures are from your local data.)"
        )
        return ChatReply(reply=fallback, used_ai=False)

    # ----- coach: daily digest + grounded chat --------------------------

    def daily_digest(self) -> DailyDigest:
        """Deterministic summary of today's standing — always available, no AI.

        These numbers are computed from the local DB and are the ground truth the
        chat coach must not contradict. Top picks are the strongest-scoring fresh,
        preferred-fit roles, so the digest doubles as "what to apply to today".
        """
        now = utc_now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        facts = self.database.active_job_facts()
        total = len(facts)
        minimum = self.config.preferences.minimum_match_score
        new_today = new_7d = strong = 0
        for _company, _title, _loc, _skills, score, first_seen in facts:
            if first_seen is not None:
                seen = first_seen if first_seen.tzinfo else first_seen.replace(tzinfo=UTC)
                if seen >= start_of_day:
                    new_today += 1
                if seen >= now - timedelta(days=7):
                    new_7d += 1
            if score is not None and score >= minimum:
                strong += 1
        applications = self.database.list_applications()
        week_cutoff = now - timedelta(days=7)

        def _aware(value: datetime) -> datetime:
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        apps_7d = sum(1 for app in applications if _aware(app.created_at) >= week_cutoff)
        # Top picks: best-scoring preferred-fit roles, freshest first among ties.
        picks = self.search_jobs(
            apply_preferences=True, sort="score", limit=5
        )
        top_picks = [f"{job.title} — {job.company}" for job in picks]
        headline = (
            f"{new_today} new today, {new_7d} this week, {total} active jobs. "
            f"{strong} clear your {minimum}+ score bar; {len(applications)} applications so far."
        )
        return DailyDigest(
            new_jobs_today=new_today,
            new_jobs_7d=new_7d,
            total_active_jobs=total,
            strong_matches=strong,
            applications_total=len(applications),
            applications_7d=apps_7d,
            top_picks=top_picks,
            headline=headline,
        )

    def chat_history(self, limit: int = 40) -> list[ChatMessage]:
        """Persisted conversation, oldest first, for rendering the transcript."""
        return [
            ChatMessage(role=role, content=content)
            for role, content in self.database.recent_chat_messages(limit)
        ]

    def clear_chat(self) -> int:
        """Forget the whole conversation. Returns how many turns were removed."""
        return self.database.clear_chat_messages()

    async def coach_reply(self, message: str) -> ChatReply:
        """Answer one coach turn, grounded in the user's real data, with memory.

        The user's message and the reply are persisted so the coach remembers across
        days (enabling "how does this week compare?"). The model is always given a
        compact JSON snapshot of the real numbers (digest + insights) and is told, in
        the system prompt, to use only those facts and never invent jobs. When no AI
        provider is usable it falls back to the deterministic digest, so the page is
        useful even offline.
        """
        message = message.strip()
        if not message:
            raise ServiceError("Type a message for the coach.")
        if len(message) > 4_000:
            raise ServiceError("That message is too long; keep it under 4,000 characters.")
        self.database.add_chat_message("user", message)

        digest = self.daily_digest()
        reply_text, used_ai = await self._coach_generate(message, digest)
        self.database.add_chat_message("assistant", reply_text)
        return ChatReply(reply=reply_text, used_ai=used_ai)

    async def _coach_generate(self, message: str, digest: DailyDigest) -> tuple[str, bool]:
        # Compact grounding snapshot — small on purpose so it fits a laptop model's
        # context and cannot be used to leak the whole DB.
        insights = self.insights_report()
        grounding = {
            "today": digest.model_dump(mode="json"),
            "top_skills_in_market": [
                {"skill": s.label, "jobs": s.count} for s in insights.tracked_skills[:8]
            ],
            "jobs_by_location": [
                {"place": s.label, "jobs": s.count} for s in insights.by_location[:6]
            ],
            "companies_by_category": [
                {"category": s.label, "boards": s.count} for s in insights.by_category
            ],
        }
        system = (
            "You are JobOS Coach, a supportive, concise career companion for one job "
            "seeker. You are given a JSON snapshot of their REAL local job-search data "
            "below. Ground every claim in these numbers. Never invent companies, job "
            "titles, counts, or deadlines that are not in the data. If asked something "
            "the data cannot answer, say so briefly. Be encouraging but honest, and "
            "keep answers short and practical. You cannot take actions, only advise.\n\n"
            f"DATA_SNAPSHOT (JSON, trusted facts):\n"
            f"{json.dumps(grounding, ensure_ascii=False)}"
        )
        # Prior turns as memory (the just-added user turn is already included).
        history = [
            {"role": role, "content": content}
            for role, content in self.database.recent_chat_messages(20)
        ]
        try:
            reply = await self.ai.chat(system=system, messages=history)
            if reply.strip():
                return reply.strip(), True
        except AIError:
            pass
        # Deterministic fallback: the digest plus the day's top picks.
        lines = [digest.headline]
        if digest.top_picks:
            lines.append("Today's best-fit picks: " + "; ".join(digest.top_picks))
        lines.append(
            "(AI coach is unavailable — configure an AI provider on the AI page for "
            "conversational replies. These numbers are from your local data.)"
        )
        return "\n".join(lines), False

    def require_job(self, job_id: int) -> JobView:
        job = self.database.get_job(job_id)
        if job is None:
            raise ServiceError(f"Job {job_id} was not found.")
        return job

    def set_job_status(self, job_id: int, status: JobStatus) -> JobView:
        self.require_job(job_id)
        self.database.set_job_status(job_id, status)
        return self.require_job(job_id)

    # ----- analysis -----------------------------------------------------

    async def analyse_job(self, job_id: int) -> tuple[JobView, JobAnalysis, ATSReportData]:
        """Analyse a posting and score the master resume against it."""
        job = self.require_job(job_id)
        master = self.require_master_resume()
        analysis = await self.analyzer.analyze(job)
        job = self.database.save_job_analysis(job_id, analysis)
        report = self.ats.score(master.resume, analysis, job, resume_version_id=master.id)
        self.database.save_ats_report(job_id, master.id, report)
        return self.require_job(job_id), analysis, report

    async def analyse_jobs(self, job_ids: list[int]) -> BatchAnalysisResult:
        """Analyse and score several jobs in sequence, isolating per-job failures.

        One bad posting should not abort the batch, so each failure is recorded and
        the run continues. Scores are persisted, so `search --sort score` reflects
        them afterwards.
        """
        analysed = 0
        errors: dict[int, str] = {}
        for job_id in job_ids:
            try:
                await self.analyse_job(job_id)
                analysed += 1
            except (ServiceError, LookupError, AIError) as exc:
                errors[job_id] = str(exc)
        return BatchAnalysisResult(requested=len(job_ids), analysed=analysed, errors=errors)

    async def analysis_for(self, job_id: int) -> tuple[JobView, JobAnalysis]:
        """Reuse a stored analysis when present so repeat work avoids AI calls."""
        job = self.require_job(job_id)
        if job.analysis is not None:
            return job, job.analysis
        analysis = await self.analyzer.analyze(job)
        return self.database.save_job_analysis(job_id, analysis), analysis

    def latest_report(self, job_id: int) -> ATSReportData | None:
        return self.database.latest_ats_report(job_id)

    # ----- optimisation -------------------------------------------------

    async def optimise_job(
        self, job_id: int, *, with_cover_letter: bool = False
    ) -> OptimizationOutcome:
        master = self.require_master_resume()
        job, analysis = await self.analysis_for(job_id)
        bundle = await self.coordinator.optimize(
            master.resume, job, analysis, resume_version_id=master.id
        )
        self.database.save_ats_report(job_id, master.id, bundle.ats_after)
        cover = None
        if with_cover_letter:
            cover = await self.cover_letters.create(
                bundle.result.optimized_resume, job, analysis, bundle.ats_after
            )
        application = self.database.create_application(
            job_id=job_id,
            master_resume_version_id=master.id,
            plan=bundle.result.validated_plan,
            optimized_resume=bundle.result.optimized_resume,
            diff=bundle.result.diff,
            ats_before=bundle.ats_before,
            ats_after=bundle.ats_after,
            cover_letter=cover,
        )
        self.database.set_job_status(job_id, JobStatus.SAVED)
        return OptimizationOutcome(
            application=application,
            job=self.require_job(job_id),
            warnings=bundle.warnings,
        )

    def require_application(self, application_id: int) -> ApplicationView:
        application = self.database.get_application(application_id)
        if application is None:
            raise ServiceError(f"Application {application_id} was not found.")
        return application

    def application_resume_yaml(self, application_id: int) -> str:
        """The application's current optimized resume as editable YAML."""
        return dump_structured_resume_yaml(self.require_application(application_id).optimized_resume)

    def save_application_resume_yaml(self, application_id: int, resume_yaml: str) -> ApplicationView:
        """Validate edited YAML and store it as this draft application's optimized resume.

        These are the user's own edits, so they are schema-validated (never silently
        broken) but not re-run through the AI TruthGuard — the human is the author
        here. The master resume is untouched; only this draft's working copy changes.
        """
        try:
            resume = load_structured_resume("edited.yaml", resume_yaml.encode("utf-8"))
        except ResumeParseError as exc:
            raise ServiceError(f"Resume not saved: {exc}") from exc
        try:
            return self.database.update_optimized_resume(application_id, resume)
        except LookupError as exc:
            raise ServiceError(str(exc)) from exc
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc

    async def approve_application(self, application_id: int) -> tuple[ApplicationView, RenderedPackage]:
        """Write the immutable package only after the human approves the diff."""
        application = self.require_application(application_id)
        job = self.require_job(application.job_id)
        if application.status is ApplicationStatus.APPROVED:
            raise ServiceError(
                f"Application {application_id} was already generated in {application.output_dir}."
            )
        # Build a display-only bullet condenser for the readable one-pager. It shortens
        # over-long bullets using the stored job analysis for relevance and validates
        # every shortened bullet with the SAME TruthGuard used during optimisation, so
        # no fact can slip in. It operates on a render copy only; the stored resume and
        # master YAML are never touched. A missing analysis (unanalysed job) yields a
        # conservative empty one, so condensing still runs safely.
        analysis = job.analysis or JobAnalysis()
        condenser = BulletCondenser(self.coordinator.truth_guard, analysis)
        # Job-relevant skills, so the one-page skill budget keeps what matches this
        # posting first (prevents trimming e.g. FastAPI/RAG for an AI role just
        # because they sit late in the list). Casefolded for comparison.
        relevant_skills = {
            skill.casefold()
            for skill in (analysis.required_skills + analysis.preferred_skills + analysis.technologies)
        }
        package = await self.packages.write(
            job=job,
            resume=application.optimized_resume,
            diff=application.diff,
            report=application.ats_after,
            cover_letter=application.cover_letter,
            condense=condenser.condense,
            relevant_skills=relevant_skills,
        )
        content_hash = hashlib.sha256(
            application.optimized_resume.model_dump_json().encode("utf-8")
        ).hexdigest()
        generated = self.database.add_resume(
            application.optimized_resume,
            source_name=f"{job.company} — {job.title}",
            source_format="generated",
            original_path=str(package.files.get("resume.pdf", "")),
            source_text="",
            content_hash=content_hash,
            is_master=False,
            parent_id=application.master_resume_version_id,
            job_id=job.id,
        )
        approved = self.database.approve_application(
            application_id,
            generated_resume_version_id=generated.id,
            output_dir=str(package.directory),
        )
        return approved, package

    def artifact_path(self, application_id: int, name: str) -> Path:
        """Resolve a generated artifact, refusing anything outside its package."""
        application = self.require_application(application_id)
        if not application.output_dir:
            raise ServiceError("This application has not been generated yet.")
        base = Path(application.output_dir).resolve()
        candidate = (base / name).resolve()
        if candidate.parent != base or not candidate.is_file():
            raise ServiceError(f"Unknown artifact {name!r}.")
        return candidate

    # ----- apply --------------------------------------------------------

    def open_application_page(self, job_id: int) -> str:
        """Open the employer's own page. JobOS never submits on your behalf."""
        job = self.require_job(job_id)
        url = open_apply_page(job.apply_url)
        self.database.set_job_status(job_id, JobStatus.OPENED)
        return url

    def mark_applied(self, job_id: int) -> JobView:
        return self.set_job_status(job_id, JobStatus.APPLIED)

    # ----- diagnostics --------------------------------------------------

    async def health(self) -> HealthReport:
        checks: list[HealthItem] = []
        checks.append(
            HealthItem(
                name="config",
                ok=True,
                detail=f"{self.config.config_path} with {len(self.config.companies)} companies",
            )
        )
        checks.append(
            HealthItem(
                name="database",
                ok=self.config.database_path.exists(),
                detail=str(self.config.database_path),
            )
        )
        master = self.master_resume()
        checks.append(
            HealthItem(
                name="master resume",
                ok=master is not None,
                detail=(
                    f"version {master.id} from {master.source_name}"
                    if master
                    else "none uploaded yet"
                ),
            )
        )
        ai_ok, ai_detail = await self.ai.health()
        checks.append(
            HealthItem(name=f"ai ({self.config.ai.provider})", ok=ai_ok, detail=ai_detail)
        )
        chromium_ok, chromium_detail = _chromium_status()
        checks.append(HealthItem(name="chromium (pdf)", ok=chromium_ok, detail=chromium_detail))
        checks.append(
            HealthItem(
                name="output directory",
                ok=self.config.output_dir.is_dir(),
                detail=str(self.config.output_dir),
            )
        )
        jobs = self.database.count_jobs()
        checks.append(HealthItem(name="jobs stored", ok=True, detail=str(jobs)))
        return HealthReport(ok=all(check.ok for check in checks), checks=checks)


def _chromium_status() -> tuple[bool, str]:
    """Report whether Playwright's Chromium is installed, without launching it."""
    if shutil.which("chromium") or shutil.which("chrome"):
        return True, "a system Chromium/Chrome is on PATH"
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not cache.exists():
        cache = Path.home() / ".cache" / "ms-playwright"
    if cache.exists():
        builds = sorted(
            path.name
            for path in cache.iterdir()
            if path.is_dir() and path.name.startswith("chromium")
        )
        if builds:
            return True, f"playwright build {builds[-1]}"
    return False, "not installed; run `uv run playwright install chromium`"
