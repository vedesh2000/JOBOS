# ATS compatibility scoring

JobOS reports an explainable compatibility estimate, not a claim about any employer's proprietary ATS. The algorithm is versioned as `jobos-ats-v1` and deliberately uses more than raw keyword frequency.

## Research basis

Career guidance consistently recommends standard section headings, simple parseable structure, exact job-language where truthful, and keywords used in context rather than stuffed repeatedly. Columbia's career center notes both exact phrases and contextual use, and Indeed describes standard headings plus extraction of contact details, titles, education, and job-posting keywords: [Columbia ATS guidance](https://www.careereducation.columbia.edu/resources/optimizing-your-resume-applicant-tracking-systems) and [Indeed ATS resume guidance](https://www.indeed.com/career-advice/resumes-cover-letters/applicant-tracking-system-resume/).

The scoring model below is JobOS's transparent engineering heuristic derived from those practices; it is not reverse-engineered vendor behavior.

_Content from linked sources was rephrased for compliance with licensing restrictions._

## Score components (100 points)

| Component | Weight | Method |
| --- | ---: | --- |
| Required-skill coverage | 25 | Required skills supported anywhere in the resume, using exact normalized terms and a small explicit alias map. Unknown requirements are not guessed. |
| Preferred-skill coverage | 10 | Same method for preferred skills, with a lower weight. |
| Contextual evidence | 15 | A matched skill earns full evidence when it appears in experience/project text and partial evidence when it appears only in the skills section. Repetition is capped. |
| Responsibility alignment | 15 | Compares meaningful job-responsibility phrases with experience/project bullets using normalized phrase and token-set similarity, excluding boilerplate stop words. |
| Experience, seniority, education | 15 | Compares explicit years/level/degree requirements with dated experience and structured education. Missing job requirements remain neutral rather than failures. |
| Role/title alignment | 10 | Compares the target title/role family with prior titles, summary, and project evidence using token-set similarity. |
| Parseability and structure | 10 | Structured contact data, standard sections, dated experience, non-empty bullets, sane length, and text-extractable generated PDF. |

The total is a weighted sum rounded to an integer. Every component includes its points, maximum, evidence, gaps, and suggestions.

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
