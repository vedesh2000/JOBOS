"""Configured, respectful asynchronous career-page crawling."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright

from jobos.ats import extract_known_skills
from jobos.config import AppConfig, CompanySettings
from jobos.database import Database
from jobos.models import CrawlSummary, JobData

USER_AGENT = "JobOS/0.1 (+local personal job search; respectful crawler)"
# Guards against a single response exhausting memory. Kept generous enough for the
# largest real boards: Greenhouse and Ashby return every description in one payload,
# so a ~800-role board is already 9-12 MB and the old 8 MB ceiling silently failed
# the biggest employers. The body is buffered in full by httpx, so the practical
# peak is this value times crawler.concurrency.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
TRACKING_QUERY_PREFIXES = ("utm_", "ref", "source", "gh_src", "lever-")
GENERIC_LINK_LABELS = {
    "apply",
    "apply now",
    "careers",
    "jobs",
    "learn more",
    "open positions",
    "opportunities",
    "search jobs",
    "view all",
    "view jobs",
}
JOB_URL_MARKERS = ("/job/", "/jobs/", "/position/", "/positions/", "/opening/")


class CrawlError(RuntimeError):
    """A source could not be crawled safely or understood."""


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith(TRACKING_QUERY_PREFIXES)
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), path.rstrip("/") or "/", urlencode(query), "")
    )


def job_fingerprint(job: JobData) -> str:
    identity = job.external_id or canonical_url(job.apply_url)
    value = "\x1f".join(
        [job.company.casefold(), identity.casefold(), job.title.casefold(), job.location.casefold()]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_html(value: str) -> str:
    """Strip markup and executable content from untrusted text."""
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean_html(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(filter(None, (_as_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("name", "value", "text", "label"):
            if key in value:
                return _as_text(value[key])
    return ""


def _class_text(node: Any) -> str:
    """Return an element's class attribute as lowercase text, whatever shape it has."""
    if not isinstance(node, Tag):
        return ""
    raw = node.get("class")
    if isinstance(raw, str):
        return raw.casefold()
    if isinstance(raw, list):
        return " ".join(str(item) for item in raw).casefold()
    return ""


def _first_tag(soup: BeautifulSoup, name: str) -> Tag | None:
    found = soup.find(name)
    return found if isinstance(found, Tag) else None


def _first_tag_by_class(soup: BeautifulSoup, pattern: str) -> Tag | None:
    found = soup.find(None, {"class": re.compile(pattern, re.IGNORECASE)})
    return found if isinstance(found, Tag) else None


def _dict_field(payload: Any, key: str) -> dict[str, Any]:
    """Read a nested mapping from untrusted JSON without trusting its shape."""
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _list_field(payload: Any, key: str) -> list[Any]:
    """Read a nested list from untrusted JSON without trusting its shape."""
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, list) else []


def _location_from_json_ld(value: Any) -> str:
    entries = value if isinstance(value, list) else [value]
    locations: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address", entry)
        if not isinstance(address, dict):
            continue
        parts = [
            _as_text(address.get("addressLocality")),
            _as_text(address.get("addressRegion")),
            _as_text(address.get("addressCountry")),
        ]
        location = ", ".join(part for part in parts if part)
        if location:
            locations.append(location)
    return " / ".join(dict.fromkeys(locations))


def _iter_json_ld(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_ld(item)
    elif isinstance(value, dict):
        graph = value.get("@graph")
        if graph:
            yield from _iter_json_ld(graph)
        item_type = value.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "JobPosting" in types:
            yield value


def parse_json_ld_jobs(html: str, company: str, source_url: str) -> list[JobData]:
    """Extract schema.org JobPosting objects without executing page scripts."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[JobData] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            parsed = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _iter_json_ld(parsed):
            title = _as_text(item.get("title"))
            description = _as_text(item.get("description") or item.get("responsibilities"))
            apply_url = _as_text(item.get("url")) or source_url
            if not title or not apply_url:
                continue
            location = _location_from_json_ld(item.get("jobLocation"))
            if str(item.get("jobLocationType", "")).upper() == "TELECOMMUTE":
                location = f"Remote{f' / {location}' if location else ''}"
            skill_text = _as_text(item.get("skills") or item.get("qualifications"))
            experience = _as_text(item.get("experienceRequirements")) or _extract_experience_text(
                description
            )
            try:
                jobs.append(
                    JobData(
                        company=company,
                        title=title,
                        location=location,
                        description=description,
                        experience=experience,
                        skills=extract_known_skills(f"{skill_text} {description}"),
                        apply_url=urljoin(source_url, apply_url),
                        source_url=source_url,
                        external_id=_as_text(item.get("identifier")),
                        employment_type=_as_text(item.get("employmentType")),
                    )
                )
            except ValueError:
                continue
    return jobs


_ATS_NAME_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com")
_CAREER_SUBDOMAINS = {
    "www", "careers", "career", "jobs", "job", "boards", "board", "apply", "work", "join", "talent",
}


def _humanise(slug: str) -> str:
    text = re.sub(r"[-_]+", " ", slug).strip()
    return text.title() if text else "Careers"


def derive_company_name(url: str) -> str:
    """Best-effort employer name from a career-page URL, for ad-hoc crawls.

    ATS boards put the employer in the first path segment (greenhouse.io/<slug>),
    Workday puts the tenant in the host, and any other site falls back to the most
    specific domain label with common prefixes (www, careers, jobs) stripped.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path_parts = [part for part in parsed.path.split("/") if part]
    if any(ats in host for ats in _ATS_NAME_HOSTS) and path_parts:
        return _humanise(path_parts[0])
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return _humanise(host.split(".", 1)[0])
    labels = [label for label in host.split(".") if label and label not in _CAREER_SUBDOMAINS]
    if len(labels) >= 2:  # drop the TLD, keep the registrable-ish label
        return _humanise(labels[-2])
    if labels:
        return _humanise(labels[0])
    return "Careers"


class CareerCrawler:
    """Crawl only configured company pages and persist normalized jobs."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        client: httpx.AsyncClient | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        self.config = config
        self.database = database
        self._client = client
        self._allow_private = allow_private_networks
        self._request_limit = asyncio.Semaphore(config.crawler.concurrency)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._cache: dict[str, str] = {}

    async def crawl(self, company_slug: str | None = None) -> CrawlSummary:
        selected = {
            slug: company
            for slug, company in self.config.companies.items()
            if company.enabled and (company_slug is None or slug == company_slug)
        }
        if company_slug and company_slug not in self.config.companies:
            raise CrawlError(f"company {company_slug!r} is not configured")
        summary = CrawlSummary(companies_requested=len(selected))
        if not selected:
            return summary
        await self._crawl_with_client(selected, summary, close_missing=True)
        return summary

    async def crawl_settings(self, company: CompanySettings) -> CrawlSummary:
        """Crawl a single ad-hoc source that is not in config, e.g. a pasted URL.

        Dispatch is identical to a configured crawl (an ATS host uses its adapter, any
        other page falls back to JSON-LD/HTML), and SSRF protection still applies.
        Unlike a configured crawl this never closes previously stored jobs: an ad-hoc,
        possibly partial fetch should only add or refresh, never retire earlier ones.
        """
        summary = CrawlSummary(companies_requested=1)
        await self._crawl_with_client(
            {f"url:{canonical_url(company.url)}": company}, summary, close_missing=False
        )
        return summary

    async def _crawl_with_client(
        self,
        selected: dict[str, CompanySettings],
        summary: CrawlSummary,
        *,
        close_missing: bool,
    ) -> None:
        if self._client is None:
            timeout = httpx.Timeout(self.config.crawler.timeout_seconds)
            async with httpx.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
                follow_redirects=False,
            ) as client:
                self._client = client
                try:
                    await self._crawl_selected(selected, summary, close_missing=close_missing)
                finally:
                    self._client = None
        else:
            await self._crawl_selected(selected, summary, close_missing=close_missing)

    async def _crawl_selected(
        self,
        selected: dict[str, CompanySettings],
        summary: CrawlSummary,
        *,
        close_missing: bool = True,
    ) -> None:
        company_limit = asyncio.Semaphore(min(self.config.crawler.concurrency, 5))

        async def run(slug: str, company: CompanySettings) -> tuple[str, list[JobData] | Exception]:
            async with company_limit:
                try:
                    return slug, await self._crawl_company(company)
                except Exception as exc:
                    return slug, exc

        results = await asyncio.gather(
            *(run(slug, company) for slug, company in selected.items())
        )
        for slug, result in results:
            if isinstance(result, Exception):
                summary.errors[slug] = str(result)
                continue
            summary.companies_succeeded += 1
            limit = self.config.crawler.per_company_limit
            seen: set[str] = set()
            fingerprints: set[str] = set()
            for job in result[:limit]:
                fingerprint = job_fingerprint(job)
                key = f"{canonical_url(job.apply_url)}:{fingerprint}"
                if key in seen:
                    continue
                seen.add(key)
                fingerprints.add(fingerprint)
                _, created = self.database.upsert_job(job, fingerprint)
                summary.jobs_seen += 1
                if created:
                    summary.jobs_added += 1
                else:
                    summary.jobs_updated += 1
            # A board that filled the limit was almost certainly cut short, and the
            # postings past the cut were never seen, so closing them would be wrong.
            # Only a board read in full can tell us what it no longer lists. Ad-hoc
            # URL crawls opt out entirely (close_missing=False).
            if close_missing and len(result) < limit:
                summary.jobs_closed += self.database.deactivate_missing_jobs(
                    selected[slug].name, fingerprints
                )

    async def _crawl_company(self, company: CompanySettings) -> list[JobData]:
        await self._validate_public_url(company.url)
        parsed = urlsplit(company.url)
        host = (parsed.hostname or "").casefold()
        path_parts = [part for part in parsed.path.split("/") if part]
        if "greenhouse.io" in host and path_parts:
            return await self._greenhouse(company, path_parts[0])
        if host.endswith("lever.co") and path_parts:
            return await self._lever(company, path_parts[0])
        if "ashbyhq.com" in host and path_parts:
            return await self._ashby(company, path_parts[0])
        if "smartrecruiters.com" in host and path_parts:
            return await self._smartrecruiters(company, path_parts[0])
        if "myworkdayjobs.com" in host and path_parts:
            return await self._workday(company, host, path_parts)
        return await self._generic(company)

    async def _greenhouse(self, company: CompanySettings, board: str) -> list[JobData]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        payload = await self._json_request("GET", url, honor_robots=False)
        jobs: list[JobData] = []
        for item in _list_field(payload, "jobs")[: self.config.crawler.per_company_limit]:
            if not isinstance(item, dict):
                continue
            location = _as_text(item.get("location"))
            description = _clean_html(_as_text(item.get("content")))
            apply_url = _as_text(item.get("absolute_url"))
            if not apply_url:
                continue
            jobs.append(
                JobData(
                    company=company.name,
                    title=_as_text(item.get("title")) or "Untitled role",
                    location=location,
                    description=description,
                    experience=_extract_experience_text(description),
                    skills=extract_known_skills(description),
                    apply_url=apply_url,
                    source_url=company.url,
                    external_id=_as_text(item.get("id")),
                )
            )
        return jobs

    async def _lever(self, company: CompanySettings, account: str) -> list[JobData]:
        url = f"https://api.lever.co/v0/postings/{account}?mode=json"
        payload = await self._json_request("GET", url, honor_robots=False)
        if not isinstance(payload, list):
            raise CrawlError("Lever returned an unexpected response")
        jobs: list[JobData] = []
        for item in payload[: self.config.crawler.per_company_limit]:
            if not isinstance(item, dict):
                continue
            categories = _dict_field(item, "categories")
            description = _as_text(item.get("descriptionPlain"))
            lists = _list_field(item, "lists")
            list_text = "\n".join(
                f"{_as_text(entry.get('text'))}\n{_as_text(entry.get('content'))}"
                for entry in lists
                if isinstance(entry, dict)
            )
            description = f"{description}\n{_clean_html(list_text)}".strip()
            apply_url = _as_text(item.get("applyUrl") or item.get("hostedUrl"))
            if not apply_url:
                continue
            jobs.append(
                JobData(
                    company=company.name,
                    title=_as_text(item.get("text")) or "Untitled role",
                    location=_as_text(categories.get("location")),
                    description=description,
                    experience=_extract_experience_text(description),
                    skills=extract_known_skills(description),
                    apply_url=apply_url,
                    source_url=company.url,
                    external_id=_as_text(item.get("id")),
                    employment_type=_as_text(categories.get("commitment")),
                )
            )
        return jobs

    async def _ashby(self, company: CompanySettings, board: str) -> list[JobData]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        payload = await self._json_request("GET", url, honor_robots=False)
        jobs: list[JobData] = []
        for item in _list_field(payload, "jobs")[: self.config.crawler.per_company_limit]:
            if not isinstance(item, dict):
                continue
            description = _clean_html(_as_text(item.get("descriptionHtml")))
            apply_url = _as_text(item.get("applyUrl") or item.get("jobUrl"))
            if not apply_url:
                continue
            jobs.append(
                JobData(
                    company=company.name,
                    title=_as_text(item.get("title")) or "Untitled role",
                    location=_as_text(item.get("location")),
                    description=description,
                    experience=_extract_experience_text(description),
                    skills=extract_known_skills(description),
                    apply_url=apply_url,
                    source_url=company.url,
                    external_id=_as_text(item.get("id")),
                    employment_type=_as_text(item.get("employmentType")),
                )
            )
        return jobs

    async def _smartrecruiters(self, company: CompanySettings, account: str) -> list[JobData]:
        url = f"https://api.smartrecruiters.com/v1/companies/{account}/postings?limit=100"
        payload = await self._json_request("GET", url, honor_robots=False)
        content = _list_field(payload, "content")
        jobs: list[JobData] = []
        for item in content[: self.config.crawler.per_company_limit]:
            if not isinstance(item, dict):
                continue
            job_id = _as_text(item.get("id"))
            if not job_id:
                continue
            detail = await self._json_request(
                "GET",
                f"https://api.smartrecruiters.com/v1/companies/{account}/postings/{job_id}",
                honor_robots=False,
            )
            sections = _dict_field(_dict_field(detail, "jobAd"), "sections")
            description = "\n".join(
                _clean_html(_as_text(value.get("text")))
                for value in sections.values()
                if isinstance(value, dict)
            )
            location = _dict_field(detail, "location")
            location_text = ", ".join(
                filter(
                    None,
                    (
                        _as_text(location.get("city")),
                        _as_text(location.get("region")),
                        _as_text(location.get("country")),
                    ),
                )
            )
            apply_url = _as_text(detail.get("applyUrl") or item.get("ref"))
            if not apply_url:
                apply_url = f"https://jobs.smartrecruiters.com/{account}/{job_id}"
            jobs.append(
                JobData(
                    company=company.name,
                    title=_as_text(detail.get("name") or item.get("name")) or "Untitled role",
                    location=location_text,
                    description=description,
                    experience=_extract_experience_text(description),
                    skills=extract_known_skills(description),
                    apply_url=apply_url,
                    source_url=company.url,
                    external_id=job_id,
                    employment_type=_as_text(detail.get("typeOfEmployment")),
                )
            )
        return jobs

    async def _workday(
        self, company: CompanySettings, host: str, path_parts: list[str]
    ) -> list[JobData]:
        tenant = host.split(".", 1)[0]
        site = path_parts[-1]
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        jobs: list[JobData] = []
        offset = 0
        limit = min(20, self.config.crawler.per_company_limit)
        seen_paths: set[str] = set()
        while len(jobs) < self.config.crawler.per_company_limit:
            payload = await self._json_request(
                "POST",
                f"{base}/jobs",
                json_body={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                honor_robots=False,
            )
            postings = _list_field(payload, "jobPostings")
            if not postings:
                break
            new_on_page = 0
            for item in postings:
                if not isinstance(item, dict):
                    continue
                external_path = _as_text(item.get("externalPath"))
                if not external_path or external_path in seen_paths:
                    continue
                seen_paths.add(external_path)
                new_on_page += 1
                detail = await self._json_request(
                    "GET", f"{base}{external_path}", honor_robots=False
                )
                info = _dict_field(detail, "jobPostingInfo")
                description = _clean_html(_as_text(info.get("jobDescription")))
                apply_url = urljoin(company.url, external_path)
                jobs.append(
                    JobData(
                        company=company.name,
                        title=_as_text(info.get("title") or item.get("title")) or "Untitled role",
                        location=_as_text(info.get("location") or item.get("locationsText")),
                        description=description,
                        experience=_extract_experience_text(description),
                        skills=extract_known_skills(description),
                        apply_url=apply_url,
                        source_url=company.url,
                        external_id=_as_text(info.get("jobReqId")) or external_path,
                        employment_type=_as_text(info.get("timeType")),
                    )
                )
                if len(jobs) >= self.config.crawler.per_company_limit:
                    break
            if not new_on_page:
                # Some tenants answer out-of-range offsets by replaying an earlier
                # page instead of returning nothing, so an all-duplicate page is the
                # real end of the list.
                break
            offset += len(postings)
            total = payload.get("total") if isinstance(payload, dict) else None
            # Many tenants report a real total only on the first page and send 0 (or
            # nothing) afterwards. Treating that as "no more results" stopped paging
            # after two pages and capped every Workday board at 40 postings, so the
            # total is only trusted when positive; the empty-page and no-new-postings
            # guards above end the loop otherwise.
            if isinstance(total, (int, float)) and total > 0 and offset >= int(total):
                break
        return jobs

    async def _generic(self, company: CompanySettings) -> list[JobData]:
        html = await self._fetch_text(company.url)
        jobs = parse_json_ld_jobs(html, company.name, company.url)
        links = self._candidate_links(html, company.url)
        if not jobs and not links and self.config.crawler.use_browser_fallback:
            browser_html = await self._browser_html(company.url)
            if browser_html:
                jobs = parse_json_ld_jobs(browser_html, company.name, company.url)
                links = self._candidate_links(browser_html, company.url)
        if links:
            selected = links[: self.config.crawler.per_company_limit]
            detail_jobs = await asyncio.gather(
                *(self._fetch_generic_job(company, link) for link in selected)
            )
            jobs.extend(job for job in detail_jobs if job is not None)
        unique: dict[str, JobData] = {}
        for job in jobs:
            unique.setdefault(canonical_url(job.apply_url), job)
        return list(unique.values())

    def _candidate_links(self, html: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[tuple[int, str]] = []
        base = canonical_url(base_url)
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = canonical_url(urljoin(base_url, href))
            if absolute == base:
                continue
            text = " ".join(anchor.get_text(" ").split())
            lower_text = text.casefold()
            lower_url = urlsplit(absolute).path.casefold()
            parent_classes = _class_text(anchor.parent)
            score = sum(marker in lower_url for marker in JOB_URL_MARKERS) * 2
            score += int(any(marker in parent_classes for marker in ("job", "role", "opening")))
            score += int(3 <= len(text) <= 180 and lower_text not in GENERIC_LINK_LABELS)
            if score >= 2:
                candidates.append((score, absolute))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return list(dict.fromkeys(url for _, url in candidates))

    async def _fetch_generic_job(
        self, company: CompanySettings, url: str
    ) -> JobData | None:
        try:
            html = await self._fetch_text(url)
        except CrawlError:
            return None
        json_ld = parse_json_ld_jobs(html, company.name, url)
        if json_ld:
            return json_ld[0]
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
            tag.decompose()
        heading = soup.find("h1")
        title = " ".join(heading.get_text(" ").split()) if isinstance(heading, Tag) else ""
        if not title or title.casefold() in GENERIC_LINK_LABELS:
            return None
        candidates: list[Tag | None] = [
            _first_tag(soup, "main"),
            _first_tag(soup, "article"),
            _first_tag_by_class(soup, r"description|job-detail|posting"),
            soup.body,
        ]
        body = next((item for item in candidates if item is not None), None)
        if body is None:
            return None
        description = "\n".join(
            line.strip() for line in body.get_text("\n").splitlines() if line.strip()
        )
        if len(description) < 100:
            return None
        location_tag = _first_tag_by_class(soup, r"location")
        location = " ".join(location_tag.get_text(" ").split()) if location_tag else ""
        apply_url = url
        for anchor in soup.find_all("a", href=True):
            text = " ".join(anchor.get_text(" ").split()).casefold()
            if text in {"apply", "apply now", "apply for this job", "submit application"}:
                apply_url = urljoin(url, str(anchor.get("href")))
                break
        try:
            return JobData(
                company=company.name,
                title=title,
                location=location,
                description=description,
                experience=_extract_experience_text(description),
                skills=extract_known_skills(description),
                apply_url=apply_url,
                source_url=company.url,
                external_id=urlsplit(url).path.rstrip("/").split("/")[-1],
            )
        except ValueError:
            return None

    async def _fetch_text(self, url: str) -> str:
        key = canonical_url(url)
        if key in self._cache:
            return self._cache[key]
        if self.config.crawler.respect_robots_txt and not await self._robots_allowed(url):
            raise CrawlError(f"robots.txt disallows JobOS from fetching {url}")
        response = await self._http_request("GET", url)
        content_type = response.headers.get("content-type", "").casefold()
        if not any(value in content_type for value in ("html", "text", "json", "xml")):
            raise CrawlError(f"unsupported content type {content_type or 'unknown'} at {url}")
        text = response.text
        self._cache[key] = text
        return text

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        honor_robots: bool,
    ) -> Any:
        if (
            honor_robots
            and self.config.crawler.respect_robots_txt
            and not await self._robots_allowed(url)
        ):
            raise CrawlError(f"robots.txt disallows JobOS from fetching {url}")
        response = await self._http_request(method, url, json_body=json_body)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise CrawlError(f"expected JSON from {url}") from exc

    async def _http_request(
        self, method: str, url: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("crawler HTTP client is not active")
        current = url
        for _ in range(5):
            await self._validate_public_url(current)
            async with self._request_limit:
                try:
                    response = await self._client.request(method, current, json=json_body)
                except httpx.TimeoutException as exc:
                    raise CrawlError(f"timed out fetching {current}") from exc
                except httpx.HTTPError as exc:
                    raise CrawlError(f"could not fetch {current}: {exc}") from exc
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise CrawlError(f"response from {current} exceeded 8 MB")
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise CrawlError(f"invalid redirect from {current}")
                current = urljoin(current, location)
                if response.status_code in {301, 302, 303}:
                    method, json_body = "GET", None
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise CrawlError(f"HTTP {response.status_code} fetching {current}") from exc
            return response
        raise CrawlError(f"too many redirects fetching {url}")

    async def _robots_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            robots_url = f"{origin}/robots.txt"
            try:
                response = await self._http_request("GET", robots_url)
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                self._robots[origin] = parser
            except CrawlError:
                self._robots[origin] = None
        cached = self._robots[origin]
        return True if cached is None else cached.can_fetch(USER_AGENT, url)

    async def _browser_html(self, url: str) -> str:
        await self._validate_public_url(url)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    accept_downloads=False,
                    service_workers="block",
                )
                page = await context.new_page()
                await page.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "media", "font", "websocket"}
                    else route.continue_(),
                )
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.config.crawler.timeout_seconds * 1_000),
                )
                await page.wait_for_timeout(1_000)
                html = await page.content()
                await browser.close()
                return html if len(html.encode("utf-8")) <= MAX_RESPONSE_BYTES else ""
        except Exception:
            return ""

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CrawlError(f"invalid HTTP(S) URL: {url}")
        if parsed.username or parsed.password:
            raise CrawlError("URLs containing credentials are not allowed")
        if self._allow_private:
            return
        try:
            results = await asyncio.to_thread(
                socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        except socket.gaierror as exc:
            raise CrawlError(f"could not resolve {parsed.hostname}") from exc
        for result in results:
            address = ipaddress.ip_address(result[4][0])
            if not address.is_global:
                raise CrawlError(f"private or local network target is not allowed: {parsed.hostname}")


def _extract_experience_text(description: str) -> str:
    matches = re.findall(
        r"[^.\n]{0,80}\b\d{1,2}(?:\+)?\s*(?:years?|yrs?)\b[^.\n]{0,80}",
        description,
        re.IGNORECASE,
    )
    return "; ".join(" ".join(match.split()) for match in matches[:5])
