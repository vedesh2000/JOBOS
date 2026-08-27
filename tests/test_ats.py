"""ATS scoring: the core feature, so it is tested behaviourally rather than by snapshot."""

from __future__ import annotations

import pytest

from jobos.agents import analyze_job_offline
from jobos.ats import (
    SCORING_VERSION,
    canonical_skill,
    experience_years,
    extract_known_skills,
    matched_skills,
    normalize_text,
    score_resume,
    skill_in_text,
)
from jobos.models import Contact, JobAnalysis, JobView, StructuredResume


def test_normalisation_and_aliases() -> None:
    assert normalize_text("Node.JS & React!") == "node js and react"
    assert canonical_skill("Postgres") == canonical_skill("PostgreSQL") == "postgresql"
    assert canonical_skill("JS") == canonical_skill("JavaScript") == "javascript"
    assert canonical_skill("k8s") == "kubernetes"


def test_skill_matching_respects_token_boundaries() -> None:
    assert skill_in_text("Python", "Built Python services")
    assert not skill_in_text("C", "Wrote CSS and CSV parsers")
    assert skill_in_text("Node.js", "Worked with nodejs daily")
    # A related technology is never treated as equivalent.
    assert not skill_in_text("Kubernetes", "Shipped Docker containers")


def test_extract_known_skills_finds_canonical_names() -> None:
    found = extract_known_skills("We use Python, Postgres and k8s in production")
    assert {"python", "postgresql", "kubernetes"} <= set(found)


def test_full_required_coverage_when_all_supported(
    resume: StructuredResume, job: JobView
) -> None:
    analysis = analyze_job_offline(job)
    report = score_resume(resume, analysis, job.title)
    required = next(c for c in report.components if c.name == "Required skill coverage")
    assert required.score == required.maximum
    assert report.scoring_version == SCORING_VERSION
    assert 0 <= report.overall_score <= 100


def test_missing_skills_are_reported_not_invented(
    resume: StructuredResume, job: JobView
) -> None:
    analysis = analyze_job_offline(job)
    report = score_resume(resume, analysis, job.title)
    assert "kubernetes" in report.missing_skills
    assert "terraform" in report.missing_skills
    assert "kubernetes" not in report.matching_skills
    # The resume itself is never mutated by scoring.
    assert "kubernetes" not in [skill.casefold() for skill in resume.skills]


def test_score_is_not_keyword_count(resume: StructuredResume, job: JobView) -> None:
    """Repeating a keyword must not raise the score."""
    analysis = analyze_job_offline(job)
    baseline = score_resume(resume, analysis, job.title).overall_score
    stuffed = resume.model_copy(deep=True)
    stuffed.summary = " ".join(["Python FastAPI PostgreSQL"] * 40)
    assert score_resume(stuffed, analysis, job.title).overall_score <= baseline


def test_contextual_evidence_beats_a_bare_skill_list(job: JobView) -> None:
    analysis = JobAnalysis(required_skills=["Python"])
    listed_only = StructuredResume(
        contact=Contact(name="A B", email="a@example.com"), skills=["Python"]
    )
    with_evidence = listed_only.model_copy(deep=True)
    with_evidence.projects = [
        __import__("jobos.models", fromlist=["Project"]).Project(
            id="prj-1",
            name="Tool",
            bullets=[
                __import__("jobos.models", fromlist=["ResumeBullet"]).ResumeBullet(
                    id="bul-1", text="Wrote a Python parser for statements."
                )
            ],
        )
    ]

    def contextual(candidate: StructuredResume) -> float:
        report = score_resume(candidate, analysis, job.title)
        return next(
            c.score for c in report.components if c.name == "Contextual skill evidence"
        )

    assert contextual(with_evidence) > contextual(listed_only)


def test_unstated_requirements_do_not_penalise(resume: StructuredResume, job: JobView) -> None:
    """A posting that states no requirement should not be scored as a failure."""
    empty = JobAnalysis()
    report = score_resume(resume, empty, job.title)
    for name in ("Required skill coverage", "Preferred skill coverage", "Responsibility alignment"):
        component = next(c for c in report.components if c.name == name)
        assert component.score == component.maximum


def test_components_sum_to_overall(resume: StructuredResume, job: JobView) -> None:
    analysis = analyze_job_offline(job)
    report = score_resume(resume, analysis, job.title)
    assert sum(c.maximum for c in report.components) == 100
    assert report.overall_score == round(sum(c.score for c in report.components))


def test_every_component_is_explainable(resume: StructuredResume, job: JobView) -> None:
    report = score_resume(resume, analyze_job_offline(job), job.title)
    assert report.components
    for component in report.components:
        assert 0 <= component.score <= component.maximum
        assert component.evidence or component.gaps, f"{component.name} explains nothing"


def test_parseability_flags_missing_structure(job: JobView) -> None:
    bare = StructuredResume(contact=Contact(name="A B", email=""))
    report = score_resume(bare, JobAnalysis(), job.title)
    parseability = next(c for c in report.components if c.name == "Parseability and structure")
    assert parseability.score < parseability.maximum
    assert any("email" in gap.casefold() for gap in parseability.gaps)


def test_experience_years_uses_structured_dates(resume: StructuredResume) -> None:
    years = experience_years(resume)
    assert years is not None and years > 4
    undated = resume.model_copy(deep=True)
    for item in undated.experience:
        item.start_date = ""
        item.end_date = ""
    assert experience_years(undated) is None


def test_matched_skills_partitions_cleanly(resume: StructuredResume) -> None:
    matched, missing, contextual = matched_skills(resume, ["Python", "Kubernetes"])
    assert matched == ["Python"] and missing == ["Kubernetes"]
    assert contextual == ["Python"]


@pytest.mark.parametrize("score", [0, 45, 79, 100])
def test_report_serialises_roundtrip(
    resume: StructuredResume, job: JobView, score: int
) -> None:
    report = score_resume(resume, analyze_job_offline(job), job.title)
    from jobos.models import ATSReportData

    assert ATSReportData.model_validate_json(report.model_dump_json()) == report
