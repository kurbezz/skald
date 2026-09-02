import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, select

from skald.db import get_engine, get_session, migrate_schema
from skald.lifecycle import try_job_lock
from skald.models import FileLifecycle, JobStatus, MediaJob, MediaType, OrganizationMode, OrganizedFile


def test_create_and_query_job(tmp_path):
    from sqlmodel import SQLModel

    engine = get_engine(str(tmp_path / "test.db"))
    SQLModel.metadata.create_all(engine)

    with get_session(engine) as session:
        job = MediaJob(
            type=MediaType.MOVIE,
            title="The Matrix",
            year=1999,
            release_title="The.Matrix.1999.1080p.BluRay.x264-GROUP",
            qbit_hash="abc123",
            category="skald-movie",
        )
        session.add(job)
        session.commit()

    with get_session(engine) as session:
        result = session.exec(select(MediaJob)).first()
        assert result.title == "The Matrix"
        assert result.status == JobStatus.QUEUED
        assert result.progress == 0.0


def test_lifecycle_schema_defaults():
    job = MediaJob(
        type=MediaType.TV,
        title="Show",
        release_title="Show.S01",
        qbit_hash="hash",
        category="skald-tv",
    )

    assert job.organization_mode == OrganizationMode.SCALAR
    assert job.operation_token is None


def test_lifecycle_schema_create_all_uses_scalar_server_default(tmp_path):
    engine = get_engine(str(tmp_path / "fresh-schema.db"))
    SQLModel.metadata.create_all(engine)

    with engine.connect() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
    organization_mode = next(column for column in columns if column[1] == "organization_mode")

    assert organization_mode[4] is not None
    assert organization_mode[4].strip("'") == "SCALAR"


def test_new_organized_file_requires_explicit_lifecycle(tmp_path):
    engine = get_engine(str(tmp_path / "explicit-lifecycle.db"))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(OrganizedFile(job_id=1, path="/library/tv/Show/Season 01/Show - S01E01.mkv"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(OrganizedFile(
            job_id=1,
            path="/library/tv/Show/Season 01/Show - S01E02.mkv",
            lifecycle=FileLifecycle.RESERVED,
        ))
        session.commit()


def test_legacy_unverified_migration_marks_ledger_rows_without_identity(tmp_path):
    engine = get_engine(str(tmp_path / "legacy-ledger.db"))
    with engine.begin() as connection:
        # A realistic pre-migration table shape (all columns a real legacy
        # database would already have, so the ORM read-back below actually
        # exercises a full row load, not just the two new columns).
        connection.exec_driver_sql(
            "CREATE TABLE mediajob ("
            "id INTEGER PRIMARY KEY, type VARCHAR NOT NULL, title VARCHAR NOT NULL, "
            "year INTEGER, season INTEGER, episode INTEGER, "
            "release_title VARCHAR NOT NULL, qbit_hash VARCHAR NOT NULL, "
            "category VARCHAR NOT NULL, status VARCHAR NOT NULL, "
            "error_message VARCHAR, content_path VARCHAR, library_path VARCHAR, "
            "progress FLOAT NOT NULL DEFAULT 0.0, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO mediajob "
            "(id, type, title, release_title, qbit_hash, category, status, "
            "progress, created_at, updated_at) VALUES "
            "(1, 'TV', 'Show', 'Show.S01', 'hash', 'skald-tv', 'ORGANIZED', "
            "1.0, '2026-09-02T00:00:00', '2026-09-02T00:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES (1, '/library/tv/Show/Season 01/Show - S01E01.mkv')"
        )

    migrate_schema(engine)

    # Raw-SQL check: migration must write the uppercase Enum *member name*
    # encoding (SQLModel's `Enum` column type reads/writes names, not the
    # lowercase Python values), or every ORM access of a migrated row raises
    # `LookupError`.
    with engine.connect() as connection:
        job = connection.exec_driver_sql(
            "SELECT organization_mode, operation_token FROM mediajob WHERE id = 1"
        ).one()
        organized_file = connection.exec_driver_sql(
            "SELECT lifecycle, operation_token, staging_path, staging_device, staging_inode, "
            "published_device, published_inode FROM organizedfile WHERE job_id = 1"
        ).one()
    assert job == ("PACK", None)
    assert organized_file == ("LEGACY_UNVERIFIED", None, None, None, None, None, None)

    # ORM read-back: the check the original migration test never performed.
    # A wrong (lowercase) encoding would raise LookupError here, not just
    # mismatch a string comparison.
    with Session(engine) as session:
        migrated_job = session.get(MediaJob, 1)
        assert migrated_job.organization_mode is OrganizationMode.PACK
        assert migrated_job.operation_token is None
        migrated_file = session.exec(
            select(OrganizedFile).where(OrganizedFile.job_id == 1)
        ).one()
        assert migrated_file.lifecycle is FileLifecycle.LEGACY_UNVERIFIED


def test_lifecycle_schema_rejects_duplicate_reservations_without_deleting_rows(tmp_path):
    engine = get_engine(str(tmp_path / "duplicate-ledger.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY, library_path VARCHAR)")
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO mediajob (id) VALUES (1), (2)")
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES (1, '/library/tv/Show/Season 01/Show - S01E01.mkv')"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (job_id, path) VALUES (2, '/library/tv/Show/Season 01/Show - S01E01.mkv')"
        )

    with pytest.raises(RuntimeError, match="duplicate ledger path reservations"):
        migrate_schema(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM organizedfile").scalar() == 2


def test_advisory_lock_is_nonblocking_and_released_after_context():
    side_effects = []

    with try_job_lock(1) as acquired:
        assert acquired
        with try_job_lock(1) as contended:
            if contended:
                side_effects.append("contended caller mutated")
            assert not contended

    with try_job_lock(1) as acquired_after_release:
        assert acquired_after_release
        side_effects.append("later caller mutated")

    assert side_effects == ["later caller mutated"]
