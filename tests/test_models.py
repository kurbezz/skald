from sqlmodel import Session, select

from skald.db import get_engine, get_session
from skald.models import JobStatus, MediaJob, MediaType


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
