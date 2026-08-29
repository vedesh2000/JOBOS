# Authoring your resume as YAML

JobOS treats a structured YAML (or JSON) file as the reliable source of truth for
your resume. Unlike a PDF or DOCX, a YAML file needs no parsing and no AI to
understand, so it always imports exactly as written. This is the recommended way to
use JobOS.

Import it with:

```
uv run jobos resume import your.resume.yaml
```

PDF, DOCX, and Markdown still work, but they rely on a best-effort parser (and an
optional AI step) that can misread dense layouts. YAML avoids that entirely.

## The one-time conversion prompt

You do not have to hand-write the YAML from scratch. Paste the prompt below into any
capable AI assistant (ChatGPT, Claude, Gemini, etc.), attach or paste your existing
resume, and it will produce a valid JobOS resume file. Save the result as
`your.resume.yaml` and import it. After that, you just edit the file directly.

---

> You are converting a resume into a strict YAML format for a tool called JobOS.
> Output **only** a YAML document, no prose, no code fences. Use the exact structure
> and field names below. Do not invent, embellish, or drop any facts — transcribe
> only what the resume actually says.
>
> Rules:
> - Top-level keys allowed: `schema_version`, `contact`, `summary`, `skills`,
>   `experience`, `projects`, `education`, `certifications`, `achievements`,
>   `languages`, `additional_sections`. No other keys.
> - `schema_version` is always `1`.
> - `contact.name` is required. `email`, `phone`, `location` are strings.
>   `contact.links` must be a list of absolute http/https URLs (turn "GitHub:
>   username" into the full `https://github.com/username` URL).
> - Every item in `experience`, `projects`, `education`, `certifications`,
>   `achievements`, and every bullet must have a unique `id`: a short lowercase
>   dashed slug, 4-80 characters (for example `exp-acme-backend`,
>   `exp-acme-bullet-api`). IDs must be unique across the whole file.
> - `experience[].bullets` and `projects[].bullets` are lists of `{id, text}`. Keep
>   one resume bullet per entry; do not merge or split them.
> - `education[]` fields: `institution`, `degree`, `field`, `start_date`,
>   `end_date`, and `details` (a list of strings, e.g. `"CGPA: 7.33"`).
> - `certifications[]` fields: `id`, `name`, optional `issuer`, optional `date`.
> - `achievements[]` are `{id, text}` items.
> - `skills` and `languages` are plain lists of strings.
> - Dates are free text exactly as written (e.g. `Aug 2021`, `Present`).
> - Put anything that does not fit a section under `additional_sections` as a map of
>   section name to a list of strings.
>
> Here is the structure to fill in:
>
> ```yaml
> schema_version: 1
> contact:
>   name: ""
>   email: ""
>   phone: ""
>   location: ""
>   links: []
> summary: ""
> skills: []
> experience:
>   - id: ""
>     company: ""
>     title: ""
>     start_date: ""
>     end_date: ""
>     location: ""
>     skills: []
>     bullets:
>       - id: ""
>         text: ""
> projects:
>   - id: ""
>     name: ""
>     url: ""
>     description: ""
>     skills: []
>     bullets:
>       - id: ""
>         text: ""
> education:
>   - id: ""
>     institution: ""
>     degree: ""
>     field: ""
>     start_date: ""
>     end_date: ""
>     details: []
> certifications:
>   - id: ""
>     name: ""
>     issuer: ""
>     date: ""
> achievements:
>   - id: ""
>     text: ""
> languages: []
> additional_sections: {}
> ```
>
> Now convert this resume:
>
> [paste your resume text here]

---

## If the import complains

JobOS validates the file and reports the exact problem, for example:

- `structured resume is not valid: contact.name: Field required` — add a name.
- `resume item IDs must be unique` — two entries share an `id`; make them distinct.
- `links.0: must be an absolute http or https URL` — use the full `https://...` form.

Fix the reported field and re-run the import. Because everything is validated up
front, a successful import means the resume stored exactly as written.

## Editing later

Your YAML file is the master. To update your resume, edit the file and re-import it;
JobOS stores a new master version and keeps the previous ones. Stable bullet `id`s
matter here: keeping the same id for a bullet lets JobOS track it across versions and
optimizations, so prefer editing a bullet's `text` over replacing its `id`.
