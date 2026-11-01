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


def _smartrecruiters_transport(
    pages: dict[int, dict[str, object]],
    *,
    expect_country: str | None = None,
) -> httpx.MockTransport:
    """Serve paginated SmartRecruiters listings keyed by offset, plus detail pages.

    ``pages`` maps an offset to the listing payload returned for that offset. Any
    posting id requested as a detail is answered with a minimal valid jobAd so the
    adapter can build a JobData.
    """
    from urllib.parse import parse_qs, urlsplit

    def handler(request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        query = parse_qs(parts.query)
        if parts.path.rstrip("/").endswith("/postings"):
            if expect_country is not None:
                assert query.get("country", [""])[0] == expect_country
            offset = int(query.get("offset", ["0"])[0])
            return httpx.Response(200, json=pages.get(offset, {"content": [], "totalFound": 0}))
        # Detail request: /postings/<id>
        job_id = parts.path.rstrip("/").split("/")[-1]
        return httpx.Response(
            200,
            json={
                "name": f"Role {job_id}",
                "applyUrl": f"https://jobs.smartrecruiters.com/Acme/{job_id}",
                "location": {"city": "Bengaluru", "country": "India"},
                "jobAd": {"sections": {"jobDescription": {"text": "Python. 4+ years."}}},
            },
        )

    return httpx.MockTransport(handler)


def _sr_listing(ids: list[int], total: int) -> dict[str, object]:
    return {
        "content": [{"id": str(i), "name": f"Role {i}"} for i in ids],
        "totalFound": total,
    }


async def test_smartrecruiters_paginates_beyond_one_page(
    config: AppConfig, database: Database
) -> None:
    """A board with more than one page must be fully read, not truncated at 100."""
    config.companies = {
        "acme": config.companies["acme"].model_copy(
            update={"url": "https://jobs.smartrecruiters.com/Acme"}
        )
    }
    config.preferences.preferred_locations = ["India", "Hyderabad"]
    # Two pages of 100, reported via totalFound=150.
    pages = {
        0: _sr_listing(list(range(0, 100)), total=150),
        100: _sr_listing(list(range(100, 150)), total=150),
    }
    transport = _smartrecruiters_transport(pages, expect_country="in")
    async with httpx.AsyncClient(transport=transport) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")
    assert summary.errors == {}
    # All 150 postings across both pages were stored, not just the first 100.
    assert len(database.list_jobs(limit=5_000)) == 150


async def test_smartrecruiters_respects_per_company_limit(
    config: AppConfig, database: Database
) -> None:
    config.companies = {
        "acme": config.companies["acme"].model_copy(
            update={"url": "https://jobs.smartrecruiters.com/Acme"}
        )
    }
    config.crawler.per_company_limit = 120
    pages = {
        0: _sr_listing(list(range(0, 100)), total=500),
        100: _sr_listing(list(range(100, 200)), total=500),
    }
    async with httpx.AsyncClient(transport=_smartrecruiters_transport(pages)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("acme")
    # Stops at the configured cap rather than draining all 500.
    assert len(database.list_jobs(limit=5_000)) == 120


async def test_smartrecruiters_country_filter_follows_preferences(
    config: AppConfig, database: Database
) -> None:
    """The India country filter is applied only when India is a preferred location."""
    config.companies = {
        "acme": config.companies["acme"].model_copy(
            update={"url": "https://jobs.smartrecruiters.com/Acme"}
        )
    }
    config.preferences.preferred_locations = ["Berlin", "Remote"]  # no India
    pages = {0: _sr_listing([1, 2], total=2)}
    # expect_country="" asserts no country param is sent when India isn't preferred.
    transport = _smartrecruiters_transport(pages, expect_country="")
    async with httpx.AsyncClient(transport=transport) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("acme")
    assert summary.errors == {}
    assert len(database.list_jobs(limit=5_000)) == 2


ORACLE_HOST = "ebwg.fa.us2.oraclecloud.com"


def _oracle_transport(total: int, page_size: int = 50) -> httpx.MockTransport:
    """Fake Oracle Recruiting Cloud: a paginated requisition list + detail endpoint."""
    from urllib.parse import parse_qs, urlsplit

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert ORACLE_HOST in url
        if "recruitingCEJobRequisitionDetails" in url:
            # finder=ById;Id="N",siteNumber=CX
            finder = parse_qs(urlsplit(url).query).get("finder", [""])[0]
            rid = finder.split('Id="', 1)[1].split('"', 1)[0] if 'Id="' in finder else "0"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "Title": f"Engineer {rid}",
                            "PrimaryLocation": "Bengaluru, India",
                            "ExternalDescriptionStr": "<p>Python and Java. 4+ years.</p>",
                            "ExternalQualificationsStr": "",
                            "ExternalResponsibilitiesStr": "",
                        }
                    ]
                },
            )
        # Listing request: read the offset out of the finder.
        finder = parse_qs(urlsplit(url).query).get("finder", [""])[0]
        offset = 0
        for part in finder.split(","):
            if part.startswith("offset="):
                offset = int(part.split("=", 1)[1])
        remaining = max(0, total - offset)
        count = min(page_size, remaining)
        reqs = [
            {
                "Id": str(offset + i),
                "Title": f"Engineer {offset + i}",
                "PrimaryLocation": "Bengaluru, India",
                "PostedDate": "2026-08-20",
                "JobType": "Full-time",
            }
            for i in range(count)
        ]
        return httpx.Response(
            200, json={"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}
        )

    return httpx.MockTransport(handler)


async def test_oracle_crawl_paginates_and_fetches_descriptions(
    config: AppConfig, database: Database
) -> None:
    from jobos.config import CompanySettings

    url = f"https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX"
    config.companies = {"aci": CompanySettings(name="ACI", url=url)}
    # 120 reqs forces three pages of 50/50/20.
    async with httpx.AsyncClient(transport=_oracle_transport(total=120)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl("aci")
    assert summary.errors == {}
    stored = database.list_jobs(limit=5_000)
    assert len(stored) == 120  # all pages read
    one = stored[0]
    assert one.location == "Bengaluru, India"
    assert "python" in [s.casefold() for s in one.skills]
    # Java mentioned as an alternative must NOT be dropped at crawl time.
    assert "java" in [s.casefold() for s in one.skills]
    assert one.posted_at is not None


async def test_oracle_respects_per_company_limit(
    config: AppConfig, database: Database
) -> None:
    from jobos.config import CompanySettings

    url = f"https://{ORACLE_HOST}/hcmUI/CandidateExperience/en/sites/CX_1001"
    config.companies = {"aci": CompanySettings(name="ACI", url=url)}
    config.crawler.per_company_limit = 30
    async with httpx.AsyncClient(transport=_oracle_transport(total=500)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        await crawler.crawl("aci")
    assert len(database.list_jobs(limit=5_000)) == 30


async def test_ats_wave_filter_crawls_only_matching_boards(
    config: AppConfig, database: Database
) -> None:
    """`ats_types` restricts a crawl to one speed wave and leaves others untouched."""
    from jobos.config import CompanySettings

    config.companies = {
        "gh": CompanySettings(name="GH", url="https://boards.greenhouse.io/gh"),
        "lev": CompanySettings(name="Lev", url="https://jobs.lever.co/lev"),
        "wd": CompanySettings(
            name="WD", url="https://wd.wd1.myworkdayjobs.com/en-US/External"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "greenhouse" in url:
            return httpx.Response(200, json={"jobs": [_role(1, "Backend Engineer")]})
        if "lever" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "2",
                        "text": "Platform Engineer",
                        "categories": {"location": "Remote"},
                        "applyUrl": "https://jobs.lever.co/lev/2",
                        "descriptionPlain": "Python. 4+ years.",
                    }
                ],
            )
        # Workday should never be called in the fast wave.
        raise AssertionError(f"workday was crawled in the fast wave: {url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl(ats_types={"greenhouse", "lever", "ashby"})

    # Only the two fast boards were requested and succeeded; Workday was excluded.
    assert summary.companies_requested == 2
    assert summary.companies_succeeded == 2
    assert {job.company for job in database.list_jobs()} == {"GH", "Lev"}


async def test_ats_wave_none_crawls_everything(
    config: AppConfig, database: Database
) -> None:
    """Omitting the wave filter crawls all enabled boards, as before."""
    config.companies = {
        "gh": config.companies["acme"].model_copy(
            update={"url": "https://boards.greenhouse.io/gh", "name": "GH"}
        ),
    }
    async with httpx.AsyncClient(transport=_greenhouse_transport({"jobs": [_role(1, "Eng")]})) as client:
        crawler = CareerCrawler(config, database, client=client, allow_private_networks=True)
        summary = await crawler.crawl(ats_types=None)
    assert summary.companies_requested == 1


async def test_crawl_waves_runs_fast_then_medium_then_slow(
    config: AppConfig, database: Database
) -> None:
    """crawl_waves crawls each speed tier in order and reports per-wave results."""
    from jobos.config import CompanySettings

    config.companies = {
        "gh": CompanySettings(name="GH", url="https://boards.greenhouse.io/gh"),
        "sr": CompanySettings(name="SR", url="https://jobs.smartrecruiters.com/SR"),
        "wd": CompanySettings(
            name="WD", url="https://wd.wd1.myworkdayjobs.com/en-US/External"
        ),
    }
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "greenhouse" in url:
            order.append("fast")
            return httpx.Response(200, json={"jobs": [_role(1, "Backend Engineer")]})
        if "smartrecruiters" in url:
            if url.rstrip("/").endswith("/postings") or "/postings?" in url:
                order.append("medium")
                return httpx.Response(200, json={"content": [], "totalFound": 0})
            return httpx.Response(200, json={"content": [], "totalFound": 0})
        if "myworkdayjobs" in url:
            order.append("slow")
            return httpx.Response(200, json={"jobPostings": [], "total": 0})
        return httpx.Response(404)

    from jobos.services import JobOSService

    service = JobOSService(config, database)
    # Drive the crawler through a shared client via monkeypatched crawl? Simpler:
    # exercise the service method with a patched AsyncClient through CareerCrawler.
    import jobos.services as services_mod

    transport = httpx.MockTransport(handler)

    class _PatchedCrawler(CareerCrawler):
        def __init__(self, cfg, db, **kw):  # type: ignore[no-untyped-def]
            super().__init__(
                cfg,
                db,
                client=httpx.AsyncClient(transport=transport),
                allow_private_networks=True,
            )

    services_mod.CareerCrawler = _PatchedCrawler  # type: ignore[misc]
    try:
        results = await service.crawl_waves()
    finally:
        services_mod.CareerCrawler = CareerCrawler  # type: ignore[misc]

    assert [r.wave for r in results] == ["fast", "medium", "slow"]
    # Fast ran before medium ran before slow.
    assert order and order[0] == "fast" and order[-1] == "slow"
    # Fast board stored its job and is searchable before the later waves matter.
    assert any(job.company == "GH" for job in database.list_jobs())
