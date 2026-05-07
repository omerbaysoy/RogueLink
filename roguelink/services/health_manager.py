"""Connection health checks: gateway/internet ping, DNS, signal, classification.

Provides a split model separating management connectivity from WAN/uplink
status so that the overall health correctly reflects what is actually working.
"""

from __future__ import annotations

import re
import shlex
import shutil
import time
from typing import Any, Dict, List, Optional

from .. import paths
from ..utils import append_log, load_json, read_text, run, save_json


PUBLIC_TARGETS = ("1.1.1.1", "8.8.8.8")
DNS_TARGETS = ("cloudflare.com", "google.com")


def _default_route() -> Dict[str, Optional[str]]:
    out, code = run("ip route show default")
    if code != 0 or not out:
        return {"gateway": None, "iface": None}
    line = out.splitlines()[0]
    parts = line.split()
    gw = None
    iface = None
    if "via" in parts:
        gw = parts[parts.index("via") + 1]
    if "dev" in parts:
        iface = parts[parts.index("dev") + 1]
    return {"gateway": gw, "iface": iface}


def _ping(target: str, count: int = 5, iface: Optional[str] = None, timeout: int = 10) -> Dict[str, Any]:
    if not shutil.which("ping"):
        return {"ok": False, "error": "ping not installed", "target": target}
    cmd = f"ping -c {count} -W 1"
    if iface:
        cmd += f" -I {shlex.quote(iface)}"
    cmd += f" {shlex.quote(target)}"
    out, code = run(cmd, timeout=timeout)
    if code != 0:
        return {"ok": False, "target": target, "error": out, "loss_pct": 100.0, "rtt_ms": None}
    loss = 100.0
    rtt_avg: Optional[float] = None
    rtt_min: Optional[float] = None
    rtt_max: Optional[float] = None
    rtt_mdev: Optional[float] = None
    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(r"(\d+(?:\.\d+)?)% packet loss", line)
            if m:
                loss = float(m.group(1))
        if line.startswith("rtt") or line.startswith("round-trip"):
            m = re.search(r"=\s*([\d./]+)\s*ms", line)
            if m:
                parts = m.group(1).split("/")
                if len(parts) >= 4:
                    rtt_min, rtt_avg, rtt_max, rtt_mdev = (float(p) for p in parts[:4])
    return {
        "ok": True,
        "target": target,
        "iface": iface,
        "loss_pct": loss,
        "rtt_ms": rtt_avg,
        "rtt_min": rtt_min,
        "rtt_max": rtt_max,
        "rtt_mdev": rtt_mdev,
    }


def _resolve(host: str) -> Dict[str, Any]:
    if shutil.which("getent"):
        out, code = run(f"getent hosts {shlex.quote(host)}", timeout=8)
        return {"ok": code == 0, "host": host, "output": out}
    if shutil.which("resolvectl"):
        out, code = run(f"resolvectl query {shlex.quote(host)}", timeout=8)
        return {"ok": code == 0, "host": host, "output": out}
    if shutil.which("nslookup"):
        out, code = run(f"nslookup {shlex.quote(host)}", timeout=8)
        return {"ok": code == 0, "host": host, "output": out}
    return {"ok": False, "host": host, "error": "no resolver tool available"}


def _wan_signal() -> Dict[str, Any]:
    from . import wan_manager  # avoid circular at import time

    status = wan_manager.status()
    iface = status.get("iface")
    info = {"iface": iface, "signal_dbm": status.get("signal"), "ssid": status.get("ssid")}
    if not iface:
        return info
    out, code = run(f"iw dev {shlex.quote(iface)} link")
    if code == 0:
        m = re.search(r"signal:\s*(-?\d+)", out)
        if m:
            info["signal_dbm"] = float(m.group(1))
    return info


def _resolv_servers() -> List[str]:
    servers: List[str] = []
    text = read_text("/etc/resolv.conf")
    for line in text.splitlines():
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2:
                servers.append(parts[1])
    return servers


def _wan_connected() -> bool:
    from . import wan_manager
    status = wan_manager.status()
    return bool(status.get("connected"))


def _management_internet() -> bool:
    """Check if the management interface has internet independently."""
    from . import management_manager
    mgmt_ip = management_manager.get_management_ip()
    return mgmt_ip is not None


def _classify(
    gateway_ok: bool,
    internet_ok: bool,
    dns_ok: bool,
    gateway_loss: float,
    public_loss: float,
    rtt_ms: Optional[float],
    signal_dbm: Optional[float],
    wan_configured: bool,
    mgmt_has_internet: bool,
) -> tuple:
    """Return (status, reason) tuple using the split connectivity model.

    Rules:
    - If gateway_ping OK + internet_ping OK + DNS OK → good/excellent
    - If no WAN configured but management has internet → partial, wan not_configured
    - If internet works but DNS fails → weak, dns_issue
    - If gateway works but public ping fails → unstable, upstream_issue
    - If nothing works → offline
    """
    # Everything works
    if internet_ok and dns_ok and gateway_ok:
        if public_loss >= 50 or gateway_loss >= 50:
            return "unstable", "high packet loss"
        if (rtt_ms is not None and rtt_ms > 200) or public_loss >= 10:
            return "weak", "high latency or moderate loss"
        if signal_dbm is not None and signal_dbm < -75:
            return "weak", "low signal strength"
        if (rtt_ms is not None and rtt_ms <= 50) and public_loss == 0:
            if signal_dbm is None or signal_dbm >= -60:
                return "excellent", "low latency, zero loss"
        return "good", "internet reachable"

    # Internet works but DNS is failing
    if internet_ok and not dns_ok:
        return "weak", "dns_issue: internet pings succeed but DNS resolution fails"

    # Gateway responds but public internet fails
    if gateway_ok and not internet_ok:
        return "unstable", "upstream_issue: gateway reachable but public internet unreachable"

    # WAN is not configured but management interface has internet
    if not wan_configured and mgmt_has_internet:
        # Check if we actually got internet through management
        if internet_ok or dns_ok:
            return "good", "internet via management, WAN not configured"
        return "partial", "management interface up but internet not verified, WAN not configured"

    # Nothing works
    if not gateway_ok and not internet_ok and not dns_ok:
        if mgmt_has_internet:
            return "partial", "management interface has IP but no internet connectivity detected"
        return "offline", "no gateway, no internet, no DNS"

    # Partial failure
    if not internet_ok:
        if gateway_ok:
            return "unstable", "gateway reachable but internet check failed"
        return "offline", "no internet connectivity"

    return "good", "internet reachable"


def check() -> Dict[str, Any]:
    started = time.time()
    route = _default_route()
    gw = route.get("gateway")
    wan_iface = route.get("iface")
    gateway_ping = _ping(gw, count=4, iface=wan_iface) if gw else {
        "ok": False,
        "error": "no default gateway",
        "loss_pct": 100.0,
        "rtt_ms": None,
    }

    # Public internet pings — do NOT bind to WAN iface exclusively
    # so they work through whatever default route exists (including management).
    public_results: List[Dict[str, Any]] = []
    for target in PUBLIC_TARGETS:
        public_results.append(_ping(target, count=4))
    public_loss = min((r.get("loss_pct") or 100.0) for r in public_results)
    public_rtt = next(
        (r.get("rtt_ms") for r in public_results if r.get("ok") and r.get("rtt_ms") is not None),
        None,
    )
    internet_ok = any(r.get("ok") and (r.get("loss_pct") or 100) < 100 for r in public_results)

    dns_results = [_resolve(host) for host in DNS_TARGETS]
    dns_ok = any(r.get("ok") for r in dns_results)

    signal = _wan_signal()
    wan_is_configured = _wan_connected()
    mgmt_has_ip = _management_internet()

    # Determine management internet status
    mgmt_internet_ok = internet_ok if not wan_is_configured else mgmt_has_ip

    gateway_ok = bool(gateway_ping.get("ok") and (gateway_ping.get("loss_pct") or 100) < 100)

    classification, reason = _classify(
        gateway_ok=gateway_ok,
        internet_ok=internet_ok,
        dns_ok=dns_ok,
        gateway_loss=gateway_ping.get("loss_pct") or 100.0,
        public_loss=public_loss,
        rtt_ms=public_rtt,
        signal_dbm=signal.get("signal_dbm"),
        wan_configured=wan_is_configured,
        mgmt_has_internet=mgmt_has_ip,
    )

    # Determine WAN status string
    if wan_is_configured:
        wan_status = "connected"
    else:
        wan_status = "not_configured"

    summary = {
        "ok": classification not in ("offline",),
        "status": classification,
        "reason": reason,
        "overall": classification,
        "wan_status": wan_status,
        "management_internet": mgmt_internet_ok,
        "checked_at": started,
        "duration_s": round(time.time() - started, 2),
        "default_route": route,
        "gateway_ping": gateway_ping,
        "public_targets": public_results,
        "dns_servers": _resolv_servers(),
        "dns_targets": dns_results,
        "dns_ok": dns_ok,
        "wan_signal": signal,
        "summary": {
            "rtt_ms": public_rtt,
            "packet_loss_pct": public_loss,
            "gateway": gw,
            "wan_iface": wan_iface,
            "signal_dbm": signal.get("signal_dbm"),
        },
    }
    save_json(paths.HEALTH_LAST, summary, mode=0o644)
    append_log(
        paths.HEALTH_LOG,
        f"health {classification} reason={reason} rtt={public_rtt} loss={public_loss}% "
        f"gw={gw} iface={wan_iface} dns={'ok' if dns_ok else 'fail'} "
        f"wan={wan_status} mgmt_internet={mgmt_internet_ok}",
    )
    return summary


def last() -> Optional[Dict[str, Any]]:
    return load_json(paths.HEALTH_LAST, default=None)
