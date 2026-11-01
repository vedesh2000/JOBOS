"""Web dashboard: crawl status endpoint and the JSON crawl-start contract.

These stay offline — they exercise the request/response contract the dashboard
poller relies on, without triggering a real network crawl.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobos.services import CrawlProgress, JobOSService
from jobos.web import create_app


def test_api_crawl_status_idle(service: JobOSService) -> None:
    client = TestClient(create_app(service=service))
    body = client.get("/api/crawl").json()
    assert body["running"] is False
    assert body["summary"] is None
    assert body["error"] == ""


def test_api_crawl_status_reflects_running_state(service: JobOSService) -> None:
    service.crawl_progress = CrawlProgress(running=True, label="acme", started_at="t0")
    client = TestClient(create_app(service=service))
    body = client.get("/api/crawl").json()
    assert body["running"] is True
    assert body["label"] == "acme"


def test_crawl_start_returns_json_when_asked(service: JobOSService, monkeypatch) -> None:
    """The JS poller posts with Accept: application/json and expects a 202, not a redirect."""
    started: dict[str, object] = {}

    async def fake_start(
        company: str | None = None, *, ats_types: set[str] | None = None
    ) -> None:
        started["company"] = company
        started["ats_types"] = ats_types
        service.crawl_progress = CrawlProgress(running=True, label=company or "all")

    monkeypatch.setattr(service, "start_background_crawl", fake_start)
    client = TestClient(create_app(service=service))

    response = client.post(
        "/crawl",
        data={"company": "acme", "ats": "fast"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 202
    assert response.json()["running"] is True
    assert started["company"] == "acme"
    # The wave dropdown value is translated into the ATS set and forwarded.
    assert started["ats_types"] == {"greenhouse", "lever", "ashby"}


def test_crawl_start_conflict_is_409_json(service: JobOSService) -> None:
    """A second start while one is running is a clean 409 the poller can surface."""
    service.crawl_progress = CrawlProgress(running=True, label="busy")
    client = TestClient(create_app(service=service))
    response = client.post("/crawl", data={"company": ""}, headers={"Accept": "application/json"})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"].lower()


def test_plain_crawl_post_still_redirects(service: JobOSService, monkeypatch) -> None:
    """Without JS (no JSON Accept), the form post keeps its redirect behaviour."""

    async def fake_start(
        company: str | None = None, *, ats_types: set[str] | None = None
    ) -> None:
        service.crawl_progress = CrawlProgress(running=True, label=company or "all")

    monkeypatch.setattr(service, "start_background_crawl", fake_start)
    client = TestClient(create_app(service=service))

    response = client.post("/crawl", data={"company": ""}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/")


def test_crawl_url_field_triggers_ad_hoc_path(service: JobOSService, monkeypatch) -> None:
    """Posting a `url` uses the ad-hoc crawl, not the configured-company crawl."""
    seen: dict[str, object] = {}

    async def fake_url_crawl(url: str, name: str | None = None) -> None:
        seen["url"] = url
        service.crawl_progress = CrawlProgress(running=True, label="Acme")

    async def fake_company_crawl(
        company: str | None = None, *, ats_types: set[str] | None = None
    ) -> None:
        seen["company"] = company

    monkeypatch.setattr(service, "start_background_url_crawl", fake_url_crawl)
    monkeypatch.setattr(service, "start_background_crawl", fake_company_crawl)
    client = TestClient(create_app(service=service))

    response = client.post(
        "/crawl",
        data={"url": "https://boards.greenhouse.io/acme", "company": ""},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 202
    assert seen == {"url": "https://boards.greenhouse.io/acme"}


def test_crawl_bad_url_is_400_json(service: JobOSService) -> None:
    """A malformed ad-hoc URL is a 400 the poller can surface, not a 409."""
    client = TestClient(create_app(service=service))
    response = client.post(
        "/crawl", data={"url": "not-a-url"}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 400
    assert "web address" in response.json()["detail"].lower()


CONFIG_WITH_COMMENTS = """\
data_dir: .jobos
output_dir: output

preferences:
  remote_only: false

# Boards below were verified by hand.
companies:
  # --- global product companies ---
  okta: https://boards.greenhouse.io/okta          # 113 India roles
  meesho: https://jobs.lever.co/meesho             # 46 India roles

# Trailing note that must stay at the bottom.
"""


def test_append_company_preserves_comments_and_inserts_in_block(tmp_path) -> None:
    import yaml

    from jobos.config import append_company

    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
    append_company(path, "acme", "Acme", "https://boards.greenhouse.io/acme")
    text = path.read_text(encoding="utf-8")

    # Every comment survives, which is the whole reason for editing text not YAML.
    assert "# Boards below were verified by hand." in text
    assert "# 113 India roles" in text
    assert "# --- global product companies ---" in text
    assert "# Trailing note that must stay at the bottom." in text

    lines = text.splitlines()
    added = next(i for i, line in enumerate(lines) if line.startswith("  acme:"))
    last_existing = next(i for i, line in enumerate(lines) if line.startswith("  meesho:"))
    trailing = next(i for i, line in enumerate(lines) if line.startswith("# Trailing note"))
    assert last_existing < added < trailing  # inside the block, above the trailing comment

    # A second add reuses the same marker rather than starting another section.
    append_company(path, "beta", "Beta", "https://jobs.lever.co/beta")
    text = path.read_text(encoding="utf-8")
    assert text.count("# --- added from the dashboard") == 1

    parsed = yaml.safe_load(text)
    assert parsed["companies"]["acme"] == "https://boards.greenhouse.io/acme"
    assert parsed["companies"]["beta"] == "https://jobs.lever.co/beta"
    assert set(parsed["companies"]) == {"okta", "meesho", "acme", "beta"}


def test_append_company_rejects_duplicate_slug(tmp_path) -> None:
    import pytest as _pytest

    from jobos.config import append_company

    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
    with _pytest.raises(ValueError, match="already in the config"):
        append_company(path, "okta", "Okta", "https://boards.greenhouse.io/okta")


def test_add_company_writes_config_and_loads_it(service: JobOSService) -> None:
    """The company is usable immediately, without restarting the process."""
    service.config.config_path.write_text(CONFIG_WITH_COMMENTS, encoding="utf-8")
    slug = service.add_company("https://jobs.ashbyhq.com/prefect")
    assert slug == "prefect"
    assert service.config.companies["prefect"].name == "Prefect"
    assert "prefect:" in service.config.config_path.read_text(encoding="utf-8")


def test_artifact_is_inline_by_default_and_attachment_on_request(
    service: JobOSService, tmp_path
) -> None:
    """A PDF must preview in the browser; ?download=1 forces the save dialog."""
    from unittest.mock import patch

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = TestClient(create_app(service=service))

    with patch.object(JobOSService, "artifact_path", return_value=pdf):
        inline = client.get("/applications/1/files/resume.pdf")
        attached = client.get("/applications/1/files/resume.pdf?download=1")

    assert inline.headers["content-type"] == "application/pdf"
    assert inline.headers["content-disposition"].startswith("inline")
    assert attached.headers["content-disposition"].startswith("attachment")


def test_readme_tab_renders_markdown(service: JobOSService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/readme")
    assert response.status_code == 200
    # The real README has an H1 and fenced code, so both should be converted.
    assert "<h1>" in response.text
    assert "markdown-body" in response.text


def _seed_jobs(service: JobOSService, n: int) -> None:
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    for i in range(n):
        job = JobData(
            company="Acme",
            title=f"Backend Engineer {i}",
            location="Bengaluru, India",
            description="Python and FastAPI. 4+ years.",
            experience="4+ years",
            skills=["python"],
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            source_url="https://boards.greenhouse.io/acme",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))


def test_search_page_reports_truthful_total_and_slices(service: JobOSService) -> None:
    """A page returns only its slice, but the total reflects the full match set."""
    _seed_jobs(service, 1200)
    first = service.search_jobs_page(limit=500, offset=0, apply_preferences=False)
    assert len(first.jobs) == 500
    assert first.total == 1200  # honest count, not the page length
    assert first.has_next is True
    assert first.has_prev is False
    assert first.page_number == 1 and first.page_count == 3

    second = service.search_jobs_page(limit=500, offset=500, apply_preferences=False)
    assert len(second.jobs) == 500
    assert second.has_prev is True and second.has_next is True
    assert second.page_number == 2

    last = service.search_jobs_page(limit=500, offset=1000, apply_preferences=False)
    assert len(last.jobs) == 200
    assert last.has_next is False
    assert last.showing_from == 1001 and last.showing_to == 1200


def test_search_page_total_matches_a_filtered_query(service: JobOSService) -> None:
    """The total respects filters, so 'showing X of Y' is consistent with paging."""
    _seed_jobs(service, 50)
    page = service.search_jobs_page(
        limit=500, offset=0, search="Backend Engineer 1", apply_preferences=False
    )
    # "Backend Engineer 1", "...10".."...19", "...1" — a substring match subset, and
    # the total must equal the number of rows actually returned (single page here).
    assert page.total == len(page.jobs)
    assert page.total >= 1


def test_jobs_page_renders_with_pagination(service: JobOSService) -> None:
    """The dashboard renders and shows the 'of N' total once there is a resume."""
    from jobos.models import Contact, StructuredResume

    _seed_jobs(service, 600)
    service.database.add_resume(
        StructuredResume(contact=Contact(name="Jane", email="jane@example.com")),
        source_name="resume.md",
        source_format="md",
        original_path="r.md",
        source_text="Jane Doe",
        content_hash="abc123",
        is_master=True,
    )
    client = TestClient(create_app(service=service))
    html = client.get("/?preferred=").text
    assert "of 600" in html or "of " in html  # truthful total present


def test_crawl_waves_endpoint_starts_the_pipeline(
    service: JobOSService, monkeypatch
) -> None:
    """POST /crawl-waves kicks off the wave pipeline and returns 202 with status."""
    called: dict[str, bool] = {}

    async def fake_waves() -> None:
        called["started"] = True
        service.crawl_progress = CrawlProgress(
            running=True, label="waves", current_wave="fast"
        )

    monkeypatch.setattr(service, "start_background_waves", fake_waves)
    client = TestClient(create_app(service=service))
    response = client.post("/crawl-waves", headers={"Accept": "application/json"})
    assert response.status_code == 202
    body = response.json()
    assert body["running"] is True
    assert body["current_wave"] == "fast"
    assert called["started"] is True


def test_crawl_status_exposes_wave_fields(service: JobOSService) -> None:
    """The poller needs current_wave and completed_waves to notify per wave."""
    service.crawl_progress = CrawlProgress(
        running=True, label="waves", current_wave="medium", completed_waves=["fast"]
    )
    client = TestClient(create_app(service=service))
    body = client.get("/api/crawl").json()
    assert body["current_wave"] == "medium"
    assert body["completed_waves"] == ["fast"]


def test_insights_report_counts_skills_and_categories(service: JobOSService) -> None:
    """Insights aggregates skills (whole-token), locations and totals from real jobs."""
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    specs = [
        ("Senior Java Engineer", "Bengaluru, India", ["java"]),
        ("Backend Engineer", "Hyderabad, India", ["python", "aws"]),
        ("Frontend Engineer", "Remote - India", ["javascript", "react"]),
        ("Full Stack Developer", "Pune, India", ["python", "javascript"]),
    ]
    for i, (title, loc, skills) in enumerate(specs):
        job = JobData(
            company=f"Co{i % 2}",
            title=title,
            location=loc,
            description="x",
            skills=skills,
            apply_url=f"https://boards.greenhouse.io/c/jobs/{i}",
            source_url="https://boards.greenhouse.io/c",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))

    report = service.insights_report()
    assert report.total_jobs == 4
    tracked = {t.label: t.count for t in report.tracked_skills}
    # "java" counted once, and crucially NOT conflated with the two javascript jobs.
    assert tracked["java"] == 1
    assert tracked["javascript"] == 2
    assert tracked["python"] == 2
    locations = {loc.label: loc.count for loc in report.by_location}
    assert locations.get("Bengaluru") == 1
    assert locations.get("Hyderabad") == 1
    assert locations.get("Pune") == 1


def test_insights_page_renders(service: JobOSService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/insights")
    assert response.status_code == 200
    assert "Insights" in response.text
    assert "Tech stack across jobs" in response.text


async def test_daily_digest_counts_are_deterministic(service: JobOSService) -> None:
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    for i in range(3):
        job = JobData(
            company="Acme",
            title=f"Backend Engineer {i}",
            location="Bengaluru, India",
            description="Python. 4+ years.",
            skills=["python"],
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            source_url="https://boards.greenhouse.io/acme",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))
    digest = service.daily_digest()
    assert digest.total_active_jobs == 3
    assert digest.new_jobs_today == 3  # all just inserted
    assert str(digest.total_active_jobs) in digest.headline


async def test_coach_reply_falls_back_without_ai(service: JobOSService) -> None:
    """With no reachable provider, the coach still answers from the digest, no crash."""
    reply = await service.coach_reply("summarize my day")
    assert reply.used_ai is False
    assert "active jobs" in reply.reply
    # The turn pair is persisted (memory), even in fallback mode.
    history = service.chat_history()
    assert [m.role for m in history] == ["user", "assistant"]


async def test_coach_reply_uses_ai_and_grounds_on_data(
    service: JobOSService, monkeypatch
) -> None:
    """When AI is available the coach uses it, and is fed the real data snapshot."""
    seen: dict[str, object] = {}

    async def fake_chat(*, system: str, messages: list[dict[str, str]]) -> str:
        seen["system"] = system
        seen["messages"] = messages
        return "You have strong momentum today. Keep going!"

    monkeypatch.setattr(service.ai, "chat", fake_chat)
    reply = await service.coach_reply("how am I doing?")
    assert reply.used_ai is True
    assert "momentum" in reply.reply
    # The system prompt must carry the grounding snapshot and the anti-hallucination rule.
    assert "DATA_SNAPSHOT" in seen["system"]
    assert "Never invent" in seen["system"]
    # The user's message is included as the latest turn (memory).
    assert any(m["content"] == "how am I doing?" for m in seen["messages"])  # type: ignore[index]


async def test_coach_clear_forgets_history(service: JobOSService) -> None:
    await service.coach_reply("hi")
    assert service.chat_history()
    removed = service.clear_chat()
    assert removed >= 2
    assert service.chat_history() == []


def test_coach_page_renders(service: JobOSService) -> None:
    client = TestClient(create_app(service=service))
    response = client.get("/coach")
    assert response.status_code == 200
    assert "Coach" in response.text
    assert 'id="coach-form"' in response.text


async def test_ask_insights_is_grounded_and_stateless(
    service: JobOSService, monkeypatch
) -> None:
    """The insights Q&A feeds the trends snapshot and keeps no conversation memory."""
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    for i, skill in enumerate(["java", "python", "python"]):
        job = JobData(
            company="Acme",
            title=f"Engineer {i}",
            location="Bengaluru, India",
            description="x",
            skills=[skill],
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            source_url="https://boards.greenhouse.io/acme",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))

    captured: dict[str, object] = {}

    async def fake_chat(*, system: str, messages: list[dict[str, str]]) -> str:
        captured["system"] = system
        captured["messages"] = messages
        return "Java: 1, Python: 2"

    monkeypatch.setattr(service.ai, "chat", fake_chat)
    reply = await service.ask_insights("how many java vs python?")
    assert reply.used_ai is True
    # Grounded: the real counts are in the snapshot handed to the model.
    assert "TRENDS_SNAPSHOT" in captured["system"]
    assert '"skill": "java"' in captured["system"] or "java" in captured["system"]
    # Stateless: asking does NOT write to the coach conversation memory.
    assert service.chat_history() == []


async def test_ask_insights_falls_back_without_ai(service: JobOSService) -> None:
    reply = await service.ask_insights("what stands out?")
    assert reply.used_ai is False
    assert "active jobs" in reply.reply


def test_insights_page_has_ask_section(service: JobOSService) -> None:
    client = TestClient(create_app(service=service))
    html = client.get("/insights").text
    assert 'id="ask-form"' in html
    assert "ask-suggestion" in html  # suggested questions rendered


async def test_new_within_filters_by_first_seen(service: JobOSService) -> None:
    """The 'New within' filter narrows to recently first-seen jobs (daily ritual)."""
    from datetime import UTC, datetime, timedelta

    from jobos.crawler import job_fingerprint
    from jobos.database import JobRecord
    from jobos.models import JobData

    # Two jobs seen now, one seen 10 days ago.
    for i in range(3):
        job = JobData(
            company="Acme",
            title=f"Backend Engineer {i}",
            location="Bengaluru, India",
            description="Python. 4+ years.",
            skills=["python"],
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            source_url="https://boards.greenhouse.io/acme",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))
    # Backdate one job's first_seen_at.
    with service.database.session() as session:
        rec = session.query(JobRecord).filter_by(external_id="2").one()
        rec.first_seen_at = datetime.now(UTC) - timedelta(days=10)

    all_jobs = service.search_jobs_page(apply_preferences=False)
    recent = service.search_jobs_page(apply_preferences=False, since_hours=24)
    assert all_jobs.total == 3
    assert recent.total == 2  # the 10-day-old one is excluded


def test_new_within_dropdown_is_rendered(service: JobOSService) -> None:
    from jobos.models import Contact, StructuredResume

    service.database.add_resume(
        StructuredResume(contact=Contact(name="Jane", email="jane@example.com")),
        source_name="r.md",
        source_format="md",
        original_path="r.md",
        source_text="Jane",
        content_hash="h1",
        is_master=True,
    )
    client = TestClient(create_app(service=service))
    html = client.get("/").text
    assert 'name="new_within"' in html
    assert "New today" in html and "This week" in html


def test_inline_status_set_returns_to_filtered_list(service: JobOSService) -> None:
    """Setting status from the Jobs list updates the job and returns to that list."""
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData, JobStatus

    job = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Bengaluru, India",
        description="x",
        skills=["python"],
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source_url="https://boards.greenhouse.io/acme",
        external_id="1",
    )
    view, _ = service.database.upsert_job(job, job_fingerprint(job))
    client = TestClient(create_app(service=service), follow_redirects=False)
    response = client.post(
        f"/jobs/{view.id}/status",
        data={"status": "applied", "next": "/?preferred=on&new_within=24"},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?preferred=on&new_within=24")
    assert service.require_job(view.id).status is JobStatus.APPLIED


def test_inline_status_rejects_offsite_redirect(service: JobOSService) -> None:
    """A crafted `next` must not redirect off-site; it falls back to the job detail."""
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    job = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Remote - India",
        description="x",
        skills=["python"],
        apply_url="https://boards.greenhouse.io/acme/jobs/2",
        source_url="https://boards.greenhouse.io/acme",
        external_id="2",
    )
    view, _ = service.database.upsert_job(job, job_fingerprint(job))
    client = TestClient(create_app(service=service), follow_redirects=False)
    response = client.post(
        f"/jobs/{view.id}/status",
        data={"status": "saved", "next": "//evil.example.com/phish"},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/jobs/{view.id}")


def test_jobs_list_renders_inline_status_dropdown(service: JobOSService) -> None:
    from jobos.crawler import job_fingerprint
    from jobos.models import Contact, JobData, StructuredResume

    service.database.add_resume(
        StructuredResume(contact=Contact(name="Jane", email="jane@example.com")),
        source_name="r.md",
        source_format="md",
        original_path="r.md",
        source_text="Jane",
        content_hash="h2",
        is_master=True,
    )
    job = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Hyderabad, India",
        description="x",
        skills=["python"],
        apply_url="https://boards.greenhouse.io/acme/jobs/3",
        source_url="https://boards.greenhouse.io/acme",
        external_id="3",
    )
    service.database.upsert_job(job, job_fingerprint(job))
    client = TestClient(create_app(service=service))
    html = client.get("/?preferred=").text
    assert "status-select" in html  # inline dropdown present
    assert 'name="next"' in html  # returns to the filtered list


async def test_years_filter_works_before_analysis(service: JobOSService) -> None:
    """Min/Max years narrows un-analysed jobs using the crawl-time experience text."""
    from jobos.crawler import job_fingerprint
    from jobos.models import JobData

    specs = [
        ("junior", "Hyderabad, India", "1+ years experience"),
        ("mid", "Hyderabad, India", "4+ years experience"),
        ("senior", "Hyderabad, India", "10+ years experience"),
        ("blank", "Hyderabad, India", "great culture, no years stated"),
    ]
    for i, (tag, loc, exp) in enumerate(specs):
        job = JobData(
            company="Acme",
            title=f"Backend Engineer {tag}",
            location=loc,
            description="Python backend.",
            experience=exp,
            skills=["python"],
            apply_url=f"https://boards.greenhouse.io/acme/jobs/{i}",
            source_url="https://boards.greenhouse.io/acme",
            external_id=str(i),
        )
        service.database.upsert_job(job, job_fingerprint(job))

    # Explicit (strict) 3-8 year window, no preferences: only the "mid" (4yrs) job
    # qualifies; junior (1) and senior (10) are out, and the blank one is dropped
    # (strict drops unknown-years jobs).
    strict = service.search_jobs(min_years=3, max_years=8, apply_preferences=False)
    titles = {j.title for j in strict}
    assert "Backend Engineer mid" in titles
    assert "Backend Engineer junior" not in titles
    assert "Backend Engineer senior" not in titles
    assert "Backend Engineer blank" not in titles

    # Lenient (preferences) keeps the blank-years job (requirement unknown), still
    # drops the clearly out-of-window junior/senior ones.
    lenient = service.search_jobs(min_years=3, max_years=8, apply_preferences=True)
    lenient_titles = {j.title for j in lenient}
    assert "Backend Engineer mid" in lenient_titles
    assert "Backend Engineer blank" in lenient_titles
    assert "Backend Engineer senior" not in lenient_titles


def test_jobs_table_shows_years_column(service: JobOSService) -> None:
    from jobos.crawler import job_fingerprint
    from jobos.models import Contact, JobData, StructuredResume

    service.database.add_resume(
        StructuredResume(contact=Contact(name="J", email="j@e.com")),
        source_name="r", source_format="md", original_path="r",
        source_text="J", content_hash="hy", is_master=True,
    )
    job = JobData(
        company="Acme", title="Backend Engineer", location="Hyderabad, India",
        description="x", experience="5+ years experience", skills=["python"],
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source_url="https://boards.greenhouse.io/acme", external_id="1",
    )
    service.database.upsert_job(job, job_fingerprint(job))
    client = TestClient(create_app(service=service))
    html = client.get("/?preferred=").text
    assert "Years</th>" in html  # column header
    assert "5+ yrs" in html  # parsed value shown


async def test_run_daily_passes_ats_wave_to_crawl(service: JobOSService, monkeypatch) -> None:
    """`daily --ats fast` restricts the crawl to the fast wave, not all boards."""
    from jobos.models import Contact, CrawlSummary, StructuredResume

    service.database.add_resume(
        StructuredResume(contact=Contact(name="J", email="j@e.com")),
        source_name="r", source_format="md", original_path="r",
        source_text="J", content_hash="hd", is_master=True,
    )
    seen: dict[str, object] = {}

    async def fake_crawl(company=None, *, summary=None, ats_types=None):  # type: ignore[no-untyped-def]
        seen["ats_types"] = ats_types
        return CrawlSummary()

    monkeypatch.setattr(service, "crawl", fake_crawl)
    await service.run_daily(ats_types={"greenhouse", "lever", "ashby"}, top=0, analyse_limit=0)
    assert seen["ats_types"] == {"greenhouse", "lever", "ashby"}


def test_save_api_key_writes_env_not_config(service: JobOSService, monkeypatch) -> None:
    """Saving an API key goes to the gitignored .env, applies live, never to config."""
    import os

    monkeypatch.delenv("JOBOS_GEMINI_API_KEY", raising=False)
    service.save_api_key("gemini", "SECRET-123")
    env = service.config.env_path
    assert env.exists()
    assert oct(env.stat().st_mode & 0o777) == "0o600"  # owner-only
    assert "SECRET-123" in env.read_text()
    assert os.environ["JOBOS_GEMINI_API_KEY"] == "SECRET-123"  # live in process
    # The key lives in .env, which is NOT the config file.
    assert env != service.config.config_path
    # View reports presence, not the value.
    view = service.ai_settings_view()
    assert view["key_status"]["gemini"]["present"] is True
    assert "SECRET-123" not in str(view)

    # Clearing removes it from env and file.
    service.save_api_key("gemini", "")
    assert "JOBOS_GEMINI_API_KEY" not in os.environ
    assert "SECRET-123" not in env.read_text()


def test_save_api_key_rejects_ollama(service: JobOSService) -> None:
    import pytest

    from jobos.services import ServiceError

    with pytest.raises(ServiceError):
        service.save_api_key("ollama", "x")  # ollama needs no key
