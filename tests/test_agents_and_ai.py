"""Job analysis, provider handling, and the token efficiency that small models need."""

from __future__ import annotations

import json

import httpx
import pytest

from jobos.agents import (
    JobAnalyzerAgent,
    ResumeAgent,
    analyze_job_offline,
    classify_skill_signals,
    requirement_excerpt,
)
from jobos.ai import AIClient, AIError, _compact_schema_hint, _extract_json
from jobos.config import AISettings
from jobos.models import JobAnalysis, JobView, StructuredResume
from tests.conftest import RecordingAI


def test_explicit_preferred_wording_wins(job: JobView) -> None:
    signals = classify_skill_signals(job)
    assert {"kubernetes", "terraform"} <= set(signals.preferred)
    assert {"Python", "FastAPI", "postgresql"} <= set(signals.required)


def test_offline_analysis_reads_years_and_seniority(job: JobView) -> None:
    analysis = analyze_job_offline(job)
    assert analysis.minimum_years == 4.0
    assert analysis.seniority == "Senior"
    assert analysis.responsibilities
    assert analysis.warnings


def test_skill_in_title_is_not_demoted() -> None:
    """A skill named in the title must still be found in the requirements line."""
    from datetime import UTC, datetime

    job = JobView(
        id=2,
        company="X",
        title="Senior Python Engineer",
        description="Required: Python and FastAPI. Build services.\n",
        apply_url="https://x.test/a",
        source_url="https://x.test/j",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    assert "python" in {skill.casefold() for skill in analyze_job_offline(job).required_skills}


def test_requirement_excerpt_drops_boilerplate(job: JobView) -> None:
    padded = job.model_copy(
        update={"description": ("We value collaboration.\n" * 80) + job.description}
    )
    excerpt = requirement_excerpt(padded, 1200)
    assert len(excerpt) <= 1200
    assert "Python and FastAPI" in excerpt
    assert excerpt.count("value collaboration") <= 1
    assert len(excerpt) < len(padded.description) / 4


def test_compact_schema_hint_names_keys() -> None:
    from jobos.agents import SummarySuggestion

    hint = _compact_schema_hint(SummarySuggestion.model_json_schema())
    assert "summary: string" in hint


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":1}', '{"a":1}'),
        ('```json\n{"a":1}\n```', '{"a":1}'),
        ('json```json\n{"a": {"b": 2}}\n```', '{"a": {"b": 2}}'),
        ('Sure: {"a":"}"}!', '{"a":"}"}'),
    ],
)
def test_json_extraction_tolerates_local_model_chatter(raw: str, expected: str) -> None:
    assert _extract_json(raw) == expected


@pytest.mark.parametrize("bad", ["", "no json", '{"a":1'])
def test_json_extraction_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        _extract_json(bad)


async def test_analysis_discards_skills_the_model_invents(job: JobView) -> None:
    fake = RecordingAI(
        [{"required": ["Python", "FastAPI", "Rust"], "preferred": [], "responsibilities": []}]
    )
    analysis = await JobAnalyzerAgent(fake.client()).analyze(job)
    assert "rust" not in {skill.casefold() for skill in analysis.required_skills}
    assert any("Ignored skills" in warning for warning in analysis.warnings)


async def test_model_cannot_promote_a_nice_to_have(job: JobView) -> None:
    fake = RecordingAI(
        [{"required": ["Python", "Kubernetes"], "preferred": [], "responsibilities": []}]
    )
    analysis = await JobAnalyzerAgent(fake.client()).analyze(job)
    assert "kubernetes" in {s.casefold() for s in analysis.preferred_skills}
    assert "kubernetes" not in {s.casefold() for s in analysis.required_skills}
    assert any("preferred because the posting says so" in w for w in analysis.warnings)


async def test_analysis_prompt_stays_small(job: JobView) -> None:
    fake = RecordingAI([{"required": [], "preferred": [], "responsibilities": []}])
    await JobAnalyzerAgent(fake.client()).analyze(job)
    request = fake.requests[-1]
    prompt = request["messages"][-1]["content"]
    assert len(prompt) < 3_000, f"prompt too large for a small model: {len(prompt)}"
    assert "OUTPUT_SCHEMA" not in prompt, "schema should not be duplicated for Ollama"
    assert request["think"] is False
    assert request["keep_alive"]
    assert request["options"]["num_predict"] == AISettings().max_output_tokens


async def test_bullet_rewrites_are_capped(resume: StructuredResume, job: JobView) -> None:
    settings = AISettings(max_bullet_rewrites=2)
    fake = RecordingAI([{"bullet_rewrites": [], "explanations": []}], settings)
    await ResumeAgent(fake.client()).propose(resume, analyze_job_offline(job))
    assert len(fake.last_payload["allowed_bullets"]) == 2


async def test_disabled_task_makes_no_network_call(job: JobView) -> None:
    settings = AISettings()
    settings.tasks.analyse_jobs = False
    fake = RecordingAI([], settings)
    analysis = await JobAnalyzerAgent(fake.client()).analyze(job)
    assert fake.requests == []
    assert analysis.required_skills


async def test_oversized_payload_is_refused_not_truncated() -> None:
    fake = RecordingAI([{}], AISettings(max_input_chars=500))
    with pytest.raises(AIError, match="budget"):
        await fake.client().generate(JobAnalysis, system="s", payload={"x": "y" * 5_000})


async def test_reasoning_only_response_gives_actionable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "", "thinking": "hmm..."}})

    client = AIClient(
        AISettings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AIError, match="reasoning"):
        await client.generate(JobAnalysis, system="s", payload={})


async def test_think_unsupported_falls_back() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "think" in body:
            return httpx.Response(400, text="does not support think")
        return httpx.Response(200, json={"message": {"content": '{"required_skills":[]}'}})

    client = AIClient(
        AISettings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await client.generate(JobAnalysis, system="s", payload={})
    assert result.required_skills == []
    assert len(calls) == 2 and "think" not in calls[1]


async def test_invalid_output_retries_once_then_fails() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(200, json={"message": {"content": "not json at all"}})

    client = AIClient(
        AISettings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(AIError, match="invalid structured output"):
        await client.generate(JobAnalysis, system="s", payload={})
    assert len(attempts) == 2, "expected exactly one bounded repair attempt"


async def test_cloud_provider_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBOS_OPENAI_API_KEY", raising=False)
    settings = AISettings(provider="openai", model="gpt-4o-mini")
    client = AIClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={})
    )))
    with pytest.raises(AIError, match="JOBOS_OPENAI_API_KEY"):
        await client.generate(JobAnalysis, system="s", payload={})


async def test_ollama_health_reports_missing_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "other-model:latest"}]})

    client = AIClient(
        AISettings(model="gemma3:12b"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    ok, detail = await client.health()
    assert ok is False and "not installed" in detail
