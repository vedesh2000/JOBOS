# Company board discovery and validation

This is a lightweight layer on top of the existing crawler. It does not replace the
config-driven registry or change how crawling, scoring, resume generation, or
application tracking work. It adds three things:

1. A way to **import** candidate company boards from public ATS directories instead
   of hand-maintaining every slug.
2. A **validator** that proves a board is real and useful before it is adopted.
3. A **sidecar registry** that records what validation and crawling learned, so
   coverage can be reported and dead or unsupported boards flagged.

## Why a separate layer

`config.yaml` stays the human-edited source of truth for which boards get crawled; it
keeps its hand-written per-board annotations. The registry
(`.jobos/company_registry.json`) is a machine-maintained index. Keeping them separate
means config is never rewritten by a comment-stripping round-trip, and adding the
feature is fully backward-compatible: an older config with no registry loads
unchanged, and no new required config keys were introduced.

## The "never trust HTTP 200" rule

A candidate board is only ever reported **valid** when, after a real crawl through the
existing adapter:

- its host is one JobOS actually has a JSON adapter for (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday) — otherwise it is flagged `UNSUPPORTED` for manual review,
  never silently HTML-scraped and reported as success;
- the ATS answered with the shape that ATS returns (a 200 that is an HTML login page
  is `MALFORMED`, not valid);
- it lists at least one parseable posting (`EMPTY` otherwise); and
- at least one posting is in India or remote-India (`IRRELEVANT` otherwise).

The validator drives the same `CareerCrawler` the real crawl uses, so a board that
validates here crawls identically later. Discovery breadth (companies hiring in India)
and application fit (target engineering roles) are counted separately:
`india_role_count` is any India posting; `relevant_role_count` additionally requires a
target title (SDE/Senior/Backend/Full Stack/Platform/AI Application Engineer and the
backend-leaning IC family).

## Where the seed came from

`src/jobos/data/discovery_seed.yaml` holds candidate board slugs harvested from a
public open ATS directory ([kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers)),
then probed against each board's own public JSON API and kept only when the board
resolved and had India roles. Nothing is invented — every slug is a real, resolving
board. Aggregators, staffing marketplaces, and talent pools were excluded, as were
boards already in `config.yaml`. The seed is re-validated live before anything is
written.

## Commands

```bash
# Validate the packaged seed (or a file of your own candidate URLs), newest coverage only.
jobos validate-boards
jobos validate-boards --file my-candidates.yaml --limit 50

# Validate and write the passing boards into config.yaml (additive, reversible).
jobos validate-boards --adopt
jobos validate-boards --adopt --adopt-limit 200

# Before/after coverage: configured vs validated boards, India roles, flagged portals.
jobos registry
```

A candidates file is either a YAML mapping (`slug: url`), a YAML list of URLs, or one
board URL per line.

## Incremental, bounded runs

Nothing changed in how jobs are stored or deduped: `upsert_job` matches on apply URL
or fingerprint, so a re-crawl updates rather than duplicates, and a board read in full
soft-closes postings it stopped listing. Previously found jobs are retained. A normal
`jobos crawl` stamps `last_crawled_at` on each registered board it covered, so the
registry stays fresh without re-validating every run. Validation itself is bounded by
the same concurrency semaphore the crawler uses.

## Rollback

Adoption only appends entries under the existing
`# --- added from the dashboard / add-company ---` marker in `config.yaml`. To roll
back:

- restore a snapshot from `.jobos/config_history/` (the UI/API save path snapshots
  automatically; before a bulk CLI adoption, copy `config.yaml` there yourself), or
- delete the appended entries under the marker, or
- delete `.jobos/company_registry.json` to reset the derived index (it rebuilds on the
  next validation/crawl).

The database is never touched by validation or adoption, so no data migration is
involved either way.
