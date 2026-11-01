"""Validated data models shared by JobOS features."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def new_id() -> str:
    """Return a compact random identifier suitable for local records."""
    return uuid4().hex


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(raw.split()).strip(" ,;|")
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def validate_web_url(value: str) -> str:
    """Validate an externally supplied HTTP(S) URL without normalizing its meaning."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("must be an absolute http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")
    return value


class StrictModel(BaseModel):
    """Base for persisted and boundary models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class Contact(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=80)
    location: str = Field(default="", max_length=200)
    links: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("links")
    @classmethod
    def validate_links(cls, values: list[str]) -> list[str]:
        return _unique_strings([validate_web_url(value) for value in values])


class ResumeBullet(StrictModel):
    id: str = Field(default_factory=new_id, min_length=4, max_length=80)
    text: str = Field(min_length=1, max_length=2_000)


class Experience(StrictModel):
    id: str = Field(default_factory=new_id, min_length=4, max_length=80)
    company: str = Field(default="", max_length=200)
    title: str = Field(default="", max_length=200)
    start_date: str = Field(default="", max_length=80)
    end_date: str = Field(default="", max_length=80)
    location: str = Field(default="", max_length=200)
    bullets: list[ResumeBullet] = Field(default_factory=list, max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("skills")
    @classmethod
    def deduplicate_skills(cls, values: list[str]) -> list[str]:
        return _unique_strings(values)


class Project(StrictModel):
    id: str = Field(default_factory=new_id, min_length=4, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2_000)
    url: str = Field(default="", max_length=2_000)
    bullets: list[ResumeBullet] = Field(default_factory=list, max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("url")
    @classmethod
    def validate_optional_url(cls, value: str) -> str:
        return validate_web_url(value) if value else value

    @field_validator("skills")
    @classmethod
    def deduplicate_skills(cls, values: list[str]) -> list[str]:
        return _unique_strings(values)


class Education(StrictModel):
    id: str = Field(default_factory=new_id, min_length=4, max_length=80)
    institution: str = Field(default="", max_length=300)
    degree: str = Field(default="", max_length=300)
    field: str = Field(default="", max_length=300)
    start_date: str = Field(default="", max_length=80)
    end_date: str = Field(default="", max_length=80)
    details: list[str] = Field(default_factory=list, max_length=30)


class Certification(StrictModel):
    id: str = Field(default_factory=new_id, min_length=4, max_length=80)
    name: str = Field(min_length=1, max_length=300)
    issuer: str = Field(default="", max_length=300)
    date: str = Field(default="", max_length=80)


class StructuredResume(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    contact: Contact
    summary: str = Field(default="", max_length=4_000)
    skills: list[str] = Field(default_factory=list, max_length=300)
    # Optional presentation-only grouping of skills (e.g. Backend, Frontend). This
    # never affects scoring or optimisation, which always use the flat `skills`
    # list; it only labels skills into categories when rendering the resume.
    skill_groups: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[Experience] = Field(default_factory=list, max_length=100)
    projects: list[Project] = Field(default_factory=list, max_length=100)
    education: list[Education] = Field(default_factory=list, max_length=50)
    certifications: list[Certification] = Field(default_factory=list, max_length=100)
    achievements: list[ResumeBullet] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=50)
    additional_sections: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("skills", "languages")
    @classmethod
    def deduplicate_lists(cls, values: list[str]) -> list[str]:
        return _unique_strings(values)

    @model_validator(mode="after")
    def unique_item_ids(self) -> Self:
        ids: list[str] = []
        ids.extend(experience.id for experience in self.experience)
        ids.extend(project.id for project in self.projects)
        ids.extend(education.id for education in self.education)
        ids.extend(certification.id for certification in self.certifications)
        ids.extend(achievement.id for achievement in self.achievements)
        for experience in self.experience:
            ids.extend(bullet.id for bullet in experience.bullets)
        for project in self.projects:
            ids.extend(bullet.id for bullet in project.bullets)
        if len(ids) != len(set(ids)):
            raise ValueError("resume item IDs must be unique")
        return self

    def searchable_text(self) -> str:
        """Return factual resume content for matching, never for persistence as a resume format."""
        parts = [self.contact.name, self.summary, *self.skills]
        for experience in self.experience:
            parts.extend(
                [experience.company, experience.title, experience.location, *experience.skills]
            )
            parts.extend(bullet.text for bullet in experience.bullets)
        for project in self.projects:
            parts.extend([project.name, project.description, *project.skills])
            parts.extend(bullet.text for bullet in project.bullets)
        for education in self.education:
            parts.extend(
                [education.institution, education.degree, education.field, *education.details]
            )
        parts.extend(certification.name for certification in self.certifications)
        parts.extend(achievement.text for achievement in self.achievements)
        parts.extend(self.languages)
        return "\n".join(part for part in parts if part)


class ResumeImportResult(StrictModel):
    version_id: int
    resume: StructuredResume
    source_text: str
    warnings: list[str] = Field(default_factory=list)


class JobStatus(StrEnum):
    NEW = "new"
    SAVED = "saved"
    OPENED = "opened"
    APPLIED = "applied"
    IGNORED = "ignored"


class JobData(StrictModel):
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    location: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=100_000)
    experience: str = Field(default="", max_length=1_000)
    skills: list[str] = Field(default_factory=list, max_length=300)
    apply_url: str = Field(max_length=4_000)
    source_url: str = Field(max_length=4_000)
    external_id: str = Field(default="", max_length=300)
    employment_type: str = Field(default="", max_length=200)
    # When the employer published the posting, when the board reports it. This is the
    # real thing to filter "posted this week" on; `first_seen_at` is only when JobOS
    # noticed it. None where a board does not expose a date.
    posted_at: datetime | None = None

    @field_validator("apply_url", "source_url")
    @classmethod
    def validate_urls(cls, value: str) -> str:
        return validate_web_url(value)

    @field_validator("location", "employment_type", mode="before")
    @classmethod
    def truncate_long_text(cls, value: Any) -> Any:
        """Trim over-long values instead of rejecting the whole posting.

        Some boards concatenate every office into one location string, which would
        otherwise fail validation and drop an entire company's jobs.
        """
        if isinstance(value, str) and len(value) > 300:
            return value[:297].rstrip() + "..."
        return value

    @field_validator("skills")
    @classmethod
    def deduplicate_skills(cls, values: list[str]) -> list[str]:
        return _unique_strings(values)


_YEARS_PATTERN = re.compile(r"\b(\d{1,2})(?:\s*\+|\s*-\s*\d{1,2})?\s*(?:years?|yrs?)\b", re.I)


def parse_min_years(text: str) -> float | None:
    """Best-effort minimum years-of-experience from free text, or None.

    Reads phrases like "5+ years", "3-5 years", "minimum 8 years". Returns the
    smallest number found (a "3-5 years" range means you need at least 3). Heuristic
    by design: it is blank when the posting does not state YoE, and is never used to
    hide a job, only to display and to filter leniently.
    """
    values = [int(m.group(1)) for m in _YEARS_PATTERN.finditer(text or "")]
    # Ignore implausible matches (e.g. "401k", "24/7") that slipped through.
    values = [v for v in values if 0 <= v <= 40]
    return float(min(values)) if values else None


class JobView(JobData):
    id: int
    status: JobStatus = JobStatus.NEW
    match_score: int | None = Field(default=None, ge=0, le=100)
    analysis: JobAnalysis | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool = True

    @property
    def min_years(self) -> float | None:
        """Minimum years of experience the posting asks for, if known.

        Prefers the analysed value; falls back to parsing the crawl-time experience
        text so an un-analysed job still shows a figure. None when the posting does
        not state one.
        """
        if self.analysis is not None and self.analysis.minimum_years is not None:
            return self.analysis.minimum_years
        return parse_min_years(self.experience or "")


class JobAnalysis(StrictModel):
    required_skills: list[str] = Field(default_factory=list, max_length=200)
    preferred_skills: list[str] = Field(default_factory=list, max_length=200)
    # Alternative requirement groups: matching ANY member satisfies the whole group,
    # e.g. [["react", "vue.js"]] for "a modern UI framework like React or Vue". Empty
    # by default, so existing stored analyses and the v1 scorer are unaffected.
    skill_groups: list[list[str]] = Field(default_factory=list, max_length=50)
    responsibilities: list[str] = Field(default_factory=list, max_length=100)
    technologies: list[str] = Field(default_factory=list, max_length=200)
    seniority: str = Field(default="", max_length=100)
    minimum_years: float | None = Field(default=None, ge=0, le=60)
    education_requirements: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=300)
    warnings: list[str] = Field(default_factory=list, max_length=50)

    @field_validator(
        "required_skills",
        "preferred_skills",
        "responsibilities",
        "technologies",
        "education_requirements",
        "keywords",
        "warnings",
    )
    @classmethod
    def deduplicate_values(cls, values: list[str]) -> list[str]:
        return _unique_strings(values)


class ScoreComponent(StrictModel):
    name: str
    score: float = Field(ge=0)
    maximum: float = Field(gt=0)
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class ATSReportData(StrictModel):
    scoring_version: str = "jobos-ats-v1"
    overall_score: int = Field(ge=0, le=100)
    components: list[ScoreComponent]
    matching_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    weak_areas: list[str] = Field(default_factory=list)
    strong_areas: list[str] = Field(default_factory=list)
    keywords_found: list[str] = Field(default_factory=list)
    keywords_missing: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    confidence: str = Field(default="medium", pattern="^(low|medium|high)$")
    job_id: int | None = None
    resume_version_id: int | None = None


class BulletRewrite(StrictModel):
    bullet_id: str = Field(min_length=4, max_length=80)
    text: str = Field(min_length=1, max_length=2_000)
    reason: str = Field(default="", max_length=1_000)


class OptimizationPlan(StrictModel):
    summary: str | None = Field(default=None, max_length=4_000)
    skill_order: list[str] = Field(default_factory=list, max_length=300)
    bullet_rewrites: list[BulletRewrite] = Field(default_factory=list, max_length=200)
    bullet_order: dict[str, list[str]] = Field(default_factory=dict)
    project_order: list[str] = Field(default_factory=list, max_length=100)
    explanations: list[str] = Field(default_factory=list, max_length=200)


class RejectedEdit(StrictModel):
    target: str
    reason: str
    proposed_text: str = ""


class ResumeDiff(StrictModel):
    summary_before: str = ""
    summary_after: str = ""
    skill_order_before: list[str] = Field(default_factory=list)
    skill_order_after: list[str] = Field(default_factory=list)
    bullet_changes: list[dict[str, str]] = Field(default_factory=list)
    ordering_changes: list[str] = Field(default_factory=list)
    rejected_edits: list[RejectedEdit] = Field(default_factory=list)


class OptimizationResult(StrictModel):
    optimized_resume: StructuredResume
    validated_plan: OptimizationPlan
    diff: ResumeDiff
    rejected_edits: list[RejectedEdit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CoverLetterDraft(StrictModel):
    markdown: str = Field(min_length=1, max_length=10_000)
    fact_sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResumeVersionView(StrictModel):
    id: int
    created_at: datetime
    source_name: str
    source_format: str
    original_path: str
    is_master: bool
    parent_id: int | None
    job_id: int | None
    content_hash: str
    resume: StructuredResume


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ApplicationView(StrictModel):
    id: int
    job_id: int
    master_resume_version_id: int
    generated_resume_version_id: int | None
    status: ApplicationStatus
    plan: OptimizationPlan
    optimized_resume: StructuredResume
    diff: ResumeDiff
    ats_before: ATSReportData
    ats_after: ATSReportData
    cover_letter: CoverLetterDraft | None
    output_dir: str
    created_at: datetime
    approved_at: datetime | None


class CrawlSummary(StrictModel):
    companies_requested: int = 0
    companies_succeeded: int = 0
    jobs_seen: int = 0
    jobs_added: int = 0
    jobs_updated: int = 0
    # Postings a company's board no longer lists, so they stop appearing in searches.
    jobs_closed: int = 0
    # Soft-deleted postings auto-pruned at the end of this crawl (older than the cutoff).
    jobs_pruned: int = 0
    errors: dict[str, str] = Field(default_factory=dict)


class CompanyCategory(StrEnum):
    """Coarse employer category, so coverage can be reported across every segment.

    This is deliberately a small, stable set the discovery pipeline can assign from
    the ATS it was found on plus light name heuristics. It is informational — it
    classifies and reports coverage, it never gates or suppresses a role. "Unknown"
    is a valid, non-pejorative value for anything the heuristic cannot place.
    """

    MNC_TECH = "mnc_tech"  # large multinational technology companies
    ENTERPRISE_SAAS = "enterprise_saas"  # publicly traded / mature SaaS
    FINANCE = "finance"  # banking, fintech, financial services
    GCC = "gcc"  # global capability / in-house centre of a non-tech multinational
    IT_SERVICES = "it_services"  # IT / engineering services and consultancies
    PRODUCT = "product"  # mature product companies
    GROWTH = "growth"  # mid-sized / growth-stage
    STARTUP = "startup"  # early-stage
    REMOTE_FIRST = "remote_first"  # remote-first international, hiring in India
    UNKNOWN = "unknown"


class BoardStatus(StrEnum):
    """Outcome of validating a candidate company board against its ATS API.

    Only ``VALID`` boards are safe to adopt into config.yaml. The other states are
    kept so the report can explain *why* a candidate was rejected rather than silently
    dropping it, and so an ``UNSUPPORTED`` portal can be surfaced for manual review
    instead of being reported as a successful crawl.
    """

    VALID = "valid"
    EMPTY = "empty"  # ATS answered cleanly but lists no postings
    MALFORMED = "malformed"  # 200 OK but the body is not the shape this ATS returns
    IRRELEVANT = "irrelevant"  # real board, but no role matching the target profile
    UNSUPPORTED = "unsupported"  # host has no JSON adapter — needs manual review
    UNREACHABLE = "unreachable"  # DNS/HTTP/robots failure, or SSRF-refused


class BoardValidation(StrictModel):
    """A single candidate board's validation result.

    ``relevant_role_count`` counts postings that match the target profile (a target
    title AND an India/remote-India location); ``india_role_count`` counts any India
    posting regardless of title. The two are kept apart so discovery breadth
    (companies hiring in India) is never confused with application fit (roles worth
    applying to).
    """

    slug: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4_000)
    ats_type: str = Field(default="", max_length=40)
    category: CompanyCategory = CompanyCategory.UNKNOWN
    # GCC (Global Capability Centre) evidence. ``is_gcc`` is set when the employer
    # name matches a known GCC parent; informational, never gates adoption.
    is_gcc: bool = False
    status: BoardStatus = BoardStatus.UNREACHABLE
    total_role_count: int = 0
    india_role_count: int = 0
    relevant_role_count: int = 0
    detail: str = Field(default="", max_length=2_000)
    validated_at: datetime = Field(default_factory=utc_now)

    @property
    def adoptable(self) -> bool:
        """Only a valid board with at least one India posting is worth adopting."""
        return self.status is BoardStatus.VALID and self.india_role_count > 0


class ValidationSummary(StrictModel):
    """Aggregate of a validation run, for the before/after coverage report."""

    candidates: int = 0
    valid: int = 0
    empty: int = 0
    malformed: int = 0
    irrelevant: int = 0
    unsupported: int = 0
    unreachable: int = 0
    india_roles: int = 0
    relevant_roles: int = 0
    duration_seconds: float = 0.0
    results: list[BoardValidation] = Field(default_factory=list)

    def record(self, result: BoardValidation) -> None:
        self.candidates += 1
        setattr(self, result.status.value, getattr(self, result.status.value) + 1)
        self.india_roles += result.india_role_count
        self.relevant_roles += result.relevant_role_count
        self.results.append(result)


class RegistryEntry(StrictModel):
    """One company's durable record in the sidecar registry.

    This is a lightweight index kept alongside (not replacing) config.yaml: config
    stays the human-edited source of truth for which boards are crawled, while the
    registry records what validation and crawling learned about each one, so coverage
    can be reported and dead boards flagged without re-validating every run.
    """

    slug: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=4_000)
    ats_type: str = Field(default="", max_length=40)
    category: CompanyCategory = CompanyCategory.UNKNOWN
    is_gcc: bool = False
    status: BoardStatus = BoardStatus.VALID
    india_role_count: int = 0
    relevant_role_count: int = 0
    last_validated_at: datetime | None = None
    last_crawled_at: datetime | None = None
    detail: str = Field(default="", max_length=2_000)


class CategoryCoverage(StrictModel):
    """Coverage for one employer category, so no segment is silently under-served."""

    category: CompanyCategory
    boards: int = 0
    india_roles: int = 0
    relevant_roles: int = 0


class CoverageReport(StrictModel):
    """Before/after view of board coverage, for the acceptance-criteria comparison."""

    configured_boards: int = 0
    validated_boards: int = 0
    supported_boards: int = 0
    unsupported_boards: int = 0
    india_roles: int = 0
    relevant_roles: int = 0
    flagged_for_review: list[str] = Field(default_factory=list)
    # Per-category breakdown (requirement 9), and the major employers still missing.
    by_category: list[CategoryCoverage] = Field(default_factory=list)
    unsupported_major_employers: list[str] = Field(default_factory=list)
    # Validated boards identified as Global Capability Centres.
    gcc_boards: int = 0


class LabelCount(StrictModel):
    """A single labelled bar for an Insights chart (e.g. 'Java' -> 412)."""

    label: str
    count: int
    # Optional secondary number, e.g. share of the total as a percentage.
    pct: float = 0.0


class InsightsReport(StrictModel):
    """Aggregated market + personal trends for the Insights dashboard.

    Everything here is computed deterministically from the local database and
    registry — no AI — so it is fast, reproducible, and also serves as the grounding
    data the Chat coach reads.
    """

    generated_at: datetime = Field(default_factory=utc_now)
    # Headline counters.
    total_companies: int = 0
    validated_companies: int = 0
    total_jobs: int = 0
    scored_jobs: int = 0
    new_jobs_7d: int = 0
    new_jobs_30d: int = 0
    # Distributions (each already sorted, largest first).
    by_category: list[LabelCount] = Field(default_factory=list)
    by_location: list[LabelCount] = Field(default_factory=list)
    top_companies: list[LabelCount] = Field(default_factory=list)
    top_skills: list[LabelCount] = Field(default_factory=list)
    # Skills the user cares about vs the market, e.g. how many jobs mention Java,
    # Python, etc. Driven by the config skills lists so it answers "how many of my
    # target jobs are Java?".
    tracked_skills: list[LabelCount] = Field(default_factory=list)
    # New jobs per day for the last 14 days, oldest first, for a sparkline.
    new_jobs_timeline: list[LabelCount] = Field(default_factory=list)
    # Match-score buckets (0-39, 40-59, 60-79, 80-100) over scored jobs.
    score_buckets: list[LabelCount] = Field(default_factory=list)


class DailyDigest(StrictModel):
    """A deterministic summary of today's standing, shown atop the coach page.

    Computed from the local data without AI so it always renders; the coach may
    phrase it more warmly when a provider is configured, but these numbers are the
    ground truth it is not allowed to contradict.
    """

    generated_at: datetime = Field(default_factory=utc_now)
    new_jobs_today: int = 0
    new_jobs_7d: int = 0
    total_active_jobs: int = 0
    strong_matches: int = 0  # jobs scoring at/above the configured minimum
    applications_total: int = 0
    applications_7d: int = 0
    top_picks: list[str] = Field(default_factory=list)  # "Title — Company" strings
    headline: str = ""  # one-line deterministic summary


class ChatMessage(StrictModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=20_000)
    created_at: datetime = Field(default_factory=utc_now)


class ChatReply(StrictModel):
    """One coach turn: the assistant's grounded answer plus whether AI was used."""

    reply: str
    used_ai: bool = False


class GccReport(StrictModel):
    """Phase 6 GCC discovery report, measured against the Nasscom-Zinnov benchmark.

    The 2,117 figure is a published industry count (Nasscom-Zinnov, FY2026) of the
    whole GCC universe in India; it is a *benchmark to measure coverage against*, not
    something discoverable from any API. Everything else here is computed from the
    local registry/config, so the report is honest about how far the verifiable,
    crawlable set falls short of that universe and why.
    """

    generated_at: datetime = Field(default_factory=utc_now)
    benchmark_total: int = 2_117  # Nasscom-Zinnov FY2026 GCC universe
    benchmark_source: str = "Nasscom-Zinnov FY2026 GCC report"

    # 1-10 from the task spec.
    gcc_candidates_seen: int = 0  # GCC-tagged boards encountered (any status)
    unique_gcc_employers: int = 0  # deduped by normalized parent identity
    already_in_jobos: int = 0  # GCC employers already configured before discovery
    careers_pages_found: int = 0  # GCC boards with a resolved careers/ATS URL
    ats_distribution: list[LabelCount] = Field(default_factory=list)
    crawlable_boards: int = 0  # VALID GCC boards (verified crawlable)
    unsupported_boards: int = 0  # GCC boards needing an adapter / manual review
    with_active_india_jobs: int = 0  # GCC boards with >=1 India role
    newly_added: int = 0  # verified GCC boards now in config
    failed_validations: list[LabelCount] = Field(default_factory=list)  # status -> count

    @property
    def coverage_pct(self) -> float:
        return round(100 * self.unique_gcc_employers / self.benchmark_total, 1)


class HealthItem(StrictModel):
    name: str
    ok: bool
    detail: str


class HealthReport(StrictModel):
    ok: bool
    checks: list[HealthItem]
    generated_at: datetime = Field(default_factory=utc_now)


JsonObject = dict[str, Any]
