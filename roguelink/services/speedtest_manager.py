"""Speed test using the speedtest-cli tool (CLI binary or Python module).

The result is persisted to ``/var/lib/roguelink/speedtest_last.json`` and
events are appended to ``/var/log/roguelink/speedtest.log``.
"""

from __future__ import annotations

import json
import shlex
import shutil
import time
from typing import Any, Dict, Optional

from .. import paths
from ..utils import append_log, run, save_json, load_json


def _have_speedtest_binary() -> bool:
    return shutil.which("speedtest-cli") is not None or shutil.which("speedtest") is not None


def _have_speedtest_module() -> bool:
    try:
        import speedtest  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def is_available() -> bool:
    return _have_speedtest_binary() or _have_speedtest_module()


def _run_binary(iface: Optional[str]) -> Dict[str, Any]:
    binary = shutil.which("speedtest-cli")
    if binary is None:
        binary = shutil.which("speedtest")
    if binary is None:
        return {"ok": False, "error": "speedtest binary not found"}
    cmd = f"{binary} --json"
    if iface:
        cmd += f" --source {shlex.quote(iface)}"
    out, code = run(cmd, timeout=120)
    if code != 0:
        return {"ok": False, "error": "speedtest binary failed", "raw": out}
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"could not parse speedtest output: {exc}", "raw": out}
    return {"ok": True, "data": data}


def _run_module() -> Dict[str, Any]:
    try:
        import speedtest  # type: ignore
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"speedtest module unavailable: {exc}"}
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        download = st.download()
        upload = st.upload()
        results = st.results.dict()
        return {"ok": True, "data": results, "download": download, "upload": upload}
    except Exception as exc:
        return {"ok": False, "error": f"speedtest module failed: {exc}"}


def _normalize(raw: Dict[str, Any], iface: Optional[str]) -> Dict[str, Any]:
    """Map speedtest-cli JSON to our result schema."""
    download_bps = raw.get("download")
    upload_bps = raw.get("upload")
    ping_ms = raw.get("ping")
    server = raw.get("server") or {}
    server_name = None
    server_location = None
    if isinstance(server, dict):
        server_name = server.get("sponsor") or server.get("name")
        server_location = server.get("name") or server.get("country")
    return {
        "ok": True,
        "timestamp": time.time(),
        "iface": iface,
        "download_mbps": round((download_bps or 0) / 1_000_000, 2) if download_bps else None,
        "upload_mbps": round((upload_bps or 0) / 1_000_000, 2) if upload_bps else None,
        "ping_ms": round(ping_ms, 2) if isinstance(ping_ms, (int, float)) else None,
        "jitter_ms": raw.get("jitter"),
        "server_name": server_name,
        "server_location": server_location,
        "raw": raw,
    }


def run_test(iface: Optional[str] = None) -> Dict[str, Any]:
    if not is_available():
        result = {
            "ok": False,
            "timestamp": time.time(),
            "iface": iface,
            "error": "speedtest is not installed (install speedtest-cli or pip speedtest-cli)",
        }
        save_json(paths.SPEEDTEST_LAST, result, mode=0o644)
        append_log(paths.SPEEDTEST_LOG, "speedtest unavailable")
        return result

    if _have_speedtest_binary():
        outcome = _run_binary(iface)
    else:
        outcome = _run_module()

    if not outcome.get("ok"):
        result = {
            "ok": False,
            "timestamp": time.time(),
            "iface": iface,
            "error": outcome.get("error"),
            "raw": outcome.get("raw"),
        }
        save_json(paths.SPEEDTEST_LAST, result, mode=0o644)
        append_log(paths.SPEEDTEST_LOG, f"speedtest failed: {outcome.get('error')}")
        return result

    raw = outcome.get("data") or {}
    if "download" in outcome and "upload" in outcome:
        # Module mode reports raw bps; merge into raw dict
        raw = dict(raw)
        raw.setdefault("download", outcome["download"])
        raw.setdefault("upload", outcome["upload"])
    result = _normalize(raw, iface)
    save_json(paths.SPEEDTEST_LAST, result, mode=0o644)
    append_log(
        paths.SPEEDTEST_LOG,
        f"speedtest ok down={result.get('download_mbps')} up={result.get('upload_mbps')} "
        f"ping={result.get('ping_ms')}",
    )
    return result


def last_result() -> Optional[Dict[str, Any]]:
    data = load_json(paths.SPEEDTEST_LAST, default=None)
    return data if data else None
