from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .models import utcnow


class URLCreate(BaseModel):
    long_url: HttpUrl
    custom_alias: Optional[str] = Field(None, min_length=3, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    expires_at: Optional[datetime] = Field(None, description="ISO-8601; must be in the future. Naive values are treated as UTC.")
    max_clicks: Optional[int] = Field(None, ge=1, description="Redirect limit; unset means unlimited.")

    @field_validator("expires_at")
    @classmethod
    def _normalise_expiry(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:  # normalise to naive UTC so comparisons never raise
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        if value <= utcnow():
            raise ValueError("expires_at must be in the future")
        return value


class URLResponse(BaseModel):
    short_code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    max_clicks: Optional[int] = None


class ClickEvent(BaseModel):
    clicked_at: datetime
    referrer: Optional[str] = None
    user_agent: Optional[str] = None


class DayCount(BaseModel):
    day: str
    clicks: int


class ReferrerCount(BaseModel):
    referrer: str
    clicks: int


class AnalyticsResponse(BaseModel):
    short_code: str
    long_url: str
    total_clicks: int
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_accessed: Optional[datetime] = None
    max_clicks: Optional[int] = None
    remaining_clicks: Optional[int] = None
    recent_clicks: List[ClickEvent] = []
    clicks_by_day: List[DayCount] = []
    top_referrers: List[ReferrerCount] = []
