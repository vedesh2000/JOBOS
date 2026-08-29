"""Application use cases shared by the web UI and the CLI."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

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
from jobos.config import AppConfig
from jobos.crawler import CareerCrawler
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
)
from jobos.renderer import PackageWriter, RenderedPackage, ResumeRenderer, open_apply_page
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

    async def crawl(self, company: str | None = None) -> CrawlSummary:
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        crawler = CareerCrawler(self.config, self.database)
        return await crawler.crawl(company)

    async def start_background_crawl(self, company: str | None = None) -> None:
        """Kick off a crawl for the web UI without holding the HTTP request open."""
        if self.crawl_progress.running:
            raise ServiceError("A crawl is already running.")
        if not self.config.companies:
            raise ServiceError(
                "No companies configured. Add entries under `companies:` in config.yaml."
            )
        self.crawl_progress = CrawlProgress(
            running=True,
            label=company or f"{len(self.config.companies)} configured companies",
            started_at=utc_now().isoformat(timespec="seconds"),
        )
        self._crawl_task = asyncio.create_task(self._run_background_crawl(company))

    async def _run_background_crawl(self, company: str | None) -> None:
        async with self._crawl_lock:
            try:
                summary = await self.crawl(company)
                self.crawl_progress.summary = summary
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
        # Preference/experience/role filters run in Python, so fetch a wide set and
        # re-apply the limit afterwards.
        needs_python_filter = (
            max_years is not None
            or min_years is not None
            or bool(seniority)
            or apply_preferences
        )
        db_limit = limit if not needs_python_filter else 5_000
        jobs = self.database.list_jobs(
            search=search,
            company=company,
            location=location,
            status=status,
            minimum_score=minimum_score,
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
