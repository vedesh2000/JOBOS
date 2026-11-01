# ATS compatibility scoring

JobOS reports an explainable compatibility estimate, not a claim about any employer's proprietary ATS. The algorithm is versioned; the current scorer is `jobos-ats-v2`, and `jobos-ats-v1` is retained unchanged so historical reports remain reproducible. Both deliberately use more than raw keyword frequency.

## Scorer versions

- **`jobos-ats-v2` (current).** Adds three corrections over v1: OR/alternative requirement groups, N/A preferred-skill coverage with transparent renormalization, and responsibility matches that cite their supporting resume bullet. See "What changed in v2" below.
- **`jobos-ats-v1` (historical).** The original scorer, preserved byte-for-byte. Any report stored under v1 reproduces exactly; JobOS never silently rescales an old result to the new algorithm. A report always records the version it was scored with, so before/after comparisons use a single version.

## Research basis

Career guidance consistently recommends standard section headings, simple parseable structure, exact job-language where truthful, and keywords used in context rather than stuffed repeatedly. Columbia's career center notes both exact phrases and contextual use, and Indeed describes standard headings plus extraction of contact details, titles, education, and job-posting keywords: [Columbia ATS guidance](https://www.careereducation.columbia.edu/resources/optimizing-your-resume-applicant-tracking-systems) and [Indeed ATS resume guidance](https://www.indeed.com/career-advice/resumes-cover-letters/applicant-tracking-system-resume/).

The scoring model below is JobOS's transparent engineering heuristic derived from those practices; it is not reverse-engineered vendor behavior.

_Content from linked sources was rephrased for compliance with licensing restrictions._

## Score components (100 points)

| Component | Weight | Method |
| --- | ---: | --- |
| Required-skill coverage | 25 | Required skills supported anywhere in the resume, using exact normalized terms and a small explicit alias map. In v2, requirements phrased as alternatives ("React or Vue") form one group that is satisfied by matching any member. Unknown requirements are not guessed. |
| Preferred-skill coverage | 10 | Same method for preferred skills, with a lower weight. In v2, when a posting lists no preferred skills the component is reported as N/A and its 10 points are removed from the denominator (the remaining 90 are rescaled to 100) rather than awarding a vacuous full score. |
| Contextual evidence | 15 | A matched skill earns full evidence when it appears in experience/project text and partial evidence when it appears only in the skills section. Repetition is capped. |
| Responsibility alignment | 15 | Compares meaningful job-responsibility phrases with experience/project bullets using normalized phrase and token-set similarity, excluding boilerplate stop words. In v2 the same similarity bar is used (it is **not** loosened), but each supported responsibility now cites the specific bullet that supports it, and a responsibility with no bullet above the bar stays an honest gap — nothing is inferred from loosely related technology. |
| Experience, seniority, education | 15 | Compares explicit years/level/degree requirements with dated experience and structured education. Missing job requirements remain neutral rather than failures. |
| Role/title alignment | 10 | Compares the target title/role family with prior titles, summary, and project evidence using token-set similarity. |
| Parseability and structure | 10 | Structured contact data, standard sections, dated experience, non-empty bullets, sane length, and text-extractable generated PDF. |

The total is a weighted sum rounded to an integer. Every component includes its points, maximum, evidence, gaps, and suggestions.

## What changed in v2

v2 corrects three issues observed in v1 without lowering any matching bar or rewarding reordering:

1. **OR / alternative requirements.** A posting that asks for "a modern UI framework like React or Vue" is one requirement, not two. v2 groups such alternatives (detected deterministically from the job text) so matching one member satisfies the group. In v1, React present + Vue absent counted Vue as an independent missing required skill and capped the component; v2 awards full credit for that requirement.
2. **N/A preferred coverage.** When a posting lists no preferred skills, v1 awarded the full 10/10 for an empty set — a vacuous boost. v2 marks the component N/A and renormalizes: its 10 points leave the denominator and the remaining components are rescaled to 100. This can move a score down relative to v1; that is the honest direction.
3. **Evidence-cited responsibility matching.** v2 keeps v1's 0.55 similarity threshold but records which resume bullet supports each matched responsibility, so a match is auditable. Unmatched responsibilities remain gaps; v2 never infers unit testing, code reviews, or mentoring from a loosely related technology.

The deterministic score is independent of the AI rewrite model. The AI optimizer is given the job's responsibilities (the scored gaps) and may only reword existing bullets using facts already present; it cannot change the scorer, and if its rewrites would lower the score the optimizer keeps the original wording and reports "no material improvement."

## Matching behavior

- Matching is case-insensitive and punctuation-normalized but token-boundary aware (`C` does not match `CSS`).
- A small reviewed alias table handles equivalences such as `Postgres`/`PostgreSQL`, `Node`/`Node.js`, and `JS`/`JavaScript`; related technologies are not treated as equivalent (`Docker` is not `Kubernetes`).
- Required/preferred classification comes from explicit phrases and structured AI extraction with source evidence; deterministic extraction is the fallback.
- Skill repetition beyond the first contextual occurrences adds no score.
- Skills found only in a list get less contextual credit than evidence in an experience/project bullet.
- Responsibility matching uses token sets/phrases, not raw occurrence count.
- A missing required skill is reported plainly. Optimization cannot insert it unless it already exists in truthful resume evidence.
- Score bands are UI guidance only: 80–100 strong, 65–79 good, 45–64 partial, below 45 weak. The configured minimum only filters the dashboard; it never deletes a job.

## Anti-gaming and confidence

The scorer caps each component and keyword contribution, detects suspicious repetition, and does not reward hidden content. Confidence is high when the job explicitly distinguishes requirements and the resume has structured dates/sections; it decreases when extraction is ambiguous or fields are missing. Reports retain the analyzed job and resume version IDs so a later resume never changes historical results.

## Optimization feedback

Suggestions are generated from component gaps in this order:

1. Surface already-supported required skills in context.
2. Reorder the most relevant existing bullets/projects/skills.
3. Clarify supported impact using the original facts and numbers.
4. Use an exact job term only when it is an approved alias for existing evidence.
5. Leave genuinely missing qualifications in the report instead of adding them to the resume.

This keeps the score useful as a diagnostic while preserving the applicant's factual record.
