"""Validate candidate company boards against their ATS public API before adoption.

The guiding rule here is that **HTTP 200 is never proof a board is valid**. A board
is only adopted into config.yaml if, after a real crawl through the existing adapters:

* its host is one JobOS actually has a JSON adapter for (otherwise it is flagged
  ``UNSUPPORTED`` for manual review rather than silently HTML-scraped),
* the ATS answered with the shape that ATS is supposed to return (a Greenhouse board
  that returns an HTML login page is ``MALFORMED``, not valid),
* it lists at least one parseable posting (an empty board is ``EMPTY``), and
* at least one posting is relevant to the candidate (India/remote-India location and,
  for the stricter "relevant" count, a target engineering title).

This module adds no new network machinery: it drives the same ``CareerCrawler`` the
real crawl uses, so a board that validates here crawls identically later.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Sequence

import httpx

from jobos.classification import classify_company, is_gcc
from jobos.config import AppConfig, CompanySettings, slugify_company
from jobos.crawler import (
    USER_AGENT,
    CareerCrawler,
    CrawlError,
    derive_company_name,
    detect_ats_type,
)
from jobos.database import Database
from jobos.models import (
    BoardStatus,
    BoardValidation,
    JobData,
    ValidationSummary,
)

# India signal in a posting's location string. Remote postings explicitly open to
# India ("Remote - India", "India (Remote)") count; a bare "Remote" does not, since we
# cannot tell it is open to India from the location alone.
_INDIA_TERMS = (
    "india",
    "hyderabad",
    "bengaluru",
    "bangalore",
    "pune",
    "gurugram",
    "gurgaon",
    "noida",
    "chennai",
    "mumbai",
    "delhi",
    "kolkata",
    "ahmedabad",
)
_REMOTE_INDIA = re.compile(r"remote[^.]*\b(india|in|apac)\b|\b(india|apac)\b[^.]*remote", re.I)

# Target role titles: SDE II / Senior / Backend / Full Stack / Platform / AI
# Application Engineer and the backend-leaning individual-contributor family. Matched
# on the title only; the description is not fetched for this coarse pass.
_TARGET_TITLE = re.compile(
    r"\b("
    r"software\s+engineer|sde|backend|back[-\s]?end|full[-\s]?stack|platform|"
    r"distributed|infrastructure|senior\s+engineer|staff\s+engineer|"
    r"application\s+engineer|developer|ai\s+engineer"
    r")\b",
    re.I,
)
# Titles that look like a match but are not the target profile.
_TITLE_EXCLUDE = re.compile(
    r"\b(manager|director|vp|head|intern|principal\s+manager|qa|sdet|"
    r"support|sales|marketing|recruit|hr|finance|data\s+scientist)\b",
    re.I,
)


def _is_remote(location: str) -> bool:
    return "remote" in (location or "").casefold()


def is_india_location(location: str) -> bool:
    text = (location or "").casefold()
    if any(term in text for term in _INDIA_TERMS):
        return True
    return bool(_REMOTE_INDIA.search(location or ""))


def is_target_title(title: str) -> bool:
    if _TITLE_EXCLUDE.search(title or ""):
        return False
    return bool(_TARGET_TITLE.search(title or ""))


class BoardValidator:
    """Validate candidate boards by crawling them through the real adapters."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        client: httpx.AsyncClient | None = None,
        allow_private_networks: bool = False,
        min_postings: int = 1,
    ) -> None:
        self.config = config
        self.database = database
        self._client = client
        self._allow_private = allow_private_networks
        # A board must expose at least this many parseable postings to count as more
        # than EMPTY. One is enough; the relevance gate does the real filtering.
        self.min_postings = max(min_postings, 1)

    async def validate_candidates(
        self,
        candidates: Sequence[CompanySettings],
        *,
        on_result: Callable[[int, int, BoardValidation], None] | None = None,
    ) -> ValidationSummary:
        """Validate a batch of candidate boards, respecting crawler concurrency.

        ``on_result`` is called as each board finishes with ``(done, total, result)``,
        so a caller can show live progress instead of waiting for the whole batch. It
        runs on the event loop thread, so it must be cheap and non-blocking.
        """
        summary = ValidationSummary()
        if not candidates:
            return summary
        started = time.monotonic()
        if self._client is None:
            timeout = httpx.Timeout(self.config.crawler.timeout_seconds)
            async with httpx.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html"},
                follow_redirects=False,
            ) as client:
                self._client = client
                try:
                    results = await self._run_batch(candidates, on_result)
                finally:
                    self._client = None
        else:
            results = await self._run_batch(candidates, on_result)
        for result in results:
            summary.record(result)
        summary.duration_seconds = round(time.monotonic() - started, 2)
        return summary

    async def _run_batch(
        self,
        candidates: Sequence[CompanySettings],
        on_result: Callable[[int, int, BoardValidation], None] | None = None,
    ) -> list[BoardValidation]:
        # Bounded the same way the crawler bounds companies, so validating a large
        # candidate list is as polite as a normal crawl.
        limit = asyncio.Semaphore(min(self.config.crawler.concurrency, 5))
        total = len(candidates)
        completed = 0
        results: list[BoardValidation | None] = [None] * total

        async def run(index: int, company: CompanySettings) -> None:
            nonlocal completed
            async with limit:
                result = await self.validate_one(company)
            results[index] = result
            completed += 1
            if on_result is not None:
                on_result(completed, total, result)

        await asyncio.gather(
            *(run(index, company) for index, company in enumerate(candidates))
        )
        return [result for result in results if result is not None]

    async def validate_one(self, company: CompanySettings) -> BoardValidation:
        """Validate a single candidate board.

        Reuses the crawler so the fetch, SSRF guard, robots handling and parsing are
        identical to a real crawl. The verdict is then computed from the parsed jobs,
        never from the HTTP status alone.
        """
        slug = slugify_company(company.name or derive_company_name(company.url))
        ats_type = detect_ats_type(company.url)
        result = BoardValidation(
            slug=slug,
            name=company.name,
            url=company.url,
            ats_type=ats_type,
            # A name/ATS-based first guess, refined in _classify once roles are known.
            category=classify_company(company.name, ats_type),
            is_gcc=is_gcc(company.name),
        )
        if not ats_type:
            result.status = BoardStatus.UNSUPPORTED
            result.detail = (
                "No JSON ATS adapter for this host; flagged for manual review rather "
                "than HTML-scraped."
            )
            return result

        crawler = CareerCrawler(
            self.config,
            self.database,
            client=self._client,
            allow_private_networks=self._allow_private,
        )
        try:
            jobs = await crawler._crawl_company(company)
        except CrawlError as exc:
            message = str(exc)
            # A board that answered but with the wrong body (not JSON, or not the
            # shape this ATS returns) is MALFORMED — a 200 that is not a real board.
            # A DNS/HTTP/robots failure or SSRF refusal is UNREACHABLE. Keeping these
            # apart is the whole point: neither is ever reported as a working board.
            malformed_markers = ("expected JSON", "unexpected response", "unsupported content")
            if any(marker in message for marker in malformed_markers):
                result.status = BoardStatus.MALFORMED
            else:
                result.status = BoardStatus.UNREACHABLE
            result.detail = message[:2_000]
            return result
        except Exception as exc:  # defensive: a candidate must never abort the batch
            result.status = BoardStatus.MALFORMED
            result.detail = f"{type(exc).__name__}: {exc}"[:2_000]
            return result

        return self._classify(result, jobs)

    def _classify(
        self, result: BoardValidation, jobs: Iterable[JobData]
    ) -> BoardValidation:
        jobs = list(jobs)
        result.total_role_count = len(jobs)
        # A well-formed ATS response that genuinely has no postings is EMPTY, not
        # malformed: the board resolves, it just has nothing open today. The adapters
        # raise CrawlError on a wrong-shaped body, so reaching here with zero jobs
        # means the ATS answered its own empty list.
        if len(jobs) < self.min_postings:
            result.status = BoardStatus.EMPTY
            result.detail = "ATS responded but listed no parseable postings."
            return result

        india = [job for job in jobs if is_india_location(job.location)]
        relevant = [
            job
            for job in india
            if is_target_title(job.title)
        ]
        result.india_role_count = len(india)
        result.relevant_role_count = len(relevant)

        # Refine the category now that roles are known: a board whose India roles are
        # overwhelmingly remote is remote-first unless a stronger name signal applies.
        remote_share = (
            sum(1 for job in india if _is_remote(job.location)) / len(india) if india else 0.0
        )
        result.category = classify_company(
            result.name, result.ats_type, remote_first=remote_share >= 0.6
        )

        if not india:
            result.status = BoardStatus.IRRELEVANT
            result.detail = (
                f"{len(jobs)} postings, none in India or remote-India; not useful for "
                "this profile."
            )
            return result

        result.status = BoardStatus.VALID
        result.detail = (
            f"{len(jobs)} postings, {len(india)} in India, {len(relevant)} matching the "
            "target engineering profile."
        )
        return result
