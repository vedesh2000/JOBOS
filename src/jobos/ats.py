"""Deterministic, explainable ATS compatibility scoring."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final

from rapidfuzz import fuzz

from jobos.models import ATSReportData, JobAnalysis, ScoreComponent, StructuredResume

SCORING_VERSION: Final = "jobos-ats-v1"

SKILL_ALIASES: dict[str, set[str]] = {
    "amazon web services": {"aws", "amazon web services"},
    "google cloud platform": {"gcp", "google cloud", "google cloud platform"},
    "microsoft azure": {"azure", "microsoft azure"},
    "javascript": {"javascript", "js", "ecmascript"},
    "typescript": {"typescript", "ts"},
    "node.js": {"node", "nodejs", "node.js"},
    "react": {"react", "react.js", "reactjs"},
    "vue.js": {"vue", "vue.js", "vuejs"},
    "next.js": {"next", "next.js", "nextjs"},
    "postgresql": {"postgres", "postgresql"},
    "mongodb": {"mongo", "mongodb"},
    "kubernetes": {"k8s", "kubernetes"},
    "continuous integration": {"ci/cd", "cicd", "continuous integration"},
    "machine learning": {"machine learning", "ml"},
    "large language models": {"large language model", "large language models", "llm", "llms"},
    "retrieval augmented generation": {"rag", "retrieval augmented generation"},
    "rest api": {"rest", "rest api", "restful", "restful api"},
    "c sharp": {"c#", "c sharp"},
    "c plus plus": {"c++", "c plus plus"},
}

KNOWN_SKILLS: set[str] = {
    "airflow",
    "angular",
    "ansible",
    "apache spark",
    "azure devops",
    "beautifulsoup",
    "celery",
    "css",
    "datadog",
    "dbt",
    "django",
    "docker",
    "dynamodb",
    "elasticsearch",
    "fastapi",
    "flask",
    "git",
    "github actions",
    "gitlab ci",
    "go",
    "grafana",
    "graphql",
    "grpc",
    "html",
    "java",
    "jenkins",
    "jest",
    "jinja2",
    "kafka",
    "kotlin",
    "langchain",
    "laravel",
    "linux",
    "llamaindex",
    "microservices",
    "mysql",
    "next.js",
    "nginx",
    "nosql",
    "numpy",
    "ollama",
    "openai",
    "opensearch",
    "pandas",
    "php",
    "playwright",
    "postgresql",
    "prometheus",
    "pytest",
    "python",
    "pytorch",
    "rabbitmq",
    "react",
    "redis",
    "ruby",
    "rust",
    "scikit-learn",
    "selenium",
    "snowflake",
    "spring boot",
    "sql",
    "sqlalchemy",
    "swift",
    "tailwind",
    "tensorflow",
    "terraform",
    "typescript",
    "vue.js",
    *SKILL_ALIASES.keys(),
}

STOP_WORDS = {
    "and",
    "are",
    "but",
    "for",
    "from",
    "have",
    "into",
    "our",
    "that",
    "the",
    "their",
    "this",
    "using",
    "with",
    "will",
    "you",
    "your",
    "work",
    "role",
    "team",
    "years",
    "experience",
    "responsible",
}


def normalize_text(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", value).split())


def canonical_skill(value: str) -> str:
    normalized = normalize_text(value)
    for canonical, aliases in SKILL_ALIASES.items():
        if normalized in {normalize_text(alias) for alias in aliases}:
            return canonical
    return normalized


def skill_in_text(skill: str, text: str) -> bool:
    normalized_text = f" {normalize_text(text)} "
    canonical = canonical_skill(skill)
    variants = SKILL_ALIASES.get(canonical, {skill, canonical})
    return any(f" {normalize_text(variant)} " in normalized_text for variant in variants)


def extract_known_skills(text: str) -> list[str]:
    found = [skill for skill in KNOWN_SKILLS if skill_in_text(skill, text)]
    return sorted({canonical_skill(skill) for skill in found})


def mentioned_skills(terms: Iterable[str], text: str) -> set[str]:
    """Return the canonical form of every term the text mentions.

    ``skill_in_text`` re-normalizes its text argument on each call, which is fine for
    a single lookup but wasteful when thousands of postings are tested against the
    same term list, so the text is normalized once here.

    Terms outside the known vocabulary still work: they fall back to matching their
    own normalized form, so a free-form term such as "full stack" is usable without
    being added to ``KNOWN_SKILLS``. Matching stays token-delimited, which is why
    "java" does not match "JavaScript".
    """
    padded = f" {normalize_text(text)} "
    found: set[str] = set()
    for term in terms:
        if not term.strip():
            continue
        canonical = canonical_skill(term)
        variants = SKILL_ALIASES.get(canonical, {term, canonical})
        if any(f" {normalize_text(variant)} " in padded for variant in variants):
            found.add(canonical)
    return found


def _resume_context(resume: StructuredResume) -> tuple[str, str, str]:
    explicit = " ".join(resume.skills)
    evidence_parts: list[str] = []
    for experience in resume.experience:
        evidence_parts.extend(experience.skills)
        evidence_parts.extend(bullet.text for bullet in experience.bullets)
    for project in resume.projects:
        evidence_parts.extend(project.skills)
        evidence_parts.extend([project.description, *(bullet.text for bullet in project.bullets)])
    evidence = " ".join(evidence_parts)
    return explicit, evidence, resume.searchable_text()


def matched_skills(
    resume: StructuredResume, skills: list[str]
) -> tuple[list[str], list[str], list[str]]:
    explicit, evidence, all_text = _resume_context(resume)
    matched: list[str] = []
    missing: list[str] = []
    contextual: list[str] = []
    for skill in skills:
        if skill_in_text(skill, all_text):
            matched.append(skill)
            if skill_in_text(skill, evidence):
                contextual.append(skill)
        else:
            missing.append(skill)
    return matched, missing, contextual


def _component(
    name: str,
    score: float,
    maximum: float,
    evidence: list[str] | None = None,
    gaps: list[str] | None = None,
) -> ScoreComponent:
    return ScoreComponent(
        name=name,
        score=round(max(0.0, min(score, maximum)), 2),
        maximum=maximum,
        evidence=evidence or [],
        gaps=gaps or [],
    )


def _coverage_component(
    name: str,
    maximum: float,
    wanted: list[str],
    matched: list[str],
    missing: list[str],
) -> ScoreComponent:
    score = maximum if not wanted else maximum * len(matched) / len(wanted)
    return _component(name, score, maximum, matched, missing)


def _content_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_text(value).split()
        if len(token) > 2 and token not in STOP_WORDS
    }


def _responsibility_component(
    resume: StructuredResume, responsibilities: list[str]
) -> ScoreComponent:
    maximum = 15.0
    bullets = [
        bullet.text
        for experience in resume.experience
        for bullet in experience.bullets
    ] + [bullet.text for project in resume.projects for bullet in project.bullets]
    if not responsibilities:
        return _component("Responsibility alignment", maximum, maximum, ["No explicit requirement"])
    if not bullets:
        return _component(
            "Responsibility alignment", 0, maximum, gaps=["No experience/project bullets to compare"]
        )
    alignments: list[float] = []
    evidence: list[str] = []
    gaps: list[str] = []
    for responsibility in responsibilities[:20]:
        best = max(fuzz.token_set_ratio(responsibility, bullet) for bullet in bullets) / 100
        token_overlap = max(
            (
                len(_content_tokens(responsibility) & _content_tokens(bullet))
                / max(1, len(_content_tokens(responsibility)))
            )
            for bullet in bullets
        )
        alignment = max(best * 0.7 + token_overlap * 0.3, 0)
        alignments.append(alignment)
        if alignment >= 0.55:
            evidence.append(responsibility)
        else:
            gaps.append(responsibility)
    score = maximum * sum(alignments) / len(alignments)
    return _component("Responsibility alignment", score, maximum, evidence[:8], gaps[:8])


def _parse_month(value: str, *, end: bool) -> int | None:
    lowered = value.casefold()
    now = datetime.now(UTC)
    if "present" in lowered or "current" in lowered:
        return now.year * 12 + now.month - 1
    year_match = re.search(r"\b(19|20)\d{2}\b", value)
    if not year_match:
        return None
    year = int(year_match.group(0))
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    month = 12 if end else 1
    for name, number in months.items():
        if name in lowered:
            month = number
            break
    return year * 12 + month - 1


def experience_years(resume: StructuredResume) -> float | None:
    months: set[int] = set()
    for experience in resume.experience:
        start = _parse_month(experience.start_date, end=False)
        end = _parse_month(experience.end_date, end=True)
        if start is None or end is None or end < start or end - start > 600:
            continue
        months.update(range(start, end + 1))
    return round(len(months) / 12, 1) if months else None


def _experience_component(resume: StructuredResume, analysis: JobAnalysis) -> ScoreComponent:
    maximum = 15.0
    years = experience_years(resume)
    evidence: list[str] = []
    gaps: list[str] = []
    years_ratio = 1.0
    if analysis.minimum_years is not None:
        if years is None:
            years_ratio = 0.35
            gaps.append("Resume dates were not structured enough to verify years of experience")
        else:
            years_ratio = min(years / max(analysis.minimum_years, 0.5), 1.0)
            evidence.append(f"{years:g} structured years vs {analysis.minimum_years:g} requested")
            if years < analysis.minimum_years:
                gaps.append(f"Job asks for {analysis.minimum_years:g}+ years")
    elif years is not None:
        evidence.append(f"{years:g} years found in structured dates")

    seniority_ratio = 1.0
    seniority = normalize_text(analysis.seniority)
    resume_titles = " ".join(item.title for item in resume.experience)
    if seniority:
        senior_terms = {"senior", "lead", "principal", "staff", "manager", "director"}
        requested = senior_terms & set(seniority.split())
        if requested and not any(skill_in_text(term, resume_titles) for term in requested):
            seniority_ratio = 0.55
            gaps.append(f"No explicit {analysis.seniority} title evidence")
        else:
            evidence.append(f"Seniority evidence aligns with {analysis.seniority}")

    education_ratio = 1.0
    if analysis.education_requirements:
        education_text = " ".join(
            f"{item.degree} {item.field} {item.institution}" for item in resume.education
        )
        matched_education = [
            requirement
            for requirement in analysis.education_requirements
            if fuzz.token_set_ratio(requirement, education_text) >= 45
        ]
        education_ratio = len(matched_education) / len(analysis.education_requirements)
        evidence.extend(matched_education)
        gaps.extend(
            requirement
            for requirement in analysis.education_requirements
            if requirement not in matched_education
        )
    score = maximum * (years_ratio * 0.55 + seniority_ratio * 0.3 + education_ratio * 0.15)
    return _component("Experience, seniority and education", score, maximum, evidence, gaps)


def _role_component(resume: StructuredResume, job_title: str) -> ScoreComponent:
    maximum = 10.0
    candidates = [item.title for item in resume.experience if item.title]
    if resume.summary:
        candidates.append(resume.summary)
    if not candidates:
        return _component("Role/title alignment", 0, maximum, gaps=["No role titles found"])
    scored = sorted(
        ((fuzz.token_set_ratio(job_title, value), value) for value in candidates), reverse=True
    )
    ratio, evidence = scored[0]
    gaps = [] if ratio >= 50 else [f"Weak title alignment with {job_title}"]
    return _component("Role/title alignment", maximum * ratio / 100, maximum, [evidence], gaps)


def _parseability_component(resume: StructuredResume) -> ScoreComponent:
    maximum = 10.0
    checks = [
        (bool(resume.contact.name), "Name is structured", "Missing name"),
        (bool(resume.contact.email), "Email is structured", "Missing email"),
        (bool(resume.skills), "Skills section is structured", "Missing skills section"),
        (bool(resume.experience), "Experience section is structured", "Missing experience section"),
        (
            any(item.bullets for item in resume.experience),
            "Experience bullets are structured",
            "No experience bullets",
        ),
        (
            any(item.start_date or item.end_date for item in resume.experience),
            "Employment dates are structured",
            "Employment dates were not parsed",
        ),
        (bool(resume.education), "Education section is structured", "Missing education section"),
        (len(resume.searchable_text()) >= 500, "Resume has useful detail", "Resume is unusually short"),
    ]
    passed = [success for success, _, _ in checks if success]
    evidence = [message for success, message, _ in checks if success]
    gaps = [failure for success, _, failure in checks if not success]
    return _component("Parseability and structure", maximum * len(passed) / len(checks), maximum, evidence, gaps)


def score_resume(
    resume: StructuredResume,
    analysis: JobAnalysis,
    job_title: str,
    *,
    job_id: int | None = None,
    resume_version_id: int | None = None,
) -> ATSReportData:
    """Score one structured resume against one structured job analysis."""
    required_match, required_missing, required_context = matched_skills(
        resume, analysis.required_skills
    )
    preferred_match, preferred_missing, preferred_context = matched_skills(
        resume, analysis.preferred_skills
    )
    required = _coverage_component(
        "Required skill coverage", 25, analysis.required_skills, required_match, required_missing
    )
    preferred = _coverage_component(
        "Preferred skill coverage", 10, analysis.preferred_skills, preferred_match, preferred_missing
    )
    all_matched = required_match + preferred_match
    all_context = list(dict.fromkeys(required_context + preferred_context))
    contextual_score = 15.0 if not all_matched else 15.0 * len(all_context) / len(all_matched)
    contextual = _component(
        "Contextual skill evidence",
        contextual_score,
        15,
        all_context,
        [skill for skill in all_matched if skill not in all_context],
    )
    components = [
        required,
        preferred,
        contextual,
        _responsibility_component(resume, analysis.responsibilities),
        _experience_component(resume, analysis),
        _role_component(resume, job_title),
        _parseability_component(resume),
    ]
    overall = round(sum(component.score for component in components))
    matching = list(dict.fromkeys(required_match + preferred_match))
    missing = list(dict.fromkeys(required_missing + preferred_missing))
    keyword_candidates = list(dict.fromkeys(analysis.keywords + analysis.technologies))
    _, keywords_missing, _ = matched_skills(resume, keyword_candidates)
    keywords_found = [item for item in keyword_candidates if item not in keywords_missing]
    weak = [gap for component in components for gap in component.gaps][:15]
    strong = [
        f"{component.name}: {component.score:g}/{component.maximum:g}"
        for component in components
        if component.score / component.maximum >= 0.75
    ]
    suggestions: list[str] = []
    if required_missing:
        suggestions.append(
            "Do not add missing required skills unless they are true; address them outside optimization: "
            + ", ".join(required_missing[:8])
        )
    if any(skill not in all_context for skill in all_matched):
        suggestions.append("Surface verified matching skills in relevant experience or project bullets.")
    responsibility = components[3]
    if responsibility.score / responsibility.maximum < 0.6:
        suggestions.append("Reorder the most relevant existing responsibility bullets toward the top.")
    parseability = components[-1]
    suggestions.extend(parseability.gaps[:3])
    explicit_signals = len(analysis.required_skills) + len(analysis.responsibilities)
    confidence = "high" if explicit_signals >= 5 else "medium" if explicit_signals >= 2 else "low"
    return ATSReportData(
        scoring_version=SCORING_VERSION,
        overall_score=overall,
        components=components,
        matching_skills=matching,
        missing_skills=missing,
        weak_areas=weak,
        strong_areas=strong,
        keywords_found=keywords_found,
        keywords_missing=keywords_missing,
        suggestions=suggestions,
        confidence=confidence,
        job_id=job_id,
        resume_version_id=resume_version_id,
    )
