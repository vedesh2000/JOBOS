"""FastAPI web UI and JSON API for the local JobOS instance."""

from __future__ import annotations

import re
from datetime import UTC, datetime
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
from jobos.resume import (
    ResumeParseError,
    dump_structured_resume_yaml,
    load_structured_resume,
)
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

# Crawl speed "waves" surfaced in the Jobs page dropdown, mirroring the CLI --ats.
ATS_WAVES: dict[str, set[str]] = {
    "fast": {"greenhouse", "lever", "ashby"},
    "medium": {"smartrecruiters", "oracle"},
    "slow": {"workday"},
}
WAVE_OPTIONS = [
    ("", "All boards"),
    ("fast", "Fast (Greenhouse / Lever / Ashby)"),
    ("medium", "Medium (SmartRecruiters / Oracle)"),
    ("slow", "Slow (Workday)"),
]
# "New within" dropdown (hours since JobOS first saw a posting) — the daily-ritual filter.
NEW_WITHIN_OPTIONS = [
    ("", "Any time"),
    ("24", "New today"),
    ("72", "Last 3 days"),
    ("168", "This week"),
]
# How many jobs one Jobs-page request renders. Raised from the old 200 so a daily
# applicant's shortlist is rarely clipped, while still bounding DOM size; anything
# beyond this is reachable via Next/Prev paging.
JOBS_PAGE_SIZE = 500


def _resolve_ats_wave(value: str) -> set[str] | None:
    """Turn a wave name into a set of ATS names, or None for all. Unknown = all."""
    return set(ATS_WAVES[value]) if value in ATS_WAVES else None


def posted_age_days(value: datetime | None) -> float:
    """Age of a posting in days; a large number when unknown, so it never reads fresh."""
    if value is None:
        return 10_000.0
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - moment).total_seconds() / 86_400, 0.0)


def posted_age(value: datetime | None) -> str:
    """Human-readable posting age, e.g. "today", "3d ago", "5w ago"."""
    if value is None:
        return "unknown"
    days = posted_age_days(value)
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)}d ago"
    if days < 60:
        return f"{int(days / 7)}w ago"
    return f"{int(days / 30)}mo ago"


def resume_filename_stem(resume: StructuredResume) -> str:
    """A stable, human download name from the contact name, e.g. "vedesh_k_v_s".

    Falls back to "resume" when the name has no usable characters, so a download
    header is always well-formed.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", resume.contact.name.casefold()).strip("_")
    return slug or "resume"


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
    templates.env.filters["posted_age"] = posted_age
    templates.env.filters["posted_age_days"] = posted_age_days
    app.state.templates = templates

    # Cache-busting version for static assets: the newest mtime across app.css/app.js.
    # Appended as ?v=... so the browser refetches whenever either file changes, which
    # avoids the classic "stale CSS/JS after an update" problem.
    def _asset_version() -> str:
        try:
            mtimes = [
                (STATIC_DIR / name).stat().st_mtime
                for name in ("app.css", "app.js")
                if (STATIC_DIR / name).exists()
            ]
            return str(int(max(mtimes))) if mtimes else "1"
        except OSError:
            return "1"

    asset_version = _asset_version()

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
            "asset_version": asset_version,
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

    @app.get("/home", response_class=HTMLResponse, include_in_schema=False)
    async def home(request: Request) -> HTMLResponse:
        """A plain-language overview of what JobOS does and how to use it.

        This is the brand/landing page, aimed at a newcomer (and at an open-source
        visitor) who needs to understand the flow before touching any tab. It reads
        live state so it can point to the right next step.
        """
        service = get_service(request)
        service.reload_config_if_changed()
        master = service.master_resume()
        return render(
            request,
            "home.html.j2",
            {
                "active": "home",
                "has_resume": master is not None,
                "company_count": len(service.config.companies),
                "job_count": service.database.count_jobs(),
                "ai_provider": service.config.ai.provider,
                "ai_model": service.config.ai.model,
            },
        )

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
        posted_days: str = "",
        new_within: str = "",
        preferred: str = "",
        sort: str = "newest",
        page: str = "1",
    ) -> Response:
        service = get_service(request)
        service.reload_config_if_changed()
        # Browsing and crawling jobs do not need a resume — only scoring/tailoring do.
        # So the Jobs page is open without one; it shows a gentle prompt instead of a
        # hard redirect, letting a new user crawl and explore before uploading.
        has_resume = service.master_resume() is not None
        job_status = JobStatus(status) if status in set(JobStatus) else None
        minimum = int(min_score) if min_score.strip().isdigit() else None
        # "New within" (hours since JobOS first saw the posting) powers the daily
        # ritual: 24 = new today, 168 = this week. Empty = any time.
        since_hours = _as_float(new_within) if new_within.strip() else None
        # A bare visit to "/" shows the curated set (your config preferences); once the
        # filter form is used, the checkbox decides. An unchecked box submits nothing,
        # so a submitted form without it means the user turned preferences off.
        submitted = bool(request.query_params)
        apply_preferences = (preferred == "on") if submitted else True
        page_number = int(page) if page.strip().isdigit() and int(page) >= 1 else 1
        offset = (page_number - 1) * JOBS_PAGE_SIZE
        result = service.search_jobs_page(
            limit=JOBS_PAGE_SIZE,
            offset=offset,
            search=q.strip(),
            company=company.strip(),
            location=location.strip(),
            status=job_status,
            minimum_score=minimum,
            max_years=_as_float(max_years),
            skills_include=_csv_terms(skills),
            skills_exclude=_csv_terms(exclude_skills),
            posted_days=_as_float(posted_days),
            since_hours=since_hours,
            sort=sort if sort in dict(SORT_OPTIONS) else "newest",
            apply_preferences=apply_preferences,
        )
        # The current filter query as a string, so an inline status change can return
        # to exactly this filtered view instead of resetting the user's triage.
        query_string = str(request.url.query)
        return render(
            request,
            "dashboard.html.j2",
            {
                "active": "jobs",
                "has_resume": has_resume,
                "jobs": result.jobs,
                "page": result,
                "query_string": query_string,
                "total": result.total,
                "scored": sum(1 for job in result.jobs if job.match_score is not None),
                "companies": service.companies_with_jobs(),
                "configured": service.config.companies,
                "config_path": service.config.config_path,
                "crawl": service.crawl_progress,
                "closed_jobs": service.closed_job_count(),
                "min_score": service.config.preferences.minimum_match_score,
                "statuses": [item.value for item in JobStatus],
                "sorts": SORT_OPTIONS,
                "waves": WAVE_OPTIONS,
                "new_within_options": NEW_WITHIN_OPTIONS,
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
                    "posted_days": posted_days,
                    "new_within": new_within,
                    "preferred": apply_preferences,
                    "sort": sort,
                },
            },
        )

    @app.get("/coach", response_class=HTMLResponse, include_in_schema=False)
    async def coach_page(request: Request) -> HTMLResponse:
        """The grounded chat coach: daily digest + persistent, data-grounded Q&A."""
        service = get_service(request)
        service.reload_config_if_changed()
        return render(
            request,
            "coach.html.j2",
            {
                "active": "coach",
                "digest": service.daily_digest(),
                "messages": service.chat_history(),
            },
        )

    @app.post("/coach/message", include_in_schema=False)
    async def coach_message(
        request: Request, message: Annotated[str, Form()] = ""
    ) -> Response:
        service = get_service(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            reply = await service.coach_reply(message)
        except ServiceError as exc:
            if wants_json:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            return back("/coach", error=str(exc))
        if wants_json:
            return JSONResponse({"reply": reply.reply, "used_ai": reply.used_ai})
        return back("/coach")

    @app.post("/coach/clear", include_in_schema=False)
    async def coach_clear(request: Request) -> Response:
        service = get_service(request)
        service.clear_chat()
        return back("/coach", notice="Conversation cleared.")

    @app.get("/insights", response_class=HTMLResponse, include_in_schema=False)
    async def insights_page(request: Request) -> HTMLResponse:
        """Market and personal trends from the local data (no AI)."""
        service = get_service(request)
        service.reload_config_if_changed()
        report = service.insights_report()
        return render(
            request,
            "insights.html.j2",
            {
                "active": "insights",
                "report": report,
                "suggestions": service.INSIGHTS_SUGGESTIONS,
            },
        )

    @app.post("/insights/ask", include_in_schema=False)
    async def insights_ask(
        request: Request, question: Annotated[str, Form()] = ""
    ) -> Response:
        """Answer one grounded question about the Insights data (stateless)."""
        service = get_service(request)
        try:
            reply = await service.ask_insights(question)
        except ServiceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"reply": reply.reply, "used_ai": reply.used_ai})

    @app.get("/resume", response_class=HTMLResponse, include_in_schema=False)
    async def resume_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        master = service.master_resume()
        _, prompt = service.resume_yaml_guide()
        return render(
            request,
            "resume_page.html.j2",
            {
                "active": "resume",
                "master": master,
                "master_yaml": dump_structured_resume_yaml(master.resume) if master else "",
                "pdf_stem": resume_filename_stem(master.resume) if master else "resume",
                "versions": service.resume_versions(),
                "yaml_prompt": prompt,
                "warnings": [],
            },
        )

    @app.get("/resume/guide", response_class=HTMLResponse, include_in_schema=False)
    async def resume_guide(request: Request) -> HTMLResponse:
        """Render the resume-YAML authoring guide (with the conversion prompt) in-app."""
        service = get_service(request)
        guide, _ = service.resume_yaml_guide()
        parser = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
        return render(
            request,
            "readme.html.j2",
            {
                "active": "resume",
                "body": parser.render(guide) if guide else "",
                "missing": not guide,
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

    @app.get("/resume/{version_id}.yaml", include_in_schema=False)
    async def resume_yaml(request: Request, version_id: int, download: bool = False) -> Response:
        """Serialize a stored version as editable YAML (the tweak-and-re-render format)."""
        service = get_service(request)
        version = service.database.get_resume(version_id)
        if version is None:
            return JSONResponse({"detail": "resume version not found"}, status_code=404)
        stem = resume_filename_stem(version.resume)
        # inline by default (view in browser); ?download=1 forces a file download.
        disposition = "attachment" if download else "inline"
        return Response(
            dump_structured_resume_yaml(version.resume),
            media_type="application/x-yaml; charset=utf-8",
            headers={"Content-Disposition": f'{disposition}; filename="{stem}.yaml"'},
        )

    @app.post("/resume/edit", include_in_schema=False)
    async def edit_resume(
        request: Request, resume_yaml: Annotated[str, Form()]
    ) -> Response:
        """Validate edited YAML and store it as a new master version.

        Older versions are kept untouched; the saved edit becomes the current master,
        exactly like a fresh upload. On a validation error the page is re-rendered with
        the error and the user's edited text preserved so nothing is lost.
        """
        service = get_service(request)
        try:
            # Route through the structured import path: schema-validated, no AI, no
            # lossy parsing. The .yaml name makes import_resume take that branch.
            result = await service.import_resume(
                "edited.yaml", resume_yaml.encode("utf-8")
            )
        except (ResumeParseError, ServiceError) as exc:
            master = service.master_resume()
            return render(
                request,
                "resume_page.html.j2",
                {
                    "active": "resume",
                    "master": master,
                    # Keep what the user typed, not the last-saved version.
                    "master_yaml": resume_yaml,
                    "pdf_stem": resume_filename_stem(master.resume) if master else "resume",
                    "versions": service.resume_versions(),
                    "warnings": [],
                    "error": f"Could not save: {exc}",
                },
                status_code=400,
            )
        return back("/resume", notice=f"Saved as version {result.version_id}.")

    @app.post("/resume/preview", include_in_schema=False)
    async def preview_resume(
        request: Request, resume_yaml: Annotated[str, Form()]
    ) -> Response:
        """Render edited YAML to themed HTML without storing anything.

        Powers the live preview pane: the editor posts its current text here and shows
        the returned HTML. A parse/validation error returns 400 with a plain message so
        the pane can display it inline.
        """
        service = get_service(request)
        try:
            resume = load_structured_resume("preview.yaml", resume_yaml.encode("utf-8"))
            html = service.renderer.render_html(resume)
        except ResumeParseError as exc:
            return Response(str(exc), media_type="text/plain; charset=utf-8", status_code=400)
        except RenderError as exc:
            return Response(str(exc), media_type="text/plain; charset=utf-8", status_code=500)
        return HTMLResponse(html)

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
        # Download under a stable, human name derived from the contact (e.g.
        # vedesh_k_v_s.pdf) rather than an opaque version number.
        stem = resume_filename_stem(version.resume)
        return Response(
            pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{stem}.pdf"'},
        )

    @app.post("/crawl", include_in_schema=False)
    async def start_crawl(
        request: Request,
        company: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
        ats: Annotated[str, Form()] = "",
    ) -> Response:
        service = get_service(request)
        # The dashboard JS posts with Accept: application/json so it can stay on the
        # page and poll for progress. A plain form post (no JS) still redirects back.
        wants_json = "application/json" in request.headers.get("accept", "")
        ats_types = _resolve_ats_wave(ats)
        try:
            if url.strip():
                # Ad-hoc: crawl any career page the user pastes, not just configured ones.
                await service.start_background_url_crawl(url.strip())
            else:
                await service.start_background_crawl(
                    company.strip() or None, ats_types=ats_types
                )
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

    @app.post("/crawl-waves", include_in_schema=False)
    async def start_crawl_waves(request: Request) -> Response:
        """Run the fast→medium→slow wave pipeline so fast jobs are applyable first."""
        service = get_service(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            await service.start_background_waves()
        except ServiceError as exc:
            status = 409 if "already running" in str(exc).casefold() else 400
            if wants_json:
                return JSONResponse({"detail": str(exc)}, status_code=status)
            return back("/", error=str(exc))
        if wants_json:
            return JSONResponse(_crawl_status(service), status_code=202)
        return back("/", notice="Wave crawl started: fast first, then medium and slow.")

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
                return back("/organizations", error=f"Added {added}, but the crawl did not start: {exc}")
            return back("/", notice=f"Added {added} and started a crawl. Watch progress here.")
        return back("/organizations", notice=f"Added {added} to your config as '{slug}'.")

    # ----- organizations (config editor) --------------------------------

    def _organizations_context(
        service: JobOSService, *, config_text: str | None = None, active_tab: str = "companies"
    ) -> dict[str, Any]:
        # Pair each configured board with what the discovery registry learned about it
        # (GCC flag, category, India role count) so the table can show those columns.
        registry = service._registry()
        companies = []
        gcc_count = 0
        for slug, settings in sorted(
            service.config.companies.items(), key=lambda item: item[1].name.casefold()
        ):
            entry = registry.entries.get(slug)
            is_gcc = bool(entry and entry.is_gcc)
            if is_gcc:
                gcc_count += 1
            companies.append(
                {
                    "slug": slug,
                    "name": settings.name,
                    "url": settings.url,
                    "ats": entry.ats_type if entry else "",
                    "category": (entry.category.value if entry else ""),
                    "is_gcc": is_gcc,
                    "india_roles": (entry.india_role_count if entry else None),
                }
            )
        backups = service.config_backups()
        return {
            "active": "organizations",
            "active_tab": active_tab,
            "config_text": config_text if config_text is not None else service.read_config_text(),
            "companies": companies,
            "gcc_count": gcc_count,
            "config_path": str(service.config.config_path),
            "preferences": service.preferences_view(),
            "backups": [path.name for path in backups],
            "backup_count": len(backups),
        }

    @app.get("/organizations", response_class=HTMLResponse, include_in_schema=False)
    async def organizations_page(request: Request, tab: str = "companies") -> HTMLResponse:
        """Edit config.yaml via friendly sub-tabs (Companies, Preferences) or raw YAML."""
        service = get_service(request)
        service.reload_config_if_changed()
        return render(request, "organizations.html.j2", _organizations_context(service, active_tab=tab))

    @app.post("/organizations/config", include_in_schema=False)
    async def save_config(
        request: Request, config_text: Annotated[str, Form()]
    ) -> Response:
        """Validate and save edited config.yaml; a bad edit is rejected, not written."""
        service = get_service(request)
        try:
            service.save_config_text(config_text)
        except ServiceError as exc:
            context = _organizations_context(service, config_text=config_text, active_tab="advanced")
            context["error"] = str(exc)
            return render(request, "organizations.html.j2", context, status_code=400)
        return back("/organizations?tab=advanced", notice="Config saved. A timestamped backup was kept.")

    @app.post("/organizations/preferences", include_in_schema=False)
    async def save_preferences(
        request: Request,
        preferred_locations: Annotated[str, Form()] = "",
        minimum_match_score: Annotated[str, Form()] = "",
        roles_include: Annotated[str, Form()] = "",
        roles_exclude: Annotated[str, Form()] = "",
        skills_include: Annotated[str, Form()] = "",
        skills_exclude: Annotated[str, Form()] = "",
        min_years: Annotated[str, Form()] = "",
        max_years: Annotated[str, Form()] = "",
        seniority: Annotated[str, Form()] = "",
        max_applications_per_company: Annotated[str, Form()] = "",
        remote_only: Annotated[str, Form()] = "",
    ) -> Response:
        """Save the friendly Preferences form, editing only the preferences: block."""
        service = get_service(request)

        def intval(value: str) -> int | None:
            value = value.strip()
            return int(value) if value.isdigit() else None

        updates: dict[str, Any] = {
            "preferred_locations": preferred_locations,
            "roles_include": roles_include,
            "roles_exclude": roles_exclude,
            "skills_include": skills_include,
            "skills_exclude": skills_exclude,
            "minimum_match_score": intval(minimum_match_score),
            "max_applications_per_company": intval(max_applications_per_company),
            "min_years": min_years,
            "max_years": max_years,
            "seniority": seniority,
            "remote_only": remote_only == "on",
        }
        try:
            service.save_preferences(updates)
        except ServiceError as exc:
            context = _organizations_context(service, active_tab="preferences")
            context["error"] = str(exc)
            return render(request, "organizations.html.j2", context, status_code=400)
        return back("/organizations?tab=preferences", notice="Preferences saved. A timestamped backup was kept.")

    # ----- AI settings --------------------------------------------------

    @app.get("/ai", response_class=HTMLResponse, include_in_schema=False)
    async def ai_page(request: Request) -> HTMLResponse:
        """Configure the AI provider, model, and per-task toggles from the browser."""
        service = get_service(request)
        view = service.ai_settings_view()
        ollama_models = await service.ollama_models()
        return render(
            request,
            "ai_settings.html.j2",
            {"active": "ai", "ai": view, "ollama_models": ollama_models},
        )

    @app.get("/ai/ollama-models", include_in_schema=False)
    async def ai_ollama_models(request: Request) -> Response:
        """Live list of installed Ollama models, for the dropdown refresh button."""
        service = get_service(request)
        return JSONResponse({"models": await service.ollama_models()})

    @app.post("/ai/key", include_in_schema=False)
    async def save_ai_key(
        request: Request,
        provider: Annotated[str, Form()] = "",
        api_key: Annotated[str, Form()] = "",
    ) -> Response:
        """Save (or clear) an API key to the gitignored local .env, never config.yaml."""
        service = get_service(request)
        try:
            service.save_api_key(provider.strip(), api_key)
        except ServiceError as exc:
            return back("/ai", error=str(exc))
        action = "saved" if api_key.strip() else "cleared"
        return back("/ai", notice=f"{provider.title()} API key {action}.")

    @app.post("/ai", include_in_schema=False)
    async def save_ai(
        request: Request,
        provider: Annotated[str, Form()] = "ollama",
        model: Annotated[str, Form()] = "",
        model_select: Annotated[str, Form()] = "",
        base_url: Annotated[str, Form()] = "",
        temperature: Annotated[str, Form()] = "",
        timeout_seconds: Annotated[str, Form()] = "",
        max_input_chars: Annotated[str, Form()] = "",
        max_output_tokens: Annotated[str, Form()] = "",
        max_bullet_rewrites: Annotated[str, Form()] = "",
        keep_alive: Annotated[str, Form()] = "",
        disable_thinking: Annotated[str, Form()] = "",
        structure_resume: Annotated[str, Form()] = "",
        analyse_jobs: Annotated[str, Form()] = "",
        rewrite_summary: Annotated[str, Form()] = "",
        rewrite_bullets: Annotated[str, Form()] = "",
        cover_letter: Annotated[str, Form()] = "",
    ) -> Response:
        service = get_service(request)

        def number(value: str) -> float | None:
            value = value.strip()
            try:
                return float(value) if value else None
            except ValueError:
                return None

        updates: dict[str, Any] = {
            "provider": provider,
            # The dropdown (model_select) wins when set; the text field is the fallback
            # for a model not yet installed / a cloud model name.
            "model": (model_select.strip() or model.strip()),
            "base_url": base_url,
            "temperature": number(temperature),
            "timeout_seconds": number(timeout_seconds),
            "max_input_chars": number(max_input_chars),
            "max_output_tokens": number(max_output_tokens),
            "max_bullet_rewrites": number(max_bullet_rewrites),
            "keep_alive": keep_alive,
            # Unchecked checkboxes are absent from the form, so a missing value means off.
            "disable_thinking": disable_thinking == "on",
            "structure_resume": structure_resume == "on",
            "analyse_jobs": analyse_jobs == "on",
            "rewrite_summary": rewrite_summary == "on",
            "rewrite_bullets": rewrite_bullets == "on",
            "cover_letter": cover_letter == "on",
        }
        try:
            service.save_ai_settings(updates)
        except ServiceError as exc:
            view = service.ai_settings_view()
            ollama_models = await service.ollama_models()
            return render(
                request,
                "ai_settings.html.j2",
                {"active": "ai", "ai": view, "ollama_models": ollama_models, "error": str(exc)},
                status_code=400,
            )
        return back("/ai", notice="AI settings saved. A timestamped backup was kept.")

    @app.post("/jobs/clear-closed", include_in_schema=False)
    async def clear_closed_jobs(request: Request) -> Response:
        """Hard-delete every soft-deleted (closed) job now, reclaiming space."""
        service = get_service(request)
        removed = service.hard_delete_closed_jobs(vacuum=True)
        return back(
            "/",
            notice=(
                f"Permanently deleted {removed} closed job(s)."
                if removed
                else "No closed jobs to delete."
            ),
        )

    @app.post("/jobs/paste", include_in_schema=False)
    async def paste_job(
        request: Request,
        title: Annotated[str, Form()],
        company: Annotated[str, Form()],
        description: Annotated[str, Form()],
        url: Annotated[str, Form()] = "",
    ) -> Response:
        """Create a job from pasted JD text (for portals the crawler cannot read).

        After saving, redirect to the job's detail page where Analyse and Optimise
        already live, so the pasted job flows through the normal pipeline.
        """
        service = get_service(request)
        try:
            job = service.add_job_from_text(
                title=title, company=company, description=description, url=url
            )
        except ServiceError as exc:
            return back("/", error=str(exc))
        return back(f"/jobs/{job.id}", notice="Job added from pasted text. Analyse it below.")

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
    async def set_status(
        request: Request,
        job_id: int,
        status: Annotated[str, Form()],
        next: Annotated[str, Form()] = "",
    ) -> Response:
        service = get_service(request)
        # Where to return after setting status. The Jobs-list dropdown passes its own
        # URL (with filters) so triage stays on the list; the job-detail page omits it
        # and falls back to the detail view. Only same-site relative paths are honored.
        destination = next if next.startswith("/") and not next.startswith("//") else f"/jobs/{job_id}"
        if status not in set(JobStatus):
            return back(destination, error=f"Unknown status {status!r}.")
        try:
            service.set_job_status(job_id, JobStatus(status))
        except ServiceError as exc:
            return back(destination, error=str(exc))
        return back(destination, notice=f"Marked {status}.")

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
                "resume_yaml": dump_structured_resume_yaml(application.optimized_resume),
                "editable": application.status.value != "approved",
            },
        )

    @app.post("/applications/{application_id}/resume", include_in_schema=False)
    async def save_application_resume(
        request: Request, application_id: int, resume_yaml: Annotated[str, Form()]
    ) -> Response:
        """Save edited YAML as this draft's optimized resume, then stay on the page."""
        service = get_service(request)
        try:
            service.save_application_resume_yaml(application_id, resume_yaml)
        except ServiceError as exc:
            return back(f"/applications/{application_id}", error=str(exc))
        return back(f"/applications/{application_id}", notice="Saved. Preview and generate below.")

    @app.post("/applications/{application_id}/preview", include_in_schema=False)
    async def preview_application_resume(
        request: Request, application_id: int, resume_yaml: Annotated[str, Form()]
    ) -> Response:
        """Render edited YAML to themed HTML without storing — powers the live preview."""
        service = get_service(request)
        try:
            resume = load_structured_resume("preview.yaml", resume_yaml.encode("utf-8"))
            html = service.renderer.render_html(resume)
        except ResumeParseError as exc:
            return Response(str(exc), media_type="text/plain; charset=utf-8", status_code=400)
        except RenderError as exc:
            return Response(str(exc), media_type="text/plain; charset=utf-8", status_code=500)
        return HTMLResponse(html)

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
        "closed_jobs": service.closed_job_count(),
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
            "posted_days": "",
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
        # Multi-wave fields so the poller can notify the user as each wave completes.
        "current_wave": progress.current_wave,
        "completed_waves": list(progress.completed_waves),
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
