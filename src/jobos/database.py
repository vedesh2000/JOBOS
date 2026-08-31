"""Small SQLAlchemy/SQLite persistence layer for JobOS."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    or_,
)
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.sql import Select, select

from jobos.models import (
    ApplicationStatus,
    ApplicationView,
    ATSReportData,
    CoverLetterDraft,
    JobAnalysis,
    JobData,
    JobStatus,
    JobView,
    OptimizationPlan,
    ResumeDiff,
    ResumeVersionView,
    StructuredResume,
    utc_now,
)


class Base(DeclarativeBase):
    pass


class ResumeVersionRecord(Base):
    __tablename__ = "resume_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    source_name: Mapped[str] = mapped_column(String(500))
    source_format: Mapped[str] = mapped_column(String(20))
    original_path: Mapped[str] = mapped_column(String(2_000), default="")
    is_master: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    resume_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_text: Mapped[str] = mapped_column(Text, default="")


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    location: Mapped[str] = mapped_column(String(300), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    experience: Mapped[str] = mapped_column(String(1_000), default="")
    skills_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    apply_url: Mapped[str] = mapped_column(String(4_000), unique=True)
    source_url: Mapped[str] = mapped_column(String(4_000))
    external_id: Mapped[str] = mapped_column(String(300), default="", index=True)
    employment_type: Mapped[str] = mapped_column(String(200), default="")
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.NEW.value, index=True)
    match_score: Mapped[int | None] = mapped_column(Integer, index=True)
    analysis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ATSReportRecord(Base):
    __tablename__ = "ats_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    resume_version_id: Mapped[int] = mapped_column(ForeignKey("resume_versions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    overall_score: Mapped[int] = mapped_column(Integer, index=True)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class ApplicationRecord(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    master_resume_version_id: Mapped[int] = mapped_column(
        ForeignKey("resume_versions.id"), index=True
    )
    generated_resume_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_versions.id")
    )
    status: Mapped[str] = mapped_column(String(30), default=ApplicationStatus.DRAFT.value)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    optimized_resume_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    ats_before_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    ats_after_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    cover_letter_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_dir: Mapped[str] = mapped_column(String(2_000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Database:
    """Own the local engine and the handful of queries needed by V1."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}, future=True
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._configure_sqlite(self.engine)

    @staticmethod
    def _configure_sqlite(engine: Engine) -> None:
        @sqlalchemy_event.listens_for(engine, "connect")
        def set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def add_resume(
        self,
        resume: StructuredResume,
        *,
        source_name: str,
        source_format: str,
        original_path: str,
        source_text: str,
        content_hash: str,
        is_master: bool = True,
        parent_id: int | None = None,
        job_id: int | None = None,
    ) -> ResumeVersionView:
        record = ResumeVersionRecord(
            source_name=source_name,
            source_format=source_format,
            original_path=original_path,
            source_text=source_text,
            content_hash=content_hash,
            resume_json=resume.model_dump(mode="json"),
            is_master=is_master,
            parent_id=parent_id,
            job_id=job_id,
        )
        with self.session() as session:
            if is_master:
                session.query(ResumeVersionRecord).filter_by(is_master=True).update(
                    {ResumeVersionRecord.is_master: False}
                )
            session.add(record)
            session.flush()
        return self._resume_view(record)

    def get_resume(self, version_id: int) -> ResumeVersionView | None:
        with self.session() as session:
            record = session.get(ResumeVersionRecord, version_id)
        return self._resume_view(record) if record else None

    def get_master_resume(self) -> ResumeVersionView | None:
        with self.session() as session:
            record = session.scalar(
                select(ResumeVersionRecord)
                .where(ResumeVersionRecord.is_master.is_(True))
                .order_by(ResumeVersionRecord.id.desc())
                .limit(1)
            )
        return self._resume_view(record) if record else None

    def list_resume_versions(self) -> list[ResumeVersionView]:
        with self.session() as session:
            records = list(
                session.scalars(
                    select(ResumeVersionRecord).order_by(ResumeVersionRecord.id.desc())
                )
            )
        return [self._resume_view(record) for record in records]

    @staticmethod
    def _resume_view(record: ResumeVersionRecord) -> ResumeVersionView:
        return ResumeVersionView(
            id=record.id,
            created_at=record.created_at,
            source_name=record.source_name,
            source_format=record.source_format,
            original_path=record.original_path,
            is_master=record.is_master,
            parent_id=record.parent_id,
            job_id=record.job_id,
            content_hash=record.content_hash,
            resume=StructuredResume.model_validate(record.resume_json),
        )

    def upsert_job(self, job: JobData, fingerprint: str) -> tuple[JobView, bool]:
        with self.session() as session:
            record = session.scalar(
                select(JobRecord).where(
                    (JobRecord.apply_url == job.apply_url) | (JobRecord.fingerprint == fingerprint)
                )
            )
            created = record is None
            if record is None:
                record = JobRecord(
                    **job.model_dump(exclude={"skills"}),
                    skills_json=job.skills,
                    fingerprint=fingerprint,
                )
                session.add(record)
            else:
                for key, value in job.model_dump(exclude={"skills"}).items():
                    setattr(record, key, value)
                record.skills_json = job.skills
                record.fingerprint = fingerprint
                record.last_seen_at = utc_now()
                record.is_active = True
            session.flush()
        return self._job_view(record), created

    def get_job(self, job_id: int) -> JobView | None:
        with self.session() as session:
            record = session.get(JobRecord, job_id)
        return self._job_view(record) if record else None

    def list_jobs(
        self,
        *,
        search: str = "",
        company: str = "",
        location: str = "",
        locations: list[str] | None = None,
        status: JobStatus | None = None,
        minimum_score: int | None = None,
        since: datetime | None = None,
        sort: str = "newest",
        limit: int = 1_000,
        offset: int = 0,
    ) -> list[JobView]:
        query: Select[tuple[JobRecord]] = select(JobRecord).where(JobRecord.is_active.is_(True))
        if since is not None:
            # `first_seen_at` is when JobOS first saw the posting, which is the best
            # available proxy for "new to me" without a reliable employer post date.
            query = query.where(JobRecord.first_seen_at >= since)
        if search:
            pattern = f"%{search}%"
            query = query.where(
                JobRecord.title.ilike(pattern)
                | JobRecord.description.ilike(pattern)
                | JobRecord.skills_json.cast(Text).ilike(pattern)
                | JobRecord.location.ilike(pattern)
            )
        if company:
            query = query.where(JobRecord.company == company)
        if location:
            query = query.where(JobRecord.location.ilike(f"%{location}%"))
        if locations:
            # Any-of, so both spellings of a city can be supplied at once. Applied in
            # SQL rather than in Python because the row cap below is reached long
            # before the preference filters run on a multi-board corpus.
            patterns = [
                JobRecord.location.ilike(f"%{place}%") for place in locations if place.strip()
            ]
            if patterns:
                query = query.where(or_(*patterns))
        if status:
            query = query.where(JobRecord.status == status.value)
        if minimum_score is not None:
            query = query.where(JobRecord.match_score >= minimum_score)
        if sort == "score":
            query = query.order_by(JobRecord.match_score.desc().nullslast())
        elif sort == "company":
            query = query.order_by(JobRecord.company.asc())
        elif sort == "title":
            query = query.order_by(JobRecord.title.asc())
        elif sort == "oldest":
            query = query.order_by(JobRecord.first_seen_at.asc())
        else:
            query = query.order_by(JobRecord.first_seen_at.desc())
        query = query.limit(min(limit, 5_000)).offset(max(offset, 0))
        with self.session() as session:
            records = list(session.scalars(query))
        return [self._job_view(record) for record in records]

    def deactivate_missing_jobs(self, company: str, seen_fingerprints: set[str]) -> int:
        """Mark this company's postings that the latest crawl did not return as closed.

        Rows are kept rather than deleted so an application already prepared against a
        posting still resolves; they simply stop showing up in ``list_jobs``.

        Only safe to call when the whole board was read. A crawl truncated by
        ``per_company_limit`` never sees the tail of the board, and would close every
        posting past the cut.
        """
        query = (
            select(JobRecord)
            .where(JobRecord.company == company)
            .where(JobRecord.is_active.is_(True))
        )
        closed = 0
        with self.session() as session:
            for record in session.scalars(query):
                if record.fingerprint in seen_fingerprints:
                    continue
                record.is_active = False
                closed += 1
            session.flush()
        return closed

    def companies(self) -> list[str]:
        query = select(JobRecord.company).distinct().order_by(JobRecord.company)
        with self.session() as session:
            return list(session.scalars(query))

    def save_job_analysis(self, job_id: int, analysis: JobAnalysis) -> JobView:
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            record.analysis_json = analysis.model_dump(mode="json")
            session.flush()
        return self._job_view(record)

    def set_job_score(self, job_id: int, score: int) -> None:
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            record.match_score = score

    def set_job_status(self, job_id: int, status: JobStatus) -> None:
        with self.session() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise LookupError(f"job {job_id} not found")
            record.status = status.value

    @staticmethod
    def _job_view(record: JobRecord) -> JobView:
        analysis = (
            JobAnalysis.model_validate(record.analysis_json) if record.analysis_json else None
        )
        return JobView(
            id=record.id,
            company=record.company,
            title=record.title,
            location=record.location,
            description=record.description,
            experience=record.experience,
            skills=record.skills_json,
            apply_url=record.apply_url,
            source_url=record.source_url,
            external_id=record.external_id,
            employment_type=record.employment_type,
            status=JobStatus(record.status),
            match_score=record.match_score,
            analysis=analysis,
            first_seen_at=record.first_seen_at,
            last_seen_at=record.last_seen_at,
            is_active=record.is_active,
        )

    def save_ats_report(
        self, job_id: int, resume_version_id: int, report: ATSReportData
    ) -> int:
        record = ATSReportRecord(
            job_id=job_id,
            resume_version_id=resume_version_id,
            overall_score=report.overall_score,
            report_json=report.model_dump(mode="json"),
        )
        with self.session() as session:
            session.add(record)
            session.flush()
        self.set_job_score(job_id, report.overall_score)
        return record.id

    def latest_ats_report(
        self, job_id: int, resume_version_id: int | None = None
    ) -> ATSReportData | None:
        query = select(ATSReportRecord).where(ATSReportRecord.job_id == job_id)
        if resume_version_id is not None:
            query = query.where(ATSReportRecord.resume_version_id == resume_version_id)
        query = query.order_by(ATSReportRecord.id.desc()).limit(1)
        with self.session() as session:
            record = session.scalar(query)
        return ATSReportData.model_validate(record.report_json) if record else None

    def create_application(
        self,
        *,
        job_id: int,
        master_resume_version_id: int,
        plan: OptimizationPlan,
        optimized_resume: StructuredResume,
        diff: ResumeDiff,
        ats_before: ATSReportData,
        ats_after: ATSReportData,
        cover_letter: CoverLetterDraft | None,
    ) -> ApplicationView:
        record = ApplicationRecord(
            job_id=job_id,
            master_resume_version_id=master_resume_version_id,
            plan_json=plan.model_dump(mode="json"),
            optimized_resume_json=optimized_resume.model_dump(mode="json"),
            diff_json=diff.model_dump(mode="json"),
            ats_before_json=ats_before.model_dump(mode="json"),
            ats_after_json=ats_after.model_dump(mode="json"),
            cover_letter_json=(cover_letter.model_dump(mode="json") if cover_letter else None),
        )
        with self.session() as session:
            session.add(record)
            session.flush()
        return self._application_view(record)

    def get_application(self, application_id: int) -> ApplicationView | None:
        with self.session() as session:
            record = session.get(ApplicationRecord, application_id)
        return self._application_view(record) if record else None

    def list_applications(
        self, *, job_id: int | None = None, limit: int = 500
    ) -> list[ApplicationView]:
        query = select(ApplicationRecord).order_by(ApplicationRecord.id.desc())
        if job_id is not None:
            query = query.where(ApplicationRecord.job_id == job_id)
        with self.session() as session:
            records = list(session.scalars(query.limit(min(limit, 2_000))))
        return [self._application_view(record) for record in records]

    def approve_application(
        self, application_id: int, *, generated_resume_version_id: int, output_dir: str
    ) -> ApplicationView:
        with self.session() as session:
            record = session.get(ApplicationRecord, application_id)
            if record is None:
                raise LookupError(f"application {application_id} not found")
            record.status = ApplicationStatus.APPROVED.value
            record.generated_resume_version_id = generated_resume_version_id
            record.output_dir = output_dir
            record.approved_at = utc_now()
            session.flush()
        return self._application_view(record)

    def package_count(self, job_id: int) -> int:
        with self.session() as session:
            return session.query(ApplicationRecord).filter_by(
                job_id=job_id, status=ApplicationStatus.APPROVED.value
            ).count()

    @staticmethod
    def _application_view(record: ApplicationRecord) -> ApplicationView:
        cover = (
            CoverLetterDraft.model_validate(record.cover_letter_json)
            if record.cover_letter_json
            else None
        )
        return ApplicationView(
            id=record.id,
            job_id=record.job_id,
            master_resume_version_id=record.master_resume_version_id,
            generated_resume_version_id=record.generated_resume_version_id,
            status=ApplicationStatus(record.status),
            plan=OptimizationPlan.model_validate(record.plan_json),
            optimized_resume=StructuredResume.model_validate(record.optimized_resume_json),
            diff=ResumeDiff.model_validate(record.diff_json),
            ats_before=ATSReportData.model_validate(record.ats_before_json),
            ats_after=ATSReportData.model_validate(record.ats_after_json),
            cover_letter=cover,
            output_dir=record.output_dir,
            created_at=record.created_at,
            approved_at=record.approved_at,
        )
