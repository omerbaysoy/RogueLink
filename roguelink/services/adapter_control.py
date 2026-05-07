"""Adapter power/reset controls (TX power, power save, soft/USB reset)."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Dict, Optional

from .. import paths
from ..utils import append_log, interface_exists, run


def _iw_info(iface: str) -> str:
    out, code = run(f"iw dev {shlex.quote(iface)} info")
    return out if code == 0 else ""


def _txpower_dbm(iface: str) -> Optional[float]:
    text = _iw_info(iface)
    m = re.search(r"txpower\s+(-?[\d.]+)\s*dBm", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _powersave_state(iface: str) -> Optional[str]:
    out, code = run(f"iw dev {shlex.quote(iface)} get power_save")
    if code != 0:
        return None
    m = re.search(r"Power save:\s*(\w+)", out)
    return m.group(1) if m else None


def _phy_for(iface: str) -> Optional[str]:
    text = _iw_info(iface)
    m = re.search(r"wiphy\s+(\d+)", text)
    return f"phy{m.group(1)}" if m else None


def _usb_reset_path(iface: str) -> Optional[str]:
    """Best-effort USB reset path (the parent USB device's authorized file)."""
    safe = os.path.basename(iface)
    real = os.path.realpath(f"/sys/class/net/{safe}/device")
    parent = real
    for _ in range(5):
        # Walk up until we find a directory containing 'idVendor' (USB device).
        if os.path.exists(os.path.join(parent, "idVendor")) and os.path.exists(
            os.path.join(parent, "authorized")
        ):
            return parent
        new_parent = os.path.dirname(parent)
        if new_parent == parent:
            break
        parent = new_parent
    return None


def status_for(iface: str) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"iface": iface, "supported": False, "error": "interface not present"}
    info = _iw_info(iface)
    supports_set_txpower = "set" in info or True  # we attempt and capture errors
    return {
        "iface": iface,
        "phy": _phy_for(iface),
        "driver_info": info[:1500],
        "txpower_dbm": _txpower_dbm(iface),
        "powersave": _powersave_state(iface),
        "usb_reset_path": _usb_reset_path(iface),
        "supports_txpower_set": True,
        "supports_powersave_set": True,
        "supports_soft_reset": True,
        "supports_usb_reset": _usb_reset_path(iface) is not None,
    }


def status_all() -> Dict[str, Dict[str, Any]]:
    from . import adapter_manager

    out: Dict[str, Dict[str, Any]] = {}
    for ad in adapter_manager.list_adapters():
        out[ad["iface"]] = status_for(ad["iface"])
    return out


def _log(action: str, iface: str, ok: bool, output: str) -> None:
    append_log(
        paths.DAEMON_LOG,
        f"adapter_control {action} iface={iface} ok={ok} :: {output[:400]}",
    )


def set_txpower(iface: str, dbm: float) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"ok": False, "error": "interface not present"}
    mbm = int(round(dbm * 100))
    out, code = run(f"iw dev {shlex.quote(iface)} set txpower fixed {mbm}")
    ok = code == 0
    _log("set_txpower", iface, ok, out)
    return {"ok": ok, "iface": iface, "dbm": dbm, "mbm": mbm, "output": out, "txpower_dbm": _txpower_dbm(iface)}


def set_txpower_auto(iface: str) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"ok": False, "error": "interface not present"}
    out, code = run(f"iw dev {shlex.quote(iface)} set txpower auto")
    ok = code == 0
    _log("txpower_auto", iface, ok, out)
    return {"ok": ok, "iface": iface, "output": out, "txpower_dbm": _txpower_dbm(iface)}


def set_powersave(iface: str, on: bool) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"ok": False, "error": "interface not present"}
    state_arg = "on" if on else "off"
    out, code = run(f"iw dev {shlex.quote(iface)} set power_save {state_arg}")
    ok = code == 0
    _log(f"powersave_{state_arg}", iface, ok, out)
    return {"ok": ok, "iface": iface, "state": state_arg, "output": out, "powersave": _powersave_state(iface)}


def soft_reset(iface: str) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"ok": False, "error": "interface not present"}
    out_down, c1 = run(f"ip link set {shlex.quote(iface)} down")
    out_up, c2 = run(f"ip link set {shlex.quote(iface)} up")
    ok = c1 == 0 and c2 == 0
    _log("soft_reset", iface, ok, f"down={out_down} | up={out_up}")
    return {"ok": ok, "iface": iface, "down_output": out_down, "up_output": out_up}


def usb_reset(iface: str) -> Dict[str, Any]:
    if not interface_exists(iface):
        return {"ok": False, "error": "interface not present"}
    path = _usb_reset_path(iface)
    if not path:
        return {"ok": False, "error": "USB device path not found", "iface": iface}
    authorized = os.path.join(path, "authorized")
    try:
        with open(authorized, "w") as f:
            f.write("0\n")
        # short pause then re-authorize
        import time as _t

        _t.sleep(1)
        with open(authorized, "w") as f:
            f.write("1\n")
    except OSError as exc:
        _log("usb_reset", iface, False, str(exc))
        return {"ok": False, "error": str(exc), "iface": iface, "path": path}
    _log("usb_reset", iface, True, f"reauthorized {path}")
    return {"ok": True, "iface": iface, "path": path}
