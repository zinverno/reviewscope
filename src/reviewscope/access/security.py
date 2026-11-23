"""Password and session material used only at the local access boundary."""

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw

ROLES = {
    "read": {"owner", "analyst", "viewer"},
    "members": {"owner"},
    "register_delivery": {"owner", "analyst"},
    "start_analysis": {"owner", "analyst"},
    "retention": {"owner"},
}


def permits(role: str, action: str) -> bool:
    return role in ROLES.get(action, ())


def normalize(login: str) -> str:
    value = login.strip().lower()
    if not login.isascii() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value):
        raise ValueError("invalid account name")
    return value


def server_key() -> bytes:
    path = Path(os.environ["RS_ACCESS_KEY_FILE"])
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("private key file required")
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError("32 random bytes required")
    return key


def password_proof(password: str, salt: bytes, key: bytes) -> bytes:
    derived = hash_secret_raw(
        password.encode(),
        salt,
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=32,
        type=Type.ID,
        version=19,
    )
    return hmac.digest(key, b"password\0" + derived, "sha256")


def credential(password: str, key: bytes) -> tuple[bytes, bytes]:
    if not 12 <= len(password) <= 256:
        raise ValueError("password length must be 12 to 256 characters")
    salt = secrets.token_bytes(16)
    return salt, hashlib.sha256(password_proof(password, salt, key)).digest()


def session_proof(token: str, key: bytes) -> str:
    return hmac.digest(key, b"session\0" + token.encode("ascii"), "sha256").hex()
