"""Board discovery, validation and the sidecar registry.

Every test is offline: candidate boards are validated through the real adapters
driven by ``httpx.MockTransport``, so the "never trust HTTP 200" rule is exercised
without touching the network.
"""

from __future__ import annotations

import httpx

from jobos.config import AppConfig, CompanySettings, load_config
from jobos.crawler import detect_ats_type
from jobos.database import Database
from jobos.discovery import (
    dedupe_against,
    load_candidates_file,
    load_seed_candidates,
)
from jobos.models import BoardStatus, BoardValidation
from jobos.registry import CompanyRegistry
from jobos.services import JobOSService
from jobos.validation import BoardValidator, is_india_location, is_target_title

# ----- pure helpers ---------------------------------------------------------


def test_detect_ats_type_covers_every_adapter() -> None:
    assert detect_ats_type("https://boards.greenhouse.io/okta") == "greenhouse"
    assert detect_ats_type("https://job-boards.greenhouse.io/okta") == "greenhouse"
    assert detect_ats_type("https://jobs.lever.co/meesho") == "lever"
    assert detect_ats_type("https://jobs.ashbyhq.com/tekion") == "ashby"
    assert detect_ats_type("https://jobs.smartrecruiters.com/Acme") == "smartrecruiters"
    assert detect_ats_type("https://acme.wd1.myworkdayjobs.com/en-US/External") == "workday"
    assert (
        detect_ats_type(
            "https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX"
        )
        == "oracle"
    )
    # No path segment, or an unsupported host, is not a supported board.
    assert detect_ats_type("https://boards.greenhouse.io") == ""
    assert detect_ats_type("https://careers.example.com/jobs") == ""
    # An Oracle Cloud host that is not a recruiting candidate-experience URL is not a board.
    assert detect_ats_type("https://ebwg.fa.us2.oraclecloud.com/fscmUI/faces/x") == ""


def test_india_and_target_matchers() -> None:
    assert is_india_location("Bengaluru, India")
    assert is_india_location("Hyderabad")
    assert is_india_location("Remote - India")
    assert not is_india_location("Remote")
    assert not is_india_location("San Francisco, CA")
    assert is_target_title("Senior Backend Engineer")
    assert is_target_title("SDE II")
    assert is_target_title("Full Stack Developer")
    assert not is_target_title("Engineering Manager")
    assert not is_target_title("Data Scientist")


# ----- validation -----------------------------------------------------------


def _gh(jobs: list[dict[str, object]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boards-api.greenhouse.io" in str(request.url)
        return httpx.Response(200, json={"jobs": jobs})

    return httpx.MockTransport(handler)


def _role(job_id: int, title: str, location: str) -> dict[str, object]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": location},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "&lt;p&gt;Python. 4+ years experience.&lt;/p&gt;",
    }


async def test_valid_board_with_india_and_target_roles(
    config: AppConfig, database: Database
) -> None:
    jobs = [
        _role(1, "Senior Backend Engineer", "Bengaluru, India"),
        _role(2, "Platform Engineer", "Hyderabad, India"),
        _role(3, "Sales Director", "London, UK"),
    ]
    async with httpx.AsyncClient(transport=_gh(jobs)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert result.status is BoardStatus.VALID
    assert result.ats_type == "greenhouse"
    assert result.india_role_count == 2
    assert result.relevant_role_count == 2  # the director is neither India nor target
    assert result.adoptable


async def test_board_with_no_india_roles_is_irrelevant(
    config: AppConfig, database: Database
) -> None:
    jobs = [_role(1, "Senior Backend Engineer", "Berlin, Germany")]
    async with httpx.AsyncClient(transport=_gh(jobs)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert result.status is BoardStatus.IRRELEVANT
    assert not result.adoptable


async def test_empty_board_is_not_valid(config: AppConfig, database: Database) -> None:
    async with httpx.AsyncClient(transport=_gh([])) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert result.status is BoardStatus.EMPTY
    assert not result.adoptable


async def test_http_200_with_wrong_shape_is_not_valid(
    config: AppConfig, database: Database
) -> None:
    """A 200 that is not the ATS's own JSON shape must never count as a valid board."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Greenhouse adapter expects {"jobs": [...]}; an HTML login page is 200 but wrong.
        return httpx.Response(200, text="<html><body>Please log in</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert result.status is BoardStatus.MALFORMED
    assert not result.adoptable


async def test_unsupported_host_is_flagged_not_scraped(
    config: AppConfig, database: Database
) -> None:
    validator = BoardValidator(config, database, allow_private_networks=True)
    result = await validator.validate_one(
        CompanySettings(name="Custom Co", url="https://careers.customco.com/jobs")
    )
    assert result.status is BoardStatus.UNSUPPORTED
    assert result.ats_type == ""
    assert not result.adoptable


async def test_unreachable_board_is_reported(config: AppConfig, database: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme")
        )
    assert result.status is BoardStatus.UNREACHABLE


async def test_validate_candidates_aggregates_a_summary(
    config: AppConfig, database: Database
) -> None:
    good = _role(1, "Backend Engineer", "Pune, India")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/good/" in str(request.url):
            return httpx.Response(200, json={"jobs": [good]})
        return httpx.Response(200, json={"jobs": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        summary = await validator.validate_candidates(
            [
                CompanySettings(name="Good", url="https://boards.greenhouse.io/good"),
                CompanySettings(name="Empty", url="https://boards.greenhouse.io/empty"),
                CompanySettings(name="Custom", url="https://careers.custom.com/jobs"),
            ]
        )
    assert summary.candidates == 3
    assert summary.valid == 1
    assert summary.empty == 1
    assert summary.unsupported == 1
    assert summary.india_roles == 1


# ----- registry -------------------------------------------------------------


def test_registry_roundtrip_and_coverage(tmp_path, config: AppConfig) -> None:
    from jobos.models import BoardValidation

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    registry = CompanyRegistry.load(data_dir)
    registry.record_validation(
        BoardValidation(
            slug="good",
            name="Good",
            url="https://boards.greenhouse.io/good",
            ats_type="greenhouse",
            status=BoardStatus.VALID,
            india_role_count=5,
            relevant_role_count=2,
        )
    )
    registry.record_validation(
        BoardValidation(
            slug="custom",
            name="Custom",
            url="https://careers.custom.com/jobs",
            status=BoardStatus.UNSUPPORTED,
        )
    )
    registry.save()

    reloaded = CompanyRegistry.load(data_dir)
    assert set(reloaded.entries) == {"good", "custom"}
    coverage = reloaded.coverage({"good", "already_configured"})
    assert coverage.configured_boards == 2
    assert coverage.validated_boards == 1
    assert coverage.india_roles == 5
    assert coverage.relevant_roles == 2
    assert coverage.unsupported_boards == 1
    assert coverage.flagged_for_review == ["Custom"]


def test_corrupt_registry_is_treated_as_empty(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "company_registry.json").write_text("{not valid json", encoding="utf-8")
    registry = CompanyRegistry.load(data_dir)
    assert registry.entries == {}


# ----- discovery seed -------------------------------------------------------


def test_seed_loads_only_supported_ats_urls() -> None:
    candidates = load_seed_candidates()
    assert len(candidates) >= 300, "seed should hold a substantial validated set"
    # Every seeded board must be on a host JobOS actually has an adapter for, so a
    # discovery run never proposes a board it cannot crawl.
    assert all(detect_ats_type(c.url) for c in candidates)


def test_candidates_file_accepts_urls_and_mappings(tmp_path) -> None:
    mapping = tmp_path / "boards.yaml"
    mapping.write_text(
        "candidates:\n  acme: https://boards.greenhouse.io/acme\n", encoding="utf-8"
    )
    from_mapping = load_candidates_file(mapping)
    assert len(from_mapping) == 1
    assert from_mapping[0].url == "https://boards.greenhouse.io/acme"

    lines = tmp_path / "boards.txt"
    lines.write_text(
        "# a comment\nhttps://jobs.lever.co/meesho\nhttps://jobs.ashbyhq.com/tekion\n",
        encoding="utf-8",
    )
    from_lines = load_candidates_file(lines)
    assert {c.url for c in from_lines} == {
        "https://jobs.lever.co/meesho",
        "https://jobs.ashbyhq.com/tekion",
    }


def test_dedupe_against_configured_slugs() -> None:
    candidates = [
        CompanySettings(name="Acme", url="https://boards.greenhouse.io/acme"),
        CompanySettings(name="Beta", url="https://boards.greenhouse.io/beta"),
    ]
    kept = dedupe_against(candidates, {"acme"})
    assert [c.name for c in kept] == ["Beta"]


# ----- service: adopt + rollback-friendliness -------------------------------


def _write_config(tmp_path) -> AppConfig:
    text = (
        "data_dir: data\n"
        "output_dir: out\n"
        "ai:\n  provider: ollama\n  model: llama3.2\n"
        "companies:\n"
        "  acme: https://boards.greenhouse.io/acme\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_adopt_writes_only_adoptable_boards_into_config(tmp_path) -> None:
    from jobos.models import BoardValidation

    config = _write_config(tmp_path)
    service = JobOSService(config, Database(config.database_path))
    service.database.initialize()
    results = [
        BoardValidation(
            slug="beta",
            name="Beta",
            url="https://boards.greenhouse.io/beta",
            ats_type="greenhouse",
            status=BoardStatus.VALID,
            india_role_count=3,
            relevant_role_count=2,
        ),
        BoardValidation(
            slug="empty",
            name="Empty",
            url="https://boards.greenhouse.io/empty",
            ats_type="greenhouse",
            status=BoardStatus.EMPTY,
        ),
    ]
    added = service.adopt_boards(results)
    assert added == ["beta"]
    # The addition is in the file (so it survives a reload) and nothing else changed.
    reloaded = load_config(config.config_path)
    assert "beta" in reloaded.companies
    assert "empty" not in reloaded.companies
    assert "acme" in reloaded.companies  # existing board untouched
    # Idempotent: re-adopting the same result does not duplicate it.
    assert service.adopt_boards(results) == []


def test_reload_config_if_changed_picks_up_external_edits(tmp_path) -> None:
    """A long-running server must see config.yaml edits made by another process."""
    import os
    import time

    config = _write_config(tmp_path)
    service = JobOSService(config, Database(config.database_path))
    service.database.initialize()
    assert set(service.config.companies) == {"acme"}

    # An unchanged file is not re-parsed.
    assert service.reload_config_if_changed() is False

    # Another process (CLI adopt, hand edit) appends a board to the file.
    text = config.config_path.read_text(encoding="utf-8")
    config.config_path.write_text(
        text + "  beta: https://boards.greenhouse.io/beta\n", encoding="utf-8"
    )
    # Ensure a distinct mtime even on coarse-grained filesystems.
    future = time.time() + 2
    os.utime(config.config_path, (future, future))

    assert service.reload_config_if_changed() is True
    assert set(service.config.companies) == {"acme", "beta"}


def test_reload_config_ignores_a_broken_edit(tmp_path) -> None:
    """A config mid-edit must not crash the page; the last good one stays live."""
    import os
    import time

    config = _write_config(tmp_path)
    service = JobOSService(config, Database(config.database_path))
    service.database.initialize()

    config.config_path.write_text("companies:\n  : : broken yaml : :\n", encoding="utf-8")
    future = time.time() + 2
    os.utime(config.config_path, (future, future))

    # Returns False and keeps the previous company map rather than raising.
    assert service.reload_config_if_changed() is False
    assert set(service.config.companies) == {"acme"}


# ----- company classification & category coverage ---------------------------


def test_classify_company_by_name_and_ats() -> None:
    from jobos.classification import classify_company
    from jobos.models import CompanyCategory

    assert classify_company("Cognizant", "smartrecruiters") is CompanyCategory.IT_SERVICES
    assert classify_company("JPMorgan Chase", "workday") is CompanyCategory.FINANCE
    assert classify_company("Google", "greenhouse") is CompanyCategory.MNC_TECH
    assert classify_company("Walmart Global Tech", "eightfold") is CompanyCategory.GCC
    assert classify_company("Salesforce", "workday") is CompanyCategory.ENTERPRISE_SAAS
    # Name wins over the ATS prior: a known startup-ATS name still classifies by name.
    assert classify_company("Infosys", "ashby") is CompanyCategory.IT_SERVICES
    # Unknown name falls back to the ATS prior, never crashes.
    assert classify_company("Totally Unknown Co", "ashby") is CompanyCategory.STARTUP
    assert classify_company("Totally Unknown Co", "") is CompanyCategory.UNKNOWN
    # remote_first only applies when no name signal matched.
    assert (
        classify_company("Totally Unknown Co", "lever", remote_first=True)
        is CompanyCategory.REMOTE_FIRST
    )
    assert classify_company("Google", "", remote_first=True) is CompanyCategory.MNC_TECH


async def test_classification_never_gates_a_role(
    config: AppConfig, database: Database
) -> None:
    """Category is informational: a role on an 'unknown' company still validates."""
    jobs = [_role(1, "Senior Backend Engineer", "Pune, India")]
    async with httpx.AsyncClient(transport=_gh(jobs)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(
                name="Nobody Has Heard Of This", url="https://boards.greenhouse.io/x"
            )
        )
    assert result.status is BoardStatus.VALID
    assert result.adoptable  # unknown/greenhouse category does not block adoption


def test_coverage_report_breaks_out_by_category(tmp_path) -> None:
    from jobos.models import BoardValidation, CompanyCategory

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    registry = CompanyRegistry.load(data_dir)
    registry.record_validation(
        BoardValidation(
            slug="cognizant", name="Cognizant", url="https://jobs.smartrecruiters.com/Cognizant",
            ats_type="smartrecruiters", category=CompanyCategory.IT_SERVICES,
            status=BoardStatus.VALID, india_role_count=20, relevant_role_count=8,
        )
    )
    registry.record_validation(
        BoardValidation(
            slug="tinyco", name="TinyCo", url="https://jobs.ashbyhq.com/tinyco",
            ats_type="ashby", category=CompanyCategory.STARTUP,
            status=BoardStatus.VALID, india_role_count=3, relevant_role_count=2,
        )
    )
    registry.save()

    coverage = CompanyRegistry.load(data_dir).coverage({"cognizant", "tinyco"})
    cats = {item.category: item for item in coverage.by_category}
    assert cats[CompanyCategory.IT_SERVICES].boards == 1
    assert cats[CompanyCategory.IT_SERVICES].relevant_roles == 8
    assert cats[CompanyCategory.STARTUP].boards == 1
    # Both segments are reported side by side (requirement 8).
    assert len(coverage.by_category) == 2
    # Major-employer checklist flags the obvious names still missing.
    assert "Google" in coverage.unsupported_major_employers
    assert "Infosys" in coverage.unsupported_major_employers


def test_enterprise_seed_targets_enterprise_ats() -> None:
    from jobos.crawler import detect_ats_type
    from jobos.discovery import load_seed_candidates

    enterprise = load_seed_candidates("enterprise")
    assert len(enterprise) >= 100
    # The enterprise seed must only reach employers via already-supported JSON
    # adapters (SmartRecruiters / Workday), never the brittle generic path.
    ats = {detect_ats_type(c.url) for c in enterprise}
    assert ats <= {"smartrecruiters", "workday"}
    assert "workday" in ats and "smartrecruiters" in ats


def test_all_seed_is_the_union_without_duplicates() -> None:
    from jobos.config import slugify_company
    from jobos.discovery import load_seed_candidates

    main = load_seed_candidates("main")
    enterprise = load_seed_candidates("enterprise")
    oracle = load_seed_candidates("oracle")
    combined = load_seed_candidates("all")
    assert len(combined) <= len(main) + len(enterprise) + len(oracle)
    slugs = [slugify_company(c.name) for c in combined]
    assert len(slugs) == len(set(slugs))  # no duplicate slugs across seeds


async def test_incremental_adoption_writes_each_board_as_it_passes(tmp_path) -> None:
    """With adopt_incrementally, a valid board is in config.yaml before the run ends."""
    config = _write_config(tmp_path)
    service = JobOSService(config, Database(config.database_path))
    service.database.initialize()

    good = {"jobs": [_role(1, "Senior Backend Engineer", "Bengaluru, India")]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=good)

    seen_in_config: list[bool] = []

    def on_result(done: int, total: int, result: BoardValidation) -> None:
        # By the time the callback fires for a passing board, it is already persisted.
        seen_in_config.append("beta" in load_config(config.config_path).companies)

    candidates = [CompanySettings(name="Beta", url="https://boards.greenhouse.io/beta")]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        from jobos.validation import BoardValidator

        validator = BoardValidator(config, service.database, client=client, allow_private_networks=True)

        # Drive the service's incremental handler by hand through the validator so the
        # test stays offline but exercises the real adoption-on-pass path.
        registry = service._registry()

        def handle(done: int, total: int, result: BoardValidation) -> None:
            registry.record_validation(result)
            if result.adoptable:
                service._adopt_one(result)
            registry.save()
            on_result(done, total, result)

        await validator.validate_candidates(candidates, on_result=handle)

    # The board is on disk (durable) even though we never called a final batch adopt.
    assert "beta" in load_config(config.config_path).companies
    assert seen_in_config == [True]


def test_adopt_one_is_idempotent(tmp_path) -> None:
    config = _write_config(tmp_path)
    service = JobOSService(config, Database(config.database_path))
    service.database.initialize()
    from jobos.models import BoardValidation, CompanyCategory

    result = BoardValidation(
        slug="gamma", name="Gamma", url="https://boards.greenhouse.io/gamma",
        ats_type="greenhouse", category=CompanyCategory.GROWTH,
        status=BoardStatus.VALID, india_role_count=2, relevant_role_count=1,
    )
    assert service._adopt_one(result) is True
    assert service._adopt_one(result) is False  # already present, no duplicate
    assert "gamma" in load_config(config.config_path).companies


def test_resolve_ats_wave_aliases_and_lists() -> None:
    from jobos.cli import _resolve_ats_wave

    assert _resolve_ats_wave("") is None  # no filter = all
    assert _resolve_ats_wave("fast") == {"greenhouse", "lever", "ashby"}
    assert _resolve_ats_wave("medium") == {"smartrecruiters", "oracle"}
    assert _resolve_ats_wave("slow") == {"workday"}
    assert _resolve_ats_wave("greenhouse,oracle") == {"greenhouse", "oracle"}
    # Unknown ATS names are rejected rather than silently crawling nothing.
    import pytest
    import typer

    with pytest.raises(typer.Exit):
        _resolve_ats_wave("nonsense")


def test_is_gcc_identifies_known_parents() -> None:
    from jobos.classification import is_gcc

    assert is_gcc("Wells Fargo") is True
    assert is_gcc("Optum Global Solutions") is True
    assert is_gcc("Bosch Group") is True
    assert is_gcc("Williams-Sonoma") is True
    # A pure tech-native startup is not a GCC.
    assert is_gcc("Some Random Startup") is False
    assert is_gcc("Acme") is False


def test_oracle_seed_is_oracle_and_tags_gcc() -> None:
    from jobos.classification import is_gcc
    from jobos.crawler import detect_ats_type
    from jobos.discovery import load_seed_candidates

    oracle = load_seed_candidates("oracle")
    assert oracle, "oracle seed should be packaged"
    assert all(detect_ats_type(c.url) == "oracle" for c in oracle)
    # At least some are recognised GCC parents.
    assert any(is_gcc(c.name) for c in oracle)


async def test_validation_tags_gcc_and_registry_counts_it(
    config: AppConfig, database: Database
) -> None:
    """A GCC-parent board validates with is_gcc set, and coverage counts it."""
    jobs = [_role(1, "Senior Backend Engineer", "Hyderabad, India")]
    async with httpx.AsyncClient(transport=_gh(jobs)) as client:
        validator = BoardValidator(config, database, client=client, allow_private_networks=True)
        result = await validator.validate_one(
            CompanySettings(name="Wells Fargo", url="https://boards.greenhouse.io/wf")
        )
    assert result.status is BoardStatus.VALID
    assert result.is_gcc is True

    registry = CompanyRegistry.load(config.data_dir)
    registry.record_validation(result)
    registry.save()
    coverage = CompanyRegistry.load(config.data_dir).coverage({"wells_fargo"})
    assert coverage.gcc_boards == 1


def test_parent_key_dedups_india_subsidiaries() -> None:
    from jobos.services import _parent_key

    assert _parent_key("Wells Fargo") == _parent_key("Wells Fargo India Solutions Pvt Ltd")
    assert _parent_key("Bosch Group") == _parent_key("Bosch Global Software Technologies")
    # Different parents must not collapse.
    assert _parent_key("Citi") != _parent_key("Barclays")


def test_gcc_report_counts_and_dedups(tmp_path) -> None:
    from jobos.models import BoardValidation, CompanyCategory

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    registry = CompanyRegistry.load(data_dir)
    # Two boards for the same GCC parent (must dedup to one employer), plus one other.
    for slug, name, ats, india in [
        ("wf1", "Wells Fargo", "workday", 30),
        ("wf2", "Wells Fargo India Solutions", "oracle", 12),
        ("bosch", "Bosch Group", "smartrecruiters", 40),
    ]:
        registry.record_validation(
            BoardValidation(
                slug=slug, name=name, url=f"https://x/{slug}", ats_type=ats,
                category=CompanyCategory.GCC, is_gcc=True,
                status=BoardStatus.VALID, india_role_count=india, relevant_role_count=5,
            )
        )
    registry.save()

    from jobos.config import AISettings, AppConfig, CompanySettings

    cfg = AppConfig(
        data_dir=data_dir,
        output_dir=tmp_path / "out",
        ai=AISettings(),
        companies={"wf1": CompanySettings(name="Wells Fargo", url="https://x/wf1")},
        config_path=tmp_path / "config.yaml",
    )
    service = JobOSService(cfg, Database(cfg.database_path))
    report = service.gcc_report()
    assert report.gcc_candidates_seen == 3
    assert report.unique_gcc_employers == 2  # Wells Fargo x2 collapsed, + Bosch
    assert report.crawlable_boards == 3
    assert report.with_active_india_jobs == 3
    assert report.benchmark_total == 2_117
    assert report.coverage_pct == round(100 * 2 / 2117, 1)
    ats = {a.label: a.count for a in report.ats_distribution}
    assert ats["workday"] == 1 and ats["oracle"] == 1 and ats["smartrecruiters"] == 1


def test_parse_min_years_handles_common_phrasings() -> None:
    from jobos.models import parse_min_years

    assert parse_min_years("5+ years experience") == 5.0
    assert parse_min_years("3-5 years") == 3.0  # range -> lower bound
    assert parse_min_years("minimum 8 years") == 8.0
    assert parse_min_years("2 yrs or more") == 2.0
    assert parse_min_years("no years here") is None
    assert parse_min_years("401k and 24/7 support") is None  # implausible/non-YoE
    assert parse_min_years("") is None


def test_jobview_min_years_prefers_analysis_then_text() -> None:
    from datetime import UTC, datetime

    from jobos.models import JobAnalysis, JobView

    now = datetime.now(UTC)
    base = {
        "id": 1, "company": "Acme", "title": "Backend Engineer",
        "location": "Hyderabad, India", "description": "x",
        "apply_url": "https://boards.greenhouse.io/a/1",
        "source_url": "https://boards.greenhouse.io/a",
        "first_seen_at": now, "last_seen_at": now,
    }
    # No analysis: falls back to parsing the experience text.
    j1 = JobView(experience="5+ years experience required", **base)
    assert j1.min_years == 5.0
    # Analysis present: its figure wins over the text.
    j2 = JobView(
        experience="5+ years",
        analysis=JobAnalysis(minimum_years=7.0),
        **base,
    )
    assert j2.min_years == 7.0
    # Neither: None.
    j3 = JobView(experience="great team, fast growth", **base)
    assert j3.min_years is None
