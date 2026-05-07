"""Shared utility helpers: subprocess wrappers, file IO, logging."""

import json
import os
import shlex
import subprocess
import time
from typing import Optional, Tuple

from . import paths


def run(cmd: str, timeout: int = 15) -> Tuple[str, int]:
    """Run a shell command. Returns (combined stdout+stderr, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}", 1
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    combined = "\n".join(part for part in (out, err) if part)
    return combined, result.returncode


def run_ok(cmd: str, timeout: int = 15) -> bool:
    _, code = run(cmd, timeout=timeout)
    return code == 0


def read_text(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace").replace("\x00", "").strip()
    except OSError:
        return ""


def write_text(path: str, content: str, mode: int = 0o644) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def save_json(path: str, data, mode: int = 0o600) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        return True
    except OSError:
        return False


def append_log(path: str, message: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}\n")
    except OSError:
        pass


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_root(action: str) -> bool:
    if not is_root():
        print(f"Error: {action} requires root. Run with sudo.")
        return False
    return True


def quote(value: str) -> str:
    return shlex.quote(value)


def pid_alive(pid_path: str, expected_marker: Optional[str] = None) -> Tuple[Optional[int], bool]:
    """Return (pid, alive_and_matches) for the pidfile."""
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None, False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode(errors="ignore").replace("\x00", " ")
    except OSError:
        return pid, False
    if expected_marker and expected_marker not in cmdline:
        return pid, False
    return pid, True


def stop_pid(pid_path: str, expected_marker: Optional[str] = None) -> None:
    pid, matches = pid_alive(pid_path, expected_marker)
    if pid and matches:
        run(f"kill {pid}")
        time.sleep(0.5)
        run(f"kill -0 {pid} && kill -9 {pid}")
    if os.path.exists(pid_path):
        try:
            os.remove(pid_path)
        except OSError:
            pass


def interface_exists(iface: str) -> bool:
    if not iface:
        return False
    return os.path.exists(f"/sys/class/net/{os.path.basename(iface)}")


def ensure_runtime_dir() -> None:
    paths.ensure_dirs()
