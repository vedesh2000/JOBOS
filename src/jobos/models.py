"""Validated data models shared by JobOS features."""

from __future__ import annotations

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


class JobView(JobData):
    id: int
    status: JobStatus = JobStatus.NEW
    match_score: int | None = Field(default=None, ge=0, le=100)
    analysis: JobAnalysis | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool = True


class JobAnalysis(StrictModel):
    required_skills: list[str] = Field(default_factory=list, max_length=200)
    preferred_skills: list[str] = Field(default_factory=list, max_length=200)
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
    errors: dict[str, str] = Field(default_factory=dict)


class HealthItem(StrictModel):
    name: str
    ok: bool
    detail: str


class HealthReport(StrictModel):
    ok: bool
    checks: list[HealthItem]
    generated_at: datetime = Field(default_factory=utc_now)


JsonObject = dict[str, Any]
