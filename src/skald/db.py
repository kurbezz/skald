from sqlmodel import Session, create_engine


def get_engine(db_path: str):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def get_session(engine) -> Session:
    return Session(engine)


def migrate_schema(engine) -> None:
    """Apply additive schema changes for existing SQLite databases."""
    with engine.begin() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
        column_names = {column[1] for column in columns}
        if "library_path" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN library_path VARCHAR")
        # NOTE: the literals below for `organization_mode`/`lifecycle` must be
        # the uppercase Python Enum *member names* (`SCALAR`, `PACK`,
        # `LEGACY_UNVERIFIED`), not their lowercase string *values*.
        # SQLModel's `Enum` column type reads/writes member names by default,
        # so every ORM access of these two new columns expects that encoding.
        # This does not apply to the pre-existing `status`/`type` columns,
        # which already hold real production data under that same
        # name-based encoding and must not be touched here.
        if "organization_mode" not in column_names:
            connection.exec_driver_sql(
                "ALTER TABLE mediajob ADD COLUMN organization_mode VARCHAR NOT NULL DEFAULT 'SCALAR'"
            )
        if "operation_token" not in column_names:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN operation_token VARCHAR")
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS organizedfile "
            "(id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL, "
            "operation_token VARCHAR, lifecycle VARCHAR NOT NULL DEFAULT 'LEGACY_UNVERIFIED', "
            "staging_path VARCHAR, staging_device INTEGER, staging_inode INTEGER, "
            "published_device INTEGER, published_inode INTEGER)"
        )
        organized_columns = {
            column[1] for column in connection.exec_driver_sql("PRAGMA table_info(organizedfile)").fetchall()
        }
        additions = {
            "operation_token": "VARCHAR",
            "lifecycle": "VARCHAR NOT NULL DEFAULT 'LEGACY_UNVERIFIED'",
            "staging_path": "VARCHAR",
            "staging_device": "INTEGER",
            "staging_inode": "INTEGER",
            "published_device": "INTEGER",
            "published_inode": "INTEGER",
        }
        for name, definition in additions.items():
            if name not in organized_columns:
                connection.exec_driver_sql(f"ALTER TABLE organizedfile ADD COLUMN {name} {definition}")

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
        duplicate = connection.exec_driver_sql(
            "SELECT path FROM organizedfile GROUP BY path HAVING COUNT(*) > 1 LIMIT 1"
        ).scalar()
        if duplicate is not None:
            raise RuntimeError(
                f"Cannot create unique organized-file ledger index: duplicate ledger path reservations for {duplicate}"
            )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_organizedfile_path ON organizedfile (path)"
        )
