"""The truthfulness promise. These are the tests that matter most."""

from __future__ import annotations

from jobos.agents import (
    ATSAgent,
    Coordinator,
    ResumeAgent,
    SkillsAgent,
    SummaryAgent,
    TruthGuard,
    analyze_job_offline,
)
from jobos.models import (
    BulletRewrite,
    JobAnalysis,
    JobView,
    OptimizationPlan,
    StructuredResume,
)


def _apply(
    resume: StructuredResume, plan: OptimizationPlan, analysis: JobAnalysis | None = None
) -> tuple[StructuredResume, list[str]]:
    result = TruthGuard().apply(resume, plan, analysis or JobAnalysis())
    return result.optimized_resume, [item.reason for item in result.rejected_edits]


def test_rejects_a_skill_the_job_wants_but_resume_lacks(
    resume: StructuredResume, job: JobView
) -> None:
    analysis = analyze_job_offline(job)
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(bullet_id="bul-api", text="Built Kubernetes platforms with FastAPI.")
        ]
    )
    optimized, reasons = _apply(resume, plan, analysis)
    assert reasons and "kubernetes" in reasons[0].casefold()
    assert "Kubernetes" not in optimized.experience[0].bullets[0].text


def test_rejects_invented_numbers(resume: StructuredResume) -> None:
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(
                bullet_id="bul-api", text="Built Python REST services serving 900k requests per day."
            )
        ]
    )
    optimized, reasons = _apply(resume, plan)
    assert any("numeric" in reason for reason in reasons)
    assert "900k" not in optimized.experience[0].bullets[0].text


def test_rejects_scope_escalation(resume: StructuredResume) -> None:
    """'Maintained' must not silently become 'managed' or 'led'."""
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(bullet_id="bul-report", text="Managed PostgreSQL reporting jobs.")
        ]
    )
    optimized, reasons = _apply(resume, plan)
    assert any("scope" in reason for reason in reasons)
    assert optimized.experience[1].bullets[0].text.startswith("Maintained")


def test_rejects_unknown_named_entity(resume: StructuredResume) -> None:
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(bullet_id="bul-api", text="Built Python REST services for Netflix.")
        ]
    )
    _, reasons = _apply(resume, plan)
    assert any("named entity" in reason for reason in reasons)


def test_rejects_unknown_bullet_id(resume: StructuredResume) -> None:
    plan = OptimizationPlan(
        bullet_rewrites=[BulletRewrite(bullet_id="bul-nope", text="Anything at all.")]
    )
    _, reasons = _apply(resume, plan)
    assert any("unknown bullet" in reason for reason in reasons)


def test_emptied_bullet_cannot_even_be_proposed() -> None:
    """Blank wording is refused by the schema before the guard is reached."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BulletRewrite(bullet_id="bul-api", text="   ")


def test_rejects_wholesale_replacement(resume: StructuredResume) -> None:
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(bullet_id="bul-api", text="Completely different unrelated sentence here.")
        ]
    )
    _, reasons = _apply(resume, plan)
    assert any("changed too much" in reason for reason in reasons)


def test_accepts_a_grounded_rewording(resume: StructuredResume) -> None:
    plan = OptimizationPlan(
        bullet_rewrites=[
            BulletRewrite(
                bullet_id="bul-api",
                text="Built Python REST services with FastAPI handling 40k requests per day.",
            )
        ]
    )
    optimized, reasons = _apply(resume, plan)
    assert reasons == []
    assert "handling 40k requests" in optimized.experience[0].bullets[0].text


def test_skills_may_reorder_but_not_change_membership(resume: StructuredResume) -> None:
    reordered = list(reversed(resume.skills))
    optimized, reasons = _apply(resume, OptimizationPlan(skill_order=reordered))
    assert reasons == [] and optimized.skills == reordered

    _, reasons = _apply(resume, OptimizationPlan(skill_order=[*resume.skills, "Kubernetes"]))
    assert any("add or remove" in reason for reason in reasons)


def test_bullet_reordering_preserves_membership(resume: StructuredResume) -> None:
    ids = [bullet.id for bullet in resume.experience[0].bullets]
    optimized, reasons = _apply(
        resume, OptimizationPlan(bullet_order={"exp-acme": list(reversed(ids))})
    )
    assert reasons == []
    assert [b.id for b in optimized.experience[0].bullets] == list(reversed(ids))

    _, reasons = _apply(resume, OptimizationPlan(bullet_order={"exp-acme": ids[:1]}))
    assert any("unknown or missing" in reason for reason in reasons)


def test_locked_facts_never_change(resume: StructuredResume, job: JobView) -> None:
    """No plan can alter employers, titles, dates, or education."""
    analysis = analyze_job_offline(job)
    plan = OptimizationPlan(
        summary="Senior architect who managed Kubernetes at Google since 2010.",
        skill_order=list(reversed(resume.skills)),
        bullet_rewrites=[
            BulletRewrite(bullet_id="bul-api", text="Led Kubernetes migration at Google in 2010.")
        ],
        bullet_order={"exp-acme": [b.id for b in reversed(resume.experience[0].bullets)]},
    )
    optimized, reasons = _apply(resume, plan, analysis)
    assert len(reasons) >= 2
    assert [e.company for e in optimized.experience] == [e.company for e in resume.experience]
    assert [e.title for e in optimized.experience] == [e.title for e in resume.experience]
    assert [(e.start_date, e.end_date) for e in optimized.experience] == [
        (e.start_date, e.end_date) for e in resume.experience
    ]
    assert optimized.education == resume.education
    assert optimized.contact == resume.contact
    assert sorted(optimized.skills) == sorted(resume.skills)


def test_summary_rejects_unsupported_skill(resume: StructuredResume, job: JobView) -> None:
    analysis = analyze_job_offline(job)
    optimized, reasons = _apply(
        resume, OptimizationPlan(summary="Engineer skilled in Kubernetes."), analysis
    )
    assert reasons and optimized.summary == resume.summary


def test_diff_records_rejections(resume: StructuredResume, job: JobView) -> None:
    result = TruthGuard().apply(
        resume,
        OptimizationPlan(
            bullet_rewrites=[BulletRewrite(bullet_id="bul-api", text="Built Kubernetes clusters.")]
        ),
        analyze_job_offline(job),
    )
    assert result.diff.rejected_edits
    assert result.diff.rejected_edits[0].proposed_text == "Built Kubernetes clusters."
    assert result.warnings


async def test_coordinator_without_ai_still_improves_ordering(
    resume: StructuredResume, job: JobView
) -> None:
    """With no model available the workflow still completes deterministically."""
    coordinator = Coordinator(
        skills=SkillsAgent(),
        summary=SummaryAgent(None),
        resume=ResumeAgent(None),
        ats=ATSAgent(),
        truth_guard=TruthGuard(),
    )
    bundle = await coordinator.optimize(resume, job, analyze_job_offline(job))
    assert bundle.result.optimized_resume.contact == resume.contact
    assert sorted(bundle.result.optimized_resume.skills) == sorted(resume.skills)
    assert bundle.ats_before.overall_score > 0
    assert bundle.result.diff.ordering_changes
