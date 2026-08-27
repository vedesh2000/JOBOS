"""Safe local resume extraction and conservative structuring."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
from pathlib import Path
from typing import Protocol

from docx import Document
from pypdf import PdfReader

from jobos.config import AppConfig
from jobos.database import Database
from jobos.models import (
    Certification,
    Contact,
    Education,
    Experience,
    Project,
    ResumeBullet,
    ResumeImportResult,
    StructuredResume,
)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_RESUME_CHARS = 150_000
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".markdown"}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)])\s+")
DATE_RANGE_RE = re.compile(
    r"(?P<start>(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)?"
    r"\s*\d{4})\s*(?:-|–|—|to)\s*(?P<end>Present|Current|(?:Jan(?:uary)?|"
    r"Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)?\s*\d{4})",
    re.IGNORECASE,
)

SECTION_ALIASES = {
    "summary": {"summary", "professional summary", "profile", "objective", "about"},
    "skills": {"skills", "technical skills", "core skills", "technologies", "expertise"},
    "experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
    },
    "projects": {"projects", "selected projects", "personal projects"},
    "education": {"education", "academic background", "qualifications"},
    "certifications": {"certifications", "certificates", "licenses"},
    "achievements": {"achievements", "awards", "honors"},
    "languages": {"languages"},
}


class ResumeStructurer(Protocol):
    async def structure_resume(self, source_text: str) -> StructuredResume:
        """Extract the source into the schema without rewriting facts."""


class ResumeParseError(ValueError):
    """Raised when a resume cannot be extracted safely."""


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) > MAX_RESUME_CHARS:
        raise ResumeParseError(f"resume text exceeds {MAX_RESUME_CHARS:,} characters")
    if len(text) < 20:
        raise ResumeParseError("resume contains too little extractable text")
    return text


def extract_resume_text(filename: str, content: bytes) -> str:
    """Extract text from a bounded supported upload without executing embedded content."""
    if not content:
        raise ResumeParseError("resume file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ResumeParseError(f"resume exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024} MB limit")
    suffix = Path(filename).suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ResumeParseError(f"unsupported resume type {suffix!r}; expected {supported}")
    try:
        if suffix == ".pdf":
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ResumeParseError("encrypted PDFs are not supported")
            if len(reader.pages) > 25:
                raise ResumeParseError("resume PDF exceeds 25 pages")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif suffix == ".docx":
            document = Document(io.BytesIO(content))
            lines = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(lines)
        else:
            text = content.decode("utf-8-sig")
    except ResumeParseError:
        raise
    except Exception as exc:
        raise ResumeParseError(f"could not read {suffix} resume: {exc}") from exc
    return _clean_text(text)


def _heading_name(line: str) -> str | None:
    match = HEADING_RE.match(line)
    candidate = match.group(1) if match else line.strip().rstrip(":")
    normalized = re.sub(r"[^a-z ]", "", candidate.casefold()).strip()
    for section, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return section
    return None


def _strip_heading_marker(line: str) -> str:
    """Drop leading Markdown heading syntax so content flows through clean.

    A line such as ``# Jane Doe`` is a heading that is not a known section, so its
    text must still reach the parsers without the ``#`` markers attached.
    """
    match = HEADING_RE.match(line)
    return match.group(1).strip() if match else line


def _split_sections(text: str) -> tuple[list[str], dict[str, list[str]]]:
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _heading_name(line)
        if heading:
            current = heading
            sections.setdefault(current, [])
        elif current:
            sections[current].append(_strip_heading_marker(line))
        else:
            preamble.append(_strip_heading_marker(line))
    return preamble, sections


def _strip_bullet(line: str) -> str:
    return BULLET_RE.sub("", line).strip()


def _parse_contact(lines: list[str]) -> Contact:
    joined = "\n".join(lines[:10])
    email_match = EMAIL_RE.search(joined)
    phone_match = PHONE_RE.search(joined)
    links = [match.rstrip(".,;") for match in URL_RE.findall(joined)]
    name = ""
    for line in lines[:5]:
        candidate = EMAIL_RE.sub("", PHONE_RE.sub("", URL_RE.sub("", line))).strip(" |,-")
        if candidate and len(candidate) <= 100 and not _heading_name(candidate):
            name = candidate
            break
    if not name:
        raise ResumeParseError("could not identify a name in the resume header")
    location = ""
    for line in lines[1:8]:
        residue = URL_RE.sub("", EMAIL_RE.sub("", PHONE_RE.sub("", line)))
        # Contact details are often pipe-separated on one line; keep the part that
        # is neither email, phone, nor link and does not repeat the name.
        candidates = [
            part.strip(" |,-")
            for part in re.split(r"[|·•]", residue)
            if part.strip(" |,-")
        ]
        for candidate in candidates:
            if candidate and candidate != name and len(candidate) <= 120:
                location = candidate
                break
        if location:
            break
    return Contact(
        name=name,
        email=email_match.group(0) if email_match else "",
        phone=phone_match.group(0) if phone_match else "",
        location=location,
        links=links,
    )


def _parse_skill_lines(lines: list[str]) -> list[str]:
    skills: list[str] = []
    for line in lines:
        clean = _strip_bullet(line)
        if ":" in clean and len(clean.split(":", 1)[0]) < 35:
            clean = clean.split(":", 1)[1]
        skills.extend(part.strip() for part in re.split(r"[,|;•]", clean))
    return [value for value in skills if value and len(value) <= 80]


def _header_parts(line: str) -> tuple[str, str]:
    for separator in (" — ", " – ", " | ", " at ", " @ "):
        if separator in line:
            left, right = (part.strip() for part in line.split(separator, 1))
            return left, right
    return line.strip(), ""


def _parse_experience(lines: list[str]) -> list[Experience]:
    result: list[Experience] = []
    current_header = ""
    current_dates = ""
    bullets: list[str] = []

    def flush() -> None:
        nonlocal current_header, current_dates, bullets
        if not current_header:
            return
        company, title = _header_parts(current_header)
        date_match = DATE_RANGE_RE.search(current_dates)
        item_index = len(result)
        result.append(
            Experience(
                id=_stable_id("exp", company, title, current_dates, str(item_index)),
                company=company,
                title=title,
                start_date=date_match.group("start").strip() if date_match else "",
                end_date=date_match.group("end").strip() if date_match else "",
                location=(
                    current_dates[date_match.end() :].strip(" |,;—–-")
                    if date_match
                    else current_dates
                ),
                bullets=[
                    ResumeBullet(
                        id=_stable_id("bullet", company, text, str(index)), text=text
                    )
                    for index, text in enumerate(bullets)
                ],
            )
        )
        current_header = ""
        current_dates = ""
        bullets = []

    for line in lines:
        clean = _strip_bullet(line)
        if not clean:
            continue
        if BULLET_RE.match(line):
            bullets.append(clean)
        elif DATE_RANGE_RE.search(clean) and current_header and not current_dates:
            current_dates = clean
        else:
            flush()
            current_header = clean
    flush()
    return result


def _parse_projects(lines: list[str]) -> list[Project]:
    result: list[Project] = []
    current_name = ""
    bullets: list[str] = []

    def flush() -> None:
        nonlocal current_name, bullets
        if not current_name:
            return
        index = len(result)
        result.append(
            Project(
                id=_stable_id("project", current_name, str(index)),
                name=current_name,
                bullets=[
                    ResumeBullet(
                        id=_stable_id("project_bullet", current_name, text, str(position)),
                        text=text,
                    )
                    for position, text in enumerate(bullets)
                ],
            )
        )
        current_name = ""
        bullets = []

    for line in lines:
        clean = _strip_bullet(line)
        if BULLET_RE.match(line) and current_name:
            bullets.append(clean)
        else:
            flush()
            current_name = clean
    flush()
    return result


def _parse_education(lines: list[str]) -> list[Education]:
    result: list[Education] = []
    for index in range(0, len(lines), 2):
        institution = _strip_bullet(lines[index])
        detail = _strip_bullet(lines[index + 1]) if index + 1 < len(lines) else ""
        degree, field = _header_parts(detail)
        result.append(
            Education(
                id=_stable_id("education", institution, detail, str(index)),
                institution=institution,
                degree=degree,
                field=field,
            )
        )
    return result


def structure_resume_offline(text: str) -> StructuredResume:
    """Conservatively map recognizable sections; preserve unknown content separately."""
    preamble, sections = _split_sections(text)
    # Fall back to raw leading lines only for header-less resumes. When recognized
    # sections exist but no preamble does, there is no name block to read.
    header_lines = preamble or ([] if sections else text.splitlines()[:10])
    contact = _parse_contact(header_lines)
    summary = " ".join(_strip_bullet(line) for line in sections.get("summary", []))
    achievements = [
        ResumeBullet(id=_stable_id("achievement", line, str(index)), text=_strip_bullet(line))
        for index, line in enumerate(sections.get("achievements", []))
        if _strip_bullet(line)
    ]
    certifications = [
        Certification(id=_stable_id("cert", line, str(index)), name=_strip_bullet(line))
        for index, line in enumerate(sections.get("certifications", []))
        if _strip_bullet(line)
    ]
    known = set(SECTION_ALIASES)
    return StructuredResume(
        contact=contact,
        summary=summary,
        skills=_parse_skill_lines(sections.get("skills", [])),
        experience=_parse_experience(sections.get("experience", [])),
        projects=_parse_projects(sections.get("projects", [])),
        education=_parse_education(sections.get("education", [])),
        certifications=certifications,
        achievements=achievements,
        languages=_parse_skill_lines(sections.get("languages", [])),
        additional_sections={
            name: [_strip_bullet(line) for line in values]
            for name, values in sections.items()
            if name not in known
        },
    )


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#.%/-]+", " ", value.casefold())).strip()


def unsupported_extractions(resume: StructuredResume, source_text: str) -> list[str]:
    """Find important extracted values that are not present in the uploaded source."""
    source = _normalized(source_text)
    checks: list[tuple[str, str]] = [("name", resume.contact.name)]
    checks.extend(("company", item.company) for item in resume.experience)
    checks.extend(("role", item.title) for item in resume.experience)
    checks.extend(
        ("experience bullet", bullet.text)
        for experience in resume.experience
        for bullet in experience.bullets
    )
    checks.extend(("project", project.name) for project in resume.projects)
    checks.extend(
        ("project bullet", bullet.text)
        for project in resume.projects
        for bullet in project.bullets
    )
    checks.extend(("institution", education.institution) for education in resume.education)
    checks.extend(("degree", education.degree) for education in resume.education)
    checks.extend(("skill", skill) for skill in resume.skills)
    unsupported: list[str] = []
    for kind, value in checks:
        normalized = _normalized(value)
        if normalized and normalized not in source:
            unsupported.append(f"{kind}: {value}")
    return unsupported


class ResumeImporter:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        structurer: ResumeStructurer | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.structurer = structurer

    async def import_bytes(self, filename: str, content: bytes) -> ResumeImportResult:
        source_text = extract_resume_text(filename, content)
        warnings: list[str] = []
        resume: StructuredResume | None = None
        if self.structurer:
            try:
                candidate = await self.structurer.structure_resume(source_text)
                unsupported = unsupported_extractions(candidate, source_text)
                if unsupported:
                    preview = "; ".join(unsupported[:5])
                    raise ResumeParseError(
                        f"AI extraction introduced unsupported values: {preview}"
                    )
                resume = candidate
            except Exception as exc:
                warnings.append(f"AI structuring was not used: {exc}")
        if resume is None:
            resume = structure_resume_offline(source_text)
            warnings.append(
                "Used the conservative offline parser. "
                "Review the structured resume before optimizing."
            )

        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(filename).suffix.casefold()
        destination = self.config.uploads_dir / f"{digest}{suffix}"
        if not destination.exists():
            with io.BytesIO(content) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
        content_hash = hashlib.sha256(
            resume.model_dump_json(exclude_none=False).encode("utf-8")
        ).hexdigest()
        stored = self.database.add_resume(
            resume,
            source_name=Path(filename).name,
            source_format=suffix.lstrip("."),
            original_path=str(destination),
            source_text=source_text,
            content_hash=content_hash,
            is_master=True,
        )
        return ResumeImportResult(
            version_id=stored.id,
            resume=resume,
            source_text=source_text,
            warnings=warnings,
        )
