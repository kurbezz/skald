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


def migrate_schema(engine) -> None:
    try:
        _migrate_schema(engine)
    except Exception:
        # SQLite can persist CREATE TABLE across the failed copy while the
        # surrounding transaction later rolls back a same-transaction DROP.
        # Use a fresh transaction so this retry-only shadow never survives.
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE IF EXISTS organizedfile_new")
        except Exception:
            pass
        raise


def _migrate_schema(engine) -> None:
    """Apply schema changes while preserving existing mediajob encodings."""
    with engine.begin() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
        column_names = {column[1] for column in columns}
        if "library_path" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN library_path VARCHAR")
        if "episode_set" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN episode_set VARCHAR")
        # SQLModel persists these Enum member names in uppercase. Do not
        # alter pre-existing mediajob status/type encodings.
        if "organization_mode" not in column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediajob ADD COLUMN organization_mode VARCHAR NOT NULL DEFAULT 'SCALAR'"
            )
        if "operation_token" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN operation_token VARCHAR")

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
