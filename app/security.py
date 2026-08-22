from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .db import db

COOKIE_NAME = "inventario_session"
SESSION_DAYS = 30
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres")
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(UTC).isoformat(),))
        conn.execute(
            "INSERT INTO sessions(user_id, token_hash, csrf_token, expires_at) VALUES(?,?,?,?)",
            (user_id, token_hash(token), csrf, expires.isoformat()),
        )
    return token, csrf


def get_session(token: str | None):
    if not token:
        return None
    now = datetime.now(UTC).isoformat()
    with db() as conn:
        row = conn.execute(
            """SELECT s.id session_id, s.csrf_token, s.expires_at,
                      u.id user_id, u.email, u.display_name, u.is_admin
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>?""",
            (token_hash(token), now),
        ).fetchone()
        return dict(row) if row else None


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))


def csrf_valid(session: dict | None, supplied: str | None) -> bool:
    return bool(session and supplied and secrets.compare_digest(session["csrf_token"], supplied))
