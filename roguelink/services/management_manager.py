"""Management interface: the onboard Pi Wi-Fi (brcmfmac).

The role is protected — the daemon refuses to reassign the management slot
to anything other than an onboard interface. Exposes helpers to read the
current management IP, SSID, gateway, mode, and lock/release static IP.
"""

import os
import re
import shlex
import shutil
import time
from typing import Dict, List, Optional

from .. import config as roguelink_config
from .. import paths, state
from ..utils import (
    append_log,
    interface_exists,
    load_json,
    read_text,
    run,
    save_json,
    write_text,
)

MGMT_WPA_CONF = os.path.join(paths.RUN_DIR, "wpa_supplicant_mgmt.conf")
MGMT_WPA_PID = os.path.join(paths.RUN_DIR, "wpa_supplicant_mgmt.pid")
MGMT_STATIC_PATH = os.path.join(paths.LIB_DIR, "mgmt_static.json")


def _wpa_conf(country: str, ssid: str, psk: str) -> str:
    if not psk:
        net = (
            f"network={{\n"
            f'    ssid="{ssid}"\n'
            f"    key_mgmt=NONE\n"
            f"    priority=10\n"
            f"}}\n"
        )
    else:
        net = (
            f"network={{\n"
            f'    ssid="{ssid}"\n'
            f'    psk="{psk}"\n'
            f"    key_mgmt=WPA-PSK\n"
            f"    priority=10\n"
            f"}}\n"
        )
    return (
        "ctrl_interface=/run/wpa_supplicant\n"
        "update_config=1\n"
        f"country={country}\n\n"
        + net
    )


def configure(ssid: str, psk: str, country: Optional[str] = None) -> Dict:
    cfg = roguelink_config.load()
    cc = country or cfg.get("general", {}).get("country_code", "US")
    cfg["management"]["ssid"] = ssid
    cfg["management"]["psk"] = psk
    roguelink_config.save(cfg)
    state.save_mgmt_profile({"ssid": ssid, "psk": psk, "country": cc})
    return {"ok": True}


def get_iface() -> Optional[str]:
    """Get management interface — prefer adapter map, fallback to config."""
    roles = state.load_adapter_map()
    return roles.get("management") or roguelink_config.load().get("management", {}).get("iface")


def get_management_ip() -> Optional[str]:
    """Get the current IP of the management interface."""
    iface = get_iface()
    if not iface or not interface_exists(iface):
        return None
    out, code = run(f"ip -4 -o addr show dev {shlex.quote(iface)}")
    if code != 0:
        return None
    m = re.search(r"inet\s+([0-9.]+)/", out or "")
    return m.group(1) if m else None


def get_management_gateway() -> Optional[str]:
    """Get gateway for management interface."""
    iface = get_iface()
    if not iface:
        return None
    out, _ = run(f"ip route show default dev {shlex.quote(iface)}")
    m = re.search(r"default via ([0-9.]+)", out or "")
    return m.group(1) if m else None


def get_management_ssid() -> Optional[str]:
    """Get SSID the management interface is connected to."""
    iface = get_iface()
    if not iface or not interface_exists(iface):
        return None
    out, _ = run(f"iw dev {shlex.quote(iface)} link")
    if out and "Connected to" in out:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                return line.split(":", 1)[1].strip()
    return None


def get_management_mode() -> str:
    """Return 'static' or 'dhcp' based on whether user locked a static IP."""
    static = load_json(MGMT_STATIC_PATH, default={})
    if static.get("enabled"):
        return "static"
    return "dhcp"


def get_bind_host() -> str:
    """Determine the host the daemon should bind to."""
    cfg = roguelink_config.load()
    host = cfg.get("general", {}).get("host", "auto")
    if host and host != "auto":
        return host
    ip = get_management_ip()
    return ip or "127.0.0.1"


def dashboard_url(api_port: int) -> str:
    ip = get_management_ip()
    return f"http://{ip}:{api_port}" if ip else f"http://<management-ip>:{api_port}"


def connect() -> Dict:
    """(Re)connect the management interface using stored SSID/PSK."""
    cfg = roguelink_config.load()
    mgmt = cfg.get("management", {})
    iface = mgmt.get("iface") or get_iface()
    ssid = mgmt.get("ssid")
    psk = mgmt.get("psk", "")
    cc = cfg.get("general", {}).get("country_code", "US")
    if not iface:
        return {"ok": False, "error": "management interface not set"}
    if not interface_exists(iface):
        return {"ok": False, "error": f"interface {iface} not present"}
    if not ssid:
        return {"ok": False, "error": "no management SSID configured"}

    write_text(MGMT_WPA_CONF, _wpa_conf(cc, ssid, psk), mode=0o600)
    run(f"pkill -f 'wpa_supplicant.*{iface}'")
    run(f"ip link set {shlex.quote(iface)} up")
    cmd = (
        f"wpa_supplicant -B -i {shlex.quote(iface)} "
        f"-c {shlex.quote(MGMT_WPA_CONF)} -P {shlex.quote(MGMT_WPA_PID)}"
    )
    out, code = run(cmd, timeout=20)
    append_log(paths.DAEMON_LOG, f"mgmt wpa_supplicant rc={code} :: {out}")
    if code != 0:
        return {"ok": False, "error": "wpa_supplicant failed", "output": out}

    if shutil.which("dhclient"):
        run(f"dhclient -v {shlex.quote(iface)}", timeout=30)
    elif shutil.which("dhcpcd"):
        run(f"dhcpcd -n {shlex.quote(iface)}", timeout=30)
    elif shutil.which("udhcpc"):
        run(f"udhcpc -i {shlex.quote(iface)} -n -q", timeout=30)
    time.sleep(1)
    return {"ok": True, "iface": iface, "ip": get_management_ip()}


# ---------------------------------------------------------------------------
# Static IP lock / DHCP release
# ---------------------------------------------------------------------------

def lock_ip(ip: str, gateway: str = "", dns: str = "") -> Dict:
    """Lock a static IP on the management interface.

    Uses ip addr/route directly (works on all Pi OS variants).
    Saves config so it can be re-applied after reboot via the daemon.
    """
    iface = get_iface()
    if not iface or not interface_exists(iface):
        return {"ok": False, "error": "management interface not found"}

    qi = shlex.quote(iface)

    # Kill DHCP clients on this iface
    run(f"pkill -f 'dhclient.*{qi}'")
    run(f"pkill -f 'dhcpcd.*{qi}'")
    time.sleep(0.3)

    # Flush and assign
    run(f"ip addr flush dev {qi}")
    out, code = run(f"ip addr add {shlex.quote(ip)}/24 dev {qi}")
    if code != 0:
        return {"ok": False, "error": f"ip addr add failed: {out}"}

    if gateway:
        run(f"ip route del default dev {qi}")
        run(f"ip route add default via {shlex.quote(gateway)} dev {qi}")

    # DNS
    dns_list = [d.strip() for d in dns.split(",") if d.strip()] if dns else []
    if dns_list:
        try:
            lines = [f"nameserver {d}" for d in dns_list]
            write_text("/etc/resolv.conf", "\n".join(lines) + "\n")
        except OSError:
            pass

    # Save static config
    save_json(MGMT_STATIC_PATH, {
        "enabled": True, "ip": ip, "gateway": gateway,
        "dns": dns_list, "iface": iface,
        "applied_at": time.time(),
    })

    append_log(paths.DAEMON_LOG, f"management IP locked: {ip} gw={gateway}")
    return {
        "ok": True, "iface": iface, "ip": ip,
        "gateway": gateway, "dns": dns_list, "mode": "static",
        "warning": "Dashboard will now be at http://" + ip + ":8080",
    }


def release_dhcp() -> Dict:
    """Release static IP and return to DHCP."""
    iface = get_iface()
    if not iface or not interface_exists(iface):
        return {"ok": False, "error": "management interface not found"}

    qi = shlex.quote(iface)
    run(f"ip addr flush dev {qi}")

    # Restart DHCP
    if shutil.which("dhclient"):
        run(f"dhclient -v {qi}", timeout=30)
    elif shutil.which("dhcpcd"):
        run(f"dhcpcd -n {qi}", timeout=30)

    # Clear static config
    save_json(MGMT_STATIC_PATH, {"enabled": False})

    time.sleep(2)
    new_ip = get_management_ip()
    append_log(paths.DAEMON_LOG, f"management IP released to DHCP: {new_ip}")
    return {
        "ok": True, "iface": iface, "ip": new_ip,
        "mode": "dhcp",
        "warning": f"Dashboard may move to http://{new_ip}:8080" if new_ip else "",
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> Dict:
    iface = get_iface()
    operstate = (
        read_text(f"/sys/class/net/{iface}/operstate")
        if iface and interface_exists(iface) else "missing"
    )
    return {
        "iface": iface,
        "ip": get_management_ip(),
        "gateway": get_management_gateway(),
        "ssid": get_management_ssid(),
        "operstate": operstate,
        "mode": get_management_mode(),
        "bind_host": get_bind_host(),
    }
