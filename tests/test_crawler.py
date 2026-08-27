"""Crawler parsing, deduplication, and network safety."""

from __future__ import annotations

import httpx
import pytest

from jobos.config import AppConfig
from jobos.crawler import (
    CareerCrawler,
    CrawlError,
    canonical_url,
    job_fingerprint,
    parse_json_ld_jobs,
)
from jobos.database import Database
from jobos.models import JobData

JSON_LD_PAGE = r"""
<html><head>
<script type="application/ld+json">
{"@type":"JobPosting","title":"Senior Backend Engineer","identifier":"REQ-1",
 "description":"\u003cp\u003eRequired: \u003cb\u003ePython\u003c/b\u003e, FastAPI. 5+ years experience.\u003c/p\u003e\u003cscript\u003ealert(1)\u003c/script\u003e",
 "url":"https://careers.example.com/jobs/backend-1","employmentType":"FULL_TIME",
 "jobLocationType":"TELECOMMUTE",
 "jobLocation":{"address":{"addressLocality":"Hyderabad","addressCountry":"IN"}}}
</script></head><body><h1>Careers</h1></body></html>
"""


def test_canonical_url_drops_tracking_and_case() -> None:
    assert (
        canonical_url("https://X.example.com/jobs/1/?utm_source=x&gh_src=y&id=7")
        == "https://x.example.com/jobs/1?id=7"
    )


def test_json_ld_extraction_strips_scripts() -> None:
    jobs = parse_json_ld_jobs(JSON_LD_PAGE, "Example", "https://careers.example.com/")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Backend Engineer"
    assert "Remote" in job.location and "Hyderabad" in job.location
    assert "alert(1)" not in job.description
    assert {"python", "fastapi"} <= {s.casefold() for s in job.skills}
    assert "5+ years" in job.experience
    assert job.external_id == "REQ-1"


def test_json_ld_page_without_postings_yields_nothing() -> None:
    assert parse_json_ld_jobs("<html><body>no jobs</body></html>", "X", "https://x.test/") == []


def test_fingerprint_is_stable_and_distinguishes_roles(job_data: JobData) -> None:
    same = job_data.model_copy(deep=True)
    assert job_fingerprint(job_data) == job_fingerprint(same)
    other = job_data.model_copy(update={"title": "Staff Python Engineer"})
    assert job_fingerprint(job_data) != job_fingerprint(other)


def _greenhouse_transport(payload: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boards-api.greenhouse.io" in str(request.url)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


GREENHOUSE_PAYLOAD: dict[str, object] = {
    "jobs": [
        {
            "id": 11,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
            "content": "&lt;p&gt;Required: Python and Docker. 4+ years experience.&lt;/p&gt;",
        },
        {
            "id": 11,
            "title": "Platform Engineer",
            "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
            "content": "&lt;p&gt;Required: Python and Docker. 4+ years experience.&lt;/p&gt;",
        },
        {"id": 12, "title": "Missing URL role", "location": {"name": "Remote"}},
    ]
}


async def test_greenhouse_crawl_dedupes_and_skips_invalid(
    config: AppConfig, database: Database
) -> None:
    async with httpx.AsyncClient(transport=_greenhouse_transport(GREENHOUSE_PAYLOAD)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")
    assert summary.companies_succeeded == 1
    assert summary.jobs_added == 1
    assert summary.errors == {}
    stored = database.list_jobs()
    assert len(stored) == 1
    assert stored[0].title == "Platform Engineer"
    assert "docker" in stored[0].skills


async def test_recrawl_updates_instead_of_duplicating(
    config: AppConfig, database: Database
) -> None:
    transport = _greenhouse_transport(GREENHOUSE_PAYLOAD)
    for _ in range(2):
        async with httpx.AsyncClient(transport=transport) as client:
            crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
            summary = await crawler.crawl("acme")
    assert summary.jobs_added == 0
    assert summary.jobs_updated == 1
    assert len(database.list_jobs()) == 1


async def test_unknown_company_is_an_error(config: AppConfig, database: Database) -> None:
    crawler = CareerCrawler(config, database, allow_private_networks=True)
    with pytest.raises(CrawlError, match="not configured"):
        await crawler.crawl("nope")


async def test_private_network_targets_are_refused(
    config: AppConfig, database: Database
) -> None:
    from jobos.config import CompanySettings

    config.companies = {"local": CompanySettings(name="Local", url="http://127.0.0.1:9/jobs")}
    crawler = CareerCrawler(config, database)
    summary = await crawler.crawl("local")
    assert "local" in summary.errors
    assert "private" in summary.errors["local"].casefold()


async def test_http_errors_are_reported_per_company(
    config: AppConfig, database: Database
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")
    assert summary.companies_succeeded == 0
    assert "503" in summary.errors["acme"]


async def test_disabled_companies_are_skipped(config: AppConfig, database: Database) -> None:
    config.companies["acme"].enabled = False
    crawler = CareerCrawler(config, database, allow_private_networks=True)
    summary = await crawler.crawl()
    assert summary.companies_requested == 0
    assert database.list_jobs() == []
