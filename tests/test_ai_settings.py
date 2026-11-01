"""Tests for the AI settings page: saving edits the ai: block only, preserves
comments elsewhere, never stores API keys, and keeps config valid."""

from __future__ import annotations

import pytest

from jobos.config import load_config, replace_top_level_block
from jobos.services import JobOSService


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    text = """data_dir: {data}
output_dir: {out}

ai:
  provider: ollama
  model: llama3.2
  # keep summary verbatim
  tasks:
    rewrite_summary: false

preferences:
  minimum_match_score: 45

# === Company annotations worth preserving ===
companies:
  okta: https://boards.greenhouse.io/okta   # 113 IN / 49 SR
""".format(data=tmp_path / "data", out=tmp_path / "out")
    path = tmp_path / "config.yaml"
    path.write_text(text)
    monkeypatch.setenv("JOBOS_CONFIG", str(path))
    return path


def test_replace_top_level_block_preserves_other_sections() -> None:
    text = "ai:\n  model: a\n\ncompanies:\n  x: url  # keep me\n"
    out = replace_top_level_block(text, "ai", "ai:\n  model: b\n")
    assert "model: b" in out
    assert "# keep me" in out
    assert "x: url" in out


def test_replace_top_level_block_raises_when_missing() -> None:
    with pytest.raises(ValueError):
        replace_top_level_block("companies:\n  x: y\n", "ai", "ai:\n  model: b\n")


def test_save_ai_settings_preserves_company_comments(config_file) -> None:
    service = JobOSService(load_config(config_file))
    service.save_ai_settings({"provider": "ollama", "temperature": 0.3, "rewrite_bullets": False})
    saved = config_file.read_text()
    # The company annotation must survive a targeted ai: edit.
    assert "113 IN / 49 SR" in saved
    assert "Company annotations worth preserving" in saved
    assert "temperature: 0.3" in saved
    # Reloads cleanly with the change applied.
    reloaded = load_config(config_file)
    assert reloaded.ai.temperature == 0.3
    assert reloaded.ai.tasks.rewrite_bullets is False
    assert "okta" in reloaded.companies


def test_save_ai_settings_rejects_unknown_provider(config_file) -> None:
    service = JobOSService(load_config(config_file))
    with pytest.raises(Exception) as exc:
        service.save_ai_settings({"provider": "bogus"})
    assert "provider" in str(exc.value).casefold()


def test_ai_view_reports_key_presence_without_values(config_file, monkeypatch) -> None:
    monkeypatch.setenv("JOBOS_OPENAI_API_KEY", "super-secret-value")
    service = JobOSService(load_config(config_file))
    view = service.ai_settings_view()
    assert view["key_status"]["openai"]["present"] is True
    assert view["key_status"]["openai"]["variable"] == "JOBOS_OPENAI_API_KEY"
    # The secret value is never part of the view.
    assert "super-secret-value" not in str(view)


def test_ai_view_lists_providers_and_tasks(config_file) -> None:
    service = JobOSService(load_config(config_file))
    view = service.ai_settings_view()
    assert "ollama" in view["providers"] and "openai" in view["providers"]
    assert set(view["tasks"]) == {
        "structure_resume",
        "analyse_jobs",
        "rewrite_summary",
        "rewrite_bullets",
        "cover_letter",
    }
