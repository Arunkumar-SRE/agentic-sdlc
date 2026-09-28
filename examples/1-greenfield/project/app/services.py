"""Business logic. Framework-free: raises domain errors that main.py maps to HTTP responses."""
import logging
import secrets
import string
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import metrics, models, schemas

log = logging.getLogger("shortener")

ALPHABET = string.ascii_letters + string.digits  # base62 -> 62^7 ~ 3.5e12 codes
CODE_LENGTH = 7
MAX_CODE_ATTEMPTS = 5
# Aliases that would be shadowed by (or confusable with) real routes.
RESERVED_ALIASES = {"api", "health", "ready", "metrics", "docs", "redoc", "openapi", "ops"}


class ServiceError(Exception):
    status_code = 500
    detail = "Internal error"


class AliasTaken(ServiceError):
    status_code, detail = 409, "Short code already exists"


class AliasReserved(ServiceError):
    status_code, detail = 422, "Alias is reserved"


class LinkNotFound(ServiceError):
    status_code, detail = 404, "Short URL not found"


class LinkExpired(ServiceError):
    status_code, detail = 410, "Short URL expired"


class CodeGenerationExhausted(ServiceError):
    status_code, detail = 503, "Could not allocate a short code, retry"


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def create_short_url(db: Session, payload: schemas.URLCreate, base_url: str) -> schemas.URLResponse:
    """Insert relying on the UNIQUE constraint (race-safe) instead of check-then-insert."""
    alias = payload.custom_alias
    if alias and alias.lower() in RESERVED_ALIASES:
        raise AliasReserved()

    for _ in range(1 if alias else MAX_CODE_ATTEMPTS):
        url = models.URL(short_code=alias or generate_code(), long_url=str(payload.long_url), expires_at=payload.expires_at)
        db.add(url)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if alias:
                raise AliasTaken()
            metrics.inc("short_code_collisions_total")
            continue
        db.refresh(url)
        metrics.inc("urls_created_total")
        return schemas.URLResponse(
            short_code=url.short_code,
            short_url=f"{base_url}/{url.short_code}",
            long_url=url.long_url,
            created_at=url.created_at,
            expires_at=url.expires_at,
        )
    raise CodeGenerationExhausted()


def _get_url(db: Session, code: str) -> models.URL:
    url = db.execute(select(models.URL).where(models.URL.short_code == code)).scalar_one_or_none()
    if url is None:
        raise LinkNotFound()
    return url


def record_click(db: Session, url_id: int, referrer: Optional[str], user_agent: Optional[str]) -> None:
    """Analytics is best-effort: a failed click write must never break the redirect (availability > accuracy)."""
    try:
        db.add(models.Click(url_id=url_id, referrer=(referrer or None) and referrer[:512], user_agent=(user_agent or None) and user_agent[:512]))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        metrics.inc("click_write_failures_total")
        log.exception("click write failed; redirect still served")


def resolve_url(db: Session, code: str, referrer: Optional[str] = None, user_agent: Optional[str] = None) -> str:
    url = _get_url(db, code)
    if url.expires_at and url.expires_at <= models.utcnow():
        raise LinkExpired()
    long_url, url_id = url.long_url, url.id  # capture before commit expires the instance
    record_click(db, url_id, referrer, user_agent)
    metrics.inc("redirects_total")
    return long_url


def get_analytics(db: Session, code: str) -> schemas.AnalyticsResponse:
    url = _get_url(db, code)
    C = models.Click
    total, last = db.execute(select(func.count(C.id), func.max(C.clicked_at)).where(C.url_id == url.id)).one()

    recent = db.execute(select(C).where(C.url_id == url.id).order_by(C.clicked_at.desc(), C.id.desc()).limit(10)).scalars().all()

    day = func.date(C.clicked_at).label("day")
    by_day = db.execute(select(day, func.count().label("n")).where(C.url_id == url.id).group_by(day).order_by(day.desc()).limit(30)).all()

    n = func.count().label("n")
    refs = db.execute(select(C.referrer, n).where(C.url_id == url.id, C.referrer.is_not(None)).group_by(C.referrer).order_by(desc(n)).limit(5)).all()

    return schemas.AnalyticsResponse(
        short_code=url.short_code,
        long_url=url.long_url,
        total_clicks=total,
        created_at=url.created_at,
        expires_at=url.expires_at,
        last_accessed=last,
        recent_clicks=[schemas.ClickEvent(clicked_at=c.clicked_at, referrer=c.referrer, user_agent=c.user_agent) for c in recent],
        clicks_by_day=[schemas.DayCount(day=str(d), clicks=c) for d, c in by_day],
        top_referrers=[schemas.ReferrerCount(referrer=r, clicks=c) for r, c in refs],
    )
