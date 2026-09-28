from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base


def utcnow() -> datetime:
    """Naive UTC 'now' (all timestamps are stored as naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class URL(Base):
    __tablename__ = "urls"

    id = Column(Integer, primary_key=True)
    short_code = Column(String(16), unique=True, index=True, nullable=False)
    long_url = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    max_clicks = Column(Integer, nullable=True)  # NULL = unlimited
    click_count = Column(Integer, nullable=False, default=0, server_default="0")

    clicks = relationship("Click", back_populates="url", cascade="all, delete-orphan")


class Click(Base):
    __tablename__ = "clicks"
    __table_args__ = (Index("ix_clicks_url_id_clicked_at", "url_id", "clicked_at"),)

    id = Column(Integer, primary_key=True)
    url_id = Column(Integer, ForeignKey("urls.id"), nullable=False)
    clicked_at = Column(DateTime, default=utcnow, nullable=False)
    referrer = Column(String(512), nullable=True)
    user_agent = Column(String(512), nullable=True)

    url = relationship("URL", back_populates="clicks")
