from sqlmodel import Session, create_engine


def get_engine(db_path: str):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def get_session(engine) -> Session:
    return Session(engine)


def migrate_schema(engine) -> None:
    """Apply additive schema changes for existing SQLite databases."""
    with engine.begin() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(mediajob)").fetchall()
        if "library_path" not in {column[1] for column in columns}:
            connection.exec_driver_sql("ALTER TABLE mediajob ADD COLUMN library_path VARCHAR")
