"""Years-of-experience and seniority search filters, and batch analysis."""

from __future__ import annotations

from jobos.models import JobAnalysis, JobData, JobStatus
from jobos.services import JobOSService


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
