"""AP mode: hostapd + dnsmasq on a chosen external Wi-Fi adapter."""

import ipaddress
import os
import re
import shlex
import time
from typing import Dict, List, Optional

from .. import config as roguelink_config
from .. import paths, state
from ..utils import (
    append_log,
    interface_exists,
    pid_alive,
    run,
    stop_pid,
    write_text,
)


HOSTAPD_TEMPLATE = """interface={iface}
driver=nl80211
ssid={ssid}
country_code={country}
hw_mode={hw_mode}
channel={channel}
ieee80211n=1
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={psk}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""

DNSMASQ_TEMPLATE = """interface={iface}
bind-interfaces
except-interface=lo
listen-address={address}
dhcp-authoritative
dhcp-range={dhcp_start},{dhcp_end},{netmask},12h
dhcp-option=option:router,{address}
dhcp-option=option:dns-server,{address}
server=1.1.1.1
server=8.8.8.8
dhcp-leasefile={leases}
log-dhcp
log-facility={log}
"""


def _hw_mode_for_channel(channel: int) -> str:
    if 1 <= channel <= 14:
        return "g"
    return "a"


def _netmask_for(subnet: str) -> str:
    return str(ipaddress.IPv4Network(subnet, strict=False).netmask)


def start(
    iface: str,
    ssid: str,
    psk: str,
    channel: Optional[int] = None,
    country: Optional[str] = None,
) -> Dict:
    if not interface_exists(iface):
        return {"ok": False, "error": f"interface {iface} not present"}
    if not ssid or len(ssid) > 32:
        return {"ok": False, "error": "SSID must be 1-32 characters"}
    if not psk or len(psk) < 8:
        return {"ok": False, "error": "WPA2 PSK must be at least 8 characters"}

    cfg = roguelink_config.load()
    ap_cfg = cfg.get("ap", {})
    chan = channel or ap_cfg.get("channel", 6)
    cc = country or ap_cfg.get("country_code") or cfg.get("general", {}).get("country_code", "US")
    subnet = ap_cfg.get("subnet", "10.42.0.0/24")
    address = ap_cfg.get("address", "10.42.0.1")
    dhcp_start = ap_cfg.get("dhcp_start", "10.42.0.10")
    dhcp_end = ap_cfg.get("dhcp_end", "10.42.0.200")
    netmask = _netmask_for(subnet)

    # Stop any pre-existing AP cleanly.
    stop()

    run(f"rfkill unblock wifi")
    run(f"ip link set {shlex.quote(iface)} down")
    run(f"ip addr flush dev {shlex.quote(iface)}")
    run(f"ip link set {shlex.quote(iface)} up")
    run(f"ip addr add {shlex.quote(address)}/{ipaddress.IPv4Network(subnet).prefixlen} "
        f"dev {shlex.quote(iface)}")

    write_text(
        paths.HOSTAPD_CONF,
        HOSTAPD_TEMPLATE.format(
            iface=iface,
            ssid=ssid,
            country=cc,
            hw_mode=_hw_mode_for_channel(chan),
            channel=chan,
            psk=psk,
        ),
        mode=0o600,
    )
    write_text(
        paths.DNSMASQ_AP_CONF,
        DNSMASQ_TEMPLATE.format(
            iface=iface,
            address=address,
            dhcp_start=dhcp_start,
            dhcp_end=dhcp_end,
            netmask=netmask,
            leases=paths.DNSMASQ_AP_LEASES,
            log=paths.AP_LOG,
        ),
        mode=0o644,
    )

    out, code = run(
        f"dnsmasq --conf-file={shlex.quote(paths.DNSMASQ_AP_CONF)} "
        f"--pid-file={shlex.quote(paths.DNSMASQ_AP_PID)}",
        timeout=10,
    )
    append_log(paths.AP_LOG, f"dnsmasq rc={code} :: {out}")
    time.sleep(1)
    _, dnsmasq_ok = pid_alive(paths.DNSMASQ_AP_PID, paths.DNSMASQ_AP_CONF)
    if code != 0 or not dnsmasq_ok:
        stop()
        return {"ok": False, "error": "dnsmasq failed to start", "output": out}

    out, code = run(
        f"hostapd -B -P {shlex.quote(paths.HOSTAPD_PID)} {shlex.quote(paths.HOSTAPD_CONF)}",
        timeout=15,
    )
    append_log(paths.AP_LOG, f"hostapd rc={code} :: {out}")
    time.sleep(1)
    _, hostapd_ok = pid_alive(paths.HOSTAPD_PID, paths.HOSTAPD_CONF)
    if code != 0 or not hostapd_ok:
        stop()
        return {"ok": False, "error": "hostapd failed to start", "output": out}

    state.save_ap_profile(
        {
            "iface": iface,
            "ssid": ssid,
            "psk": psk,
            "channel": chan,
            "country": cc,
            "subnet": subnet,
            "address": address,
            "started_at": time.time(),
        }
    )
    return {"ok": True, "iface": iface, "ssid": ssid}


def stop() -> Dict:
    stop_pid(paths.HOSTAPD_PID, paths.HOSTAPD_CONF)
    stop_pid(paths.DNSMASQ_AP_PID, paths.DNSMASQ_AP_CONF)
    profile = state.load_ap_profile()
    iface = profile.get("iface")
    if iface and interface_exists(iface):
        run(f"ip addr flush dev {shlex.quote(iface)}")
        run(f"ip link set {shlex.quote(iface)} down")
    for path in (paths.HOSTAPD_CONF, paths.DNSMASQ_AP_CONF):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    state.save_ap_profile({})
    return {"ok": True}


def is_running() -> bool:
    _, h_ok = pid_alive(paths.HOSTAPD_PID, paths.HOSTAPD_CONF)
    _, d_ok = pid_alive(paths.DNSMASQ_AP_PID, paths.DNSMASQ_AP_CONF)
    return h_ok and d_ok


def status() -> Dict:
    profile = state.load_ap_profile()
    return {
        "running": is_running(),
        "iface": profile.get("iface"),
        "ssid": profile.get("ssid"),
        "channel": profile.get("channel"),
        "subnet": profile.get("subnet"),
        "address": profile.get("address"),
        "started_at": profile.get("started_at"),
    }


def clients() -> List[Dict]:
    """Parse AP DHCP leases plus arp neighbors for connected stations."""
    return _read_leases(paths.DNSMASQ_AP_LEASES)


def _read_leases(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                expires, mac, ip, hostname = parts[0], parts[1], parts[2], parts[3]
                out.append(
                    {
                        "expires": expires,
                        "mac": mac,
                        "ip": ip,
                        "hostname": hostname if hostname != "*" else "",
                    }
                )
    except OSError:
        return []
    return out
