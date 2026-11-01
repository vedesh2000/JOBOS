"""Tests for the readable one-pager: theme, emphasis, links, and the display
condensing/budget layer. These never touch the master YAML or TruthGuard rules."""

from __future__ import annotations

import re

from jobos.agents import TruthGuard
from jobos.models import (
    Contact,
    Experience,
    JobAnalysis,
    ResumeBullet,
    StructuredResume,
)
from jobos.renderer import (
    THEMES,
    BulletCondenser,
    RenderBudget,
    ResumeRenderer,
    _deterministic_condense,
    _emphasize_bullet,
    _is_clean_word_subset,
    _safe_link,
    condense_trace_to_markdown,
)


def _resume(bullets: list[str], skills: list[str] | None = None) -> StructuredResume:
    return StructuredResume(
        contact=Contact(
            name="Test User",
            email="test@example.com",
            links=["https://github.com/testuser", "https://linkedin.com/in/testuser"],
        ),
        summary="Backend engineer.",
        skills=skills or ["Python", "AWS"],
        experience=[
            Experience(
                id="exp-1",
                company="Acme",
                title="Senior Engineer",
                start_date="Jan 2021",
                end_date="Present",
                skills=["Python", "AWS"],
                bullets=[
                    ResumeBullet(id=f"bull-{i}", text=text) for i, text in enumerate(bullets)
                ],
            )
        ],
    )


# --- theme config -----------------------------------------------------------

def test_readable_theme_registered_and_legacy_preserved() -> None:
    assert "onepager-readable" in THEMES
    assert "onepager" in THEMES  # legacy still present
    readable = THEMES["onepager-readable"]
    # Readable body stays in the 9pt range and never renders below ~8.5pt effective.
    base_pt = float(readable["base_font_size"].replace("pt", ""))
    assert 8.8 <= base_pt <= 9.6
    assert readable["rich"] == "true"
    # Minimum effective size stays at or above ~8.5pt (base * min_scale).
    assert base_pt * float(readable["min_scale"]) >= 8.5
    # Legacy onepager unchanged: no rich emphasis, old 8.2pt base.
    assert THEMES["onepager"]["base_font_size"] == "8.2pt"
    assert "rich" not in THEMES["onepager"]
    # Legacy onepager unchanged: no rich emphasis, old 8.2pt base.
    assert THEMES["onepager"]["base_font_size"] == "8.2pt"
    assert "rich" not in THEMES["onepager"]


# --- safe links -------------------------------------------------------------

def test_safe_link_accepts_https_and_mailto_and_labels_known_hosts() -> None:
    assert _safe_link("https://github.com/x") == {"href": "https://github.com/x", "label": "GitHub"}
    assert _safe_link("https://www.linkedin.com/in/x")["label"] == "LinkedIn"
    assert _safe_link("https://leetcode.com/u/x")["label"] == "LeetCode"
    assert _safe_link("a@b.com")["href"] == "mailto:a@b.com"


def test_safe_link_rejects_unsafe_schemes() -> None:
    assert _safe_link("javascript:alert(1)") is None
    assert _safe_link("data:text/html,x") is None
    assert _safe_link("file:///etc/passwd") is None


# --- emphasis ---------------------------------------------------------------

def test_emphasize_bolds_metrics_and_skills_and_escapes() -> None:
    html = str(_emphasize_bullet("Cut latency by 90% using Python & <b>Redis</b>", ["Python"]))
    assert "<strong>90%</strong>" in html
    assert "<strong>Python</strong>" in html
    # Original angle brackets/ampersands are escaped, not rendered as markup.
    assert "&lt;b&gt;" in html and "&amp;" in html


def test_emphasize_adds_no_text() -> None:
    original = "Improved throughput by 2x for 500 users"
    html = str(_emphasize_bullet(original, []))
    # Stripping the strong tags recovers exactly the escaped original.
    stripped = re.sub(r"</?strong>", "", html)
    assert stripped == "Improved throughput by 2x for 500 users"


# --- deterministic condensing safety ----------------------------------------

def test_condense_strips_leading_label_cleanly() -> None:
    text = (
        "GDPR verification framework: architected reusable two-stage inline and "
        "post-deletion verification across many databases and downstream systems here"
    )
    out = _deterministic_condense(text, target=100)
    assert out.startswith("Architected reusable")
    assert "framework:" not in out


def test_condense_never_introduces_new_words() -> None:
    text = "Reliability work: integrated incomplete or inconsistent deletion state detection across teams"
    out = _deterministic_condense(text, target=40)
    assert _is_clean_word_subset(out, text)
    assert "inor" not in out.casefold()


def test_clean_word_subset_detects_mangling() -> None:
    assert _is_clean_word_subset("Architected reusable verification", "X: architected reusable verification")
    assert not _is_clean_word_subset("inor inconsistent", "incomplete or inconsistent")


def test_condenser_preserves_ids_and_numbers_and_never_mutates_master() -> None:
    long_bullet = (
        "Payments reliability: redesigned cart APIs for mixed-basket discounts, migrated REST "
        "endpoints to GraphQL, and introduced idempotency improving load time by 30% and "
        "reducing failed transactions by 20% across the platform"
    )
    resume = _resume([long_bullet])
    before = resume.model_copy(deep=True)
    condenser = BulletCondenser(TruthGuard(), JobAnalysis(required_skills=["graphql"]))
    display, trace = condenser.condense(resume)

    # Master copy untouched.
    assert resume == before
    # IDs stable.
    assert display.experience[0].bullets[0].id == "bull-0"
    changed = [item for item in trace if item.changed]
    if changed:
        shown = display.experience[0].bullets[0].text
        # Numbers preserved, none invented.
        assert set(re.findall(r"\d+", shown)) <= set(re.findall(r"\d+", long_bullet))
        assert _is_clean_word_subset(shown, long_bullet)
        # Traceability: original recorded.
        assert changed[0].original == long_bullet


def test_condenser_keeps_short_bullets_untouched() -> None:
    resume = _resume(["Short bullet under budget."])
    _, trace = BulletCondenser(TruthGuard(), JobAnalysis()).condense(resume)
    assert all(not item.changed for item in trace)


# --- budget -----------------------------------------------------------------

def test_budget_trims_bullets_and_skills_with_reasons_and_preserves_source() -> None:
    bullets = [f"Bullet number {i} with enough length to matter here." for i in range(8)]
    resume = _resume(bullets, skills=[f"skill{i}" for i in range(40)])
    before = resume.model_copy(deep=True)
    budget = RenderBudget(recent_role_bullets=4, older_role_bullets=4, project_bullets=4, max_skills=10)
    trimmed, omitted = budget.apply(resume)

    assert resume == before  # never mutates input
    assert len(trimmed.experience[0].bullets) == 4
    assert len(trimmed.skills) == 10
    # Every omission is recorded with a reason.
    assert all(item.reason for item in omitted)
    assert any(item.kind == "bullet" for item in omitted)
    assert any(item.kind == "skill" for item in omitted)


def test_condense_trace_markdown_lists_before_and_after() -> None:
    from jobos.renderer import CondensedBullet

    trace = [CondensedBullet("b0", "Acme", "Original long text here", "Short text", True)]
    md = condense_trace_to_markdown(trace)
    assert "b0" in md and "Original long text here" in md and "Short text" in md


# --- rendering / backward compatibility -------------------------------------

def test_readable_render_has_links_and_emphasis() -> None:
    resume = _resume(
        ["Cut cost by 25% with Python across many services and jobs that need shortening here now"],
        skills=["Python", "AWS"],
    )
    html = ResumeRenderer("onepager-readable").render_html(resume)
    assert 'href="mailto:test@example.com"' in html
    assert ">GitHub</a>" in html
    assert "<strong>" in html


def test_legacy_onepager_applies_no_budget_or_condense() -> None:
    r = ResumeRenderer("onepager")
    assert r.applies_budget is False
    resume = _resume([f"Bullet {i} text." for i in range(8)])
    display, omitted = r.budgeted(resume)
    assert display is resume and omitted == []


# --- resume YAML authoring help ---------------------------------------------

def test_resume_yaml_guide_extracts_clean_prompt() -> None:
    """The in-app prompt is extracted from the shipped guide without '>' markers."""
    from jobos.config import load_config
    from jobos.services import JobOSService

    service = JobOSService(load_config("config.yaml"))
    guide, prompt = service.resume_yaml_guide()
    # Guide and prompt are present and substantial.
    assert len(guide) > 500
    assert len(prompt) > 200
    # The prompt carries the real schema keys, so an AI fills the right structure.
    assert "schema_version" in prompt
    assert "experience" in prompt and "bullets" in prompt
    # Blockquote markers are stripped so the copied text is clean.
    assert not any(line.startswith(">") for line in prompt.splitlines())
    # It ends by asking for the user's resume.
    assert "paste your resume" in prompt.lower()


# --- relevance-aware skill trimming -----------------------------------------

def test_budget_keeps_job_relevant_skills_first() -> None:
    """When the skill budget trims, job-relevant skills must survive over unrelated
    ones that merely sit earlier in the list (the bug where an AI JD lost RAG/LLMs)."""
    resume = StructuredResume(
        contact=Contact(name="T", email="t@example.com", links=[]),
        summary="Engineer.",
        # Relevant skills deliberately placed LAST so position-only trimming drops them.
        skills=["Docker", "CI/CD", "GitHub Actions", "PagerDuty", "Fastapi", "Rag", "Llms"],
    )
    jd = {"fastapi", "rag", "llms"}
    trimmed, omitted = RenderBudget(
        recent_role_bullets=5, older_role_bullets=4, project_bullets=4, max_skills=4
    ).apply(resume, relevant_skills=jd)
    kept = {s.casefold() for s in trimmed.skills}
    # All three relevant skills survive even though they were last in the list.
    assert {"fastapi", "rag", "llms"} <= kept
    assert len(trimmed.skills) == 4
    # Original order is preserved among the kept skills.
    assert trimmed.skills == [s for s in resume.skills if s in set(trimmed.skills)]


def test_budget_without_relevance_is_position_based() -> None:
    """No relevance hint -> old first-N behaviour (backward compatible)."""
    resume = StructuredResume(
        contact=Contact(name="T", email="t@example.com", links=[]),
        summary="Engineer.",
        skills=["A", "B", "C", "D", "E"],
    )
    trimmed, _ = RenderBudget(max_skills=3).apply(resume)
    assert trimmed.skills == ["A", "B", "C"]
