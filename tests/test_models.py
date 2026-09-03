import csv
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Session, select

import skald.db as db
from skald.db import get_engine, get_session, migrate_schema
from skald.lifecycle import try_job_lock
from skald.models import (
    DownloadedQuality,
    FileLifecycle,
    JobStatus,
    MediaJob,
    MediaSubscription,
    MediaType,
    NotificationChannel,
    NotificationDeliveryAttempt,
    OrganizationMode,
    OrganizedFile,
    QualityProfile,
    SubscriptionEvent,
    SubscriptionEventKind,
)


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


def test_migration_converts_legacy_singleton_to_exactly_movie_and_tv_profiles(tmp_path):
    engine = get_engine(str(tmp_path / "legacy-quality.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE qualityprofile (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "allowed_resolutions JSON NOT NULL, excluded_tokens JSON NOT NULL, "
            "minimum_seeders INTEGER NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO qualityprofile VALUES "
            "(1, '[\"720p\"]', '[\"CAM\"]', 9, '2026-09-03T00:00:00+00:00')"
        )

    migrate_schema(engine)
    migrate_schema(engine)

    with Session(engine) as session:
        profiles = session.exec(select(QualityProfile).order_by(QualityProfile.media_type)).all()

    assert [(profile.media_type, profile.allowed_resolutions, profile.minimum_seeders)
            for profile in profiles] == [
        (MediaType.MOVIE, ["720p"], 9),
        (MediaType.TV, ["720p"], 9),
    ]
    assert all(profile.excluded_tokens == ["CAM"] for profile in profiles)
    assert all(profile.allowed_audio == [] and profile.allowed_hdr == [] for profile in profiles)
    assert all(profile.minimum_size_bytes is None and profile.maximum_size_bytes is None for profile in profiles)


def test_migration_repairs_partial_qualityprofile_and_preserves_compatible_values(tmp_path):
    engine = get_engine(str(tmp_path / "partial-quality.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE qualityprofile (id INTEGER PRIMARY KEY, media_type VARCHAR, "
            "allowed_resolutions JSON, minimum_seeders INTEGER, updated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO qualityprofile VALUES "
            "(4, 'MOVIE', '[\"720p\"]', 8, '2026-09-03T04:05:06+00:00')"
        )

    migrate_schema(engine)
    migrate_schema(engine)

    with engine.connect() as connection:
        columns = {
            column[1] for column in connection.exec_driver_sql("PRAGMA table_info(qualityprofile)").fetchall()
        }
        ddl = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'qualityprofile'"
        ).scalar()
        rows = connection.exec_driver_sql(
            "SELECT media_type, allowed_resolutions, minimum_seeders, allowed_audio, allowed_hdr, "
            "preferred_resolutions, preferred_audio, preferred_hdr, preferred_size_bands, updated_at "
            "FROM qualityprofile ORDER BY media_type"
        ).fetchall()

    assert {"allowed_audio", "allowed_hdr", "preferred_size_bands", "maximum_size_bytes"} <= columns
    assert "CONSTRAINT uq_qualityprofile_media_type" in ddl
    assert "CONSTRAINT ck_qualityprofile_media_type" in ddl
    assert rows == [
        ("MOVIE", '["720p"]', 8, "[]", "[]", "[]", "[]", "[]", "[]", "2026-09-03T04:05:06+00:00"),
        ("TV", '["1080p", "2160p"]', 5, "[]", "[]", "[]", "[]", "[]", "[]", rows[1][9]),
    ]


def test_migrate_schema_emits_event_uniqueness_indexes_and_defaults(tmp_path):
    engine = get_engine(str(tmp_path / "migration-ddl.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO mediasubscription VALUES (1)")
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO mediajob VALUES (2)")
        connection.exec_driver_sql("CREATE TABLE subscriptionrelease (id INTEGER PRIMARY KEY, size_bytes INTEGER NOT NULL)")
        connection.exec_driver_sql("INSERT INTO subscriptionrelease VALUES (7, 1234)")
        connection.exec_driver_sql(
            "CREATE TABLE qualityprofile (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "allowed_resolutions JSON NOT NULL, excluded_tokens JSON NOT NULL, "
            "minimum_seeders INTEGER NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO qualityprofile VALUES "
            "(1, '[\"720p\"]', '[\"CAM\"]', 9, '2026-09-03T00:00:00+00:00')"
        )

    migrate_schema(engine)
    migrate_schema(engine)

    with engine.connect() as connection:
        profile_rows = connection.exec_driver_sql(
            "SELECT media_type, allowed_audio, allowed_hdr, minimum_size_bytes, maximum_size_bytes, "
            "preferred_resolutions, preferred_audio, preferred_hdr, preferred_size_bands, updated_at "
            "FROM qualityprofile ORDER BY media_type"
        ).fetchall()
        job = connection.exec_driver_sql(
            "SELECT source_subscription_id, source_subscription_release_id FROM mediajob WHERE id = 2"
        ).one()
        release = connection.exec_driver_sql(
            "SELECT resolution, audio, hdr, size_bytes FROM subscriptionrelease WHERE id = 7"
        ).one()
        indexes = {
            name: sql for name, sql in connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'subscriptionevent'"
            ).fetchall()
        }

    assert profile_rows == [
        ("MOVIE", "[]", "[]", None, None, "[]", "[]", "[]", "[]", "2026-09-03T00:00:00+00:00"),
        ("TV", "[]", "[]", None, None, "[]", "[]", "[]", "[]", "2026-09-03T00:00:00+00:00"),
    ]
    assert job == (None, None)
    assert release == (None, None, None, 1234)
    assert "WHERE kind = 'RELEASE_MATCH'" in indexes["uq_subscriptionevent_release_match"]
    assert "WHERE kind = 'UPGRADE_PROPOSAL'" in indexes["uq_subscriptionevent_upgrade_proposal"]


def test_migration_rebuilds_downloaded_quality_for_multiple_targets_and_cascades_jobs(tmp_path):
    engine = get_engine(str(tmp_path / "downloaded-quality-rebuild.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO mediasubscription VALUES (1)")
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO mediajob VALUES (2)")
        connection.exec_driver_sql("CREATE TABLE subscriptionrelease (id INTEGER PRIMARY KEY, size_bytes INTEGER NOT NULL)")
        connection.exec_driver_sql("INSERT INTO subscriptionrelease VALUES (3, 1)")
        connection.exec_driver_sql(
            "CREATE TABLE downloadedquality ("
            "id INTEGER PRIMARY KEY, media_type VARCHAR NOT NULL, target_key VARCHAR NOT NULL, "
            "subscription_id INTEGER REFERENCES mediasubscription(id), "
            "media_job_id INTEGER NOT NULL REFERENCES mediajob(id), resolution VARCHAR NOT NULL, "
            "audio VARCHAR NOT NULL, hdr VARCHAR NOT NULL, size_bytes INTEGER, score_version VARCHAR NOT NULL, "
            "quality_score JSON NOT NULL, recorded_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_downloaded_quality_target UNIQUE (media_type, target_key), "
            "CONSTRAINT uq_downloaded_quality_job UNIQUE (media_job_id))"
        )
        connection.exec_driver_sql(
            "INSERT INTO downloadedquality VALUES "
            "(4, 'TV', 'tv:tmdb:1:season:1:episode:1', 1, 2, '1080p', '5.1', 'hdr', "
            "NULL, 'v1', '[3,2,2]', '2026-09-03T00:00:00', '2026-09-03T00:00:00')"
        )

    migrate_schema(engine)
    migrate_schema(engine)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO downloadedquality "
            "(media_type, target_key, subscription_id, media_job_id, resolution, audio, hdr, size_bytes, "
            "score_version, quality_score, recorded_at, updated_at) VALUES "
            "('TV', 'tv:tmdb:1:season:1:episode:2', 1, 2, '1080p', '5.1', 'hdr', "
            "NULL, 'v1', '[3,2,2]', '2026-09-03T00:00:00', '2026-09-03T00:00:00')"
        )
        ddl = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'downloadedquality'"
        ).scalar()
        connection.exec_driver_sql("DELETE FROM mediajob WHERE id = 2")
        remaining = connection.exec_driver_sql("SELECT COUNT(*) FROM downloadedquality").scalar()

    assert "UNIQUE (media_job_id)" not in ddl
    assert "media_job_id INTEGER NOT NULL REFERENCES mediajob(id) ON DELETE CASCADE" in ddl
    assert remaining == 0


def test_migration_cleans_both_shadow_tables_after_failure_and_retry_preserves_data(tmp_path, monkeypatch):
    engine = get_engine(str(tmp_path / "migration-recovery.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE qualityprofile (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "allowed_resolutions JSON NOT NULL, excluded_tokens JSON NOT NULL, "
            "minimum_seeders INTEGER NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO qualityprofile VALUES (1, '[\"720p\"]', '[\"CAM\"]', 9, '2026-09-03T00:00:00')"
        )
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO organizedfile VALUES (1, 999, '/library/orphan.mkv')")

    original = db._migrate_qualityprofile

    def create_interrupted_quality_shadow(connection):
        original(connection)
        connection.exec_driver_sql("CREATE TABLE qualityprofile_new AS SELECT * FROM qualityprofile")
        connection.exec_driver_sql("DROP TABLE qualityprofile")

    monkeypatch.setattr(db, "_migrate_qualityprofile", create_interrupted_quality_shadow)
    with pytest.raises(IntegrityError):
        migrate_schema(engine)
    monkeypatch.setattr(db, "_migrate_qualityprofile", original)

    with engine.begin() as connection:
        assert connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = 'qualityprofile_new'"
        ).scalar() is None
        assert connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = 'organizedfile_new'"
        ).scalar() is None
        assert connection.exec_driver_sql(
            "SELECT minimum_seeders FROM qualityprofile WHERE id = 1"
        ).scalar() == 9
        connection.exec_driver_sql("DELETE FROM organizedfile WHERE id = 1")

    migrate_schema(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM qualityprofile").scalar() == 2
        assert connection.exec_driver_sql(
            "SELECT allowed_resolutions, minimum_seeders FROM qualityprofile WHERE media_type = 'MOVIE'"
        ).one() == ('["720p"]', 9)


def test_migration_recovers_quality_shadow_when_a_failed_later_step_lost_the_main_table(tmp_path, monkeypatch):
    engine = get_engine(str(tmp_path / "missing-quality-main.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE qualityprofile (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "allowed_resolutions JSON NOT NULL, excluded_tokens JSON NOT NULL, "
            "minimum_seeders INTEGER NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO qualityprofile VALUES (1, '[\"720p\"]', '[\"CAM\"]', 9, '2026-09-03T00:00:00')"
        )

    def lose_main_after_quality_copy(failing_engine):
        with failing_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE qualityprofile_new AS SELECT * FROM qualityprofile")
            connection.exec_driver_sql("DROP TABLE qualityprofile")
        raise RuntimeError("later migration failed")

    monkeypatch.setattr(db, "_migrate_schema", lose_main_after_quality_copy)
    with pytest.raises(RuntimeError, match="later migration failed"):
        migrate_schema(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = 'qualityprofile_new'"
        ).scalar() is None
        assert connection.exec_driver_sql(
            "SELECT minimum_seeders FROM qualityprofile WHERE id = 1"
        ).scalar() == 9


def test_subscription_release_retains_existing_size_bytes_and_adds_only_observed_attributes(tmp_path):
    engine = get_engine(str(tmp_path / "existing-release.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)")
        connection.exec_driver_sql("INSERT INTO mediajob VALUES (3, 'Existing film job')")
        connection.exec_driver_sql(
            "CREATE TABLE subscriptionrelease (id INTEGER PRIMARY KEY, size_bytes INTEGER NOT NULL)"
        )
        connection.exec_driver_sql("INSERT INTO subscriptionrelease VALUES (7, 1234)")

    migrate_schema(engine)

    with engine.connect() as connection:
        release_columns = [
            column[1]
            for column in connection.exec_driver_sql("PRAGMA table_info(subscriptionrelease)").fetchall()
        ]
        assert connection.exec_driver_sql(
            "SELECT size_bytes FROM subscriptionrelease WHERE id = 7"
        ).scalar() == 1234
        assert connection.exec_driver_sql("SELECT title FROM mediajob WHERE id = 3").scalar() == "Existing film job"

    assert release_columns.count("size_bytes") == 1
    assert {"resolution", "audio", "hdr"} <= set(release_columns)


def test_quality_and_event_schema_constraints_reject_duplicate_contracts(tmp_path):
    engine = get_engine(str(tmp_path / "quality-contracts.db"))
    SQLModel.metadata.create_all(engine)
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        subscription = MediaSubscription(tmdb_id=1, type=MediaType.MOVIE, title="Film")
        job = MediaJob(
            type=MediaType.MOVIE, title="Film", release_title="Film.2160p", qbit_hash="hash",
            category="skald-movie",
        )
        other_job = MediaJob(
            type=MediaType.MOVIE, title="Other", release_title="Other.1080p", qbit_hash="other-hash",
            category="skald-movie",
        )
        third_job = MediaJob(
            type=MediaType.MOVIE, title="Third", release_title="Third.720p", qbit_hash="third-hash",
            category="skald-movie",
        )
        session.add_all([subscription, job, other_job, third_job])
        session.commit()
        session.add(QualityProfile(media_type=MediaType.MOVIE))
        session.commit()
        session.add(QualityProfile(media_type=MediaType.MOVIE))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add_all([
            DownloadedQuality(
                media_type=MediaType.MOVIE, target_key="movie:tmdb:1", media_job_id=job.id,
                resolution="2160p", audio="atmos", hdr="dolby_vision", size_bytes=None,
                score_version="v1", quality_score=[4, 4, 5],
            ),
            SubscriptionEvent(
                subscription_id=subscription.id, media_type=MediaType.MOVIE,
                kind=SubscriptionEventKind.RELEASE_MATCH, dedupe_key="release:1",
                title="New release", body="Film.2160p",
            ),
        ])
        session.commit()
        session.add(DownloadedQuality(
            media_type=MediaType.MOVIE, target_key="movie:tmdb:2", media_job_id=job.id,
            resolution="1080p", audio="5.1", hdr="hdr", size_bytes=None,
            score_version="v1", quality_score=[3, 2, 2],
        ))
        session.commit()

        session.add(DownloadedQuality(
            media_type=MediaType.MOVIE, target_key="movie:tmdb:1", media_job_id=other_job.id,
            resolution="1080p", audio="5.1", hdr="hdr", size_bytes=None,
            score_version="v1", quality_score=[3, 2, 2],
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        event = session.exec(select(SubscriptionEvent)).one()
        session.add(SubscriptionEvent(
            subscription_id=subscription.id, media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.RELEASE_MATCH, dedupe_key="release:1",
            title="Duplicate release", body="Film.2160p",
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        upgrade = SubscriptionEvent(
            subscription_id=subscription.id, media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.UPGRADE_PROPOSAL, dedupe_key="upgrade:movie:tmdb:1:1",
            title="Upgrade", body="Film.2160p",
        )
        session.add(upgrade)
        session.commit()
        session.add(SubscriptionEvent(
            subscription_id=subscription.id, media_type=MediaType.MOVIE,
            kind=SubscriptionEventKind.UPGRADE_PROPOSAL, dedupe_key="upgrade:movie:tmdb:1:1",
            title="Duplicate upgrade", body="Film.2160p",
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(NotificationDeliveryAttempt(event_id=event.id, channel=NotificationChannel.EMAIL))
        session.commit()
        session.add(NotificationDeliveryAttempt(event_id=event.id, channel=NotificationChannel.EMAIL))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO qualityprofile "
                "(media_type, allowed_resolutions, allowed_audio, allowed_hdr, minimum_seeders, "
                "excluded_tokens, preferred_resolutions, preferred_audio, preferred_hdr, "
                "preferred_size_bands, updated_at) "
                "VALUES ('BOOK', '[]', '[]', '[]', 0, '[]', '[]', '[]', '[]', '[]', ?) ",
                (now,),
            )


def test_migrated_event_indexes_reject_semantic_duplicates_with_divergent_dedupe_keys(tmp_path):
    engine = get_engine(str(tmp_path / "event-indexes.db"))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE mediasubscription (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO mediasubscription VALUES (1)")
        connection.exec_driver_sql("CREATE TABLE mediajob (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE subscriptionrelease (id INTEGER PRIMARY KEY, size_bytes INTEGER)")
        connection.exec_driver_sql("INSERT INTO subscriptionrelease VALUES (7, 1)")

    migrate_schema(engine)
    event_sql = (
        "INSERT INTO subscriptionevent "
        "(subscription_id, subscription_release_id, media_type, target_key, kind, dedupe_key, title, body, created_at) "
        "VALUES (1, 7, 'MOVIE', ?, ?, ?, 'Event', 'Body', CURRENT_TIMESTAMP)"
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(event_sql, (None, "RELEASE_MATCH", "release:first"))
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(event_sql, (None, "RELEASE_MATCH", "release:second"))
        connection.exec_driver_sql(event_sql, ("movie:tmdb:1", "UPGRADE_PROPOSAL", "upgrade:first"))
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(event_sql, ("movie:tmdb:1", "UPGRADE_PROPOSAL", "upgrade:second"))
