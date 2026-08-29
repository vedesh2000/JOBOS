"""Render resumes to Markdown, HTML, and PDF, and write versioned output packages."""

from __future__ import annotations

import io
import json
import re
import shutil
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pypdf import PdfReader

from jobos.models import (
    ATSReportData,
    CoverLetterDraft,
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

    def render_html(self, resume: StructuredResume) -> str:
        template = self._environment.get_template("resume.html.j2")
        return template.render(resume=resume, **THEMES[self.theme])

    async def render_pdf(self, html: str) -> bytes:
        """Print the resume with Chromium, then verify the text actually extracts."""
        options = THEMES[self.theme]
        fit_one_page = options.get("single_page") == "true"
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content(html, wait_until="load")
                    scale = 1.0
                    if fit_one_page:
                        scale = await self._one_page_scale(page)
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
    async def _one_page_scale(page: object) -> float:
        """Find the largest print scale that keeps the resume on a single page.

        Chromium's print pagination breaks earlier than any DOM height measurement
        predicts, so rather than estimate, this renders at decreasing scales and
        returns the first that produces exactly one page. It never goes below 0.8,
        because past that the 8pt base font is too small to read and trimming content
        is the honest fix instead.
        """
        for scale in (1.0, 0.95, 0.92, 0.9, 0.87, 0.84, 0.8):
            pdf = await page.pdf(  # type: ignore[attr-defined]
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                scale=scale,
            )
            if len(PdfReader(io.BytesIO(pdf)).pages) == 1:
                return scale
        return 0.8


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
    ) -> RenderedPackage:
        base = self.output_dir / safe_path_name(job.company) / safe_path_name(job.title, "role")
        base.mkdir(parents=True, exist_ok=True)
        target, version = next_version_directory(base)
        staging = base / f".staging-v{version:03d}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        try:
            html = self.renderer.render_html(resume)
            pdf_bytes = await self.renderer.render_pdf(html)
            verify_pdf(pdf_bytes, expected_text=resume.contact.name)

            payload: dict[str, str | bytes] = {
                "resume.html": html,
                "resume.md": resume_to_markdown(resume),
                "resume.json": resume.model_dump_json(indent=2) + "\n",
                "resume.diff.md": diff_to_markdown(diff, job=job),
                "ats_report.md": ats_report_to_markdown(report, job),
                "job.json": json.dumps(job.model_dump(mode="json"), indent=2) + "\n",
                "resume.pdf": pdf_bytes,
            }
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
