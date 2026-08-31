# JobOS

A local-first job application assistant. It finds relevant roles, scores your resume
against each one honestly, and prepares tailored application packages for you to
review. It never submits anything on your behalf, and it never invents facts about
you.

Everything runs on your machine: a SQLite database, local files, and a web dashboard
bound to loopback. AI is optional; every AI step has a deterministic fallback.

---

## Table of contents

- [What it does](#what-it-does)
- [Install](#install)
- [Quick start](#quick-start)
- [The daily workflow](#the-daily-workflow)
- [Your resume as YAML](#your-resume-as-yaml)
- [Configuration](#configuration)
- [Command reference](#command-reference)
- [How scoring works](#how-scoring-works)
- [Tweaking the PDF](#tweaking-the-pdf)
- [What it will not do](#what-it-will-not-do)
- [Known limitations](#known-limitations)
- [Development](#development)

---

## What it does

1. **Crawls** career boards you explicitly list (Greenhouse, Lever, Ashby,
   SmartRecruiters, Workday via their public JSON APIs; other sites via JSON-LD and
   conservative HTML parsing).
2. **Analyses** each posting into structured requirements: skills, years, seniority.
3. **Scores** your resume against the posting with an explainable ATS-style score.
4. **Optimises** your resume for a specific role by rewording and reordering
   *existing* content, with a truth guard that rejects anything unsupported.
5. **Generates** a versioned package: tailored resume as PDF, HTML, Markdown and JSON,
   plus a diff, an ATS report, and a snapshot of the posting.
6. **Stops for your review.** You open the employer's page and apply yourself.

---

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <your-repo-url> && cd JobOS
uv sync
uv run playwright install chromium   # one-time, needed for PDF rendering
```

Optional, for AI-assisted wording: install [Ollama](https://ollama.com) and pull a
model (`ollama pull gemma3:12b`). Without it, JobOS uses deterministic fallbacks.

---

## Quick start

```bash
uv run jobos init                       # creates config.yaml from the example
uv run jobos doctor                     # verifies model, Chromium, database, resume
uv run jobos resume import my.resume.yaml
uv run jobos crawl
uv run jobos analyse-all --location India --limit 40
uv run jobos report --location India    # per-company shortlist
uv run jobos optimise <job_id>
uv run jobos diff <application_id>
uv run jobos generate <application_id>
uv run jobos apply <job_id>
uv run jobos status <job_id> applied
```

Run `uv run jobos doctor` any time something looks wrong; it checks each dependency
and tells you what needs attention.

---

## The daily workflow

One command does everything up to your review:

```bash
uv run jobos daily --location India
```

It will:

1. crawl every configured board,
2. score the relevant roles that are not analysed yet,
3. build a per-company shortlist that respects your application quota,
4. generate tailored packages for the strongest picks (10 by default),
5. print an action list with the package path and `apply` command for each.

Then, for each package you like:

```bash
open "<package dir>/resume.pdf"     # review it
uv run jobos apply <job_id>          # opens the employer's form in your browser
uv run jobos status <job_id> applied # records it, consuming that company's quota
```

Marking jobs `applied` matters: it is what keeps the per-company quota honest.

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--since-hours 24` | only roles first seen in the last 24 hours |
| `--top 10` | how many packages to prepare |
| `--quota 3` | applications allowed per company (overrides config) |
| `--analyse-limit 40` | cap on how many roles to score this run |
| `--no-crawl` | use stored jobs, skip fetching |
| `--json` | machine-readable output |

The run is **idempotent**: a job whose package already exists is reported rather than
regenerated, so running it twice costs nothing.

> `--since-hours` uses when *JobOS first saw* a posting, not the employer's publish
> date, because boards rarely expose a reliable one. After your first run, "new to
> JobOS" is effectively "new to you". On a first run, or a day with few new postings,
> `--since-hours 24` can legitimately return nothing; drop it to use your full set.

---

## Your resume as YAML

**Author your resume as YAML.** It is the reliable path: no PDF parsing, no AI
transcription, so it imports exactly as written and gets validated up front.

```bash
uv run jobos resume import my.resume.yaml
```

`.yaml`, `.yml`, and `.json` are loaded directly with no parsing step. PDF, DOCX, and
Markdown also work, but rely on a best-effort parser that can misread dense layouts.

To convert an existing resume, use the copy-paste prompt in
[docs/resume-yaml-guide.md](docs/resume-yaml-guide.md) with any AI assistant, save the
result, and import it.

Minimal shape:

```yaml
schema_version: 1
contact:
  name: Your Name
  email: you@example.com
  location: Bengaluru, India
  links: [https://github.com/you]
summary: One paragraph.
skills: [Python, FastAPI, PostgreSQL]
skill_groups:                      # optional, presentation only
  Languages: [Python, SQL]
  Backend: [FastAPI, PostgreSQL]
experience:
  - id: exp-acme
    company: Acme
    title: Senior Engineer
    start_date: Aug 2021
    end_date: Present
    bullets:
      - id: exp-acme-bullet-api
        text: Built the billing API serving 1M requests a day.
education:
  - id: edu-nit
    institution: NIT
    degree: B.Tech
    field: Computer Science
```

Rules that keep it valid: `contact.name` is required, every `id` is 4-80 characters
and unique across the file, links must be absolute URLs, and unknown keys are
rejected. Errors name the exact field.

`skills` is the flat list used for scoring. `skill_groups` only changes how the Skills
section is laid out on the rendered resume.

---

## Configuration

`config.yaml` (created by `jobos init`, git-ignored because it is personal).

### Preferences: what counts as a job that fits you

```yaml
preferences:
  preferred_locations: [India, Bengaluru, Bangalore, Hyderabad, Pune, Remote]
  remote_only: false
  minimum_match_score: 45
  roles_include: [engineer, developer, sde, software, backend, "full stack"]
  roles_exclude: [manager, director, intern, vp, head]
  skills_include: [python, react, "full stack", node.js, typescript]
  skills_exclude: [spring boot, kotlin, scala]
  min_years:
  max_years: 8
  seniority: ""
  max_applications_per_company: 3
```

`search --preferred`, `report`, `daily`, and `analyse-all --preferred` apply these
together:

- **`preferred_locations`** keeps a job whose location contains any one of the
  entries. Listing both spellings ("Bengaluru" and "Bangalore") means a single search
  covers both. An explicit `--location` on the command overrides this list.
- **`roles_include` / `roles_exclude`** filter on the job title, so you only see
  individual-contributor engineering roles and not management. Note that `principal`
  is usually a senior IC grade at product companies, so excluding it drops real
  matches; leave it out of `roles_exclude`.
- **`skills_include` / `skills_exclude`** filter on the posting's tech stack — its
  skills, title, and description. Matching is on whole tokens, so excluding `java`
  keeps a JavaScript role, and known aliases fold together (`reactjs` counts as
  `react`, `k8s` as `kubernetes`). Terms outside the known vocabulary match literally,
  so `full stack` works. Include is any-of; exclude wins over include. Excluding
  `spring boot` is a sharper way to drop JVM-first roles than excluding `java`, which
  also discards polyglot "Java or Python" postings you might want.
- **`min_years` / `max_years`** bound the experience a posting asks for. This needs
  analysis data, so it only constrains *analysed* jobs; unanalysed roles are kept so a
  pre-analysis shortlist is never empty. The explicit `--max-years` flag is strict and
  does drop them.
- **`max_applications_per_company`** caps how many roles `report` and `daily`
  recommend per employer, so a limited application quota goes to the best matches.

### AI tasks

```yaml
ai:
  provider: ollama          # or openai, anthropic, gemini, openrouter
  model: gemma3:12b
  base_url: http://127.0.0.1:11434
  max_output_tokens: 2000
  tasks:
    structure_resume: true
    analyse_jobs: true
    rewrite_summary: false  # keep your hand-written summary verbatim
    rewrite_bullets: true
    cover_letter: true
```

Every task can be turned off and has a deterministic fallback. Cloud provider API keys
are read only from environment variables (`JOBOS_OPENAI_API_KEY`,
`JOBOS_ANTHROPIC_API_KEY`, ...), never from this file.

Turning `rewrite_summary` off is often worth it: the truth guard can only keep summary
claims it can verify elsewhere in your resume, so a metric-rich summary may get
stripped. With it off, your original summary is preserved.

### Rendering

```yaml
render:
  theme: onepager     # classic, compact, letter, or onepager
```

`onepager` is dense and fits substantial content on one ATS-readable page, scaling the
print output as needed rather than dropping below a readable font size.

### Companies

Only the companies you list are ever crawled.

```yaml
companies:
  okta: https://boards.greenhouse.io/okta
  meesho: https://jobs.lever.co/meesho
  openai: https://jobs.ashbyhq.com/openai
  # extended form:
  # acme:
  #   name: Acme
  #   url: https://boards.greenhouse.io/acme
  #   enabled: true
```

`config.example.yaml` ships with a curated, commented list of boards verified to carry
India-based roles. To find a company's real board, look at where its careers page
submits applications; most product companies use Greenhouse, Lever, or Ashby.

### Adding a company

From the dashboard, use **Add a company permanently** under the crawl bar, or:

```bash
jobos add-company https://boards.greenhouse.io/acme --crawl
```

Both write the entry under `companies:` in `config.yaml` and load it immediately, so no
restart is needed. The file is edited as text rather than rewritten from parsed YAML, so
your comments survive; added entries collect under one marker at the end of the block.

### Crawling a one-off URL

You do not have to add a company to config to try it. Paste any careers-page or
job-board URL and JobOS crawls it once, storing the jobs under an employer name
derived from the URL:

```bash
jobos crawl https://boards.greenhouse.io/somecompany
jobos crawl https://careers.example.com/jobs --name "Example"
```

In the web dashboard, the crawl bar has a URL box next to the company dropdown for the
same thing. Ad-hoc crawls use the same adapters as configured ones (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday by host; JSON-LD/HTML otherwise), apply the same SSRF
protection, and never close jobs from a previous run. They are not written to config,
so re-crawling means pasting the URL again — add it under `companies:` to keep it.

---

## Command reference

### Setup and diagnostics

| Command | Purpose |
| --- | --- |
| `jobos init` | create `config.yaml` from the example |
| `jobos doctor` | check model, Chromium, database, resume, config |
| `jobos config` | show the loaded configuration |

### Resume

| Command | Purpose |
| --- | --- |
| `jobos resume import <file>` | import YAML, JSON, PDF, DOCX, or Markdown as master |
| `jobos resume show --json` | print the structured master resume |
| `jobos resume versions` | list every stored version |
| `jobos render <file> --out DIR` | render any resume YAML/JSON to MD, HTML, PDF |
| `jobos export <app_id> --out F` | dump a tailored resume as editable YAML |

### Jobs

| Command | Purpose |
| --- | --- |
| `jobos crawl [company\|url]` | fetch postings from configured boards, or a pasted career-page URL |
| `jobos search [query]` | search stored jobs |
| `jobos analyse <job_id>` | analyse and score one job |
| `jobos analyse-all [query]` | analyse and score a filtered set |
| `jobos report` | per-company shortlist bounded by quota |
| `jobos daily` | the full morning run |
| `jobos score <job_id>` | show a stored ATS report |

`search` accepts `--company`, `--location`, `--status`, `--min-score`, `--max-years`,
`--min-years`, `--seniority`, `--skill` (repeatable, any-of), `--exclude-skill`
(repeatable), `--sort` (`newest`, `oldest`, `score`, `company`, `title`), `--limit`,
`--preferred`, and `--json`. `analyse-all` accepts the same selection filters plus
`--preferred`, so you can score only the roles that pass your preferences instead of
spending model time on ones you have ruled out:

```bash
jobos search --location Hyderabad --skill python --skill react --exclude-skill "spring boot"
jobos analyse-all --preferred --location Bengaluru
```

### Applications

| Command | Purpose |
| --- | --- |
| `jobos optimise <job_id>` | build a tailored draft (add `--cover-letter`) |
| `jobos diff <app_id>` | review what changed and what was rejected |
| `jobos generate <app_id>` | write the versioned package |
| `jobos history` | list all applications and their IDs |
| `jobos apply <job_id>` | open the employer's application page |
| `jobos status <job_id> <state>` | set `new`, `saved`, `opened`, `applied`, `ignored` |

Two ID spaces: **job IDs** come from `search`; **application IDs** come from
`optimise` and are listed by `history`.

### Web dashboard

```bash
uv run jobos serve      # http://127.0.0.1:8000
```

Bound to loopback so only your machine can reach it. There is no authentication, so do
not expose it on a network interface.

---

## How scoring works

The score is a transparent heuristic (`jobos-ats-v1`), not a real employer ATS. It
compares your resume against the posting's extracted requirements across weighted
components and shows the breakdown, matching and missing skills, and suggestions. See
[docs/ats-scoring.md](docs/ats-scoring.md).

Read it as a **relative** diagnostic, not a verdict. One caveat worth knowing: roles
with few hard requirements (some non-technical postings) can score deceptively high
because your resume trivially covers them. This is why `roles_include` /
`roles_exclude` matter — filter to relevant roles first, then compare scores within
that set.

---

## Tweaking the PDF

Everything about content and layout is editable, and the loop is fast.

**Tweak your master resume:**

```bash
# edit my.resume.yaml, then
uv run jobos render my.resume.yaml --out preview
open preview/resume.pdf
```

**Tweak an already-tailored resume**, without re-running the pipeline:

```bash
uv run jobos export 6 --out tailored.resume.yaml   # 6 = application ID
# edit tailored.resume.yaml
uv run jobos render tailored.resume.yaml --out final
open final/resume.pdf
```

Try themes without editing anything:

```bash
uv run jobos render my.resume.yaml --theme compact --out /tmp/try
```

Exported tweaks are a detached copy: the generated package stays immutable, and your
edited PDF lands in the output folder you chose. To make a change permanent for future
tailoring, re-import your master with `jobos resume import`.

---

## What it will not do

These are deliberate design choices, not missing features.

- **It does not submit applications.** `apply` opens the employer's own page; you fill
  the form. Automated submission is against most employers' terms and produces worse
  outcomes.
- **It does not invent facts.** The truth guard rejects new skills, changed companies,
  titles or dates, and unsupported numbers. Optimisation is limited to rewording and
  reordering what your resume already says.
- **It does not scrape sites that forbid it.** JobOS uses public vendor APIs and
  conservative public HTML, respects `robots.txt`, and never logs in or bypasses bot
  protection. It will not scrape LinkedIn.
- **It does not phone home.** Your resume and job data stay on your machine. Cloud AI
  is opt-in per provider and keys come from the environment.

Read every diff before you generate, and read every PDF before you apply. JobOS is a
drafting assistant with a human in the loop, not an autopilot.

---

## Known limitations

- **Very large boards** stream every job description in one response; the crawler caps
  a single response at 32 MB to bound memory. The largest boards seen (Databricks,
  OpenAI, Airwallex) sit at 9-12 MB and load fine, but a company that grows past 32 MB
  would be skipped with an error while the rest of the crawl continues.
- **Board slugs change.** A company can 404 one week and work the next. `crawl`
  reports per-company errors and keeps going. Postings a board no longer lists are
  marked closed and drop out of searches (shown as `closed` in the crawl summary); a
  board truncated by `per_company_limit` never closes anything, since its tail was
  never fetched.
- **Workday and SmartRecruiters are the slow adapters.** They have no location facet
  and need one request per posting (Greenhouse, Lever and Ashby return a whole board in
  one response). Those per-posting requests are issued concurrently, bounded by
  `crawler.concurrency`, but paging is still serial, so a large tenant (Adobe,
  Salesforce, Target) walked up to `per_company_limit` still takes minutes. Their India
  roles are not listed first, so raising the limit trades crawl time for coverage.
  Rough guide per board: Greenhouse/Ashby a second or two, Workday around 0.3s per
  posting. A full crawl of ~100 boards is ~20 minutes, dominated by these two.
- **The PDF/DOCX parser is best-effort.** Dense two-column resumes with unusual bullet
  characters can parse poorly. Use YAML for reliable results.
- **AI structuring of a whole resume is unreliable** with small or reasoning-heavy
  models, which tend to truncate the JSON. This is exactly why YAML import exists.
- **Match scores need analysis.** Filters on years, seniority, and score only apply to
  analysed jobs; run `analyse-all` before relying on a ranking.

---

## Development

```bash
uv run pytest -q          # test suite
uv run mypy src           # strict type checking
uv run ruff check         # lint
```

Architecture notes are in [docs/architecture.md](docs/architecture.md), scoring in
[docs/ats-scoring.md](docs/ats-scoring.md), and resume authoring in
[docs/resume-yaml-guide.md](docs/resume-yaml-guide.md).

Your personal files (`config.yaml`, `*.resume.yaml`, `.jobos/`, `output/`) are
git-ignored so the repository stays free of personal data.
