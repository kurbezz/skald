import csv
import json

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


def test_fresh_organizedfile_contract_has_fk_indexes_and_uppercase_legacy_default(tmp_path):
    engine = get_engine(str(tmp_path / "fresh-organizedfile.db"))
    SQLModel.metadata.create_all(engine)

    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list(organizedfile)").fetchall()
        columns = connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
        indexes = connection.exec_driver_sql("PRAGMA index_list(organizedfile)").fetchall()
        index_columns = {
            index[1]: [
                column[2]
                for column in connection.exec_driver_sql(f"PRAGMA index_info({index[1]})").fetchall()
            ]
            for index in indexes
        }

    assert any(key[2:5] == ("mediajob", "job_id", "id") for key in foreign_keys)
    lifecycle = next(column for column in columns if column[1] == "lifecycle")
    assert lifecycle[4].strip("'") == "LEGACY_UNVERIFIED"
    assert lifecycle[3] == 1
    assert all(next(column for column in columns if column[1] == name)[3] == 0 for name in (
        "operation_token", "staging_path", "staging_device", "staging_inode",
        "published_device", "published_inode",
    ))
    assert any(index[1] == "ix_organizedfile_job_id" for index in indexes)
    assert any(
        index[2] and index_columns[index[1]] == ["path"]
        for index in indexes
    )


def test_get_engine_enforces_foreign_keys_on_each_connection_and_can_opt_out(tmp_path):
    engine = get_engine(str(tmp_path / "foreign-keys.db"))
    SQLModel.metadata.create_all(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    engine.dispose()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    with Session(engine) as session:
        session.add(OrganizedFile(
            job_id=999,
            path="/library/tv/Show/Season 01/Show - S01E01.mkv",
            lifecycle=FileLifecycle.PUBLISHED,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    repair_engine = get_engine(str(tmp_path / "repair.db"), enforce_foreign_keys=False)
    with repair_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0


def test_migrate_schema_adds_nullable_episode_set_to_legacy_mediajob(tmp_path):
    engine = get_engine(str(tmp_path / "legacy-episode-set.db"))
    with engine.begin() as connection:
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
            "INSERT INTO mediajob "
            "(id, type, title, release_title, qbit_hash, category, status, "
            "progress, created_at, updated_at) VALUES "
            "(1, 'TV', 'Show', 'Show.S01', 'hash', 'skald-tv', 'QUEUED', "
            "0.0, '2026-09-02T00:00:00', '2026-09-02T00:00:00')"
        )

    migrate_schema(engine)

    with engine.connect() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
        episode_set = next(column for column in columns if column[1] == "episode_set")
        persisted_episode_set = connection.exec_driver_sql(
            "SELECT episode_set FROM mediajob WHERE id = 1"
        ).scalar()

    assert episode_set[2] == "VARCHAR"
    assert episode_set[3] == 0
    assert persisted_episode_set is None

    with Session(engine) as session:
        assert session.get(MediaJob, 1).episode_set is None


def test_new_organized_file_uses_uppercase_legacy_default(tmp_path):
    engine = get_engine(str(tmp_path / "explicit-lifecycle.db"))
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV, title="Show", release_title="Show.S01", qbit_hash="hash", category="skald-tv"
        )
        session.add(job)
        session.commit()
        session.add(OrganizedFile(job_id=job.id, path="/library/tv/Show/Season 01/Show - S01E01.mkv"))
        session.commit()

    with engine.connect() as connection:
        lifecycle = connection.exec_driver_sql(
            "SELECT lifecycle FROM organizedfile WHERE job_id = 1"
        ).scalar()
    assert lifecycle == "LEGACY_UNVERIFIED"


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

    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list(organizedfile)").fetchall()
        columns = connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
        indexes = connection.exec_driver_sql("PRAGMA index_list(organizedfile)").fetchall()
    assert any(key[2:5] == ("mediajob", "job_id", "id") for key in foreign_keys)
    assert next(column for column in columns if column[1] == "lifecycle")[4].strip("'") == "LEGACY_UNVERIFIED"
    assert any(index[1] == "ix_organizedfile_job_id" for index in indexes)
    assert all(next(column for column in columns if column[1] == name)[3] == 0 for name in (
        "operation_token", "staging_path", "staging_device", "staging_inode",
        "published_device", "published_inode",
    ))

    # ORM read-back: the check the original migration test never performed.
    # A wrong (lowercase) encoding would raise LookupError here, not just
    # mismatch a string comparison.
    with Session(engine) as session:
        migrated_job = session.get(MediaJob, 1)
        assert migrated_job.type is MediaType.TV
        assert migrated_job.status is JobStatus.ORGANIZED
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


def _seed_valid_and_orphan_ledger_rows(tmp_path):
    engine = get_engine(str(tmp_path / "orphan-ledger.db"), enforce_foreign_keys=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        job = MediaJob(
            type=MediaType.TV,
            title="Show",
            release_title="Show.S01",
            qbit_hash="valid-hash",
            category="skald-tv",
        )
        session.add(job)
        session.commit()
        valid = OrganizedFile(
            job_id=job.id,
            path="/library/tv/Show/Season 01/Show - S01E01.mkv",
            lifecycle=FileLifecycle.PUBLISHED,
        )
        orphan = OrganizedFile(
            job_id=999,
            path="/library/tv/Show/Season 01/Show - S01E02.mkv",
            lifecycle=FileLifecycle.LEGACY_UNVERIFIED,
        )
        session.add_all([valid, orphan])
        session.commit()
        return engine, valid.id, orphan.id


def test_export_and_purge_orphans_writes_exact_json_and_retains_valid_rows(tmp_path):
    from skald.migrate import export_and_purge_orphans

    engine, valid_id, orphan_id = _seed_valid_and_orphan_ledger_rows(tmp_path)
    audit_path = tmp_path / "orphans.json"

    assert export_and_purge_orphans(engine, audit_path, audit_format="json") == 1
    assert json.loads(audit_path.read_text()) == [{
        "id": orphan_id,
        "job_id": 999,
        "path": "/library/tv/Show/Season 01/Show - S01E02.mkv",
        "operation_token": None,
        "lifecycle": "LEGACY_UNVERIFIED",
        "staging_path": None,
        "staging_device": None,
        "staging_inode": None,
        "published_device": None,
        "published_inode": None,
    }]
    with Session(engine) as session:
        assert session.get(OrganizedFile, valid_id) is not None
        assert session.get(OrganizedFile, orphan_id) is None


def test_export_and_purge_orphans_writes_exact_csv(tmp_path):
    from skald.migrate import export_and_purge_orphans

    engine, _, orphan_id = _seed_valid_and_orphan_ledger_rows(tmp_path)
    audit_path = tmp_path / "orphans.csv"

    assert export_and_purge_orphans(engine, audit_path, audit_format="csv") == 1
    with audit_path.open(newline="") as audit_file:
        rows = list(csv.DictReader(audit_file))
    assert rows == [{
        "id": str(orphan_id),
        "job_id": "999",
        "path": "/library/tv/Show/Season 01/Show - S01E02.mkv",
        "operation_token": "",
        "lifecycle": "LEGACY_UNVERIFIED",
        "staging_path": "",
        "staging_device": "",
        "staging_inode": "",
        "published_device": "",
        "published_inode": "",
    }]


def test_export_and_purge_orphans_rolls_back_when_audit_write_fails(tmp_path, monkeypatch):
    from skald.migrate import export_and_purge_orphans

    engine, valid_id, orphan_id = _seed_valid_and_orphan_ledger_rows(tmp_path)

    def fail_audit_write(*args, **kwargs):
        raise OSError("audit device full")

    monkeypatch.setattr("skald.migrate._write_audit", fail_audit_write)

    with pytest.raises(OSError, match="audit device full"):
        export_and_purge_orphans(engine, tmp_path / "orphans.json", audit_format="json")

    with Session(engine) as session:
        assert session.get(OrganizedFile, valid_id) is not None
        assert session.get(OrganizedFile, orphan_id) is not None


def test_export_and_purge_orphans_audits_pre_constraint_legacy_rows(tmp_path):
    from skald.migrate import export_and_purge_orphans

    engine = get_engine(str(tmp_path / "pre-constraint.db"), enforce_foreign_keys=False)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (id, job_id, path) VALUES (7, 999, '/library/orphan.mkv')"
        )

    audit_path = tmp_path / "legacy-orphans.json"
    assert export_and_purge_orphans(engine, audit_path, audit_format="json") == 1
    assert json.loads(audit_path.read_text()) == [{
        "id": 7,
        "job_id": 999,
        "path": "/library/orphan.mkv",
        "operation_token": None,
        "lifecycle": None,
        "staging_path": None,
        "staging_device": None,
        "staging_inode": None,
        "published_device": None,
        "published_inode": None,
    }]


def test_fk_invalid_rebuild_cleans_shadow_table_for_offline_repair_and_retry(tmp_path):
    from skald.migrate import export_and_purge_orphans

    database = tmp_path / "fk-invalid-rebuild.db"
    legacy_engine = get_engine(str(database), enforce_foreign_keys=False)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE mediajob (id INTEGER PRIMARY KEY, library_path VARCHAR)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile (id, job_id, path) VALUES (7, 999, '/library/orphan.mkv')"
        )

    application_engine = get_engine(str(database))
    with pytest.raises(IntegrityError):
        migrate_schema(application_engine)

    with application_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'organizedfile_new'"
        ).scalar() is None
        assert connection.exec_driver_sql("SELECT id, job_id, path FROM organizedfile").one() == (
            7,
            999,
            "/library/orphan.mkv",
        )

    repair_engine = get_engine(str(database), enforce_foreign_keys=False)
    assert export_and_purge_orphans(
        repair_engine, tmp_path / "orphans.json", audit_format="json"
    ) == 1

    migrate_schema(application_engine)
    with application_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM organizedfile").scalar() == 0
        assert connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'organizedfile_new'"
        ).scalar() is None
