"""FastAPI web UI and JSON API for the local JobOS instance."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
        sort: str = "newest",
    ) -> Response:
        service = get_service(request)
        if service.master_resume() is None:
            return back("/resume", notice="Upload your master resume to get started.")
        job_status = JobStatus(status) if status in set(JobStatus) else None
        minimum = int(min_score) if min_score.strip().isdigit() else None
        jobs = service.search_jobs(
            search=q.strip(),
            company=company.strip(),
            location=location.strip(),
            status=job_status,
            minimum_score=minimum,
            sort=sort if sort in dict(SORT_OPTIONS) else "newest",
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
                "query": {
                    "q": q,
                    "company": company,
                    "location": location,
                    "status": status,
                    "min_score": min_score,
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

    @app.post("/crawl", include_in_schema=False)
    async def start_crawl(request: Request, company: Annotated[str, Form()] = "") -> Response:
        service = get_service(request)
        try:
            await service.start_background_crawl(company.strip() or None)
        except ServiceError as exc:
            return back("/", error=str(exc))
        return back("/", notice="Crawl started. Reload in a moment to see new jobs.")

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
    async def artifact(request: Request, application_id: int, name: str) -> Response:
        service = get_service(request)
        if name not in ARTIFACT_MEDIA_TYPES:
            return JSONResponse({"detail": "unknown artifact"}, status_code=404)
        try:
            path = service.artifact_path(application_id, name)
        except ServiceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=404)
        return FileResponse(path, media_type=ARTIFACT_MEDIA_TYPES[name], filename=name)

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

    @app.get("/api/jobs", tags=["jobs"])
    async def api_jobs(
        request: Request,
        q: str = "",
        company: str = "",
        location: str = "",
        status: JobStatus | None = None,
        min_score: int | None = None,
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
            sort=sort,
            limit=limit,
            offset=offset,
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
        "query": {
            "q": "",
            "company": "",
            "location": "",
            "status": "",
            "min_score": "",
            "sort": "newest",
        },
        "error": error,
    }


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value[:300], safe="")
