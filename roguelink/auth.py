"""Lightweight password auth for the dashboard.

Default credentials are ``admin`` / ``roguelink`` and are written on first
install if no auth file exists. The operator can change them from the
dashboard System page or via ``roguelink set-password``.
"""

import hashlib
import secrets
from typing import Tuple

from . import paths
from .utils import load_json, save_json


PBKDF_ITERATIONS = 200_000

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "roguelink"


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


def ensure_default_password() -> Tuple[bool, str, str]:
    """Create default admin/roguelink credentials if no auth file exists.

    Returns (created, username, password). When ``created`` is False the
    existing credentials were left untouched.
    """
    if is_configured():
        data = load()
        return False, data.get("username", DEFAULT_USERNAME), ""
    set_password(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    return True, DEFAULT_USERNAME, DEFAULT_PASSWORD


def change_password(username: str, current: str, new_password: str) -> Tuple[bool, str]:
    """Change password after verifying ``current``. Returns (ok, message)."""
    if not new_password or len(new_password) < 4:
        return False, "new password must be at least 4 characters"
    if not verify(username, current):
        return False, "current password is incorrect"
    set_password(username, new_password)
    return True, "password updated"
