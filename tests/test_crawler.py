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


def _role(job_id: int, title: str) -> dict[str, object]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": "Bengaluru, India"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "&lt;p&gt;Required: Python. 4+ years experience.&lt;/p&gt;",
    }


async def test_roles_dropped_from_the_board_are_closed(
    config: AppConfig, database: Database
) -> None:
    """A posting the board stopped listing must leave the searchable set."""
    first = {"jobs": [_role(1, "Backend Engineer"), _role(2, "Frontend Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(first)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    assert len(database.list_jobs()) == 2

    # The second role is gone from the board on the next crawl.
    second = {"jobs": [_role(1, "Backend Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(second)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")

    assert summary.jobs_closed == 1
    remaining = database.list_jobs()
    assert len(remaining) == 1
    assert remaining[0].title == "Backend Engineer"


async def test_a_truncated_board_never_closes_the_roles_it_did_not_see(
    config: AppConfig, database: Database
) -> None:
    """Hitting per_company_limit means the tail was never fetched, so nothing closes."""
    config.crawler.per_company_limit = 3
    full = {"jobs": [_role(index, f"Engineer {index}") for index in range(1, 6)]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(full)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    stored = len(database.list_jobs())
    assert stored == 3  # capped

    async with httpx.AsyncClient(transport=_greenhouse_transport(full)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")

    assert summary.jobs_closed == 0
    assert len(database.list_jobs()) == stored


async def test_a_failed_company_does_not_close_its_roles(
    config: AppConfig, database: Database
) -> None:
    """A transient board outage must not wipe out everything stored for it."""
    payload = {"jobs": [_role(1, "Backend Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(payload)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    assert len(database.list_jobs()) == 1

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")

    assert summary.errors  # the company failed
    assert summary.jobs_closed == 0
    assert len(database.list_jobs()) == 1


def test_derive_company_name_from_various_urls() -> None:
    from jobos.crawler import derive_company_name

    assert derive_company_name("https://boards.greenhouse.io/acme") == "Acme"
    assert derive_company_name("https://job-boards.greenhouse.io/big-corp") == "Big Corp"
    assert derive_company_name("https://jobs.lever.co/rocket-labs") == "Rocket Labs"
    assert derive_company_name("https://acme.wd1.myworkdayjobs.com/en-US/External") == "Acme"
    assert derive_company_name("https://careers.foobar.com/openings") == "Foobar"
    assert derive_company_name("https://www.zoho.com/careers/") == "Zoho"


async def test_crawl_settings_stores_jobs_for_ad_hoc_url(
    config: AppConfig, database: Database
) -> None:
    """An ad-hoc URL not in config is crawled through the normal adapters."""
    from jobos.config import CompanySettings

    payload = {"jobs": [_role(1, "Backend Engineer"), _role(2, "Platform Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(payload)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl_settings(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert summary.companies_succeeded == 1
    assert summary.jobs_added == 2
    assert {job.title for job in database.list_jobs()} == {"Backend Engineer", "Platform Engineer"}


async def test_ad_hoc_crawl_never_closes_existing_jobs(
    config: AppConfig, database: Database
) -> None:
    """A configured crawl closes delisted roles; an ad-hoc re-crawl must not."""
    from jobos.config import CompanySettings

    two = {"jobs": [_role(1, "Backend Engineer"), _role(2, "Platform Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(two)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    assert len(database.list_jobs()) == 2

    # Same source, ad-hoc, now returning only one role: the other must stay active.
    one = {"jobs": [_role(1, "Backend Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(one)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl_settings(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert summary.jobs_closed == 0
    assert len(database.list_jobs()) == 2


def test_as_datetime_handles_every_board_format() -> None:
    """Each adapter reports its publish date differently; all must parse."""
    from datetime import UTC, datetime

    from jobos.crawler import _as_datetime

    # Greenhouse / Ashby / SmartRecruiters: ISO 8601, with an offset or a Z.
    # 10:28 at UTC-4 is 14:28 UTC.
    assert _as_datetime("2026-07-15T10:28:32-04:00") == datetime(
        2026, 7, 15, 14, 28, 32, tzinfo=UTC
    )
    assert _as_datetime("2026-08-21T11:45:11.299Z") == datetime(
        2026, 8, 21, 11, 45, 11, 299_000, tzinfo=UTC
    )
    # Workday: a plain calendar date, assumed UTC.
    workday = _as_datetime("2026-08-30")
    assert workday is not None and workday.tzinfo is not None
    # Lever: epoch milliseconds.
    lever = _as_datetime(1782901087094)
    assert lever is not None and lever.year == 2026
    # Junk and blanks are dropped rather than guessed at.
    assert _as_datetime(None) is None
    assert _as_datetime("") is None
    assert _as_datetime("Posted Yesterday") is None
    assert _as_datetime(True) is None


async def test_greenhouse_captures_the_published_date(
    config: AppConfig, database: Database
) -> None:
    """A crawl records the employer's publish date so `posted_days` can use it."""
    payload = {
        "jobs": [
            {
                "id": 7,
                "title": "Backend Engineer",
                "location": {"name": "Bengaluru, India"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                "content": "&lt;p&gt;Python. 4+ years.&lt;/p&gt;",
                "first_published": "2026-08-20T10:00:00+00:00",
                "updated_at": "2026-08-28T10:00:00+00:00",
            }
        ]
    }
    async with httpx.AsyncClient(transport=_greenhouse_transport(payload)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    stored = database.list_jobs()[0]
    assert stored.posted_at is not None
    # first_published wins over updated_at: an edit is not a repost.
    assert stored.posted_at.day == 20


async def test_each_company_is_stored_as_it_finishes(
    config: AppConfig, database: Database
) -> None:
    """A slow board must not hold back rows from one that already finished.

    Previously every company was fetched before anything was written, so a long crawl
    showed no progress and an interruption lost all of it.
    """
    from jobos.config import CompanySettings

    config.companies = {
        "fast": CompanySettings(name="Fast", url="https://boards.greenhouse.io/fast"),
        "slow": CompanySettings(name="Slow", url="https://boards.greenhouse.io/slow"),
    }
    # One company at a time, so "fast" is fully handled before "slow" is requested.
    config.crawler.concurrency = 1
    rows_when_slow_ran: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/slow/" in str(request.url):
            rows_when_slow_ran.append(len(database.list_jobs()))
            return httpx.Response(200, json={"jobs": [_role(2, "Slow Engineer")]})
        return httpx.Response(200, json={"jobs": [_role(1, "Fast Engineer")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl()

    # The heart of it: the first board's rows were already committed while the second
    # was still being fetched. Under the old batch-at-the-end write this was 0.
    assert rows_when_slow_ran == [1]
    assert summary.companies_succeeded == 2
    assert len(database.list_jobs()) == 2


async def test_a_supplied_summary_is_filled_in_place(
    config: AppConfig, database: Database
) -> None:
    """The dashboard polls this object while the crawl runs, so it must be the same one."""
    from jobos.models import CrawlSummary

    live = CrawlSummary()
    payload = {"jobs": [_role(1, "Backend Engineer")]}
    async with httpx.AsyncClient(transport=_greenhouse_transport(payload)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        returned = await crawler.crawl("acme", summary=live)

    assert returned is live
    assert live.companies_requested == 1
    assert live.jobs_added == 1
