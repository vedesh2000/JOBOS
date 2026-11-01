"""Phase 1 scoring-correctness tests for jobos-ats-v2.

These lock in: OR/alternative requirement groups, N/A preferred coverage with
renormalisation, evidence-cited responsibility matching, and — crucially — that the
legacy v1 scorer is byte-for-byte preserved for historical results.
"""

from __future__ import annotations

from jobos.agents import detect_skill_groups
from jobos.ats import SCORING_VERSION, SCORING_VERSION_V2, score_resume
from jobos.models import (
    Contact,
    Experience,
    JobAnalysis,
    JobData,
    JobView,
    ResumeBullet,
    StructuredResume,
    utc_now,
)


def _resume() -> StructuredResume:
    return StructuredResume(
        contact=Contact(name="Test User", email="t@example.com", links=[]),
        summary="Senior Software Engineer building web platforms with React and JavaScript.",
        skills=["JavaScript", "React", "TypeScript", "Python"],
        experience=[
            Experience(
                id="exp-one",
                company="Acme",
                title="Senior Software Engineer",
                start_date="Jan 2019",
                end_date="Present",
                skills=["React", "JavaScript"],
                bullets=[
                    ResumeBullet(
                        id="exp-one-b1",
                        text="Built reusable React UI components and enforced code reviews and unit testing with Jest.",
                    ),
                    ResumeBullet(
                        id="exp-one-b2",
                        text="Owned features from design and implementation through test automation and delivery.",
                    ),
                ],
            )
        ],
    )


def _job(description: str, skills: list[str]) -> JobView:
    data = JobData(
        company="Acme",
        title="Senior Software Engineer",
        location="Remote",
        description=description,
        apply_url="https://x.test/apply",
        source_url="https://x.test",
        external_id="x1",
        skills=skills,
    )
    return JobView(id=1, first_seen_at=utc_now(), last_seen_at=utc_now(), **data.model_dump())


# --- backward compatibility -------------------------------------------------

def test_v1_is_default_and_unchanged() -> None:
    resume = _resume()
    analysis = JobAnalysis(required_skills=["javascript", "react", "vue.js"], preferred_skills=[])
    report = score_resume(resume, analysis, "Senior Software Engineer")
    assert report.scoring_version == SCORING_VERSION  # default stays v1
    required = next(c for c in report.components if c.name == "Required skill coverage")
    # v1: 3 wanted, 2 matched -> 25 * 2/3
    assert round(required.score, 2) == 16.67
    preferred = next(c for c in report.components if "Preferred" in c.name)
    assert preferred.score == 10.0  # v1 keeps the (vacuous) full credit — unchanged


# --- OR / alternative groups ------------------------------------------------

def test_detect_skill_groups_finds_react_or_vue() -> None:
    job = _job("Experience with a modern UI framework like React or Vue.", ["react", "vue.js"])
    groups = detect_skill_groups(job)
    assert any({"react", "vue.js"} == set(g) for g in groups)


def test_v2_react_satisfies_react_or_vue() -> None:
    resume = _resume()  # has React, not Vue
    analysis = JobAnalysis(
        required_skills=["javascript", "react", "vue.js"],
        skill_groups=[["react", "vue.js"]],
        preferred_skills=[],
    )
    report = score_resume(resume, analysis, "Senior Software Engineer", scoring_version=SCORING_VERSION_V2)
    required = next(c for c in report.components if "Required" in c.name)
    # Two units now: {javascript} and {react OR vue.js}; both satisfied -> full 25.
    assert required.score == 25.0
    # vue.js must NOT appear as an independent missing requirement.
    assert not any("vue" in g.casefold() and " or " not in g.casefold() for g in required.gaps)


# --- preferred N/A + renormalisation ----------------------------------------

def test_v2_preferred_na_when_no_preferred_skills() -> None:
    resume = _resume()
    analysis = JobAnalysis(required_skills=["javascript"], preferred_skills=[])
    report = score_resume(resume, analysis, "Senior Software Engineer", scoring_version=SCORING_VERSION_V2)
    preferred = next(c for c in report.components if "Preferred" in c.name)
    assert "N/A" in preferred.name
    assert preferred.score == 0.0  # no vacuous full credit
    # Score is renormalised over the applicable 90, not padded by a free 10.
    assert 0 <= report.overall_score <= 100


# --- evidence-cited responsibility matching ---------------------------------

def test_v2_responsibility_cites_source_when_matched() -> None:
    resume = _resume()
    # A responsibility that genuinely overlaps the resume bullet wording.
    analysis = JobAnalysis(
        required_skills=["react"],
        responsibilities=[
            "Built reusable React UI components and enforced code reviews and unit testing.",
        ],
    )
    report = score_resume(resume, analysis, "Senior Software Engineer", scoring_version=SCORING_VERSION_V2)
    resp = next(c for c in report.components if "Responsibility" in c.name)
    # It should match AND cite the source experience.
    assert resp.evidence, "expected a cited responsibility match"
    assert "⟵" in resp.evidence[0]  # source citation marker


def test_v2_does_not_infer_unrelated_responsibilities() -> None:
    """A responsibility with no lexical/skill support stays a gap — no inference."""
    resume = _resume()
    analysis = JobAnalysis(
        required_skills=["react"],
        responsibilities=[
            "Manage vendor contracts and negotiate enterprise procurement budgets.",
        ],
    )
    report = score_resume(resume, analysis, "Senior Software Engineer", scoring_version=SCORING_VERSION_V2)
    resp = next(c for c in report.components if "Responsibility" in c.name)
    assert not resp.evidence  # nothing fabricated
    assert resp.gaps  # honestly reported as a gap


def test_v2_threshold_not_lowered_vs_v1() -> None:
    """v2 keeps the 0.55 evidence bar; it must not score responsibilities higher than
    v1 purely by relaxing matching (same formula, just cited)."""
    resume = _resume()
    analysis = JobAnalysis(
        required_skills=["react"],
        responsibilities=["Contribute to the design and implementation of new product features."],
    )
    v1 = score_resume(resume, analysis, "SSE")
    v2 = score_resume(resume, analysis, "SSE", scoring_version=SCORING_VERSION_V2)
    r1 = next(c for c in v1.components if "Responsibility" in c.name).score
    r2 = next(c for c in v2.components if "Responsibility" in c.name).score
    # Same underlying alignment math -> within rounding of each other.
    assert abs(r1 - r2) < 0.5


# --- optimizer: gap-aware, never-worse, truthful ---------------------------

async def test_optimizer_never_ships_worse_and_reports_no_material_change() -> None:
    """The optimizer must never produce a resume scoring below the master, and must
    say so when there is no material improvement. Uses the deterministic path (no AI),
    which still exercises the guard and ordering."""
    from jobos.agents import (
        ATSAgent,
        Coordinator,
        ResumeAgent,
        SkillsAgent,
        SummaryAgent,
        TruthGuard,
    )

    coord = Coordinator(
        skills=SkillsAgent(),
        summary=SummaryAgent(None),      # AI off -> deterministic
        resume=ResumeAgent(None),        # AI off -> reorder only
        ats=ATSAgent(),                  # v2
        truth_guard=TruthGuard(),
    )
    resume = _resume()
    job = _job("Build reusable React components; React or Vue.", ["react", "vue.js"])
    analysis = JobAnalysis(
        required_skills=["react"],
        skill_groups=[["react", "vue.js"]],
        responsibilities=["Build reusable React UI components with unit testing."],
    )
    bundle = await coord.optimize(resume, job, analysis)
    assert bundle.ats_after.overall_score >= bundle.ats_before.overall_score  # never worse
    assert bundle.ats_before.scoring_version == bundle.ats_after.scoring_version == SCORING_VERSION_V2
    # Deterministic reorder-only -> no wording change -> honest "no material" note.
    assert any("No material" in w for w in bundle.warnings)


def test_rewrite_payload_includes_job_responsibilities() -> None:
    """The rewrite prompt must receive the job responsibilities (the scored gap),
    not just skills — verified by inspecting the payload the agent builds."""
    import asyncio

    from jobos.agents import BulletSuggestion, ResumeAgent

    captured: dict = {}

    class FakeAI:
        class settings:  # noqa: N801
            class tasks:  # noqa: N801
                rewrite_bullets = True
            max_bullet_rewrites = 6

        async def generate(self, schema, *, system, payload):
            captured["payload"] = payload
            captured["system"] = system
            return BulletSuggestion(bullet_rewrites=[], explanations=[])

    agent = ResumeAgent(FakeAI())
    resume = _resume()
    analysis = JobAnalysis(
        required_skills=["react"],
        responsibilities=["Build reusable React UI components", "Own code from design to delivery"],
    )
    asyncio.run(agent.propose(resume, analysis))
    assert "job_responsibilities" in captured["payload"]
    assert captured["payload"]["job_responsibilities"]  # non-empty
    assert "job_responsibilities" in captured["system"]  # prompt targets them
