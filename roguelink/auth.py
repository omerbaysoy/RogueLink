"""Lightweight password auth for the dashboard.

Stores a salted SHA-256 hash on disk. The plaintext password is never stored
after the initial generation step.
"""

import hashlib
import os
import secrets
from typing import Optional, Tuple

from . import paths
from .utils import load_json, save_json, write_text


PBKDF_ITERATIONS = 200_000


def _hash(password: str, salt: str) -> str:
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF_ITERATIONS
    )
    return derived.hex()


def load() -> dict:
    return load_json(paths.AUTH_PATH, default={})


def is_configured() -> bool:
    data = load()
    return bool(data.get("username") and data.get("hash") and data.get("salt"))


def set_password(username: str, password: str) -> bool:
    salt = secrets.token_hex(16)
    hashed = _hash(password, salt)
    return save_json(
        paths.AUTH_PATH,
        {
            "username": username,
            "salt": salt,
            "hash": hashed,
            "iterations": PBKDF_ITERATIONS,
        },
        mode=0o600,
    )


def verify(username: str, password: str) -> bool:
    data = load()
    if not data:
        return False
    if data.get("username") != username:
        return False
    salt = data.get("salt", "")
    if not salt:
        return False
    expected = data.get("hash", "")
    candidate = _hash(password, salt)
    return secrets.compare_digest(expected, candidate)


def ensure_initial_password(username: str = "admin") -> Tuple[bool, Optional[str]]:
    """If no password is configured, generate one. Returns (created, password)."""
    if is_configured():
        return False, None
    password = secrets.token_urlsafe(12)
    set_password(username, password)
    write_text(
        paths.INITIAL_PASSWORD_PATH,
        f"# RogueLink initial admin credentials\n"
        f"# Delete this file after you change the password.\n"
        f"username={username}\n"
        f"password={password}\n",
        mode=0o600,
    )
    return True, password


def clear_initial_password_file() -> None:
    if os.path.exists(paths.INITIAL_PASSWORD_PATH):
        try:
            os.remove(paths.INITIAL_PASSWORD_PATH)
        except OSError:
            pass
