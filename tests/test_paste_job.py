"""Tests for adding a job from pasted JD text (for portals the crawler can't read)."""

from __future__ import annotations

import pytest

from jobos.services import JobOSService, ServiceError

JD = (
    "AI Engineer at NetApp, Bangalore. Build AI features using Python, machine learning, "
    "and large language models (LLMs). Experience with RAG and AWS required. 5+ years of "
    "software engineering. Build scalable backend services and REST APIs."
)


async def test_add_job_from_text_derives_skills_and_scores(service: JobOSService) -> None:
    from tests.conftest import RESUME_MARKDOWN

    await service.import_resume("m.md", RESUME_MARKDOWN.encode())
    job = service.add_job_from_text(title="AI Engineer", company="NetApp", description=JD)

    # Skills and the years line are derived from the text, like a crawled job.
    assert "python" in job.skills
    assert any("year" in job.experience.lower() for _ in [0]) or job.experience
    # It flows through the normal analyse/score pipeline.
    _, analysis, report = await service.analyse_job(job.id)
    assert report.overall_score > 0
    assert analysis.required_skills


def test_add_job_from_text_accepts_url_and_placeholder(service: JobOSService) -> None:
    with_url = service.add_job_from_text(
        title="AI Engineer", company="NetApp", description=JD,
        url="https://jobs.netapp.com/job/x/1430527700/",
    )
    assert with_url.apply_url.startswith("https://jobs.netapp.com")

    no_url = service.add_job_from_text(title="AI Engineer 2", company="NetApp", description=JD)
    # No URL -> a stable local placeholder, never a fake employer link.
    assert "localhost/jobos/pasted/" in no_url.apply_url


def test_add_job_from_text_validates_input(service: JobOSService) -> None:
    with pytest.raises(ServiceError):
        service.add_job_from_text(title="", company="NetApp", description=JD)
    with pytest.raises(ServiceError):
        service.add_job_from_text(title="AI Engineer", company="", description=JD)
    with pytest.raises(ServiceError):
        service.add_job_from_text(title="AI Engineer", company="NetApp", description="too short")
