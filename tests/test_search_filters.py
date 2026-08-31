"""Years-of-experience and seniority search filters, and batch analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobos.models import JobAnalysis, JobData, JobStatus, StructuredResume
from jobos.services import JobOSService, ServiceError


def _seed(service: JobOSService, title: str, *, fp: str, years: float | None, level: str) -> int:
    job_data = JobData(
        company="Acme",
        title=title,
        location="Bengaluru, India",
        description="Backend role. Required: Python and FastAPI.",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{fp}",
        source_url="https://boards.greenhouse.io/acme",
        external_id=fp,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    # Attach a stored analysis so the years/seniority filters have data to read.
    service.database.save_job_analysis(
        view.id,
        JobAnalysis(minimum_years=years, seniority=level),
    )
    return view.id


def test_max_and_min_years_filter(service: JobOSService) -> None:
    junior = _seed(service, "Backend Engineer", fp="j", years=2.0, level="")
    mid = _seed(service, "Senior Backend Engineer", fp="m", years=5.0, level="Senior")
    staff = _seed(service, "Staff Engineer", fp="s", years=10.0, level="Staff")

    at_most_6 = {job.id for job in service.search_jobs(max_years=6)}
    assert junior in at_most_6 and mid in at_most_6
    assert staff not in at_most_6

    at_least_8 = {job.id for job in service.search_jobs(min_years=8)}
    assert at_least_8 == {staff}


def test_seniority_filter(service: JobOSService) -> None:
    _seed(service, "Backend Engineer", fp="j", years=2.0, level="")
    senior = _seed(service, "Senior Backend Engineer", fp="m", years=5.0, level="Senior")

    seniors = {job.id for job in service.search_jobs(seniority="Senior")}
    assert seniors == {senior}


def test_years_filter_excludes_jobs_without_analysis(service: JobOSService) -> None:
    """A job with no stored requirement cannot satisfy a years filter."""
    analysed = _seed(service, "Senior Backend Engineer", fp="a", years=5.0, level="Senior")
    # An unanalysed job: seeded without save_job_analysis.
    unanalysed_data = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Bengaluru, India",
        description="Backend role.",
        apply_url="https://boards.greenhouse.io/acme/jobs/u",
        source_url="https://boards.greenhouse.io/acme",
        external_id="u",
    )
    service.database.upsert_job(unanalysed_data, fingerprint="u")

    filtered = {job.id for job in service.search_jobs(max_years=6)}
    assert filtered == {analysed}
    # Without a years/seniority filter, both jobs are returned.
    assert len(service.search_jobs()) == 2


async def test_analyse_jobs_batch_scores_and_isolates_failures(service: JobOSService) -> None:
    from tests.conftest import RESUME_MARKDOWN

    await service.import_resume("master.md", RESUME_MARKDOWN.encode())
    good = _seed(service, "Senior Backend Engineer", fp="g", years=5.0, level="Senior")

    result = await service.analyse_jobs([good, 999_999])
    assert result.requested == 2
    assert result.analysed == 1
    assert 999_999 in result.errors  # missing job is reported, batch still completes

    # The analysed job now has a match score, so score sorting surfaces it.
    scored = service.search_jobs(sort="score")
    assert scored and scored[0].id == good
    assert scored[0].match_score is not None


def test_preferences_filter_titles_and_keep_unanalysed(service: JobOSService) -> None:
    """`--preferred` applies role include/exclude and a lenient years window."""
    service.config.preferences.roles_include = ["engineer", "developer"]
    service.config.preferences.roles_exclude = ["manager", "principal"]
    service.config.preferences.max_years = 8
    service.config.preferences.minimum_match_score = 0

    sde = _seed(service, "Backend Engineer", fp="p1", years=5.0, level="")
    senior = _seed(service, "Senior Software Developer", fp="p2", years=6.0, level="Senior")
    mgr = _seed(service, "Engineering Manager - Backend", fp="p3", years=10.0, level="")
    staff_over = _seed(service, "Staff Engineer", fp="p4", years=12.0, level="Staff")
    # Unanalysed engineer role: kept under preferences despite the years window.
    unanalysed_data = JobData(
        company="Acme",
        title="Software Engineer II",
        location="Bengaluru, India",
        description="Backend role.",
        apply_url="https://boards.greenhouse.io/acme/jobs/p5",
        source_url="https://boards.greenhouse.io/acme",
        external_id="p5",
    )
    unanalysed, _ = service.database.upsert_job(unanalysed_data, fingerprint="p5")

    ids = {job.id for job in service.search_jobs(apply_preferences=True)}
    assert sde in ids and senior in ids  # matching engineer/developer titles within years
    assert unanalysed.id in ids  # unanalysed engineer kept (lenient years)
    assert mgr not in ids  # excluded by "manager"
    assert staff_over not in ids  # analysed and over the 8-year window


def _seed_scored(
    service: JobOSService, title: str, *, company: str, fp: str, score: int
) -> int:
    job_data = JobData(
        company=company,
        title=title,
        location="Bengaluru, India",
        description="Backend role. Required: Python.",
        apply_url=f"https://boards.greenhouse.io/x/jobs/{fp}",
        source_url="https://boards.greenhouse.io/x",
        external_id=fp,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    service.database.save_job_analysis(view.id, JobAnalysis(minimum_years=5.0, seniority=""))
    service.database.set_job_score(view.id, score)
    return view.id


def test_company_shortlist_respects_quota_and_ranks_by_score(service: JobOSService) -> None:
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_applications_per_company = 2

    best = _seed_scored(service, "Backend Engineer", company="Acme", fp="a1", score=90)
    second = _seed_scored(service, "Software Engineer II", company="Acme", fp="a2", score=80)
    third = _seed_scored(service, "Platform Engineer", company="Acme", fp="a3", score=70)
    other = _seed_scored(service, "Backend Engineer", company="Zeta", fp="z1", score=85)

    shortlists = service.company_shortlists()
    by_company = {item.company: item for item in shortlists}

    acme = by_company["Acme"]
    assert [job.id for job in acme.picks] == [best, second], "highest scores fill the quota"
    assert acme.slots_left == 2
    assert acme.relevant == 3
    assert acme.overflow == 1, "the third role is beyond quota"
    assert third not in {job.id for job in acme.picks}

    # Companies are ordered by their strongest opportunity.
    assert [item.company for item in shortlists] == ["Acme", "Zeta"]
    assert [job.id for job in by_company["Zeta"].picks] == [other]


def test_applied_roles_consume_company_quota(service: JobOSService) -> None:
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_applications_per_company = 2

    applied = _seed_scored(service, "Backend Engineer", company="Acme", fp="b1", score=90)
    remaining = _seed_scored(service, "Software Engineer II", company="Acme", fp="b2", score=80)
    _seed_scored(service, "Platform Engineer", company="Acme", fp="b3", score=70)
    service.set_job_status(applied, JobStatus.APPLIED)

    acme = next(item for item in service.company_shortlists() if item.company == "Acme")
    assert acme.applied == 1
    assert acme.slots_left == 1, "one slot consumed by the applied role"
    assert [job.id for job in acme.picks] == [remaining]
    assert applied not in {job.id for job in acme.picks}


def test_exhausted_companies_are_hidden_unless_requested(service: JobOSService) -> None:
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_applications_per_company = 1

    used = _seed_scored(service, "Backend Engineer", company="Acme", fp="c1", score=90)
    _seed_scored(service, "Software Engineer II", company="Acme", fp="c2", score=80)
    service.set_job_status(used, JobStatus.APPLIED)

    assert all(item.company != "Acme" for item in service.company_shortlists())
    with_exhausted = service.company_shortlists(include_exhausted=True)
    acme = next(item for item in with_exhausted if item.company == "Acme")
    assert acme.exhausted is True and acme.picks == []


async def test_daily_prepares_top_packages_and_is_idempotent(service: JobOSService) -> None:
    """The daily run prepares the best packages, then reuses them on a second run."""
    from tests.conftest import RESUME_MARKDOWN

    await service.import_resume("master.md", RESUME_MARKDOWN.encode())
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_applications_per_company = 3

    best = _seed_scored(service, "Backend Engineer", company="Acme", fp="d1", score=90)
    second = _seed_scored(service, "Software Engineer II", company="Acme", fp="d2", score=80)
    _seed_scored(service, "Platform Engineer", company="Acme", fp="d3", score=70)

    first_run = await service.run_daily(top=2, do_crawl=False)
    prepared_ids = [item.job.id for item in first_run.prepared]
    assert prepared_ids == [best, second], "only the two strongest are prepared"
    assert not first_run.preparation_errors
    for item in first_run.prepared:
        assert Path(item.output_dir).is_dir(), "a reviewable package exists on disk"

    # A second run must not redo the work; it reports the existing packages instead.
    second_run = await service.run_daily(top=2, do_crawl=False)
    assert second_run.prepared == []
    assert {item.job.id for item in second_run.already_prepared} == {best, second}
    assert len(second_run.ready) == 2


async def test_daily_requires_a_master_resume(service: JobOSService) -> None:
    """Without a resume there is nothing to tailor, and it should fail before crawling."""
    with pytest.raises(ServiceError, match="resume"):
        await service.run_daily(do_crawl=False)


def test_since_hours_limits_results_to_recent_jobs(service: JobOSService) -> None:
    """A recency window keeps newly seen roles and drops older ones."""
    from datetime import UTC, datetime, timedelta

    from jobos.database import JobRecord

    recent = _seed_scored(service, "Backend Engineer", company="Acme", fp="r1", score=90)
    stale = _seed_scored(service, "Software Engineer II", company="Acme", fp="r2", score=80)
    service.config.preferences.minimum_match_score = 0

    # Backdate one posting so it falls outside a 24 hour window.
    with service.database.session() as session:
        record = session.get(JobRecord, stale)
        assert record is not None
        record.first_seen_at = datetime.now(UTC) - timedelta(days=3)

    assert {job.id for job in service.search_jobs(since_hours=24)} == {recent}
    # Without a window both roles are returned.
    assert {job.id for job in service.search_jobs()} == {recent, stale}


def _seed_stack(
    service: JobOSService,
    title: str,
    *,
    fp: str,
    description: str,
    skills: list[str] | None = None,
) -> int:
    """Seed a job with a specific tech stack, without any stored analysis."""
    job_data = JobData(
        company="Acme",
        title=title,
        location="Bengaluru, India",
        description=description,
        skills=skills or [],
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{fp}",
        source_url="https://boards.greenhouse.io/acme",
        external_id=fp,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    return view.id


def test_skills_include_is_any_of(service: JobOSService) -> None:
    """A job needs only one of the wanted skills, and unrelated stacks drop out."""
    python_job = _seed_stack(
        service, "Backend Engineer", fp="s1", description="Build services in Python and Django."
    )
    react_job = _seed_stack(
        service, "Frontend Engineer", fp="s2", description="Ship UI with React and TypeScript."
    )
    rust_job = _seed_stack(
        service, "Systems Engineer", fp="s3", description="Low latency work in Rust and C++."
    )

    ids = {job.id for job in service.search_jobs(skills_include=["python", "react"])}
    assert python_job in ids
    assert react_job in ids
    assert rust_job not in ids


def test_skills_exclude_does_not_confuse_java_with_javascript(service: JobOSService) -> None:
    """Token matching keeps JavaScript roles when Java is excluded."""
    java_job = _seed_stack(
        service, "Backend Engineer", fp="s4", description="Strong Java and Spring Boot required."
    )
    js_job = _seed_stack(
        service,
        "Full Stack Engineer",
        fp="s5",
        description="JavaScript, Node.js and React across the stack.",
    )

    ids = {job.id for job in service.search_jobs(skills_exclude=["java"])}
    assert js_job in ids
    assert java_job not in ids


def test_skills_filter_reads_stored_skills_and_title(service: JobOSService) -> None:
    """The crawler's canonical skills list and the title both count as evidence."""
    from_skills = _seed_stack(
        service,
        "Platform Engineer",
        fp="s6",
        description="Work on our internal platform.",
        skills=["python", "kubernetes"],
    )
    from_title = _seed_stack(
        service, "Full-Stack Engineer", fp="s7", description="Own features end to end."
    )
    neither = _seed_stack(
        service, "Data Engineer", fp="s8", description="Pipelines in Scala and Spark."
    )

    ids = {job.id for job in service.search_jobs(skills_include=["python", "full stack"])}
    # "Full-Stack" in the title normalizes to "full stack", so a free-form term works.
    assert from_skills in ids
    assert from_title in ids
    assert neither not in ids


def test_skills_exclude_wins_over_include(service: JobOSService) -> None:
    """A job matching both lists is dropped, mirroring the title filter semantics."""
    both = _seed_stack(
        service, "Backend Engineer", fp="s9", description="Python and Java microservices."
    )
    clean = _seed_stack(
        service, "Backend Engineer", fp="s10", description="Python and PostgreSQL only."
    )

    ids = {
        job.id
        for job in service.search_jobs(skills_include=["python"], skills_exclude=["java"])
    }
    assert clean in ids
    assert both not in ids


def test_preferences_apply_skill_filters(service: JobOSService) -> None:
    """`--preferred` picks the skill lists up from config like the other filters."""
    service.config.preferences.roles_include = []
    service.config.preferences.roles_exclude = []
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_years = None
    service.config.preferences.skills_include = ["python", "react"]
    service.config.preferences.skills_exclude = ["java"]

    wanted = _seed_stack(
        service, "Backend Engineer", fp="s11", description="Python, FastAPI, PostgreSQL."
    )
    java_heavy = _seed_stack(
        service, "Backend Engineer", fp="s12", description="Java, Spring Boot, Kafka."
    )
    off_stack = _seed_stack(
        service, "Backend Engineer", fp="s13", description="Ruby on Rails shop."
    )

    ids = {job.id for job in service.search_jobs(apply_preferences=True)}
    assert wanted in ids
    assert java_heavy not in ids
    assert off_stack not in ids


def test_explicit_skill_filter_scans_beyond_the_row_limit(service: JobOSService) -> None:
    """An explicit skill filter must widen the DB fetch, not filter only the first page."""
    for index in range(12):
        _seed_stack(
            service, "Backend Engineer", fp=f"bulk{index}", description="Java and Spring Boot."
        )
    needle = _seed_stack(
        service, "Backend Engineer", fp="needle", description="Python and FastAPI."
    )

    # The Python job was seeded last, so a naive limit=3 fetch would miss it if the
    # skill filter ran after the limit instead of before it.
    jobs = service.search_jobs(skills_include=["python"], limit=3, sort="oldest")
    assert needle in {job.id for job in jobs}


def _seed_at(service: JobOSService, location: str, *, fp: str) -> int:
    """Seed a job in a given location with a stack that passes the skill filters."""
    job_data = JobData(
        company="Acme",
        title="Backend Engineer",
        location=location,
        description="Python and FastAPI services.",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{fp}",
        source_url="https://boards.greenhouse.io/acme",
        external_id=fp,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    return view.id


def test_preferred_locations_match_any_spelling(service: JobOSService) -> None:
    """One search covers both spellings of a city and drops other places."""
    service.config.preferences.preferred_locations = ["Bengaluru", "Bangalore", "Remote"]
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_years = None

    bengaluru = _seed_at(service, "Bengaluru, India", fp="l1")
    bangalore = _seed_at(service, "Bangalore, India", fp="l2")
    remote = _seed_at(service, "Remote - India", fp="l3")
    dublin = _seed_at(service, "Dublin, Ireland", fp="l4")

    ids = {job.id for job in service.search_jobs(apply_preferences=True)}
    assert {bengaluru, bangalore, remote} <= ids
    assert dublin not in ids


def test_explicit_location_overrides_preferred_locations(service: JobOSService) -> None:
    """Asking for one place must not be widened back out by the preferred list."""
    service.config.preferences.preferred_locations = ["Bengaluru", "Bangalore"]
    service.config.preferences.minimum_match_score = 0
    service.config.preferences.max_years = None

    bengaluru = _seed_at(service, "Bengaluru, India", fp="l5")
    bangalore = _seed_at(service, "Bangalore, India", fp="l6")

    ids = {
        job.id for job in service.search_jobs(location="Bengaluru", apply_preferences=True)
    }
    assert bengaluru in ids
    assert bangalore not in ids


def _seed_closed(service: JobOSService, *, fp: str, days_old: float) -> int:
    """Seed a job then mark it closed and last-seen `days_old` days ago."""
    from datetime import timedelta

    from jobos.database import JobRecord
    from jobos.models import utc_now

    job_data = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Bengaluru, India",
        description="Python and FastAPI services.",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{fp}",
        source_url="https://boards.greenhouse.io/acme",
        external_id=fp,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    with service.database.session() as session:
        record = session.get(JobRecord, view.id)
        assert record is not None
        record.is_active = False
        record.last_seen_at = utc_now() - timedelta(days=days_old)
    return view.id


def test_prune_dry_run_counts_without_deleting(service: JobOSService) -> None:
    """Without --yes (commit=False) prune reports but removes nothing."""
    _seed_closed(service, fp="old1", days_old=40)
    _seed_closed(service, fp="old2", days_old=40)

    would = service.prune_jobs(older_than_days=30, commit=False)
    assert would == 2
    # A second dry run still finds both, proving the first deleted nothing.
    assert service.prune_jobs(older_than_days=30, commit=False) == 2


def test_prune_removes_only_old_closed_jobs(service: JobOSService) -> None:
    """Old closed jobs go; recently-closed and active jobs stay."""
    old = _seed_closed(service, fp="old", days_old=40)
    recent = _seed_closed(service, fp="recent", days_old=5)
    active_data = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Bengaluru, India",
        description="Python role.",
        apply_url="https://boards.greenhouse.io/acme/jobs/active",
        source_url="https://boards.greenhouse.io/acme",
        external_id="active",
    )
    active, _ = service.database.upsert_job(active_data, fingerprint="active")

    removed = service.prune_jobs(older_than_days=30, commit=True)
    assert removed == 1
    assert service.database.get_job(old) is None
    assert service.database.get_job(recent) is not None
    assert service.database.get_job(active.id) is not None


def test_prune_keeps_jobs_with_an_application(
    service: JobOSService, resume: StructuredResume
) -> None:
    """A closed, old job that has a prepared application is never pruned."""
    from jobos.database import ApplicationRecord

    version = service.database.add_resume(
        resume,
        source_name="test",
        source_format="yaml",
        original_path="test.yaml",
        source_text="",
        content_hash="hash-prune-test",
    )
    protected = _seed_closed(service, fp="hasapp", days_old=90)
    with service.database.session() as session:
        session.add(
            ApplicationRecord(
                job_id=protected,
                master_resume_version_id=version.id,
                plan_json={},
                optimized_resume_json={},
                diff_json={},
                ats_before_json={},
                ats_after_json={},
            )
        )

    removed = service.prune_jobs(older_than_days=30, commit=True)
    assert removed == 0
    assert service.database.get_job(protected) is not None


def _seed_posted(service: JobOSService, *, fp: str, days_ago: float | None) -> int:
    """Seed a job posted `days_ago` days back, or with no posting date at all."""
    from datetime import timedelta

    from jobos.models import utc_now

    posted = None if days_ago is None else utc_now() - timedelta(days=days_ago)
    job_data = JobData(
        company="Acme",
        title="Backend Engineer",
        location="Bengaluru, India",
        description="Python and FastAPI services.",
        apply_url=f"https://boards.greenhouse.io/acme/jobs/{fp}",
        source_url="https://boards.greenhouse.io/acme",
        external_id=fp,
        posted_at=posted,
    )
    view, _ = service.database.upsert_job(job_data, fingerprint=fp)
    return view.id


def test_posted_days_uses_the_employer_date(service: JobOSService) -> None:
    """`posted_days` filters on the board's publish date, not when JobOS saw the job."""
    fresh = _seed_posted(service, fp="fresh", days_ago=2)
    stale = _seed_posted(service, fp="stale", days_ago=40)

    ids = {job.id for job in service.search_jobs(posted_days=5)}
    assert fresh in ids
    assert stale not in ids
    # Everything is still reachable without the filter, even though all rows were
    # first seen just now.
    assert {fresh, stale} <= {job.id for job in service.search_jobs()}


def test_posted_days_excludes_jobs_without_a_date(service: JobOSService) -> None:
    """A board that reports no date is skipped rather than assumed fresh."""
    dated = _seed_posted(service, fp="dated", days_ago=1)
    undated = _seed_posted(service, fp="undated", days_ago=None)

    ids = {job.id for job in service.search_jobs(posted_days=7)}
    assert dated in ids
    assert undated not in ids


def test_posted_at_round_trips_through_the_database(service: JobOSService) -> None:
    """The stored date survives a read back, so the UI can show a real age."""
    job_id = _seed_posted(service, fp="rt", days_ago=3)
    stored = service.database.get_job(job_id)
    assert stored is not None
    assert stored.posted_at is not None
    from jobos.models import utc_now

    age_days = (utc_now() - stored.posted_at).total_seconds() / 86_400
    assert 2.9 < age_days < 3.1
