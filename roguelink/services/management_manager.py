"""Management interface: the onboard Pi Wi-Fi (brcmfmac).

The role is protected — the daemon refuses to reassign the management slot
to anything other than an onboard interface. We expose helpers to read the
current management IP, edit the SSID/PSK profile, and (re)bring the
interface up using wpa_supplicant.
"""

import os
import re
import shlex
import shutil
import time
from typing import Dict, Optional

from .. import config as roguelink_config
from .. import paths, state
from ..utils import (
    append_log,
    interface_exists,
    read_text,
    run,
    write_text,
)

MGMT_WPA_CONF = os.path.join(paths.RUN_DIR, "wpa_supplicant_mgmt.conf")
MGMT_WPA_PID = os.path.join(paths.RUN_DIR, "wpa_supplicant_mgmt.pid")


def _wpa_conf(country: str, ssid: str, psk: str) -> str:
    if not psk:
        net = (
            f"network={{\n"
            f"    ssid=\"{ssid}\"\n"
            f"    key_mgmt=NONE\n"
            f"    priority=10\n"
            f"}}\n"
        )
    else:
        net = (
            f"network={{\n"
            f"    ssid=\"{ssid}\"\n"
            f"    psk=\"{psk}\"\n"
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
    roles = state.load_adapter_map()
    return roles.get("management") or roguelink_config.load().get("management", {}).get("iface")


def get_management_ip() -> Optional[str]:
    iface = get_iface()
    if not iface or not interface_exists(iface):
        return None
    out, code = run(f"ip -4 -o addr show dev {shlex.quote(iface)}")
    if code != 0:
        return None
    m = re.search(r"inet\s+([0-9.]+)/", out or "")
    return m.group(1) if m else None


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
    elif shutil.which("udhcpc"):
        run(f"udhcpc -i {shlex.quote(iface)} -n -q", timeout=30)
    time.sleep(1)
    return {"ok": True, "iface": iface, "ip": get_management_ip()}


def status() -> Dict:
    iface = get_iface()
    operstate = (
        read_text(f"/sys/class/net/{iface}/operstate") if iface and interface_exists(iface) else "missing"
    )
    return {
        "iface": iface,
        "ip": get_management_ip(),
        "operstate": operstate,
    }
