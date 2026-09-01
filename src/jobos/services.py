"""Application use cases shared by the web UI and the CLI."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

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
from jobos.ats import mentioned_skills
from jobos.config import AppConfig, CompanySettings, append_company, slugify_company
from jobos.crawler import CareerCrawler, derive_company_name
from jobos.database import Database
from jobos.models import (
    ApplicationStatus,
    ApplicationView,
    ATSReportData,
    CrawlSummary,
    HealthItem,
    HealthReport,
    JobAnalysis,
    JobStatus,
    JobView,
    ResumeImportResult,
    ResumeVersionView,
    utc_now,
    validate_web_url,
)
from jobos.renderer import (
    PackageWriter,
    RenderedPackage,
    RenderError,
    ResumeRenderer,
    open_apply_page,
)
from jobos.resume import ResumeImporter


class ServiceError(RuntimeError):
    """A use case could not complete; the message is safe to show the user."""


@dataclass(slots=True)
class CrawlProgress:
    """Status of the most recent background crawl, for the web UI."""

    running: bool = False
    label: str = ""
    started_at: str = ""
    finished_at: str = ""
    summary: CrawlSummary | None = None
    error: str = ""


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
        self, company: str | None = None, *, summary: CrawlSummary | None = None
    ) -> CrawlSummary:
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        crawler = CareerCrawler(self.config, self.database)
        return await crawler.crawl(company, summary=summary)

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

    @staticmethod
    def _validate_crawl_url(url: str) -> str:
        try:
            return validate_web_url(url.strip())
        except ValueError as exc:
            raise ServiceError(f"That does not look like a web address: {exc}") from exc

    async def start_background_crawl(self, company: str | None = None) -> None:
        """Kick off a crawl of configured companies for the web UI, without blocking."""
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        label = company or f"{len(self.config.companies)} configured companies"
        live = CrawlSummary()
        self._launch_background_crawl(self.crawl(company, summary=live), label, live)

    async def start_background_url_crawl(self, url: str, name: str | None = None) -> None:
        """Kick off an ad-hoc crawl of a pasted URL for the web UI, without blocking."""
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        clean = self._validate_crawl_url(url)
        label = (name or "").strip() or derive_company_name(clean)
        live = CrawlSummary()
        self._launch_background_crawl(self.crawl_url(clean, name, summary=live), label, live)

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
        result: list[JobView] = []
        for job in jobs:
            analysis = job.analysis
            if analysis is None:
                if lenient:
                    result.append(job)
                continue
            if max_years is not None or min_years is not None:
                years = analysis.minimum_years
                if years is None:
                    if lenient:
                        result.append(job)
                    continue
                if max_years is not None and years > max_years:
                    continue
                if min_years is not None and years < min_years:
                    continue
            if wanted_level and analysis.seniority.casefold() != wanted_level:
                if lenient and not analysis.seniority:
                    result.append(job)
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
    ) -> DailyRunResult:
        """One morning run: crawl, score what is new, then prepare the best packages.

        Everything up to the human review is automated, and it stops there: JobOS
        never submits an application. Work is idempotent, so a job whose package was
        already generated is reported rather than optimised again.
        """
        # Fail before any slow work if there is nothing to tailor from.
        self.require_master_resume()
        result = DailyRunResult()
        if do_crawl:
            result.crawl = await self.crawl()

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

    async def approve_application(self, application_id: int) -> tuple[ApplicationView, RenderedPackage]:
        """Write the immutable package only after the human approves the diff."""
        application = self.require_application(application_id)
        job = self.require_job(application.job_id)
        if application.status is ApplicationStatus.APPROVED:
            raise ServiceError(
                f"Application {application_id} was already generated in {application.output_dir}."
            )
        package = await self.packages.write(
            job=job,
            resume=application.optimized_resume,
            diff=application.diff,
            report=application.ats_after,
            cover_letter=application.cover_letter,
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
        jobs = len(self.database.list_jobs(limit=5_000))
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
