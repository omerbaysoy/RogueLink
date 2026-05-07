"""WAN uplink: an external Wi-Fi adapter joins an upstream Wi-Fi network.

We use wpa_supplicant directly (per spec) and run dhclient/udhcpc on the
chosen interface. SSID scanning uses ``iw scan``.
"""

import os
import re
import shlex
import shutil
import time
from typing import Dict, List, Optional

from .. import paths, state
from ..utils import (
    append_log,
    interface_exists,
    read_text,
    run,
    stop_pid,
    write_text,
)


def _wpa_conf(country: str, ssid: str, psk: str) -> str:
    if not psk:
        net_block = (
            f"network={{\n"
            f"    ssid=\"{ssid}\"\n"
            f"    key_mgmt=NONE\n"
            f"    priority=10\n"
            f"}}\n"
        )
    else:
        net_block = (
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
        + net_block
    )


def _kill_existing(iface: str) -> None:
    stop_pid(paths.WAN_WPA_PID, paths.WAN_WPA_CONF)
    run(f"pkill -f 'wpa_supplicant.*{iface}'")
    run(f"pkill -f 'dhclient.*{iface}'")


def _bring_up(iface: str) -> None:
    run(f"rfkill unblock wifi")
    run(f"ip link set {shlex.quote(iface)} up")


def _start_dhcp(iface: str) -> bool:
    if shutil.which("dhclient"):
        out, code = run(f"dhclient -v {shlex.quote(iface)}", timeout=30)
        append_log(paths.WAN_LOG, f"dhclient rc={code} :: {out}")
        return code == 0
    if shutil.which("udhcpc"):
        out, code = run(f"udhcpc -i {shlex.quote(iface)} -n -q", timeout=30)
        append_log(paths.WAN_LOG, f"udhcpc rc={code} :: {out}")
        return code == 0
    return False


def scan(iface: str) -> List[Dict]:
    if not interface_exists(iface):
        return []
    _bring_up(iface)
    out, code = run(f"iw dev {shlex.quote(iface)} scan", timeout=20)
    if code != 0 or not out:
        return []
    networks: List[Dict] = []
    current: Dict[str, object] = {}
    for line in out.splitlines():
        if line.startswith("BSS "):
            if current.get("ssid"):
                networks.append(current)  # type: ignore[arg-type]
            bssid_match = re.match(r"BSS\s+([0-9a-f:]{17})", line)
            current = {
                "bssid": bssid_match.group(1) if bssid_match else "",
                "ssid": "",
                "signal": None,
                "channel": None,
                "encryption": "Open",
            }
            continue
        line = line.strip()
        if line.startswith("SSID:"):
            current["ssid"] = line.split(":", 1)[1].strip()
        elif line.startswith("signal:"):
            try:
                current["signal"] = float(line.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        elif line.startswith("DS Parameter set: channel"):
            try:
                current["channel"] = int(line.rsplit(" ", 1)[-1])
            except ValueError:
                pass
        elif line.startswith("freq:"):
            try:
                freq = int(line.split(":", 1)[1].strip())
                current.setdefault("channel", _freq_to_channel(freq))
            except ValueError:
                pass
        elif "RSN:" in line or "WPA:" in line:
            current["encryption"] = "WPA2"
        elif "Privacy" in line and current.get("encryption") == "Open":
            current["encryption"] = "WEP/WPA"
    if current.get("ssid"):
        networks.append(current)  # type: ignore[arg-type]

    # Deduplicate by SSID, keeping strongest signal.
    by_ssid: Dict[str, Dict] = {}
    for net in networks:
        key = str(net.get("ssid"))
        if key in by_ssid:
            old = by_ssid[key]
            if (net.get("signal") or -999) > (old.get("signal") or -999):
                by_ssid[key] = net
        else:
            by_ssid[key] = net
    return sorted(by_ssid.values(), key=lambda n: n.get("signal") or -999, reverse=True)


def _freq_to_channel(freq_mhz: int) -> Optional[int]:
    if 2412 <= freq_mhz <= 2484:
        return (freq_mhz - 2407) // 5 if freq_mhz != 2484 else 14
    if 5000 <= freq_mhz <= 5900:
        return (freq_mhz - 5000) // 5
    return None


def connect(iface: str, ssid: str, psk: str, country: str = "US") -> Dict:
    if not interface_exists(iface):
        return {"ok": False, "error": f"interface {iface} not present"}
    if not ssid:
        return {"ok": False, "error": "SSID required"}

    _kill_existing(iface)
    _bring_up(iface)
    write_text(paths.WAN_WPA_CONF, _wpa_conf(country, ssid, psk), mode=0o600)

    cmd = (
        f"wpa_supplicant -B -i {shlex.quote(iface)} "
        f"-c {shlex.quote(paths.WAN_WPA_CONF)} "
        f"-P {shlex.quote(paths.WAN_WPA_PID)}"
    )
    out, code = run(cmd, timeout=20)
    append_log(paths.WAN_LOG, f"wpa_supplicant rc={code} :: {out}")
    if code != 0:
        return {"ok": False, "error": "wpa_supplicant failed", "output": out}

    # Wait briefly for association.
    associated = False
    for _ in range(15):
        link_out, _ = run(f"iw dev {shlex.quote(iface)} link")
        if link_out and "Connected to" in link_out:
            associated = True
            break
        time.sleep(1)

    if not associated:
        append_log(paths.WAN_LOG, f"association timeout for ssid={ssid}")
        return {"ok": False, "error": "association timeout"}

    dhcp_ok = _start_dhcp(iface)
    state.save_wan_profile(
        {
            "iface": iface,
            "ssid": ssid,
            "psk": psk,
            "country": country,
            "connected_at": time.time(),
        }
    )
    return {"ok": True, "associated": True, "dhcp": dhcp_ok}


def disconnect(iface: Optional[str] = None) -> Dict:
    profile = state.load_wan_profile()
    target = iface or profile.get("iface")
    if target:
        _kill_existing(target)
        run(f"ip addr flush dev {shlex.quote(target)}")
        run(f"ip link set {shlex.quote(target)} down")
    state.save_wan_profile({})
    append_log(paths.WAN_LOG, f"disconnect iface={target}")
    return {"ok": True, "iface": target}


def status() -> Dict:
    profile = state.load_wan_profile()
    iface = profile.get("iface")
    if not iface or not interface_exists(iface):
        return {
            "connected": False,
            "iface": iface,
            "ssid": profile.get("ssid"),
            "ip": None,
            "gateway": None,
            "dns": [],
            "signal": None,
        }
    operstate = read_text(f"/sys/class/net/{iface}/operstate")
    link_out, _ = run(f"iw dev {shlex.quote(iface)} link")
    ssid = ""
    signal: Optional[float] = None
    if "Connected to" in link_out:
        for line in link_out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssid = line.split(":", 1)[1].strip()
            if line.startswith("signal:"):
                try:
                    signal = float(line.split(":", 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
    ip_out, _ = run(f"ip -4 -o addr show dev {shlex.quote(iface)}")
    ip_addr: Optional[str] = None
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    if m:
        ip_addr = m.group(1)
    gw_out, _ = run(f"ip route show default dev {shlex.quote(iface)}")
    gw_match = re.search(r"default via ([0-9.]+)", gw_out or "")
    gateway = gw_match.group(1) if gw_match else None
    dns_servers: List[str] = []
    resolv = read_text("/etc/resolv.conf")
    for line in resolv.splitlines():
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2:
                dns_servers.append(parts[1])
    return {
        "connected": operstate == "up" and ssid != "",
        "iface": iface,
        "operstate": operstate,
        "ssid": ssid or profile.get("ssid"),
        "ip": ip_addr,
        "gateway": gateway,
        "dns": dns_servers,
        "signal": signal,
    }
