"""Database wiring. SQLite for the demo; set DATABASE_URL to a Postgres URL to scale out."""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./url_shortener.db")
_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)
if _is_sqlite and DATABASE_URL not in ("sqlite://", "sqlite:///:memory:"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # Write-ahead logging lets reads proceed during a write and cuts fsyncs; busy_timeout waits instead of failing
        # when another request holds the write lock. SQLite still has one writer: the production answer is PostgreSQL
        # and moving click writes off the redirect path (queue + batch insert).
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
