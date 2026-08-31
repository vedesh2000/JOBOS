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
