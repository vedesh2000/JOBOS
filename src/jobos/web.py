"""FastAPI web UI and JSON API for the local JobOS instance."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from jobos.config import AppConfig, load_config
from jobos.models import (
    ApplicationView,
    ATSReportData,
    HealthReport,
    JobStatus,
    JobView,
    ResumeVersionView,
    StructuredResume,
)
from jobos.renderer import RenderError, resume_to_markdown
from jobos.resume import ResumeParseError
from jobos.services import JobOSService, ServiceError

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
ARTIFACT_MEDIA_TYPES = {
    "resume.pdf": "application/pdf",
    "resume.html": "text/html; charset=utf-8",
    "resume.md": "text/markdown; charset=utf-8",
    "resume.json": "application/json",
    "resume.diff.md": "text/markdown; charset=utf-8",
    "ats_report.md": "text/markdown; charset=utf-8",
    "cover_letter.md": "text/markdown; charset=utf-8",
    "job.json": "application/json",
}
SORT_OPTIONS = [
    ("newest", "Newest first"),
    ("oldest", "Oldest first"),
    ("score", "Match score"),
    ("company", "Company"),
    ("title", "Role"),
]


def score_band(score: int | None) -> str:
    """Map a score to a CSS class. Bands are presentation guidance only."""
    if score is None:
        return "unknown"
    if score >= 80:
        return "strong"
    if score >= 65:
        return "good"
    if score >= 45:
        return "partial"
    return "weak"


def create_app(config: AppConfig | None = None, service: JobOSService | None = None) -> FastAPI:
    """Build the local app. Bound to loopback by default and intentionally unauthenticated."""
    resolved_config = config or (service.config if service else load_config())
    app = FastAPI(
        title="JobOS",
        version="0.1.0",
        description=(
            "Local single-user job application assistant. Prepares application packages; "
            "never submits them."
        ),
    )
    app.state.service = service or JobOSService(resolved_config)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    templates.env.filters["score_band"] = score_band
    app.state.templates = templates

    def get_service(request: Request) -> JobOSService:
        result: JobOSService = request.app.state.service
        return result

    def render(
        request: Request, template: str, context: dict[str, Any], status_code: int = 200
    ) -> HTMLResponse:
        payload = {
            "request": request,
            "notice": request.query_params.get("notice", ""),
            "error": request.query_params.get("error", ""),
            **context,
        }
        return templates.TemplateResponse(request, template, payload, status_code=status_code)

    def back(url: str, *, notice: str = "", error: str = "") -> RedirectResponse:
        separator = "&" if "?" in url else "?"
        if notice:
            url = f"{url}{separator}notice={_quote(notice)}"
        elif error:
            url = f"{url}{separator}error={_quote(error)}"
        return RedirectResponse(url, status_code=303)

    @app.middleware("http")
    async def block_cross_site_writes(request: Request, call_next: Any) -> Response:
        """Stop other sites from POSTing to this local server from your browser."""
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin:
                expected = request.headers.get("host", "")
                if urlsplit(origin).netloc != expected:
                    return JSONResponse(
                        {"detail": "cross-site request blocked"}, status_code=403
                    )
        response: Response = await call_next(request)
        return response

    # ----- pages --------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(
        request: Request,
        q: str = "",
        company: str = "",
        location: str = "",
        status: str = "",
        min_score: str = "",
        max_years: str = "",
        skills: str = "",
        exclude_skills: str = "",
        preferred: str = "",
        sort: str = "newest",
    ) -> Response:
        service = get_service(request)
        if service.master_resume() is None:
            return back("/resume", notice="Upload your master resume to get started.")
        job_status = JobStatus(status) if status in set(JobStatus) else None
        minimum = int(min_score) if min_score.strip().isdigit() else None
        # A bare visit to "/" shows the curated set (your config preferences); once the
        # filter form is used, the checkbox decides. An unchecked box submits nothing,
        # so a submitted form without it means the user turned preferences off.
        submitted = bool(request.query_params)
        apply_preferences = (preferred == "on") if submitted else True
        jobs = service.search_jobs(
            search=q.strip(),
            company=company.strip(),
            location=location.strip(),
            status=job_status,
            minimum_score=minimum,
            max_years=_as_float(max_years),
            skills_include=_csv_terms(skills),
            skills_exclude=_csv_terms(exclude_skills),
            sort=sort if sort in dict(SORT_OPTIONS) else "newest",
            apply_preferences=apply_preferences,
        )
        return render(
            request,
            "dashboard.html.j2",
            {
                "active": "jobs",
                "jobs": jobs,
                "total": len(jobs),
                "scored": sum(1 for job in jobs if job.match_score is not None),
                "companies": service.companies_with_jobs(),
                "configured": service.config.companies,
                "config_path": service.config.config_path,
                "crawl": service.crawl_progress,
                "min_score": service.config.preferences.minimum_match_score,
                "statuses": [item.value for item in JobStatus],
                "sorts": SORT_OPTIONS,
                "preferred": apply_preferences,
                "query": {
                    "q": q,
                    "company": company,
                    "location": location,
                    "status": status,
                    "min_score": min_score,
                    "max_years": max_years,
                    "skills": skills,
                    "exclude_skills": exclude_skills,
                    "preferred": apply_preferences,
                    "sort": sort,
                },
            },
        )

    @app.get("/resume", response_class=HTMLResponse, include_in_schema=False)
    async def resume_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        return render(
            request,
            "resume_page.html.j2",
            {
                "active": "resume",
                "master": service.master_resume(),
                "versions": service.resume_versions(),
                "warnings": [],
            },
        )

    @app.post("/resume/upload", include_in_schema=False)
    async def upload_resume(request: Request, file: UploadFile) -> Response:
        service = get_service(request)
        try:
            content = await file.read()
            result = await service.import_resume(file.filename or "resume", content)
        except (ResumeParseError, ServiceError) as exc:
            return back("/resume", error=str(exc))
        if result.warnings:
            return render(
                request,
                "resume_page.html.j2",
                {
                    "active": "resume",
                    "master": service.master_resume(),
                    "versions": service.resume_versions(),
                    "warnings": result.warnings,
                    "notice": f"Imported as version {result.version_id}.",
                },
            )
        return back("/resume", notice=f"Imported as version {result.version_id}.")

    @app.get("/resume/{version_id}.json", include_in_schema=False)
    async def resume_json(request: Request, version_id: int) -> Response:
        service = get_service(request)
        version = service.database.get_resume(version_id)
        if version is None:
            return JSONResponse({"detail": "resume version not found"}, status_code=404)
        return Response(
            version.resume.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'inline; filename="resume-v{version_id}.json"'},
        )

    @app.get("/resume/{version_id}.html", include_in_schema=False)
    async def resume_preview(request: Request, version_id: int) -> Response:
        """Render a stored version with the configured theme, for viewing in the browser.

        HTML rather than PDF so it is instant: the PDF path has to start Chromium.
        """
        service = get_service(request)
        version = service.database.get_resume(version_id)
        if version is None:
            return JSONResponse({"detail": "resume version not found"}, status_code=404)
        try:
            return HTMLResponse(service.renderer.render_html(version.resume))
        except RenderError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.get("/resume/{version_id}.pdf", include_in_schema=False)
    async def resume_pdf(request: Request, version_id: int) -> Response:
        """Render a stored version to PDF on demand. Slower: this starts Chromium."""
        service = get_service(request)
        version = service.database.get_resume(version_id)
        if version is None:
            return JSONResponse({"detail": "resume version not found"}, status_code=404)
        try:
            html = service.renderer.render_html(version.resume)
            pdf = await service.renderer.render_pdf(html)
        except RenderError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return Response(
            pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="resume-v{version_id}.pdf"'},
        )

    @app.post("/crawl", include_in_schema=False)
    async def start_crawl(
        request: Request,
        company: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
    ) -> Response:
        service = get_service(request)
        # The dashboard JS posts with Accept: application/json so it can stay on the
        # page and poll for progress. A plain form post (no JS) still redirects back.
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            if url.strip():
                # Ad-hoc: crawl any career page the user pastes, not just configured ones.
                await service.start_background_url_crawl(url.strip())
            else:
                await service.start_background_crawl(company.strip() or None)
        except ServiceError as exc:
            # 409 only when a crawl is already running; other issues (bad URL, no
            # companies) are the caller's input, so 400.
            status = 409 if "already running" in str(exc).casefold() else 400
            if wants_json:
                return JSONResponse({"detail": str(exc)}, status_code=status)
            return back("/", error=str(exc))
        if wants_json:
            return JSONResponse(_crawl_status(service), status_code=202)
        return back("/", notice="Crawl started. Reload in a moment to see new jobs.")

    @app.post("/companies", include_in_schema=False)
    async def add_company(
        request: Request,
        url: Annotated[str, Form()],
        name: Annotated[str, Form()] = "",
        crawl: Annotated[str, Form()] = "",
    ) -> Response:
        """Save a company to config.yaml, optionally crawling it right away."""
        service = get_service(request)
        try:
            slug = service.add_company(url, name or None)
        except ServiceError as exc:
            return back("/", error=str(exc))
        added = service.config.companies[slug].name
        if crawl == "1":
            try:
                await service.start_background_crawl(slug)
            except ServiceError as exc:
                return back("/", error=f"Added {added}, but the crawl did not start: {exc}")
            return back("/", notice=f"Added {added} to your config and started a crawl.")
        return back("/", notice=f"Added {added} to your config as '{slug}'.")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
    async def job_detail(request: Request, job_id: int) -> HTMLResponse:
        service = get_service(request)
        try:
            job = service.require_job(job_id)
        except ServiceError as exc:
            return render(
                request, "dashboard.html.j2", _empty_dashboard(service, str(exc)), status_code=404
            )
        return render(
            request,
            "job_detail.html.j2",
            {
                "active": "jobs",
                "job": job,
                "analysis": job.analysis,
                "report": service.latest_report(job_id),
                "applications": service.database.list_applications(job_id=job_id),
            },
        )

    @app.post("/jobs/{job_id}/analyse", include_in_schema=False)
    async def analyse(request: Request, job_id: int) -> Response:
        service = get_service(request)
        try:
            _, _, report = await service.analyse_job(job_id)
        except ServiceError as exc:
            return back(f"/jobs/{job_id}", error=str(exc))
        return back(f"/jobs/{job_id}", notice=f"Scored {report.overall_score}/100.")

    @app.post("/jobs/{job_id}/optimise", include_in_schema=False)
    async def optimise(
        request: Request, job_id: int, cover_letter: Annotated[str, Form()] = "0"
    ) -> Response:
        service = get_service(request)
        try:
            outcome = await service.optimise_job(
                job_id, with_cover_letter=cover_letter == "1"
            )
        except ServiceError as exc:
            return back(f"/jobs/{job_id}", error=str(exc))
        return back(f"/applications/{outcome.application.id}", notice="Review the changes below.")

    @app.post("/jobs/{job_id}/apply", include_in_schema=False)
    async def apply(request: Request, job_id: int) -> Response:
        service = get_service(request)
        try:
            url = service.open_application_page(job_id)
        except (ServiceError, RenderError) as exc:
            return back(f"/jobs/{job_id}", error=str(exc))
        return back(f"/jobs/{job_id}", notice=f"Opened {url} in your browser.")

    @app.post("/jobs/{job_id}/status", include_in_schema=False)
    async def set_status(request: Request, job_id: int, status: Annotated[str, Form()]) -> Response:
        service = get_service(request)
        if status not in set(JobStatus):
            return back(f"/jobs/{job_id}", error=f"Unknown status {status!r}.")
        try:
            service.set_job_status(job_id, JobStatus(status))
        except ServiceError as exc:
            return back(f"/jobs/{job_id}", error=str(exc))
        return back(f"/jobs/{job_id}", notice=f"Marked {status}.")

    @app.get(
        "/applications/{application_id}", response_class=HTMLResponse, include_in_schema=False
    )
    async def review(request: Request, application_id: int) -> HTMLResponse:
        service = get_service(request)
        try:
            application = service.require_application(application_id)
            job = service.require_job(application.job_id)
        except ServiceError as exc:
            return render(
                request, "dashboard.html.j2", _empty_dashboard(service, str(exc)), status_code=404
            )
        artifacts = []
        if application.output_dir:
            directory = Path(application.output_dir)
            artifacts = [name for name in ARTIFACT_MEDIA_TYPES if (directory / name).is_file()]
        return render(
            request,
            "review.html.j2",
            {
                "active": "jobs",
                "application": application,
                "job": job,
                "diff": application.diff,
                "artifacts": artifacts,
                "warnings": [],
                "preview": resume_to_markdown(application.optimized_resume),
            },
        )

    @app.post("/applications/{application_id}/approve", include_in_schema=False)
    async def approve(request: Request, application_id: int) -> Response:
        service = get_service(request)
        try:
            _, package = await service.approve_application(application_id)
        except (ServiceError, RenderError) as exc:
            return back(f"/applications/{application_id}", error=str(exc))
        return back(
            f"/applications/{application_id}",
            notice=f"Generated version {package.version} in {package.directory}.",
        )

    @app.get("/applications/{application_id}/files/{name}", include_in_schema=False)
    async def artifact(
        request: Request, application_id: int, name: str, download: bool = False
    ) -> Response:
        service = get_service(request)
        if name not in ARTIFACT_MEDIA_TYPES:
            return JSONResponse({"detail": "unknown artifact"}, status_code=404)
        try:
            path = service.artifact_path(application_id, name)
        except ServiceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        # Passing `filename` makes Starlette send Content-Disposition: attachment, which
        # forces a download and stops the browser previewing the PDF. Default to inline
        # so it opens in the viewer, and only attach when ?download=1 is asked for.
        if download:
            return FileResponse(path, media_type=ARTIFACT_MEDIA_TYPES[name], filename=name)
        return FileResponse(
            path,
            media_type=ARTIFACT_MEDIA_TYPES[name],
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history(request: Request) -> HTMLResponse:
        service = get_service(request)
        applications = service.database.list_applications()
        rows = [
            {"application": application, "job": service.database.get_job(application.job_id)}
            for application in applications
        ]
        return render(
            request,
            "history.html.j2",
            {
                "active": "history",
                "rows": rows,
                "generated": sum(1 for item in applications if item.output_dir),
                "resume_versions": service.resume_versions(),
            },
        )

    @app.get("/readme", response_class=HTMLResponse, include_in_schema=False)
    async def readme(request: Request) -> HTMLResponse:
        """Render the project README so the docs are reachable without leaving the app."""
        document = _find_readme()
        if document is None:
            return render(
                request,
                "readme.html.j2",
                {"active": "readme", "body": "", "missing": True},
            )
        # html=False keeps raw HTML in the source escaped rather than injected.
        parser = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
        return render(
            request,
            "readme.html.j2",
            {
                "active": "readme",
                "body": parser.render(document.read_text(encoding="utf-8")),
                "missing": False,
            },
        )

    @app.get("/doctor", response_class=HTMLResponse, include_in_schema=False)
    async def doctor(request: Request) -> HTMLResponse:
        service = get_service(request)
        return render(
            request,
            "doctor.html.j2",
            {"active": "doctor", "health": await service.health(), "config": service.config},
        )

    # ----- JSON API -----------------------------------------------------

    @app.get("/api/health", tags=["system"])
    async def api_health(request: Request) -> HealthReport:
        return await get_service(request).health()

    @app.get("/api/crawl", tags=["system"])
    async def api_crawl_status(request: Request) -> dict[str, Any]:
        """Current background-crawl status, polled by the dashboard spinner."""
        return _crawl_status(get_service(request))

    @app.get("/api/jobs", tags=["jobs"])
    async def api_jobs(
        request: Request,
        q: str = "",
        company: str = "",
        location: str = "",
        status: JobStatus | None = None,
        min_score: int | None = None,
        max_years: float | None = None,
        skill: Annotated[list[str] | None, Query()] = None,
        exclude_skill: Annotated[list[str] | None, Query()] = None,
        preferred: bool = False,
        sort: str = "newest",
        limit: int = 200,
        offset: int = 0,
    ) -> list[JobView]:
        return get_service(request).search_jobs(
            search=q,
            company=company,
            location=location,
            status=status,
            minimum_score=min_score,
            max_years=max_years,
            skills_include=skill,
            skills_exclude=exclude_skill,
            sort=sort,
            limit=limit,
            offset=offset,
            apply_preferences=preferred,
        )

    @app.get("/api/jobs/{job_id}", tags=["jobs"])
    async def api_job(request: Request, job_id: int) -> JobView:
        return get_service(request).require_job(job_id)

    @app.post("/api/jobs/{job_id}/analyse", tags=["jobs"])
    async def api_analyse(request: Request, job_id: int) -> ATSReportData:
        _, _, report = await get_service(request).analyse_job(job_id)
        return report

    @app.get("/api/jobs/{job_id}/report", tags=["jobs"])
    async def api_report(request: Request, job_id: int) -> ATSReportData:
        report = get_service(request).latest_report(job_id)
        if report is None:
            raise ServiceError(f"Job {job_id} has not been scored yet.")
        return report

    @app.post("/api/jobs/{job_id}/optimise", tags=["applications"])
    async def api_optimise(
        request: Request, job_id: int, cover_letter: bool = False
    ) -> ApplicationView:
        outcome = await get_service(request).optimise_job(
            job_id, with_cover_letter=cover_letter
        )
        return outcome.application

    @app.get("/api/applications", tags=["applications"])
    async def api_applications(request: Request, job_id: int | None = None) -> list[ApplicationView]:
        return get_service(request).database.list_applications(job_id=job_id)

    @app.get("/api/applications/{application_id}", tags=["applications"])
    async def api_application(request: Request, application_id: int) -> ApplicationView:
        return get_service(request).require_application(application_id)

    @app.post("/api/applications/{application_id}/approve", tags=["applications"])
    async def api_approve(request: Request, application_id: int) -> ApplicationView:
        approved, _ = await get_service(request).approve_application(application_id)
        return approved

    @app.get("/api/resume", tags=["resume"])
    async def api_resume(request: Request) -> StructuredResume:
        return get_service(request).require_master_resume().resume

    @app.get("/api/resume/versions", tags=["resume"])
    async def api_resume_versions(request: Request) -> list[ResumeVersionView]:
        return get_service(request).resume_versions()

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> Response:
        report = await get_service(request).health()
        return JSONResponse(
            {"ok": report.ok}, status_code=200 if report.ok else 503
        )

    # ----- errors -------------------------------------------------------

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: Exception) -> Response:
        message = str(exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=400)
        return back("/", error=message)

    @app.exception_handler(RenderError)
    async def render_error_handler(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=500)
        return back("/", error=str(exc))

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn jobos.web:app_factory --factory`, which enables reload."""
    return create_app()


def _empty_dashboard(service: JobOSService, error: str) -> dict[str, Any]:
    return {
        "active": "jobs",
        "jobs": [],
        "total": 0,
        "scored": 0,
        "companies": service.companies_with_jobs(),
        "configured": service.config.companies,
        "config_path": service.config.config_path,
        "crawl": service.crawl_progress,
        "min_score": service.config.preferences.minimum_match_score,
        "statuses": [item.value for item in JobStatus],
        "sorts": SORT_OPTIONS,
        "preferred": True,
        "query": {
            "q": "",
            "company": "",
            "location": "",
            "status": "",
            "min_score": "",
            "max_years": "",
            "skills": "",
            "exclude_skills": "",
            "preferred": True,
            "sort": "newest",
        },
        "error": error,
    }


def _find_readme() -> Path | None:
    """Locate README.md, whether running from a checkout or an installed package."""
    for candidate in (
        Path(__file__).resolve().parents[2] / "README.md",  # src/jobos -> repo root
        Path.cwd() / "README.md",
    ):
        if candidate.is_file():
            return candidate
    return None


def _crawl_status(service: JobOSService) -> dict[str, Any]:
    """Serialise the background-crawl progress for the status endpoint and the poller."""
    progress = service.crawl_progress
    return {
        "running": progress.running,
        "label": progress.label,
        "started_at": progress.started_at,
        "finished_at": progress.finished_at,
        "error": progress.error,
        "summary": progress.summary.model_dump(mode="json") if progress.summary else None,
    }


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value[:300], safe="")


def _csv_terms(value: str) -> list[str] | None:
    """Split a comma-separated filter field into terms; None when nothing was typed.

    None (not an empty list) so the service falls back to config preferences instead
    of treating the field as "match no skills".
    """
    terms = [term.strip() for term in value.split(",") if term.strip()]
    return terms or None


def _as_float(value: str) -> float | None:
    """Parse a numeric filter field, tolerating blanks and junk from the form."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
