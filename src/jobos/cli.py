"""JobOS command line interface."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from jobos.ai import AIError
from jobos.config import AppConfig, load_config
from jobos.crawler import CrawlError
from jobos.models import JobStatus, JobView
from jobos.renderer import (
    RenderError,
    ResumeRenderer,
    ats_report_to_markdown,
    diff_to_markdown,
    resume_to_markdown,
)
from jobos.resume import (
    ResumeParseError,
    dump_structured_resume_yaml,
    load_structured_resume,
)
from jobos.services import JobOSService, ServiceError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "JobOS prepares job application packages on your machine. "
        "It never submits an application for you."
    ),
)
resume_app = typer.Typer(no_args_is_help=True, help="Manage your master resume.")
app.add_typer(resume_app, name="resume")

console = Console()
errors = Console(stderr=True)
_state: dict[str, Path | None] = {"config": None}

KNOWN_ERRORS = (ServiceError, CrawlError, RenderError, ResumeParseError, AIError, LookupError)


@app.callback()
def main(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to config.yaml.", show_default=False),
    ] = None,
) -> None:
    _state["config"] = config


def _config() -> AppConfig:
    try:
        return load_config(_state["config"])
    except (FileNotFoundError, ValueError) as exc:
        errors.print(f"[red]Configuration problem:[/red] {exc}")
        raise typer.Exit(1) from exc


def _service() -> JobOSService:
    try:
        return JobOSService(_config())
    except KNOWN_ERRORS as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one async use case and turn known failures into clean exit codes."""
    try:
        return asyncio.run(coroutine)
    except KNOWN_ERRORS as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _guard[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except KNOWN_ERRORS as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _emit(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))


def _score_text(score: int | None) -> str:
    if score is None:
        return "[dim]—[/dim]"
    colour = "green" if score >= 65 else "yellow" if score >= 45 else "red"
    return f"[{colour}]{score}[/{colour}]"


def _job_table(jobs: list[JobView], title: str) -> Table:
    table = Table(title=title, header_style="bold", show_lines=False)
    table.add_column("ID", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Location")
    table.add_column("Match", justify="right")
    table.add_column("Status")
    for job in jobs:
        table.add_row(
            str(job.id),
            job.company,
            job.title[:58],
            (job.location or "—")[:26],
            _score_text(job.match_score),
            job.status.value,
        )
    return table


# ----- setup and diagnostics ---------------------------------------------


@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config.")] = False,
) -> None:
    """Create config.yaml from the bundled example."""
    target = Path(_state["config"] or "config.yaml")
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    if target.exists() and not force:
        console.print(f"[yellow]{target} already exists.[/yellow] Use --force to overwrite.")
        raise typer.Exit(1)
    if not example.exists():
        errors.print(f"[red]Example config not found at {example}[/red]")
        raise typer.Exit(1)
    shutil.copyfile(example, target)
    console.print(f"[green]Wrote {target}[/green]")
    console.print("Next: add companies under `companies:`, then run [bold]jobos doctor[/bold].")


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check the model, Chromium, database, resume, and configuration."""
    service = _service()
    report = _run(service.health())
    if json_output:
        _emit(report.model_dump(mode="json"))
        raise typer.Exit(0 if report.ok else 1)
    table = Table(title="JobOS doctor", header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    for check in report.checks:
        table.add_row(
            check.name,
            "[green]ok[/green]" if check.ok else "[red]check[/red]",
            check.detail,
        )
    console.print(table)
    if not report.ok:
        console.print("[yellow]Some checks need attention.[/yellow]")
        raise typer.Exit(1)
    console.print("[green]Everything looks ready.[/green]")


@app.command("config")
def show_config(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the effective configuration."""
    config = _config()
    payload = config.model_dump(mode="json")
    if json_output:
        _emit(payload)
        return
    console.print(f"[bold]Config file:[/bold] {config.config_path}")
    console.print(f"[bold]Data dir:[/bold] {config.data_dir}")
    console.print(f"[bold]Output dir:[/bold] {config.output_dir}")
    console.print(
        f"[bold]AI:[/bold] {config.ai.provider} / {config.ai.model} "
        f"(temp {config.ai.temperature}, budget {config.ai.max_input_chars} chars, "
        f"max {config.ai.max_bullet_rewrites} bullet rewrites)"
    )
    enabled = [name for name, on in config.ai.tasks.model_dump().items() if on]
    console.print(f"[bold]AI tasks on:[/bold] {', '.join(enabled) or 'none'}")
    console.print(f"[bold]Theme:[/bold] {config.render.theme}")
    console.print(
        f"[bold]Preferences:[/bold] min score {config.preferences.minimum_match_score}, "
        f"remote only {config.preferences.remote_only}"
    )
    if config.companies:
        table = Table(title=f"{len(config.companies)} configured companies", header_style="bold")
        table.add_column("Key")
        table.add_column("Name")
        table.add_column("Careers URL", overflow="fold")
        table.add_column("Enabled")
        for slug, company in config.companies.items():
            table.add_row(slug, company.name, company.url, "yes" if company.enabled else "no")
        console.print(table)
    else:
        console.print("[yellow]No companies configured yet.[/yellow]")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address. Loopback by default.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on code changes.")] = False,
) -> None:
    """Start the local web dashboard."""
    import os

    import uvicorn

    config = _config()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print(
            f"[yellow]Warning:[/yellow] binding to {host} exposes JobOS on your network. "
            "It has no authentication and your resume data would be readable by anyone "
            "who can reach this port."
        )
    os.environ["JOBOS_CONFIG"] = str(config.config_path)
    console.print(f"JobOS dashboard on [bold]http://{host}:{port}[/bold] (Ctrl+C to stop)")
    uvicorn.run(
        "jobos.web:app_factory", factory=True, host=host, port=port, reload=reload, log_level="info"
    )


# ----- resume -------------------------------------------------------------


@resume_app.command("import")
def resume_import(
    path: Annotated[Path, typer.Argument(help="PDF, DOCX, or Markdown resume.")],
) -> None:
    """Import a resume and make it the current master."""
    service = _service()
    if not path.is_file():
        errors.print(f"[red]No such file: {path}[/red]")
        raise typer.Exit(1)
    result = _run(service.import_resume(path.name, path.read_bytes()))
    resume = result.resume
    console.print(f"[green]Imported version {result.version_id}[/green] from {path.name}")
    console.print(
        f"{resume.contact.name} · {len(resume.skills)} skills · "
        f"{len(resume.experience)} roles · {len(resume.projects)} projects · "
        f"{len(resume.education)} education entries"
    )
    for warning in result.warnings:
        console.print(f"[yellow]•[/yellow] {warning}")
    console.print("Review it with [bold]jobos resume show --json[/bold] before optimising.")


@resume_app.command("show")
def resume_show(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the current master resume."""
    service = _service()
    master = _guard(service.require_master_resume)
    if json_output:
        _emit(master.resume.model_dump(mode="json"))
        return
    console.print(
        f"[bold]Version {master.id}[/bold] from {master.source_name} "
        f"({master.created_at:%Y-%m-%d %H:%M} UTC, hash {master.content_hash[:12]})"
    )
    console.print(resume_to_markdown(master.resume))


@resume_app.command("versions")
def resume_versions(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List every stored resume version."""
    service = _service()
    versions = service.resume_versions()
    if json_output:
        _emit([version.model_dump(mode="json") for version in versions])
        return
    table = Table(title=f"{len(versions)} resume versions", header_style="bold")
    for column in ("ID", "Source", "Kind", "Parent", "Created", "Hash"):
        table.add_column(column)
    for version in versions:
        table.add_row(
            str(version.id),
            version.source_name[:40],
            "master" if version.is_master else version.source_format,
            str(version.parent_id or "—"),
            f"{version.created_at:%Y-%m-%d %H:%M}",
            version.content_hash[:12],
        )
    console.print(table)


# ----- jobs ---------------------------------------------------------------


@app.command()
def crawl(
    company: Annotated[
        str | None, typer.Argument(help="Company key from config.yaml. Omit to crawl all.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Crawl configured career pages and store new jobs."""
    service = _service()
    summary = _run(service.crawl(company))
    if json_output:
        _emit(summary.model_dump(mode="json"))
        raise typer.Exit(0 if not summary.errors else 1)
    console.print(
        f"[green]{summary.jobs_added} added[/green], {summary.jobs_updated} updated, "
        f"{summary.jobs_seen} seen across "
        f"{summary.companies_succeeded}/{summary.companies_requested} companies."
    )
    for name, message in summary.errors.items():
        console.print(f"[red]{name}:[/red] {message}")
    if summary.errors:
        raise typer.Exit(1)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Free text, e.g. python remote aws.")] = "",
    company: Annotated[str, typer.Option(help="Exact company name.")] = "",
    location: Annotated[str, typer.Option(help="Location substring.")] = "",
    status: Annotated[str | None, typer.Option(help="new, saved, opened, applied, ignored.")] = None,
    min_score: Annotated[int | None, typer.Option(help="Minimum match score.")] = None,
    max_years: Annotated[
        float | None,
        typer.Option(help="Max required years of experience (analysed jobs only)."),
    ] = None,
    min_years: Annotated[
        float | None,
        typer.Option(help="Min required years of experience (analysed jobs only)."),
    ] = None,
    seniority: Annotated[
        str,
        typer.Option(help="Filter by seniority, e.g. Senior, Staff, Lead (analysed jobs only)."),
    ] = "",
    sort: Annotated[str, typer.Option(help="newest, oldest, score, company, title.")] = "newest",
    limit: Annotated[int, typer.Option(help="Maximum rows.")] = 50,
    preferred: Annotated[
        bool, typer.Option("--preferred", help="Apply config preferences as filters.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search stored jobs."""
    service = _service()
    if status is not None and status not in set(JobStatus):
        errors.print(f"[red]Unknown status {status!r}.[/red]")
        raise typer.Exit(2)
    jobs = service.search_jobs(
        search=query,
        company=company,
        location=location,
        status=JobStatus(status) if status else None,
        minimum_score=min_score,
        max_years=max_years,
        min_years=min_years,
        seniority=seniority,
        sort=sort,
        limit=limit,
        apply_preferences=preferred,
    )
    if json_output:
        _emit([job.model_dump(mode="json") for job in jobs])
        return
    if not jobs:
        console.print("[yellow]No jobs matched.[/yellow] Try `jobos crawl` first.")
        return
    console.print(_job_table(jobs, f"{len(jobs)} jobs"))


@app.command("analyse")
def analyse(
    job_id: Annotated[int, typer.Argument(help="Job ID from `jobos search`.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Analyse a job and score your master resume against it."""
    service = _service()
    job, analysis, report = _run(service.analyse_job(job_id))
    if json_output:
        _emit(
            {
                "job": job.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
            }
        )
        return
    console.print(f"[bold]{job.title}[/bold] at {job.company}")
    console.print(f"Required: {', '.join(analysis.required_skills) or 'none detected'}")
    console.print(f"Preferred: {', '.join(analysis.preferred_skills) or 'none detected'}")
    if analysis.minimum_years:
        console.print(f"Experience asked for: {analysis.minimum_years:g}+ years")
    console.print(ats_report_to_markdown(report, job))


@app.command("analyse-all")
def analyse_all(
    query: Annotated[str, typer.Argument(help="Free text to select jobs, e.g. backend.")] = "",
    company: Annotated[str, typer.Option(help="Exact company name.")] = "",
    location: Annotated[str, typer.Option(help="Location substring, e.g. India.")] = "",
    status: Annotated[str | None, typer.Option(help="new, saved, opened, applied, ignored.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum jobs to analyse.")] = 25,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Analyse and score every job matching a filter, so you can sort by match score.

    Analysis is what produces match scores and the years/seniority data, so run this
    over a filtered set (for example `--location India backend`), then narrow with
    `search --sort score --max-years 6`.
    """
    service = _service()
    if status is not None and status not in set(JobStatus):
        errors.print(f"[red]Unknown status {status!r}.[/red]")
        raise typer.Exit(2)
    jobs = service.search_jobs(
        search=query,
        company=company,
        location=location,
        status=JobStatus(status) if status else None,
        limit=limit,
    )
    if not jobs:
        console.print("[yellow]No jobs matched.[/yellow] Try `jobos crawl` or a broader filter.")
        raise typer.Exit(1)
    result = _run(service.analyse_jobs([job.id for job in jobs]))
    if json_output:
        _emit(
            {
                "requested": result.requested,
                "analysed": result.analysed,
                "errors": {str(job_id): message for job_id, message in result.errors.items()},
            }
        )
        raise typer.Exit(0 if not result.errors else 1)
    console.print(
        f"[green]Analysed {result.analysed}[/green] of {result.requested} jobs. "
        "Now try [bold]jobos search --sort score[/bold]."
    )
    for job_id, message in result.errors.items():
        console.print(f"[red]job {job_id}:[/red] {message}")
    if result.errors:
        raise typer.Exit(1)


@app.command()
def score(
    job_id: Annotated[int, typer.Argument(help="Job ID.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the most recent ATS score for a job."""
    service = _service()
    job = _guard(lambda: service.require_job(job_id))
    report = service.latest_report(job_id)
    if report is None:
        console.print("[yellow]Not scored yet.[/yellow] Run `jobos analyse` first.")
        raise typer.Exit(1)
    if json_output:
        _emit(report.model_dump(mode="json"))
        return
    console.print(f"[bold]{job.company} — {job.title}[/bold]")
    table = Table(header_style="bold")
    table.add_column("Component")
    table.add_column("Score", justify="right")
    table.add_column("Max", justify="right")
    for component in report.components:
        table.add_row(component.name, f"{component.score:g}", f"{component.maximum:g}")
    table.add_row("[bold]Total[/bold]", f"[bold]{report.overall_score}[/bold]", "100")
    console.print(table)
    console.print(f"Missing skills: {', '.join(report.missing_skills) or 'none'}")
    console.print(f"Confidence: {report.confidence} · scorer {report.scoring_version}")


@app.command("optimise")
def optimise(
    job_id: Annotated[int, typer.Argument(help="Job ID.")],
    cover_letter: Annotated[
        bool, typer.Option("--cover-letter", help="Also draft a cover letter.")
    ] = False,
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Generate the package immediately, skipping manual review."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Propose truthful improvements for one job, then review or generate."""
    service = _service()
    outcome = _run(service.optimise_job(job_id, with_cover_letter=cover_letter))
    application = outcome.application
    package_dir = ""
    if approve:
        approved, package = _run(service.approve_application(application.id))
        application = approved
        package_dir = str(package.directory)

    if json_output:
        _emit(
            {
                "application_id": application.id,
                "status": application.status.value,
                "score_before": application.ats_before.overall_score,
                "score_after": application.ats_after.overall_score,
                "rejected_edits": len(application.diff.rejected_edits),
                "output_dir": package_dir or application.output_dir,
                "warnings": outcome.warnings,
            }
        )
        return

    console.print(
        f"[bold]Application {application.id}[/bold] for {outcome.job.company} — {outcome.job.title}"
    )
    console.print(
        f"ATS estimate {application.ats_before.overall_score} → "
        f"{application.ats_after.overall_score}"
    )
    console.print(
        f"{len(application.diff.bullet_changes)} bullets reworded, "
        f"{len(application.diff.ordering_changes)} ordering changes, "
        f"{len(application.diff.rejected_edits)} edits rejected"
    )
    for warning in outcome.warnings:
        console.print(f"[yellow]•[/yellow] {warning}")
    for rejected in application.diff.rejected_edits:
        console.print(f"  [yellow]rejected[/yellow] {rejected.target}: {rejected.reason}")
    if package_dir:
        console.print(f"[green]Package written to {package_dir}[/green]")
    else:
        console.print(
            f"Review with [bold]jobos diff {application.id}[/bold], then generate with "
            f"[bold]jobos generate {application.id}[/bold]."
        )


@app.command()
def diff(
    application_id: Annotated[int, typer.Argument(help="Application ID from `jobos optimise`.")],
) -> None:
    """Show exactly what an optimisation changed, and what it refused to change."""
    service = _service()
    application = _guard(lambda: service.require_application(application_id))
    job = _guard(lambda: service.require_job(application.job_id))
    console.print(diff_to_markdown(application.diff, job=job))


@app.command()
def generate(
    application_id: Annotated[int, typer.Argument(help="Application ID to approve.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Approve a reviewed optimisation and write the application package."""
    service = _service()
    application, package = _run(service.approve_application(application_id))
    if json_output:
        _emit(
            {
                "application_id": application.id,
                "version": package.version,
                "directory": str(package.directory),
                "files": sorted(package.files),
            }
        )
        return
    console.print(f"[green]Wrote version {package.version} to {package.directory}[/green]")
    for name in sorted(package.files):
        console.print(f"  {name}")


@app.command()
def render(
    resume_file: Annotated[
        Path, typer.Argument(help="A resume .yaml, .yml, or .json file.")
    ],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")] = Path("."),
    theme: Annotated[
        str | None, typer.Option(help="classic, compact, letter, or onepager.")
    ] = None,
) -> None:
    """Render a resume file (YAML or JSON) to Markdown, HTML, and PDF.

    This is the quick tweak loop: edit your resume YAML, re-run `render`, and get an
    updated PDF without touching the job pipeline or the stored master.
    """
    config = _config()
    if not resume_file.is_file():
        errors.print(f"[red]No such file: {resume_file}[/red]")
        raise typer.Exit(1)
    try:
        resume = load_structured_resume(resume_file.name, resume_file.read_bytes())
    except ResumeParseError as exc:
        errors.print(f"[red]{resume_file} is not a valid JobOS resume:[/red] {exc}")
        raise typer.Exit(1) from exc
    renderer = _guard(lambda: ResumeRenderer(theme or config.render.theme))
    html = renderer.render_html(resume)
    pdf = _run(renderer.render_pdf(html))
    out.mkdir(parents=True, exist_ok=True)
    written = {
        "resume.html": html,
        "resume.md": resume_to_markdown(resume),
    }
    for name, text in written.items():
        (out / name).write_text(text, encoding="utf-8")
    (out / "resume.pdf").write_bytes(pdf)
    console.print(f"[green]Rendered to {out.resolve()}[/green]")
    for name in (*written, "resume.pdf"):
        console.print(f"  {name}")


@app.command()
def export(
    application_id: Annotated[
        int, typer.Argument(help="Application ID from `jobos history`.")
    ],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Where to write the editable YAML.")
    ] = Path("tailored.resume.yaml"),
) -> None:
    """Export a tailored resume as editable YAML so you can tweak, then re-render.

    Use this after `generate` when you want to adjust the tailored resume by hand:
      1. jobos export <application_id> --out tailored.resume.yaml
      2. edit tailored.resume.yaml
      3. jobos render tailored.resume.yaml --out final
    No re-optimisation needed; you are editing the already-tailored resume directly.
    """
    service = _service()
    application = _guard(lambda: service.require_application(application_id))
    out.write_text(dump_structured_resume_yaml(application.optimized_resume), encoding="utf-8")
    console.print(f"[green]Exported tailored resume to {out.resolve()}[/green]")
    console.print(
        f"Edit it, then run [bold]jobos render {out} --out final[/bold] for an updated PDF."
    )


@app.command()
def apply(
    job_id: Annotated[int, typer.Argument(help="Job ID to open.")],
) -> None:
    """Open the employer's own application page in your browser."""
    service = _service()
    url = _guard(lambda: service.open_application_page(job_id))
    console.print(f"[green]Opened[/green] {url}")
    console.print(
        "JobOS does not submit applications. Complete the form yourself, then run "
        f"[bold]jobos status {job_id} applied[/bold]."
    )


@app.command()
def status(
    job_id: Annotated[int, typer.Argument(help="Job ID.")],
    value: Annotated[str, typer.Argument(help="new, saved, opened, applied, or ignored.")],
) -> None:
    """Record where you are with a job."""
    service = _service()
    if value not in set(JobStatus):
        errors.print(f"[red]Unknown status {value!r}.[/red]")
        raise typer.Exit(2)
    job = _guard(lambda: service.set_job_status(job_id, JobStatus(value)))
    console.print(f"{job.company} — {job.title} is now [bold]{job.status.value}[/bold]")


@app.command()
def history(
    limit: Annotated[int, typer.Option(help="Maximum rows.")] = 30,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List optimisation attempts and generated packages."""
    service = _service()
    applications = service.database.list_applications(limit=limit)
    if json_output:
        _emit([application.model_dump(mode="json") for application in applications])
        return
    if not applications:
        console.print("[yellow]Nothing yet.[/yellow] Run `jobos optimise <job_id>` first.")
        return
    table = Table(title=f"{len(applications)} applications", header_style="bold")
    for column in ("ID", "Company", "Role", "Before", "After", "Rejected", "Status", "Output"):
        table.add_column(column, overflow="fold")
    for application in applications:
        job = service.database.get_job(application.job_id)
        table.add_row(
            str(application.id),
            job.company if job else "—",
            (job.title[:34] if job else "—"),
            str(application.ats_before.overall_score),
            str(application.ats_after.overall_score),
            str(len(application.diff.rejected_edits)),
            application.status.value,
            application.output_dir or "—",
        )
    console.print(table)


if __name__ == "__main__":
    app()
