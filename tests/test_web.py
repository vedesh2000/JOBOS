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

    async def fake_start(company: str | None = None) -> None:
        started["company"] = company
        service.crawl_progress = CrawlProgress(running=True, label=company or "all")

    monkeypatch.setattr(service, "start_background_crawl", fake_start)
    client = TestClient(create_app(service=service))

    response = client.post(
        "/crawl", data={"company": "acme"}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 202
    assert response.json()["running"] is True
    assert started["company"] == "acme"


def test_crawl_start_conflict_is_409_json(service: JobOSService) -> None:
    """A second start while one is running is a clean 409 the poller can surface."""
    service.crawl_progress = CrawlProgress(running=True, label="busy")
    client = TestClient(create_app(service=service))
    response = client.post("/crawl", data={"company": ""}, headers={"Accept": "application/json"})
    assert response.status_code == 409
    assert "already running" in response.json()["detail"].lower()


def test_plain_crawl_post_still_redirects(service: JobOSService, monkeypatch) -> None:
    """Without JS (no JSON Accept), the form post keeps its redirect behaviour."""

    async def fake_start(company: str | None = None) -> None:
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

    async def fake_company_crawl(company: str | None = None) -> None:
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
