# JobOS V1 design

JobOS V1 is deliberately a single-user local application. It has one Python package, one SQLite database, one output directory, server-rendered FastAPI pages, and a Typer CLI. It is sized for roughly 100 configured companies and 1,000–10,000 stored jobs, not for a hosted multi-tenant service.

## Decisions

- **One process:** CLI commands run directly; the web app performs bounded async crawl work in-process.
- **One database:** four main records—resume versions, jobs, ATS reports, and application drafts/packages. SQLAlchemy creates the SQLite schema on first run.
- **One configuration file:** YAML declares companies, AI selection, crawl limits, and preferences. Secrets come from environment variables.
- **No plugin framework:** provider and crawler implementations are ordinary classes selected by small factories.
- **No DI framework:** constructors receive their direct dependencies; `services.py` wires them together.
- **No queue or workflow engine:** an async semaphore bounds crawling. A failed operation reports failure and can be rerun.
- **No React build:** Jinja templates, small vanilla JavaScript helpers, and local CSS provide the dashboard and dark mode.
- **No application submission:** Apply opens the official HTTPS page and records that it was opened. V1 does not log in, bypass CAPTCHA, or submit forms.
- **Versioned outputs:** approved packages go to `output/<company>/<role>/vNNN/`; older packages are never overwritten.

## Modules

```text
src/jobos/
├── models.py       # strict structured resume, job analysis, ATS and edit models
├── config.py       # YAML loading and validation
├── database.py     # four SQLAlchemy tables and small repository methods
├── resume.py       # PDF/DOCX/Markdown extraction and structured parsing
├── ai.py           # Ollama/OpenAI/Anthropic/Gemini/OpenRouter HTTP clients
├── ats.py          # deterministic, explainable ATS score
├── agents.py       # small analyzer/skills/summary/resume/ATS/PDF/coordinator classes
├── crawler.py      # configured Greenhouse/Lever/generic crawling
├── renderer.py     # Markdown, Jinja HTML, Chromium PDF, package output
├── services.py     # application use cases and dependency construction
├── web.py          # FastAPI pages and JSON endpoints
├── cli.py          # Typer commands
├── templates/      # server and document templates
└── static/         # local CSS and JavaScript
```

The files are organized by useful behavior, not abstract layers. If a file becomes difficult to navigate, split it around an actual feature boundary at that time.

## Truthfulness contract

The master resume is structured and immutable once stored. Optimization is a constrained plan, not a replacement resume from the model:

- summary replacement;
- bullet replacement by stable bullet ID;
- ordering of existing skills, bullets, and projects;
- no operation exists for companies, roles, dates, education, projects, or adding skills.

Before a draft is shown, the truth guard rejects new numbers, missing job skills, changed protected values, unknown IDs, suspicious new proper nouns, and low-overlap bullet rewrites. A human reviews a semantic diff before package generation. This cannot mathematically prove natural-language equivalence, so ambiguous wording is shown rather than silently approved; the structured operation surface and deterministic checks reduce the risk substantially.

## Main workflow

1. Upload PDF, DOCX, or Markdown.
2. Extract local text; ask the selected model to structure it without rewriting. If the model is unavailable, use a conservative section parser.
3. Store a new master resume version and keep the original upload locally.
4. Crawl only companies named in `config.yaml`; use vendor JSON endpoints where recognized and generic HTML/JSON-LD otherwise.
5. Search/filter jobs in SQLite.
6. Analyze a selected job, calculate the deterministic ATS report, and store it.
7. Generate a constrained optimization proposal. Deterministic agents still reorder relevant existing content if AI is unavailable.
8. Validate the proposal and show before/after plus rejected edits.
9. On approval, render Markdown, HTML, JSON, PDF, diff, ATS report, cover letter when requested, and job JSON into a new package version.
10. Open the official application URL from the dashboard or CLI.

## Security and operational boundaries

- Bind the development server to `127.0.0.1` by default.
- Accept only bounded PDF/DOCX/Markdown uploads; never execute document macros or embedded content.
- Treat career HTML and job descriptions as untrusted data; scripts are not executed except inside the isolated Playwright browser used to render public pages.
- Only `http`/`https` career and apply URLs are accepted; loopback/private-network crawling is rejected by default.
- Job text is quoted as data in AI prompts, model output is parsed as JSON, and it has no tools or code-execution path.
- Cloud providers are used only when selected explicitly in YAML and require environment-based API keys.
- Generated HTML is autoescaped and has no remote assets or scripts.

## Intentional V1 limits

Generic crawling cannot perfectly understand every career site. The implementation favors vendor APIs, JSON-LD, and conservative link extraction; unsupported pages fail visibly and can be improved with a small site-specific parser later. Resume parsing is also imperfect, so the UI exposes structured JSON and original text for review. V1 opens application pages but does not prefill arbitrary third-party forms because safe generic field detection is unreliable and potentially contrary to site terms.

## Running well on small local models

JobOS is built to stay useful on a laptop model such as Gemma 3 12B, and to degrade rather than fail on a 1B model. Every AI task has a deterministic fallback, so the workflow always completes.

Token discipline, rather than a larger model, does most of the work:

- **Send an excerpt, not the posting.** `requirement_excerpt` scores each line for requirement markers, known skills, years-of-experience patterns, and action verbs, then keeps the densest lines within `ai.max_input_chars`. On a typical padded posting this cut 7,223 characters to 142.
- **Never repeat the schema for Ollama.** Ollama constrains decoding with the schema it already received, so embedding it in the prompt only burns context. Cloud providers without that guarantee still get it.
- **Classify instead of extract.** The model receives a candidate skill list found deterministically and sorts it into required/preferred. Picking from a list is far more reliable for a small model than open-ended extraction, and any skill it invents is discarded because it is not in the list.
- **Cap the rewrite surface.** Only the `ai.max_bullet_rewrites` most job-relevant bullets are sent, so cost stays flat regardless of resume length.
- **Keep the model warm.** `ai.keep_alive` avoids reloading weights on every call during a run.
- **Refuse oversized payloads.** A caller that exceeds four times the budget gets an error instead of a silently truncated prompt.

Deterministic components carry the parts that must be exact: ATS scoring, skill matching, date and seniority parsing, ordering, diffing, and the truth guard. A model is never the only thing standing between a hallucination and your PDF.

Per-job AI calls in a full optimization run: one classification, one summary, one bullet batch, and one optional cover letter — four small calls, not one large one.
