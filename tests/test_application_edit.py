"""Tests for editing/previewing/generating the tailored resume on the application
review page — edit, preview, and generate without leaving the page."""

from __future__ import annotations

import pytest

from jobos.services import JobOSService, ServiceError


async def _draft_application(service: JobOSService) -> int:
    from tests.conftest import RESUME_MARKDOWN

    await service.import_resume("m.md", RESUME_MARKDOWN.encode())
    job = service.add_job_from_text(
        title="Backend Engineer",
        company="Acme",
        description="Backend role. Required Python and FastAPI. Build REST APIs and tests. 4+ years.",
    )
    await service.analyse_job(job.id)
    outcome = await service.optimise_job(job.id)
    return outcome.application.id


async def test_application_resume_yaml_roundtrips(service: JobOSService) -> None:
    app_id = await _draft_application(service)
    y = service.application_resume_yaml(app_id)
    assert "contact" in y and "experience" in y
    # Edit and save -> stored on the application, master untouched.
    master_before = service.master_resume().resume.model_dump()
    updated = service.save_application_resume_yaml(app_id, y)
    assert updated.id == app_id
    assert service.master_resume().resume.model_dump() == master_before


async def test_save_application_resume_rejects_invalid_yaml(service: JobOSService) -> None:
    app_id = await _draft_application(service)
    with pytest.raises(ServiceError):
        service.save_application_resume_yaml(app_id, "contact: {name: }\n:::not valid")


async def test_cannot_edit_after_generation(service: JobOSService) -> None:
    app_id = await _draft_application(service)
    y = service.application_resume_yaml(app_id)
    try:
        await service.approve_application(app_id)
    except Exception as exc:  # Chromium may be unavailable in CI
        pytest.skip(f"PDF generation unavailable: {exc}")
    # Once generated, the application is immutable.
    with pytest.raises(ServiceError):
        service.save_application_resume_yaml(app_id, y)
