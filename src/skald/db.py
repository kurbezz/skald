from sqlmodel import Session, create_engine


def get_engine(db_path: str):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def get_session(engine) -> Session:
    return Session(engine)
