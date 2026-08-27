"""Resume import, structured parsing, and configuration loading."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from docx import Document
from pydantic import ValidationError

from jobos.config import AppConfig, load_config
from jobos.database import Database
from jobos.models import StructuredResume
from jobos.resume import (
    MAX_UPLOAD_BYTES,
    ResumeImporter,
    ResumeParseError,
    extract_resume_text,
    structure_resume_offline,
    unsupported_extractions,
)
from tests.conftest import RESUME_MARKDOWN


def test_markdown_extraction_and_structuring() -> None:
    text = extract_resume_text("master.md", RESUME_MARKDOWN.encode())
    resume = structure_resume_offline(text)
    assert resume.contact.name == "Jane Doe"
    assert resume.contact.email == "jane@example.com"
    assert "Hyderabad" in resume.contact.location
    assert resume.contact.links == ["https://github.com/janedoe"]
    assert {"Python", "FastAPI", "PostgreSQL"} <= set(resume.skills)
    assert [item.company for item in resume.experience] == ["Acme Corp", "Beta Labs"]
    assert resume.experience[0].start_date == "Jan 2021"
    assert resume.experience[0].end_date == "Present"
    assert len(resume.experience[0].bullets) == 3
    assert resume.projects[0].name == "Ledger Toolkit"
    assert resume.education[0].institution == "Example University"
    assert resume.certifications[0].name == "AWS Certified Developer"


def test_docx_extraction() -> None:
    document = Document()
    for line in RESUME_MARKDOWN.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    text = extract_resume_text("master.docx", buffer.getvalue())
    assert "Jane Doe" in text and "Acme Corp" in text


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("resume.txt", b"hello there this is plain text", "unsupported"),
        ("resume.md", b"", "empty"),
        ("resume.md", b"too short", "little"),
        ("resume.md", b"x" * (MAX_UPLOAD_BYTES + 1), "limit"),
    ],
)
def test_rejected_uploads(name: str, content: bytes, message: str) -> None:
    with pytest.raises(ResumeParseError, match=message):
        extract_resume_text(name, content)


def test_structuring_requires_a_name() -> None:
    with pytest.raises(ResumeParseError, match="name"):
        structure_resume_offline("## Skills\nPython, SQL\n\n## Experience\nSomewhere")


def test_unsupported_extractions_detects_invented_values(resume: StructuredResume) -> None:
    source = "Jane Doe\nAcme Corp\nSenior Software Engineer\nPython"
    findings = unsupported_extractions(resume, source)
    assert any("Example University" in item for item in findings)
    assert not any(item.startswith("name:") for item in findings)


async def test_importer_stores_master_and_warns(config: AppConfig, database: Database) -> None:
    importer = ResumeImporter(config, database)
    result = await importer.import_bytes("master.md", RESUME_MARKDOWN.encode())
    assert result.version_id > 0
    assert any("offline parser" in warning for warning in result.warnings)
    master = database.get_master_resume()
    assert master is not None and master.resume.contact.name == "Jane Doe"
    assert Path(master.original_path).is_file(), "original upload should be kept"


async def test_new_upload_supersedes_previous_master(
    config: AppConfig, database: Database
) -> None:
    importer = ResumeImporter(config, database)
    first = await importer.import_bytes("a.md", RESUME_MARKDOWN.encode())
    second = await importer.import_bytes(
        "b.md", RESUME_MARKDOWN.replace("Jane Doe", "Jane A Doe").encode()
    )
    master = database.get_master_resume()
    assert master is not None and master.id == second.version_id
    versions = {version.id: version for version in database.list_resume_versions()}
    assert versions[first.version_id].is_master is False
    assert versions[first.version_id].resume.contact.name == "Jane Doe", "history preserved"


async def test_ai_structuring_that_invents_facts_is_discarded(
    config: AppConfig, database: Database
) -> None:
    class Liar:
        async def structure_resume(self, source_text: str) -> StructuredResume:
            resume = structure_resume_offline(source_text)
            resume.experience[0].company = "Google"
            return resume

    importer = ResumeImporter(config, database, Liar())
    result = await importer.import_bytes("master.md", RESUME_MARKDOWN.encode())
    assert result.resume.experience[0].company == "Acme Corp"
    assert any("unsupported values" in warning for warning in result.warnings)


def test_config_accepts_short_and_extended_company_forms(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "companies": {
                    "stripe": "https://stripe.com/jobs",
                    "microsoft": {
                        "name": "Microsoft",
                        "url": "https://careers.microsoft.com/",
                        "enabled": False,
                    },
                }
            }
        )
    )
    config = load_config(path)
    assert config.companies["stripe"].name == "Stripe"
    assert config.companies["stripe"].url == "https://stripe.com/jobs"
    assert config.companies["microsoft"].enabled is False
    assert config.data_dir == (tmp_path / ".jobos").resolve()


def test_config_treats_empty_companies_as_none(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("companies:\n")
    assert load_config(path).companies == {}


def test_config_rejects_bad_shapes(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("companies:\n  - a\n  - b\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)

    path.write_text("companies:\n  acme: not-a-url\n")
    with pytest.raises(ValidationError):
        load_config(path)

    path.write_text("unknown_key: 1\n")
    with pytest.raises(ValidationError):
        load_config(path)


def test_missing_config_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        load_config(tmp_path / "absent.yaml")


def test_shipped_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    raw = yaml.safe_load(example.read_text()) or {}
    raw["config_path"] = example
    config = AppConfig.model_validate(raw)
    assert config.ai.provider == "ollama"
    assert config.companies == {}
    assert config.ai.tasks.structure_resume is True


def test_cloud_api_keys_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobos.config import AISettings

    settings = AISettings(provider="openai", model="gpt-4o-mini")
    monkeypatch.delenv("JOBOS_OPENAI_API_KEY", raising=False)
    assert settings.api_key is None
    monkeypatch.setenv("JOBOS_OPENAI_API_KEY", "secret-value")
    assert settings.api_key == "secret-value"
    assert "secret-value" not in settings.model_dump_json()
