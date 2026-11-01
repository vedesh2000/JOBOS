"""Render resumes to Markdown, HTML, and PDF, and write versioned output packages."""

from __future__ import annotations

import io
import json
import re
import shutil
import unicodedata
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pypdf import PdfReader

from jobos.models import (
    ATSReportData,
    CoverLetterDraft,
    JobAnalysis,
    JobView,
    ResumeDiff,
    StructuredResume,
    validate_web_url,
)

TEMPLATE_DIR: Final = Path(__file__).parent / "templates"
THEMES: Final[dict[str, dict[str, str]]] = {
    "classic": {
        "page_size": "A4",
        "margin": "14mm 15mm",
        "base_font_size": "10.5pt",
        "font_stack": '-apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif',
    },
    "compact": {
        "page_size": "A4",
        "margin": "11mm 13mm",
        "base_font_size": "9.8pt",
        "font_stack": '-apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif',
    },
    "letter": {
        "page_size": "Letter",
        "margin": "0.55in 0.6in",
        "base_font_size": "10.5pt",
        "font_stack": '"Times New Roman", Times, Georgia, serif',
    },
    "onepager": {
        "page_size": "A4",
        "margin": "6mm 9mm",
        "base_font_size": "8.2pt",
        "font_stack": '-apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif',
        # Tight vertical rhythm so dense content still fits one page. Single column
        # and real selectable text keep it ATS-parseable.
        "line_height": "1.18",
        "section_gap": "6px",
        "entry_head_gap": "4px",
        "entry_gap": "3px",
        "li_gap": "1px",
        # Scale borderline content back onto a single page (see _one_page_scale).
        "single_page": "true",
    },
    # Readable one-pager: larger body text and humane spacing, fitting a single page
    # by trimming the lowest-priority display content (see content budget in the
    # renderer) rather than shrinking the font to an unreadable size. Keeps the plain
    # single-column, selectable-text layout so it stays ATS-parseable. Enables bold
    # emphasis on metrics and clickable links via the template.
    "onepager-readable": {
        "page_size": "A4",
        "margin": "8mm 11mm",
        "base_font_size": "9pt",
        "font_stack": '-apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif',
        "name_font_size": "15pt",
        "heading_font_size": "10pt",
        "line_height": "1.2",
        "section_gap": "6px",
        "entry_head_gap": "5px",
        "entry_gap": "4px",
        "li_gap": "1px",
        "single_page": "true",
        # Never shrink below this effective scale: 9pt * 0.95 ~= 8.55pt floor.
        "min_scale": "0.95",
        # Turn on render-time emphasis and clickable links in the template.
        "rich": "true",
    },
}


class RenderError(RuntimeError):
    """Rendering failed or produced a PDF that would not parse cleanly."""


@dataclass(slots=True)
class RenderedPackage:
    """Where a generated application package landed on disk."""

    directory: Path
    version: int
    files: dict[str, Path] = field(default_factory=dict)


def safe_path_name(value: str, fallback: str = "unknown") -> str:
    """Turn untrusted company/role text into one safe, readable path segment."""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        char for char in normalized if char.isprintable() and not unicodedata.combining(char)
    )
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = normalized[:80].strip(" .")
    return normalized or fallback


def _bullet_lines(texts: list[str]) -> str:
    return "\n".join(f"- {text}" for text in texts)


# --- Render-time emphasis, links, and content budget ------------------------
# These operate only on a deep copy at render time. They never mutate the master
# YAML or the stored tailored resume, never invent text, and never emit raw HTML
# from untrusted input: emphasis wraps matched substrings of the ORIGINAL escaped
# text in <strong>, and links are built from scheme-validated URLs.

# A metric is a number optionally carrying %, +, x, k/m/b, or a currency sign, or a
# range like "50-60%". Matching these and bolding them mirrors the reference resume,
# where quantified impact ("90%", "750+", "4,000+") carries the visual weight.
_METRIC_RE: Final = re.compile(
    r"(?<![\w.])(?:[$₹€£]\s?)?\d[\d,]*(?:\.\d+)?\s?(?:[-–]\s?\d[\d,]*(?:\.\d+)?)?"
    r"\s?(?:%|\+|x|k|m|b|bn|hrs?|ms)?",
    re.IGNORECASE,
)

_SAFE_URL_SCHEMES: Final = ("http://", "https://", "mailto:")


def _emphasize_bullet(text: str, skills: list[str]) -> str:
    """Return HTML for a bullet with metrics (and a few key skills) bolded.

    The input is first HTML-escaped, so no markup from the resume text can execute.
    Only safe, deterministic spans are then wrapped in <strong>: numeric metrics
    always, plus any skill token from the role's own skill list that appears in the
    bullet (so "Python/Django" stands out where it is genuinely used). This adds no
    words and changes no facts.
    """
    from markupsafe import Markup, escape

    escaped = str(escape(text))

    spans: list[tuple[int, int]] = []
    for match in _METRIC_RE.finditer(escaped):
        token = match.group(0).strip()
        # Ignore a bare "+"/"%" with no digit, and lone separators.
        if token and any(ch.isdigit() for ch in token):
            spans.append((match.start(), match.start() + len(match.group(0).rstrip())))

    # Bold the role's own skills where they occur, longest first so "node.js" wins
    # over "node". Word-boundary, case-insensitive, on the escaped text.
    for skill in sorted({s for s in skills if len(s) >= 2}, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w.]){re.escape(str(escape(skill)))}(?![\w.])", re.IGNORECASE)
        for match in pattern.finditer(escaped):
            spans.append((match.start(), match.end()))

    if not spans:
        return Markup(escaped)

    # Merge overlapping spans, then wrap from the right so indices stay valid.
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = escaped
    for start, end in reversed(merged):
        result = f"{result[:start]}<strong>{result[start:end]}</strong>{result[end:]}"
    return Markup(result)


def _link_label(url: str) -> str:
    """A short, human label for a known profile link, else the bare host/path."""
    lowered = url.casefold()
    if lowered.startswith("mailto:"):
        return url[len("mailto:") :]
    known = {
        "github.com": "GitHub",
        "linkedin.com": "LinkedIn",
        "leetcode.com": "LeetCode",
        "gitlab.com": "GitLab",
    }
    for host, label in known.items():
        if host in lowered:
            return label
    cleaned = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return cleaned


def _safe_link(url: str) -> dict[str, str] | None:
    """Validate a URL for use as a PDF hyperlink; return {href,label} or None.

    Only mailto/http/https are allowed. Anything else (javascript:, data:, etc.) is
    dropped rather than rendered, so no unsafe scheme can reach the PDF.
    """
    value = (url or "").strip()
    if "@" in value and not value.casefold().startswith(_SAFE_URL_SCHEMES) and "/" not in value:
        value = f"mailto:{value}"
    if not value.casefold().startswith(_SAFE_URL_SCHEMES):
        return None
    return {"href": value, "label": _link_label(value)}


@dataclass(slots=True)
class OmittedItem:
    """One display item left out of the PDF to keep it readable on one page."""

    kind: str  # "bullet" | "skill"
    location: str  # experience/project title, or "skills"
    identifier: str  # bullet id, or the skill name
    text: str
    reason: str


@dataclass(slots=True)
class RenderBudget:
    """Deterministic per-section display caps applied only at render time."""

    recent_role_bullets: int = 5
    older_role_bullets: int = 4
    project_bullets: int = 4
    max_skills: int = 32

    def apply(
        self,
        resume: StructuredResume,
        *,
        relevant_skills: set[str] | None = None,
    ) -> tuple[StructuredResume, list[OmittedItem]]:
        """Return a trimmed deep copy plus a record of everything removed.

        Bullet selection relies on the order the resume already carries (the
        ResumeAgent sorts bullets by relevance before this point), so it keeps the
        strongest items. Skill selection, when ``relevant_skills`` is given, keeps the
        skills that match the target job FIRST (in their original order) before filling
        the remaining budget with the rest — so a job-relevant skill is never dropped
        in favour of an unrelated one that merely sits earlier in the list. No text is
        altered, no IDs change, nothing is rewritten, and the input resume is never
        mutated. Whole experiences, projects, titles, dates, contact and education are
        always preserved.
        """
        trimmed = resume.model_copy(deep=True)
        omitted: list[OmittedItem] = []

        for index, experience in enumerate(trimmed.experience):
            cap = self.recent_role_bullets if index == 0 else self.older_role_bullets
            if len(experience.bullets) > cap:
                for bullet in experience.bullets[cap:]:
                    omitted.append(
                        OmittedItem(
                            kind="bullet",
                            location=f"{experience.title} — {experience.company}".strip(" —"),
                            identifier=bullet.id,
                            text=bullet.text,
                            reason=f"Beyond the {cap}-bullet display budget for this role "
                            "(kept in the stored resume and Markdown).",
                        )
                    )
                experience.bullets = experience.bullets[:cap]

        for project in trimmed.projects:
            if len(project.bullets) > self.project_bullets:
                for bullet in project.bullets[self.project_bullets :]:
                    omitted.append(
                        OmittedItem(
                            kind="bullet",
                            location=project.name,
                            identifier=bullet.id,
                            text=bullet.text,
                            reason=f"Beyond the {self.project_bullets}-bullet display budget "
                            "for this project (kept in the stored resume and Markdown).",
                        )
                    )
                project.bullets = project.bullets[: self.project_bullets]

        if len(trimmed.skills) > self.max_skills:
            # Keep job-relevant skills first (preserving their original order), then
            # fill the remaining budget with the rest. Without a relevance hint this
            # is exactly the old first-N behaviour.
            relevant = relevant_skills or set()
            prioritized = [s for s in trimmed.skills if s.casefold() in relevant] + [
                s for s in trimmed.skills if s.casefold() not in relevant
            ]
            keep_list = prioritized[: self.max_skills]
            kept = set(keep_list)
            for skill in trimmed.skills:
                if skill not in kept:
                    omitted.append(
                        OmittedItem(
                            kind="skill",
                            location="skills",
                            identifier=skill,
                            text=skill,
                            reason=f"Beyond the {self.max_skills}-skill display budget "
                            "(kept in the stored resume and Markdown).",
                        )
                    )
            # Preserve the resume's original skill order among the kept ones.
            trimmed.skills = [s for s in trimmed.skills if s in kept]
            # Keep skill_groups consistent with the trimmed flat list.
            if trimmed.skill_groups:
                trimmed.skill_groups = {
                    label: [item for item in items if item in kept]
                    for label, items in trimmed.skill_groups.items()
                }
                trimmed.skill_groups = {
                    label: items for label, items in trimmed.skill_groups.items() if items
                }
        return trimmed, omitted


# --- PDF-presentation condensing layer --------------------------------------
# Shortens the DISPLAY copy of selected bullets only, by deleting redundant
# wording — never adding or changing words. The master YAML and stored resume are
# never touched. Each condensed bullet is validated against the original with the
# existing TruthGuard; if the trimmed text would change a fact, it is discarded and
# the original wording is shown. IDs are preserved; a trace records original->shown.

# A condensed bullet must keep at least this much text so evidence is not gutted.
_MIN_CONDENSED_CHARS: Final = 60


def _is_clean_word_subset(candidate: str, original: str) -> bool:
    """True when every word in ``candidate`` also appears in ``original``.

    This catches mangled output (e.g. a regex that spliced "incomplete or" into
    "inor"): such corruption introduces a token the original never had. Punctuation
    and case are ignored. It is a guard, not the transform — the transform only
    removes whole leading/trailing segments, so a clean reduction always passes.
    """
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+(?:[.+#-][a-z0-9]+)*", value.casefold())

    original_words = set(words(original))
    return all(word in original_words for word in words(candidate))


class _RewriteValidator(Protocol):
    """The slice of TruthGuard the condenser needs, to avoid importing agents."""

    def _unsafe_rewrite_reason(
        self,
        original: str,
        proposed: str,
        resume: StructuredResume,
        analysis: JobAnalysis,
        experience_skills: list[str],
    ) -> str | None: ...


@dataclass(slots=True)
class CondensedBullet:
    """Trace of one display-condensed bullet, for auditability."""

    bullet_id: str
    location: str
    original: str
    shown: str
    changed: bool
    note: str = ""


@dataclass(slots=True)
class OnePageResult:
    """Everything a one-page render produced, for the package writer and tests."""

    pdf: bytes
    html: str
    omitted: list[OmittedItem]
    condensed: list[CondensedBullet]
    diagnostic: str


CondenseFn = Callable[
    [StructuredResume], tuple[StructuredResume, list[CondensedBullet]]
]


def _deterministic_condense(text: str, target: int = 180) -> str:
    """Shorten by deleting redundancy only — never adding or altering words.

    Strips a redundant opening verb phrase and known filler, collapses the
    "label: body" lead where the label merely restates the body, and trims trailing
    parenthetical asides that carry no digit or known skill. The result is always a
    subsequence-ish reduction of the original, so no fact can be introduced.
    """
    condensed = " ".join(text.split())
    if len(condensed) <= target:
        return condensed

    # The only transformations applied are ones that stay grammatical and never alter
    # meaning mid-sentence. Crude word deletion inside a sentence is deliberately NOT
    # done: it produced corrupted text like "inor inconsistent". We only (1) drop a
    # redundant leading "Topic: " label, and (2) drop a trailing digit-free aside.

    # (1) "GDPR verification framework: architected reusable..." -> "Architected
    # reusable...". The label is a topic restated by the body, so removing it loses no
    # fact. Require the body to start with a capitalized word (a clean clause) and to
    # retain enough length to remain substantive.
    label_match = re.match(r"^([A-Z][A-Za-z0-9/ &+.-]{2,45}):\s+(.+)$", condensed)
    if label_match:
        body = label_match.group(2).strip()
        if len(body) >= _MIN_CONDENSED_CHARS and body[:1].isalpha():
            condensed = body[0].upper() + body[1:]

    # (2) Drop a single trailing parenthetical that carries no number (and so no
    # metric). Anything with a digit is kept — it may be quantified evidence.
    paren = re.search(r"\s*\(([^()]*)\)\s*$", condensed)
    if paren and not re.search(r"\d", paren.group(1)):
        candidate = condensed[: paren.start()].rstrip(" ,;")
        if len(candidate) >= _MIN_CONDENSED_CHARS:
            condensed = candidate

    return condensed.strip(" ,;")


class BulletCondenser:
    """Produce a readable, shorter DISPLAY copy of a resume, validated for truth.

    This is a presentation transform: it shortens bullets that exceed ``soft_limit``
    using deterministic deletion, then validates each result with the existing
    TruthGuard against the original bullet. A rewrite that would change any fact is
    rejected and the original wording is kept. The master YAML and stored resume are
    never modified; only a deep copy is returned, plus a per-bullet trace.
    """

    def __init__(
        self, truth_guard: _RewriteValidator, analysis: JobAnalysis, *, soft_limit: int = 180
    ) -> None:
        self._guard = truth_guard
        self._analysis = analysis
        self._soft_limit = soft_limit

    def condense(
        self, resume: StructuredResume
    ) -> tuple[StructuredResume, list[CondensedBullet]]:
        display = resume.model_copy(deep=True)
        trace: list[CondensedBullet] = []

        def process(bullet_id: str, location: str, text: str, skills: list[str]) -> str:
            if len(text) <= self._soft_limit:
                return text
            candidate = _deterministic_condense(text, self._soft_limit)
            if candidate == text or len(candidate) < _MIN_CONDENSED_CHARS:
                trace.append(CondensedBullet(bullet_id, location, text, text, False,
                                             "No safe reduction found; kept original."))
                return text
            # Guard against mangled output: every word shown must exist in the original.
            if not _is_clean_word_subset(candidate, text):
                trace.append(CondensedBullet(bullet_id, location, text, text, False,
                                             "Kept original (condensed text was not a clean subset)."))
                return text
            # Reuse the existing TruthGuard contract: a condensed bullet must not
            # change numbers, add skills/scope, or drift too far from the original.
            reason = self._guard._unsafe_rewrite_reason(  # noqa: SLF001 (intentional reuse)
                text, candidate, resume, self._analysis, skills
            )
            if reason:
                trace.append(CondensedBullet(bullet_id, location, text, text, False,
                                             f"Kept original (TruthGuard: {reason})"))
                return text
            trace.append(CondensedBullet(bullet_id, location, text, candidate, True))
            return candidate

        for experience in display.experience:
            loc = f"{experience.title} — {experience.company}".strip(" —")
            for bullet in experience.bullets:
                bullet.text = process(bullet.id, loc, bullet.text, experience.skills)
        for project in display.projects:
            for bullet in project.bullets:
                bullet.text = process(bullet.id, project.name, bullet.text, project.skills)
        return display, trace


def condense_trace_to_markdown(trace: list[CondensedBullet]) -> str:
    """Audit report: original vs shown text for every condensed bullet."""
    lines = ["# PDF bullet condensing (display only)", ""]
    changed = [item for item in trace if item.changed]
    if not changed:
        lines.append("No bullets were shortened; all fit within the readable budget.")
        return "\n".join(lines) + "\n"
    lines.append(
        "The one-page PDF shows shortened wording for these bullets. The master YAML "
        "and `resume.json` keep the full original text; IDs are unchanged. Every "
        "shortened bullet was validated against its original by the TruthGuard."
    )
    for item in changed:
        lines += [
            "",
            f"### {item.location} — `{item.bullet_id}`",
            "",
            f"- **Original:** {item.original}",
            f"- **Shown:** {item.shown}",
        ]
    kept = [item for item in trace if not item.changed and item.note]
    if kept:
        lines += ["", "## Kept original wording", ""]
        for item in kept:
            lines.append(f"- `{item.bullet_id}`: {item.note}")
    return "\n".join(lines).strip() + "\n"


def omitted_items_to_markdown(items: list[OmittedItem], *, diagnostic: str = "") -> str:
    """A readable report of what the PDF left out and why, for the package."""
    lines = ["# Content omitted from the one-page PDF", ""]
    if diagnostic:
        lines += ["> **Overflow warning:** " + diagnostic, ""]
    if not items:
        lines.append("Nothing was omitted; the full tailored resume fit on one page.")
        return "\n".join(lines) + "\n"
    lines.append(
        "These items are preserved in `resume.json` and `resume.md`; only the "
        "one-page PDF trims them for readability. Facts were never changed."
    )
    bullets = [item for item in items if item.kind == "bullet"]
    skills = [item for item in items if item.kind == "skill"]
    if bullets:
        lines += ["", "## Bullets"]
        for item in bullets:
            lines += ["", f"### {item.location} — `{item.identifier}`", "",
                      f"- **Text:** {item.text}", f"- **Reason:** {item.reason}"]
    if skills:
        lines += ["", "## Skills", "",
                  ", ".join(item.identifier for item in skills),
                  "", f"_{skills[0].reason}_"]
    return "\n".join(lines).strip() + "\n"


def resume_to_markdown(resume: StructuredResume) -> str:
    """Render the resume as Markdown, mirroring the HTML section order."""
    contact_parts = [
        part
        for part in (resume.contact.email, resume.contact.phone, resume.contact.location)
        if part
    ]
    contact_parts.extend(resume.contact.links)
    lines = [f"# {resume.contact.name}"]
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    if resume.summary:
        lines += ["", "## Summary", "", resume.summary]
    if resume.skills:
        lines += ["", "## Skills", "", ", ".join(resume.skills)]
    if resume.experience:
        lines += ["", "## Experience"]
        for job in resume.experience:
            heading = " — ".join(part for part in (job.title, job.company) if part)
            dates = " – ".join(part for part in (job.start_date, job.end_date) if part)
            lines += ["", f"### {heading}" + (f" ({dates})" if dates else "")]
            if job.location:
                lines.append(f"*{job.location}*")
            if job.bullets:
                lines += ["", _bullet_lines([bullet.text for bullet in job.bullets])]
            if job.skills:
                lines += ["", f"*Technologies: {', '.join(job.skills)}*"]
    if resume.projects:
        lines += ["", "## Projects"]
        for project in resume.projects:
            lines += ["", f"### {project.name}"]
            if project.url:
                lines.append(project.url)
            if project.description:
                lines += ["", project.description]
            if project.bullets:
                lines += ["", _bullet_lines([bullet.text for bullet in project.bullets])]
            if project.skills:
                lines += ["", f"*Technologies: {', '.join(project.skills)}*"]
    if resume.education:
        lines += ["", "## Education"]
        for item in resume.education:
            heading = " — ".join(part for part in (item.degree, item.field) if part)
            dates = " – ".join(part for part in (item.start_date, item.end_date) if part)
            lines += ["", f"### {heading or item.institution}" + (f" ({dates})" if dates else "")]
            if item.institution and heading:
                lines.append(item.institution)
            if item.details:
                lines += ["", _bullet_lines(item.details)]
    if resume.certifications:
        entries = [
            " — ".join(part for part in (item.name, item.issuer) if part)
            + (f" ({item.date})" if item.date else "")
            for item in resume.certifications
        ]
        lines += ["", "## Certifications", "", _bullet_lines(entries)]
    if resume.achievements:
        lines += [
            "",
            "## Achievements",
            "",
            _bullet_lines([item.text for item in resume.achievements]),
        ]
    if resume.languages:
        lines += ["", "## Languages", "", ", ".join(resume.languages)]
    for name, values in resume.additional_sections.items():
        if values:
            lines += ["", f"## {name.replace('_', ' ').title()}", "", _bullet_lines(values)]
    return "\n".join(lines).strip() + "\n"


def diff_to_markdown(diff: ResumeDiff, *, job: JobView | None = None) -> str:
    """Render a reviewable before/after report, including every rejected edit."""
    title = "# Resume changes"
    if job:
        title += f" — {job.company} / {job.title}"
    lines = [title, ""]
    lines.append(
        "Facts are never modified. Only wording, ordering, and emphasis may change, "
        "and every rejected edit is listed below."
    )
    if diff.summary_before != diff.summary_after:
        lines += [
            "",
            "## Summary",
            "",
            "**Before**",
            "",
            f"> {diff.summary_before or '(empty)'}",
            "",
            "**After**",
            "",
            f"> {diff.summary_after or '(empty)'}",
        ]
    else:
        lines += ["", "## Summary", "", "Unchanged."]
    if diff.bullet_changes:
        lines += ["", "## Reworded bullets"]
        for change in diff.bullet_changes:
            lines += [
                "",
                f"### `{change.get('bullet_id', 'unknown')}`",
                "",
                f"- **Before:** {change.get('before', '')}",
                f"- **After:** {change.get('after', '')}",
            ]
    else:
        lines += ["", "## Reworded bullets", "", "None."]
    if diff.skill_order_before != diff.skill_order_after:
        lines += [
            "",
            "## Skill order",
            "",
            f"- **Before:** {', '.join(diff.skill_order_before) or '(none)'}",
            f"- **After:** {', '.join(diff.skill_order_after) or '(none)'}",
        ]
    if diff.ordering_changes:
        lines += ["", "## Reordering", "", _bullet_lines(diff.ordering_changes)]
    if diff.rejected_edits:
        lines += ["", "## Rejected edits", ""]
        lines.append("These proposals were blocked because they were not supported by your resume.")
        for rejected in diff.rejected_edits:
            lines += ["", f"### `{rejected.target}`", "", f"- **Reason:** {rejected.reason}"]
            if rejected.proposed_text:
                lines.append(f"- **Proposed:** {rejected.proposed_text}")
    else:
        lines += ["", "## Rejected edits", "", "None."]
    return "\n".join(lines).strip() + "\n"


def ats_report_to_markdown(report: ATSReportData, job: JobView | None = None) -> str:
    """Render the ATS report with component scores, evidence, and gaps."""
    heading = "# ATS compatibility report"
    if job:
        heading += f" — {job.company} / {job.title}"
    lines = [
        heading,
        "",
        f"**Score:** {report.overall_score}/100  ",
        f"**Confidence:** {report.confidence}  ",
        f"**Scorer:** `{report.scoring_version}`",
        "",
        "This is an estimate produced by JobOS, not a reading from an employer's ATS.",
        "",
        "## Component scores",
        "",
        "| Component | Score | Max |",
        "| --- | ---: | ---: |",
    ]
    for component in report.components:
        lines.append(f"| {component.name} | {component.score:g} | {component.maximum:g} |")
    sections: list[tuple[str, list[str]]] = [
        ("Matching skills", report.matching_skills),
        ("Missing skills", report.missing_skills),
        ("Strong areas", report.strong_areas),
        ("Weak areas", report.weak_areas),
        ("Keywords found", report.keywords_found),
        ("Keywords missing", report.keywords_missing),
        ("Suggestions", report.suggestions),
    ]
    for name, values in sections:
        lines += ["", f"## {name}", "", _bullet_lines(values) if values else "None."]
    lines += ["", "## Evidence by component"]
    for component in report.components:
        lines += ["", f"### {component.name}"]
        lines.append(
            f"- **Evidence:** {', '.join(component.evidence) if component.evidence else 'none'}"
        )
        lines.append(f"- **Gaps:** {', '.join(component.gaps) if component.gaps else 'none'}")
    return "\n".join(lines).strip() + "\n"


class ResumeRenderer:
    """Turn a structured resume into HTML and a text-extractable PDF."""

    def __init__(self, theme: str = "classic") -> None:
        if theme not in THEMES:
            available = ", ".join(sorted(THEMES))
            raise RenderError(f"unknown theme {theme!r}; available themes: {available}")
        self.theme = theme
        self._environment = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        # Deterministic, safe render-time helpers. emphasize returns Markup (already
        # escaped) so autoescape leaves it intact; safe_link validates the scheme.
        self._environment.filters["emphasize"] = _emphasize_bullet
        self._environment.filters["safe_link"] = _safe_link

    @property
    def applies_budget(self) -> bool:
        """Only the readable one-pager trims display content; others render as-is."""
        return THEMES[self.theme].get("rich") == "true"

    def budgeted(
        self,
        resume: StructuredResume,
        budget: RenderBudget | None = None,
        *,
        relevant_skills: set[str] | None = None,
    ) -> tuple[StructuredResume, list[OmittedItem]]:
        """Apply the render-time display budget for themes that use it.

        Returns the (possibly trimmed) deep copy to render and the list of omitted
        items. ``relevant_skills`` (casefolded) keeps job-relevant skills first when
        trimming. For non-budget themes the resume is returned unchanged with no
        omissions, so legacy behaviour is identical.
        """
        if not self.applies_budget:
            return resume, []
        return (budget or RenderBudget()).apply(resume, relevant_skills=relevant_skills)

    async def render_one_page(
        self,
        resume: StructuredResume,
        *,
        condense: CondenseFn | None = None,
        relevant_skills: set[str] | None = None,
    ) -> OnePageResult:
        """Render a readable one-page PDF, condensing and trimming display content.

        The optional ``condense`` callable (supplied by the service, which owns the
        TruthGuard and job analysis) first shortens over-long bullets on a display
        copy — validated for truth, master YAML untouched. Then progressively tighter
        display budgets are tried, each letting ``render_pdf`` scale down only to the
        theme's readable floor. It returns the first combination that fits one page.
        If nothing fits at a readable size it returns the tightest attempt plus a
        diagnostic naming what still overflows — never a silent unreadable or
        multi-page "success".

        For non-budget themes this is a thin wrapper over ``render_pdf`` with no
        condensing or trimming, preserving existing behaviour.
        """
        if not self.applies_budget:
            html = self.render_html(resume)
            return OnePageResult(await self.render_pdf(html), html, [], [], "")

        # Condense first (display-only, truth-validated), then budget the result.
        condensed_resume = resume
        condense_trace: list[CondensedBullet] = []
        if condense is not None:
            condensed_resume, condense_trace = condense(resume)

        # Tiers run MOST GENEROUS first, stepping down only as needed, so the page is
        # filled rather than trimmed-then-left-with-whitespace. The first tier shows
        # essentially everything (a large project keeps most bullets); tighter tiers
        # kick in only when content genuinely overflows one readable page. This is why
        # freeing space elsewhere (e.g. compact achievements/certifications) directly
        # buys back experience bullets here.
        tiers = [
            RenderBudget(recent_role_bullets=8, older_role_bullets=8, project_bullets=8, max_skills=60),
            RenderBudget(recent_role_bullets=6, older_role_bullets=6, project_bullets=6, max_skills=50),
            RenderBudget(recent_role_bullets=6, older_role_bullets=5, project_bullets=5, max_skills=45),
            RenderBudget(recent_role_bullets=5, older_role_bullets=4, project_bullets=5, max_skills=40),
            RenderBudget(recent_role_bullets=5, older_role_bullets=4, project_bullets=4, max_skills=32),
            RenderBudget(recent_role_bullets=4, older_role_bullets=4, project_bullets=4, max_skills=28),
            RenderBudget(recent_role_bullets=4, older_role_bullets=3, project_bullets=3, max_skills=24),
        ]
        last = OnePageResult(b"", "", [], condense_trace, "")
        for budget in tiers:
            display, omitted = self.budgeted(
                condensed_resume, budget, relevant_skills=relevant_skills
            )
            html = self.render_html(display)
            pdf, pages = await self._render_counting_pages(html)
            last = OnePageResult(pdf, html, omitted, condense_trace, "")
            if pages == 1:
                return last
        diagnostic = (
            "Content still exceeds one A4 page at the minimum readable size "
            f"({THEMES[self.theme]['base_font_size']} x {THEMES[self.theme].get('min_scale', '0.92')} "
            "scale), even after condensing bullets and trimming to the tightest "
            "readable budget. Shorten the summary or the longest remaining bullets in "
            "the resume to fit one page without making the text smaller."
        )
        return OnePageResult(last.pdf, last.html, last.omitted, condense_trace, diagnostic)

    async def _render_counting_pages(self, html: str) -> tuple[bytes, int]:
        """Render the PDF (honoring the theme scale floor) and report its page count."""
        pdf = await self.render_pdf(html)
        return pdf, len(PdfReader(io.BytesIO(pdf)).pages)

    def render_html(self, resume: StructuredResume) -> str:
        template = self._environment.get_template("resume.html.j2")
        options = {key: value for key, value in THEMES[self.theme].items() if key != "rich"}
        return template.render(
            resume=resume,
            rich=THEMES[self.theme].get("rich") == "true",
            **options,
        )

    async def render_pdf(self, html: str) -> bytes:
        """Print the resume with Chromium, then verify the text actually extracts."""
        options = THEMES[self.theme]
        fit_one_page = options.get("single_page") == "true"
        min_scale = float(options.get("min_scale", "0.8"))
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content(html, wait_until="load")
                    scale = 1.0
                    if fit_one_page:
                        scale = await self._one_page_scale(page, min_scale=min_scale)
                    pdf_bytes = await page.pdf(
                        format=options["page_size"],
                        print_background=True,
                        prefer_css_page_size=True,
                        scale=scale,
                    )
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise RenderError(
                "Chromium could not render the PDF. Install it once with "
                f"`uv run playwright install chromium`. Original error: {exc}"
            ) from exc
        verify_pdf(pdf_bytes)
        return pdf_bytes

    @staticmethod
    async def _one_page_scale(page: object, *, min_scale: float = 0.8) -> float:
        """Find the largest print scale that keeps the resume on a single page.

        Chromium's print pagination breaks earlier than any DOM height measurement
        predicts, so rather than estimate, this renders at decreasing scales and
        returns the first that produces exactly one page. It never goes below
        ``min_scale``: past that the body font is too small to read, and trimming
        display content (the render-time budget) is the honest fix instead. The
        readable theme sets this to 0.92 so the effective body never drops below
        ~8.5pt; the legacy ``onepager`` keeps the old 0.8 floor for backward
        compatibility.
        """
        steps = [1.0, 0.97, 0.95, 0.93, 0.92, 0.9, 0.87, 0.84, 0.8]
        for scale in steps:
            if scale < min_scale:
                break
            pdf = await page.pdf(  # type: ignore[attr-defined]
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                scale=scale,
            )
            if len(PdfReader(io.BytesIO(pdf)).pages) == 1:
                return scale
        return min_scale


def verify_pdf(pdf_bytes: bytes, *, expected_text: str = "") -> str:
    """Fail loudly if a generated PDF has no selectable text an ATS could read."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise RenderError("generated file is not a valid PDF")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise RenderError(f"generated PDF could not be parsed: {exc}") from exc
    if reader.is_encrypted:
        raise RenderError("generated PDF is encrypted and would not parse in an ATS")
    if len(text.strip()) < 100:
        raise RenderError(
            "generated PDF contains little or no extractable text, so an ATS could not read it"
        )
    if expected_text and expected_text.casefold() not in text.casefold():
        raise RenderError(f"generated PDF is missing expected text: {expected_text!r}")
    return text


def next_version_directory(base: Path) -> tuple[Path, int]:
    """Pick the next vNNN directory so an existing package is never overwritten."""
    versions = [
        int(path.name[1:])
        for path in base.glob("v*")
        if path.is_dir() and path.name[1:].isdigit()
    ]
    version = max(versions, default=0) + 1
    return base / f"v{version:03d}", version


class PackageWriter:
    """Write one immutable application package per generation."""

    def __init__(self, output_dir: Path, renderer: ResumeRenderer | None = None) -> None:
        self.output_dir = output_dir
        self.renderer = renderer or ResumeRenderer()

    async def write(
        self,
        *,
        job: JobView,
        resume: StructuredResume,
        diff: ResumeDiff,
        report: ATSReportData,
        cover_letter: CoverLetterDraft | None = None,
        condense: CondenseFn | None = None,
        relevant_skills: set[str] | None = None,
    ) -> RenderedPackage:
        base = self.output_dir / safe_path_name(job.company) / safe_path_name(job.title, "role")
        base.mkdir(parents=True, exist_ok=True)
        target, version = next_version_directory(base)
        staging = base / f".staging-v{version:03d}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        try:
            # The one-page PDF is rendered from a display-budgeted copy so the
            # readable theme fits a page by trimming the lowest-priority bullets and
            # skills rather than shrinking the font below a readable size. resume.json
            # and resume.md keep the FULL resume, so nothing is lost — only the
            # one-page PDF is trimmed. HTML mirrors what the PDF shows.
            result = await self.renderer.render_one_page(
                resume, condense=condense, relevant_skills=relevant_skills
            )
            verify_pdf(result.pdf, expected_text=resume.contact.name)

            payload: dict[str, str | bytes] = {
                "resume.html": result.html,
                "resume.md": resume_to_markdown(resume),
                "resume.json": resume.model_dump_json(indent=2) + "\n",
                "resume.diff.md": diff_to_markdown(diff, job=job),
                "ats_report.md": ats_report_to_markdown(report, job),
                "job.json": json.dumps(job.model_dump(mode="json"), indent=2) + "\n",
                "resume.pdf": result.pdf,
            }
            if result.omitted or result.diagnostic:
                payload["omitted.md"] = omitted_items_to_markdown(
                    result.omitted, diagnostic=result.diagnostic
                )
            if any(item.changed for item in result.condensed):
                payload["condensed.md"] = condense_trace_to_markdown(result.condensed)
            if cover_letter:
                payload["cover_letter.md"] = cover_letter.markdown.rstrip() + "\n"

            files: dict[str, Path] = {}
            for name, content in payload.items():
                path = staging / name
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")
                files[name] = target / name
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        _update_latest_pointer(base, target)
        return RenderedPackage(directory=target, version=version, files=files)


def _update_latest_pointer(base: Path, target: Path) -> None:
    """Point `latest` at the newest package, falling back to a text file."""
    link = base / "latest"
    try:
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                return
            link.unlink()
        link.symlink_to(target.name)
    except OSError:
        (base / "LATEST.txt").write_text(target.name + "\n", encoding="utf-8")


def open_apply_page(url: str) -> str:
    """Open the official application page in the default browser.

    JobOS never submits the application, logs in, or bypasses bot protection.
    """
    validated = validate_web_url(url)
    if not webbrowser.open(validated, new=2):
        raise RenderError(f"could not open a browser for {validated}")
    return validated
