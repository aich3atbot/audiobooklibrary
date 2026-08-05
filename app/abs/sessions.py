"""Server-side refresh-token sessions, so a sign-out actually revokes.

Access tokens stay stateless (they expire in an hour); the *refresh* token is
what keeps a client logged in for 30 days, so it gets a row. Mirrors ABS's
`TokenManager`/session model — including the rotation grace period, without
which the concurrent refreshes clients fire on resume would knock each other
out. Only SHA-256 hashes are stored.

Contract: docs/abs-api-contract.md.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.abs import payloads, tokens
from app.models import AuthSession, User

# How long the previous refresh token keeps working after a rotation. Upstream
# default (REFRESH_TOKEN_GRACE_PERIOD); a client whose rotation response never
# arrived can still recover instead of being logged out.
GRACE_PERIOD = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client_info(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    user_agent = request.headers.get("user-agent")
    host = request.client.host if request.client else None
    return (user_agent[:500] if user_agent else None), host


def create(
    db: Session, user: User, refresh_token: str, request: Request | None = None
) -> AuthSession:
    """Record a new login. Also drops the user's expired rows — sessions are
    only ever created here, so this is enough to keep the table bounded."""
    prune_expired(db, user)
    user_agent, ip_address = _client_info(request)
    session = AuthSession(
        user_id=user.id,
        token_hash=token_hash(refresh_token),
        expires_at=_utcnow() + tokens.REFRESH_TOKEN_EXPIRY,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    db.add(session)
    db.commit()
    return session


def find(db: Session, refresh_token: str) -> AuthSession | None:
    """The session a refresh token belongs to, current or within grace."""
    digest = token_hash(refresh_token)
    return db.scalar(
        select(AuthSession).where(
            or_(AuthSession.token_hash == digest, AuthSession.last_token_hash == digest)
        )
    )


def resolve(db: Session, refresh_token: str) -> tuple[AuthSession | None, bool]:
    """-> (session, is_grace). A `None` session means reject the refresh: no
    such session (signed out, or revoked from another device), the session has
    expired, or the token is a superseded one whose grace window has closed.

    `is_grace` marks a token that has already been rotated away but is still
    inside the window — the caller must not rotate again on it."""
    session = find(db, refresh_token)
    if session is None:
        return None, False

    if session.token_hash != token_hash(refresh_token):
        within_grace = (
            session.last_token_expires_at is not None
            and session.last_token_expires_at > _utcnow()
        )
        return (session, True) if within_grace else (None, False)

    if session.expires_at <= _utcnow():
        db.delete(session)
        db.commit()
        return None, False
    return session, False


def rotate(db: Session, session: AuthSession, new_refresh_token: str) -> None:
    session.last_token_hash = session.token_hash
    session.last_token_expires_at = _utcnow() + GRACE_PERIOD
    session.token_hash = token_hash(new_refresh_token)
    session.expires_at = _utcnow() + tokens.REFRESH_TOKEN_EXPIRY
    db.commit()


def touch(db: Session, session: AuthSession) -> None:
    """Bump updatedAt without rotating (the grace-period path), so the client's
    sessions list still shows the device as recently active."""
    session.updated_at = _utcnow()
    db.commit()


def revoke(db: Session, refresh_token: str) -> AuthSession | None:
    """Sign out one device. Returns the session that was removed, so the caller
    can act on its user (an `allDevices` logout needs it)."""
    session = find(db, refresh_token)
    if session is None:
        return None
    db.delete(session)
    db.commit()
    return session


def revoke_all(db: Session, user: User) -> int:
    count = db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    db.commit()
    return count


def revoke_by_uuid(db: Session, user: User, session_uuid: str) -> bool:
    session = db.scalar(
        select(AuthSession).where(
            AuthSession.uuid == session_uuid, AuthSession.user_id == user.id
        )
    )
    if session is None:
        return False
    db.delete(session)
    db.commit()
    return True


def prune_expired(db: Session, user: User | None = None) -> None:
    statement = delete(AuthSession).where(AuthSession.expires_at <= _utcnow())
    if user is not None:
        statement = statement.where(AuthSession.user_id == user.id)
    db.execute(statement)
    db.commit()


def active_for(db: Session, user: User) -> list[AuthSession]:
    return list(
        db.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.expires_at > _utcnow())
            .order_by(AuthSession.updated_at.desc())
        )
    )


def session_json(session: AuthSession, current: bool) -> dict:
    """`deviceInfo` is upstream's *parsed* user agent. We don't parse UAs, so
    it stays null and clients fall back to the raw string (which is how they
    label the app's own sessions, e.g. "Absorb/1.2.3")."""
    return {
        "id": session.uuid,
        "ipAddress": session.ip_address,
        "userAgent": session.user_agent,
        "deviceInfo": None,
        "createdAt": payloads.epoch_ms(session.created_at),
        "updatedAt": payloads.epoch_ms(session.updated_at),
        "current": current,
    }
