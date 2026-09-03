import json

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine


def get_engine(db_path: str, *, enforce_foreign_keys: bool = True) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    if enforce_foreign_keys:
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


def get_session(engine) -> Session:
    return Session(engine)


def _organizedfile_requires_rebuild(connection) -> bool:
    columns = {
        column[1]: column
        for column in connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
    }
    required_columns = {
        "id", "job_id", "path", "operation_token", "lifecycle", "staging_path",
        "staging_device", "staging_inode", "published_device", "published_inode",
    }
    if not required_columns.issubset(columns):
        return True
    lifecycle = columns["lifecycle"]
    if lifecycle[3] != 1 or lifecycle[4] is None or lifecycle[4].strip("'") != "LEGACY_UNVERIFIED":
        return True
    foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list(organizedfile)").fetchall()
    if not any(key[2:5] == ("mediajob", "job_id", "id") for key in foreign_keys):
        return True
    indexes = connection.exec_driver_sql("PRAGMA index_list(organizedfile)").fetchall()
    for index in indexes:
        if index[2] and [column[2] for column in connection.exec_driver_sql(
            f"PRAGMA index_info({index[1]})"
        ).fetchall()] == ["path"]:
            return False
    return True


def _rebuild_organizedfile(connection) -> None:
    old_columns = {
        column[1]
        for column in connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
    }
    duplicate = connection.exec_driver_sql(
        "SELECT path FROM organizedfile GROUP BY path HAVING COUNT(*) > 1 LIMIT 1"
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            f"Cannot create unique organized-file ledger index: duplicate ledger path reservations for {duplicate}"
        )
    try:
        connection.exec_driver_sql(
            "CREATE TABLE organizedfile_new ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "job_id INTEGER NOT NULL REFERENCES mediajob(id), "
            "path VARCHAR NOT NULL UNIQUE, "
            "operation_token VARCHAR, "
            "lifecycle VARCHAR NOT NULL DEFAULT 'LEGACY_UNVERIFIED', "
            "staging_path VARCHAR, staging_device INTEGER, staging_inode INTEGER, "
            "published_device INTEGER, published_inode INTEGER)"
        )

        def value_or_default(column: str, default: str = "NULL") -> str:
            return column if column in old_columns else default

        lifecycle = (
            "COALESCE(lifecycle, 'LEGACY_UNVERIFIED')"
            if "lifecycle" in old_columns else "'LEGACY_UNVERIFIED'"
        )
        connection.exec_driver_sql(
            "INSERT INTO organizedfile_new "
            "(id, job_id, path, operation_token, lifecycle, staging_path, staging_device, staging_inode, "
            "published_device, published_inode) "
            "SELECT id, job_id, path, "
            f"{value_or_default('operation_token')}, {lifecycle}, "
            f"{value_or_default('staging_path')}, {value_or_default('staging_device')}, "
            f"{value_or_default('staging_inode')}, {value_or_default('published_device')}, "
            f"{value_or_default('published_inode')} FROM organizedfile"
        )
        connection.exec_driver_sql("DROP TABLE organizedfile")
        connection.exec_driver_sql("ALTER TABLE organizedfile_new RENAME TO organizedfile")
    except Exception:
        try:
            connection.exec_driver_sql("DROP TABLE IF EXISTS organizedfile_new")
        except Exception:
            pass
        raise


_DEFAULT_RESOLUTIONS = ["1080p", "2160p"]
_DEFAULT_EXCLUDED_TOKENS = ["CAM", "TS", "TeleSync"]
_RESOLUTION_ALIASES = {
    "4k": "2160p",
    "480p": "480p",
    "720p": "720p",
    "1080p": "1080p",
    "2160p": "2160p",
}


def _table_exists(connection, table: str) -> bool:
    return connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).scalar() is not None


def _recover_shadow_table(connection, shadow: str, primary: str) -> None:
    """Discard an abandoned copy, or promote it when the original was lost."""
    if not _table_exists(connection, shadow):
        return
    if _table_exists(connection, primary):
        connection.exec_driver_sql(f"DROP TABLE {shadow}")
    else:
        connection.exec_driver_sql(f"ALTER TABLE {shadow} RENAME TO {primary}")


def _valid_legacy_list(value) -> list[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded) else []


def _legacy_resolutions(value) -> list[str]:
    resolutions: list[str] = []
    for item in _valid_legacy_list(value):
        normalized = _RESOLUTION_ALIASES.get(item.casefold())
        if normalized is not None and normalized not in resolutions:
            resolutions.append(normalized)
    return resolutions or _DEFAULT_RESOLUTIONS.copy()


def _legacy_excluded_tokens(value) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for item in _valid_legacy_list(value):
        token = item.strip()
        folded = token.casefold()
        if token and len(token) <= 64 and folded not in seen:
            seen.add(folded)
            tokens.append(token)
    return tokens or _DEFAULT_EXCLUDED_TOKENS.copy()


def _create_qualityprofile_table(connection, table: str = "qualityprofile") -> None:
    connection.exec_driver_sql(
        f"CREATE TABLE {table} ("
        "id INTEGER NOT NULL PRIMARY KEY, "
        "media_type VARCHAR NOT NULL, "
        "allowed_resolutions JSON NOT NULL, allowed_audio JSON NOT NULL, allowed_hdr JSON NOT NULL, "
        "minimum_size_bytes INTEGER, maximum_size_bytes INTEGER, minimum_seeders INTEGER NOT NULL, "
        "excluded_tokens JSON NOT NULL, preferred_resolutions JSON NOT NULL, "
        "preferred_audio JSON NOT NULL, preferred_hdr JSON NOT NULL, "
        "preferred_size_bands JSON NOT NULL, updated_at DATETIME NOT NULL, "
        "CONSTRAINT uq_qualityprofile_media_type UNIQUE (media_type), "
        "CONSTRAINT ck_qualityprofile_media_type CHECK (media_type IN ('MOVIE', 'TV'))"
        ")"
    )


def _insert_default_quality_profiles(connection) -> None:
    values = (
        json.dumps(_DEFAULT_RESOLUTIONS), json.dumps([]), json.dumps([]), 5,
        json.dumps(_DEFAULT_EXCLUDED_TOKENS), json.dumps([]), json.dumps([]),
        json.dumps([]), json.dumps([]),
    )
    for media_type in ("MOVIE", "TV"):
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO qualityprofile "
            "(media_type, allowed_resolutions, allowed_audio, allowed_hdr, minimum_size_bytes, "
            "maximum_size_bytes, minimum_seeders, excluded_tokens, preferred_resolutions, "
            "preferred_audio, preferred_hdr, preferred_size_bands, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (media_type, *values),
        )


_QUALITYPROFILE_COLUMNS = {
    "id", "media_type", "allowed_resolutions", "allowed_audio", "allowed_hdr",
    "minimum_size_bytes", "maximum_size_bytes", "minimum_seeders", "excluded_tokens",
    "preferred_resolutions", "preferred_audio", "preferred_hdr", "preferred_size_bands",
    "updated_at",
}


def _json_list(value, default: list) -> list:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default.copy()
    return decoded if isinstance(decoded, list) else default.copy()


def _qualityprofile_requires_rebuild(connection, columns: set[str]) -> bool:
    if not _QUALITYPROFILE_COLUMNS.issubset(columns):
        return True
    ddl = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'qualityprofile'"
    ).scalar() or ""
    normalized_ddl = ddl.upper()
    return (
        "CONSTRAINT UQ_QUALITYPROFILE_MEDIA_TYPE" not in normalized_ddl
        or "CONSTRAINT CK_QUALITYPROFILE_MEDIA_TYPE" not in normalized_ddl
    )


def _profile_values(row, columns: set[str]) -> tuple:
    values = row._mapping

    def value(name: str):
        return values[name] if name in columns else None

    def string_list(name: str) -> list[str]:
        return _valid_legacy_list(value(name))

    def optional_integer(name: str):
        candidate = value(name)
        return candidate if isinstance(candidate, int) else None

    seeders = value("minimum_seeders")
    return (
        _legacy_resolutions(value("allowed_resolutions")),
        string_list("allowed_audio"),
        string_list("allowed_hdr"),
        optional_integer("minimum_size_bytes"),
        optional_integer("maximum_size_bytes"),
        seeders if isinstance(seeders, int) and seeders >= 0 else 5,
        _legacy_excluded_tokens(value("excluded_tokens")),
        string_list("preferred_resolutions"),
        string_list("preferred_audio"),
        string_list("preferred_hdr"),
        _json_list(value("preferred_size_bands"), []),
        value("updated_at"),
    )


def _insert_qualityprofile(connection, media_type: str, values: tuple) -> None:
    (
        resolutions, allowed_audio, allowed_hdr, minimum_size, maximum_size, minimum_seeders,
        excluded_tokens, preferred_resolutions, preferred_audio, preferred_hdr, preferred_size_bands,
        updated_at,
    ) = values
    connection.exec_driver_sql(
        "INSERT INTO qualityprofile_new "
        "(media_type, allowed_resolutions, allowed_audio, allowed_hdr, minimum_size_bytes, "
        "maximum_size_bytes, minimum_seeders, excluded_tokens, preferred_resolutions, "
        "preferred_audio, preferred_hdr, preferred_size_bands, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
        (
            media_type, json.dumps(resolutions), json.dumps(allowed_audio), json.dumps(allowed_hdr),
            minimum_size, maximum_size, minimum_seeders, json.dumps(excluded_tokens),
            json.dumps(preferred_resolutions), json.dumps(preferred_audio), json.dumps(preferred_hdr),
            json.dumps(preferred_size_bands), updated_at,
        ),
    )


def _migrate_qualityprofile(connection) -> None:
    if not _table_exists(connection, "qualityprofile"):
        _create_qualityprofile_table(connection)
        _insert_default_quality_profiles(connection)
        return

    columns = {
        column[1] for column in connection.exec_driver_sql("PRAGMA table_info(qualityprofile)").fetchall()
    }
    if "media_type" in columns and not _qualityprofile_requires_rebuild(connection, columns):
        _insert_default_quality_profiles(connection)
        return

    rows = connection.exec_driver_sql("SELECT * FROM qualityprofile ORDER BY id").fetchall()
    preserved: dict[str, tuple] = {}
    for row in rows:
        raw_media_type = row._mapping.get("media_type") if "media_type" in columns else None
        media_type = str(raw_media_type).upper() if raw_media_type is not None else None
        if media_type in ("MOVIE", "TV") and media_type not in preserved:
            preserved[media_type] = _profile_values(row, columns)
    if "media_type" not in columns and rows:
        legacy_values = _profile_values(rows[0], columns)
        preserved = {"MOVIE": legacy_values, "TV": legacy_values}

    connection.exec_driver_sql("DROP TABLE IF EXISTS qualityprofile_new")
    _create_qualityprofile_table(connection, "qualityprofile_new")
    for media_type in ("MOVIE", "TV"):
        _insert_qualityprofile(
            connection,
            media_type,
            preserved.get(
                media_type,
                (_DEFAULT_RESOLUTIONS, [], [], None, None, 5, _DEFAULT_EXCLUDED_TOKENS, [], [], [], [], None),
            ),
        )
    connection.exec_driver_sql("DROP TABLE qualityprofile")
    connection.exec_driver_sql("ALTER TABLE qualityprofile_new RENAME TO qualityprofile")


def migrate_schema(engine) -> None:
    try:
        _migrate_schema(engine)
    except Exception:
        # SQLite can persist CREATE TABLE across the failed copy while the
        # surrounding transaction later rolls back a same-transaction DROP.
        # Use a fresh transaction so this retry-only shadow never survives.
        try:
            with engine.begin() as connection:
                _recover_shadow_table(connection, "qualityprofile_new", "qualityprofile")
                _recover_shadow_table(connection, "organizedfile_new", "organizedfile")
        except Exception:
            pass
        raise


def _migrate_schema(engine) -> None:
    """Apply schema changes while preserving existing mediajob encodings."""
    with engine.begin() as connection:
        _migrate_qualityprofile(connection)

        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
        column_names = {column[1] for column in columns}
        if columns and "library_path" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN library_path VARCHAR")
        if columns and "episode_set" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN episode_set VARCHAR")
        # SQLModel persists these Enum member names in uppercase. Do not
        # alter pre-existing mediajob status/type encodings.
        if columns and "organization_mode" not in column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediajob ADD COLUMN organization_mode VARCHAR NOT NULL DEFAULT 'SCALAR'"
            )
        if columns and "operation_token" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN operation_token VARCHAR")
        if columns and "source_subscription_id" not in column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediajob ADD COLUMN source_subscription_id INTEGER "
                "REFERENCES mediasubscription(id)"
            )
        if columns and "source_subscription_release_id" not in column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediajob ADD COLUMN source_subscription_release_id INTEGER "
                "REFERENCES subscriptionrelease(id)"
            )
        if columns:
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_mediajob_source_subscription_id "
                "ON mediajob (source_subscription_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_mediajob_source_subscription_release_id "
                "ON mediajob (source_subscription_release_id)"
            )

        subscription_columns = connection.exec_driver_sql(
            "PRAGMA table_info(mediasubscription)"
        ).fetchall()
        subscription_column_names = {column[1] for column in subscription_columns}
        if subscription_columns and "auto_download" not in subscription_column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediasubscription ADD COLUMN auto_download BOOLEAN NOT NULL DEFAULT 0"
            )
        if subscription_columns and "auto_grabbed_release_id" not in subscription_column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediasubscription ADD COLUMN auto_grabbed_release_id INTEGER"
            )

        release_columns = connection.exec_driver_sql("PRAGMA table_info(subscriptionrelease)").fetchall()
        release_column_names = {column[1] for column in release_columns}
        for column in ("resolution", "audio", "hdr"):
            if release_columns and column not in release_column_names:
                connection.exec_driver_sql(f"ALTER TABLE subscriptionrelease ADD COLUMN {column} VARCHAR")

        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS downloadedquality ("
            "id INTEGER NOT NULL PRIMARY KEY, media_type VARCHAR NOT NULL, target_key VARCHAR NOT NULL, "
            "subscription_id INTEGER REFERENCES mediasubscription(id), "
            "media_job_id INTEGER NOT NULL REFERENCES mediajob(id), resolution VARCHAR NOT NULL, "
            "audio VARCHAR NOT NULL, hdr VARCHAR NOT NULL, size_bytes INTEGER, score_version VARCHAR NOT NULL, "
            "quality_score JSON NOT NULL, recorded_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_downloaded_quality_target UNIQUE (media_type, target_key), "
            "CONSTRAINT uq_downloaded_quality_job UNIQUE (media_job_id))"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_downloadedquality_subscription_id "
            "ON downloadedquality (subscription_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_downloadedquality_media_job_id ON downloadedquality (media_job_id)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS subscriptionevent ("
            "id INTEGER NOT NULL PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES mediasubscription(id), "
            "subscription_release_id INTEGER REFERENCES subscriptionrelease(id), media_type VARCHAR NOT NULL, "
            "target_key VARCHAR, kind VARCHAR NOT NULL, dedupe_key VARCHAR NOT NULL, title VARCHAR NOT NULL, "
            "body VARCHAR NOT NULL, prior_quality_score JSON, current_quality_score JSON, "
            "created_at DATETIME NOT NULL, read_at DATETIME, "
            "CONSTRAINT uq_subscription_event_dedupe UNIQUE (dedupe_key))"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionevent_subscription_id ON subscriptionevent (subscription_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionevent_subscription_release_id "
            "ON subscriptionevent (subscription_release_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionevent_target_key ON subscriptionevent (target_key)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionevent_dedupe_key ON subscriptionevent (dedupe_key)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionevent_created_at ON subscriptionevent (created_at)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptionevent_release_match "
            "ON subscriptionevent (subscription_release_id, kind) "
            "WHERE kind = 'RELEASE_MATCH' AND subscription_release_id IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptionevent_upgrade_proposal "
            "ON subscriptionevent (target_key, subscription_release_id, kind) "
            "WHERE kind = 'UPGRADE_PROPOSAL' AND target_key IS NOT NULL "
            "AND subscription_release_id IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS notificationdeliveryattempt ("
            "id INTEGER NOT NULL PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES subscriptionevent(id), "
            "channel VARCHAR NOT NULL, outcome VARCHAR NOT NULL, attempted_at DATETIME NOT NULL, "
            "provider_message_id VARCHAR, error_summary VARCHAR, "
            "CONSTRAINT uq_delivery_event_channel UNIQUE (event_id, channel))"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_notificationdeliveryattempt_event_id "
            "ON notificationdeliveryattempt (event_id)"
        )

        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS tvsubscriptionscope ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "subscription_id INTEGER NOT NULL REFERENCES mediasubscription(id) ON DELETE CASCADE, "
            "tmdb_series_id INTEGER NOT NULL, tmdb_season_id INTEGER, tmdb_episode_id INTEGER, "
            "season_number INTEGER, episode_number INTEGER, "
            "includes_future_content BOOLEAN NOT NULL DEFAULT 0, "
            "CONSTRAINT ck_tvsubscriptionscope_shape CHECK ("
            "(includes_future_content = 1 AND tmdb_season_id IS NULL AND tmdb_episode_id IS NULL "
            "AND season_number IS NULL AND episode_number IS NULL) "
            "OR (includes_future_content = 0 AND tmdb_season_id IS NOT NULL AND season_number IS NOT NULL "
            "AND ((tmdb_episode_id IS NULL AND episode_number IS NULL) "
            "OR (tmdb_episode_id IS NOT NULL AND episode_number IS NOT NULL))))"
            ")"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tvsubscriptionscope_subscription_id "
            "ON tvsubscriptionscope (subscription_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tvsubscriptionscope_tmdb_series_id "
            "ON tvsubscriptionscope (tmdb_series_id)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS subscriptionreleasescope ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "subscription_release_id INTEGER NOT NULL REFERENCES subscriptionrelease(id) ON DELETE CASCADE, "
            "tv_subscription_scope_id INTEGER NOT NULL REFERENCES tvsubscriptionscope(id) ON DELETE CASCADE, "
            "CONSTRAINT uq_subscription_release_scope "
            "UNIQUE (subscription_release_id, tv_subscription_scope_id))"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionreleasescope_subscription_release_id "
            "ON subscriptionreleasescope (subscription_release_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_subscriptionreleasescope_tv_subscription_scope_id "
            "ON subscriptionreleasescope (tv_subscription_scope_id)"
        )

        table_exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'organizedfile'"
        ).scalar() is not None
        if not table_exists:
            connection.exec_driver_sql(
                "CREATE TABLE organizedfile ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "job_id INTEGER NOT NULL REFERENCES mediajob(id), "
                "path VARCHAR NOT NULL UNIQUE, "
                "operation_token VARCHAR, "
                "lifecycle VARCHAR NOT NULL DEFAULT 'LEGACY_UNVERIFIED', "
                "staging_path VARCHAR, staging_device INTEGER, staging_inode INTEGER, "
                "published_device INTEGER, published_inode INTEGER)"
            )
        elif _organizedfile_requires_rebuild(connection):
            _rebuild_organizedfile(connection)

        if columns:
            connection.exec_driver_sql(
                "UPDATE mediajob SET organization_mode = 'SCALAR' WHERE organization_mode IS NULL"
            )
            connection.exec_driver_sql(
                "UPDATE mediajob SET organization_mode = 'PACK' "
                "WHERE id IN (SELECT DISTINCT job_id FROM organizedfile)"
            )
        connection.exec_driver_sql(
            "UPDATE organizedfile SET lifecycle = 'LEGACY_UNVERIFIED' WHERE lifecycle IS NULL"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_organizedfile_job_id ON organizedfile (job_id)"
        )
