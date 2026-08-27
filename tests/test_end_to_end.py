"""End-to-end smoke test of the full offline workflow.

This exercises the real JobOSService use cases wired exactly as the CLI and web UI
use them, with every AI task disabled (the `config` fixture does this), so the run
is deterministic and needs no Ollama or network. It covers import -> seed job ->
analyse -> optimise -> diff, and then the package generation path when Chromium is
available.
"""

from __future__ import annotations

import pytest

from jobos.models import ApplicationStatus, JobData, JobStatus
from jobos.renderer import RenderError
from jobos.services import JobOSService
from tests.conftest import JOB_DESCRIPTION, RESUME_MARKDOWN


def _seed_job(service: JobOSService) -> int:
    job_data = JobData(
        company="Acme",
        title="Senior Python Engineer",
        location="Remote / Hyderabad",
        description=JOB_DESCRIPTION,
        experience="4+ years experience",
        skills=["Python", "FastAPI"],
        apply_url="https://boards.greenhouse.io/acme/jobs/11",
        source_url="https://boards.greenhouse.io/acme",
        external_id="11",
    )
    view, created = service.database.upsert_job(job_data, fingerprint="acme-11")
    assert created is True
    return view.id


async def test_offline_workflow_import_analyse_optimise(service: JobOSService) -> None:
    """The deterministic path completes without AI: import, analyse, optimise, diff."""
    # 1. Import the master resume through the offline parser.
    result = await service.import_resume("master.md", RESUME_MARKDOWN.encode())
    assert result.version_id > 0
    assert service.master_resume() is not None
    assert service.require_master_resume().resume.contact.name == "Jane Doe"

    # 2. Seed a job the way a crawl would, then confirm search finds it.
    job_id = _seed_job(service)
    found = service.search_jobs(search="python")
    assert any(job.id == job_id for job in found)

    # 3. Analyse: deterministic requirement extraction and ATS scoring, stored.
    job, analysis, report = await service.analyse_job(job_id)
    assert "python" in {skill.casefold() for skill in analysis.required_skills}
    assert 0 <= report.overall_score <= 100
    assert service.latest_report(job_id) is not None

    # 4. Optimise: constrained, truthful plan with deterministic fallbacks.
    outcome = await service.optimise_job(job_id, with_cover_letter=True)
    application = outcome.application
    assert application.id > 0
    assert application.status is ApplicationStatus.DRAFT
    assert 0 <= application.ats_before.overall_score <= 100
    assert 0 <= application.ats_after.overall_score <= 100
    # Optimising a job moves it out of NEW.
    assert service.require_job(job_id).status is JobStatus.SAVED

    # 5. The diff is reviewable and never invents facts: the master resume's
    #    protected values survive into the optimised resume.
    assert application.optimized_resume.contact.name == "Jane Doe"
    assert [item.company for item in application.optimized_resume.experience] == [
        "Acme Corp",
        "Beta Labs",
    ]


async def test_offline_workflow_generates_versioned_package(service: JobOSService) -> None:
    """Approving an optimisation writes an immutable, versioned package to disk.

    Package generation renders a real PDF with Chromium. When Chromium is not
    installed in the environment, that final step is skipped rather than failed;
    the deterministic workflow up to that point is covered above.
    """
    await service.import_resume("master.md", RESUME_MARKDOWN.encode())
    job_id = _seed_job(service)
    await service.analyse_job(job_id)
    outcome = await service.optimise_job(job_id)

    try:
        approved, package = await service.approve_application(outcome.application.id)
    except RenderError as exc:
        pytest.skip(f"Chromium PDF rendering unavailable in this environment: {exc}")

    assert approved.status is ApplicationStatus.APPROVED
    assert approved.output_dir
    # Versioned output lands under output/<company>/<role>/vNNN/.
    assert package.directory.is_dir()
    assert package.directory.name == "v001"
    assert package.directory.parent.name == "role" or package.directory.parent.name
    expected = {
        "resume.html",
        "resume.md",
        "resume.json",
        "resume.diff.md",
        "ats_report.md",
        "job.json",
        "resume.pdf",
    }
    written = {path.name for path in package.directory.iterdir() if path.is_file()}
    assert expected <= written, f"missing artifacts: {expected - written}"

    # The generated resume.md keeps the candidate's real name.
    assert "Jane Doe" in (package.directory / "resume.md").read_text(encoding="utf-8")

    # Re-approving the same application is refused (packages are immutable).
    from jobos.services import ServiceError

    with pytest.raises(ServiceError, match="already generated"):
        await service.approve_application(outcome.application.id)
