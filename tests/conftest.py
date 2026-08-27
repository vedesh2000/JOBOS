"""Shared fixtures. Every fixture is local and offline."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobos.ai import AIClient
from jobos.config import AISettings, AppConfig, CompanySettings
from jobos.database import Database
from jobos.models import (
    Certification,
    Contact,
    Education,
    Experience,
    JobData,
    JobView,
    Project,
    ResumeBullet,
    StructuredResume,
)
from jobos.services import JobOSService

RESUME_MARKDOWN = """# Jane Doe
jane@example.com | +91 90000 00000 | Hyderabad, India
https://github.com/janedoe

## Summary
Backend engineer building Python services and data pipelines for billing systems.

## Skills
Python, FastAPI, PostgreSQL, AWS, Docker, SQL

## Experience
Acme Corp — Senior Software Engineer
Jan 2021 - Present | Hyderabad
- Built Python REST services with FastAPI serving 40k requests per day.
- Reduced AWS spend by 18% by right-sizing workloads.
- Wrote integration tests that cut regressions in the billing module.

Beta Labs — Software Engineer
Jun 2018 - Dec 2020 | Bengaluru
- Maintained PostgreSQL reporting jobs for internal analytics.

## Projects
Ledger Toolkit
- Parsed statements from three bank formats using Python.

## Education
Example University
B.Tech — Computer Science

## Certifications
AWS Certified Developer
"""

JOB_DESCRIPTION = (
    "About Acme: we value collaboration and impact.\n"
    "Required: strong Python and FastAPI experience with PostgreSQL. "
    "Must have 4+ years experience.\n"
    "Preferred: Kubernetes and Terraform are nice to have.\n"
    "You will design reliable backend services, own billing integrations, and mentor engineers.\n"
    "Bachelor's degree in Computer Science or equivalent.\n"
)


@pytest.fixture
def resume() -> StructuredResume:
    return StructuredResume(
        contact=Contact(
            name="Jane Doe",
            email="jane@example.com",
            phone="+91 90000 00000",
            location="Hyderabad, India",
            links=["https://github.com/janedoe"],
        ),
        summary="Backend engineer focused on Python services and data pipelines.",
        skills=["Python", "FastAPI", "PostgreSQL", "AWS", "Docker"],
        experience=[
            Experience(
                id="exp-acme",
                company="Acme Corp",
                title="Senior Software Engineer",
                start_date="Jan 2021",
                end_date="Present",
                location="Hyderabad",
                skills=["Python", "FastAPI", "AWS"],
                bullets=[
                    ResumeBullet(
                        id="bul-api",
                        text="Built Python REST services with FastAPI serving 40k requests per day.",
                    ),
                    ResumeBullet(
                        id="bul-cost", text="Reduced AWS spend by 18% by right-sizing workloads."
                    ),
                    ResumeBullet(
                        id="bul-test",
                        text="Wrote integration tests that cut regressions in the billing module.",
                    ),
                ],
            ),
            Experience(
                id="exp-beta",
                company="Beta Labs",
                title="Software Engineer",
                start_date="Jun 2018",
                end_date="Dec 2020",
                skills=["Python", "PostgreSQL"],
                bullets=[
                    ResumeBullet(
                        id="bul-report",
                        text="Maintained PostgreSQL reporting jobs for internal analytics.",
                    )
                ],
            ),
        ],
        projects=[
            Project(
                id="prj-ledger",
                name="Ledger Toolkit",
                url="https://github.com/janedoe/ledger",
                description="Command line tool for reconciling statements.",
                skills=["Python"],
                bullets=[
                    ResumeBullet(id="pbul-parse", text="Parsed statements from three bank formats.")
                ],
            )
        ],
        education=[
            Education(
                id="edu-1",
                institution="Example University",
                degree="B.Tech",
                field="Computer Science",
                start_date="2014",
                end_date="2018",
            )
        ],
        certifications=[
            Certification(id="cert-1", name="AWS Certified Developer", issuer="Amazon", date="2022")
        ],
        achievements=[
            ResumeBullet(id="achv-1", text="Received an internal engineering award in 2023.")
        ],
        languages=["English", "Telugu"],
    )


@pytest.fixture
def job_data() -> JobData:
    return JobData(
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


@pytest.fixture
def job(job_data: JobData) -> JobView:
    now = datetime.now(UTC)
    return JobView(id=1, **job_data.model_dump(), first_seen_at=now, last_seen_at=now)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """A fully offline configuration with every AI task disabled by default."""
    settings = AISettings()
    for name in type(settings.tasks).model_fields:
        setattr(settings.tasks, name, False)
    return AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "out",
        ai=settings,
        companies={"acme": CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")},
        config_path=tmp_path / "config.yaml",
    )


@pytest.fixture
def database(config: AppConfig) -> Iterator[Database]:
    config.ensure_directories()
    db = Database(config.database_path)
    db.initialize()
    yield db


@pytest.fixture
def service(config: AppConfig, database: Database) -> JobOSService:
    return JobOSService(config, database)


class RecordingAI:
    """A fake Ollama endpoint that records requests and replays canned objects."""

    def __init__(self, responses: list[dict[str, Any]], settings: AISettings | None = None) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.settings = settings or AISettings()

    def client(self) -> AIClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            payload = self.responses.pop(0) if self.responses else {}
            return httpx.Response(200, json={"message": {"content": json.dumps(payload)}})

        transport = httpx.MockTransport(handler)
        return AIClient(self.settings, client=httpx.AsyncClient(transport=transport))

    @property
    def last_payload(self) -> dict[str, Any]:
        content = self.requests[-1]["messages"][-1]["content"]
        return dict(json.loads(content.split("UNTRUSTED_DATA:\n", 1)[1]))


@pytest.fixture
def recording_ai() -> type[RecordingAI]:
    return RecordingAI
