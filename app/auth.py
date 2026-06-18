from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import jwt


ALGORITHM = "HS256"


def create_access_token(*, subject: str, payload: dict, secret: str, ttl_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=ttl_minutes)
    to_encode = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        **payload,
    }
    return jwt.encode(to_encode, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[ALGORITHM])
