"""eth0 LAN output: assign address, run dnsmasq for DHCP/DNS."""

import ipaddress
import os
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


def start(iface: Optional[str] = None) -> Dict:
    cfg = roguelink_config.load().get("lan", {})
    target = iface or cfg.get("iface", "eth0")
    if not interface_exists(target):
        return {"ok": False, "error": f"interface {target} not present"}

    subnet = cfg.get("subnet", "10.42.1.0/24")
    address = cfg.get("address", "10.42.1.1")
    dhcp_start = cfg.get("dhcp_start", "10.42.1.10")
    dhcp_end = cfg.get("dhcp_end", "10.42.1.200")
    prefix = ipaddress.IPv4Network(subnet, strict=False).prefixlen
    netmask = str(ipaddress.IPv4Network(subnet, strict=False).netmask)

    stop()

    run(f"ip link set {shlex.quote(target)} up")
    run(f"ip addr flush dev {shlex.quote(target)}")
    run(f"ip addr add {shlex.quote(address)}/{prefix} dev {shlex.quote(target)}")

    write_text(
        paths.DNSMASQ_LAN_CONF,
        DNSMASQ_TEMPLATE.format(
            iface=target,
            address=address,
            dhcp_start=dhcp_start,
            dhcp_end=dhcp_end,
            netmask=netmask,
            leases=paths.DNSMASQ_LAN_LEASES,
            log=paths.LAN_LOG,
        ),
        mode=0o644,
    )

    out, code = run(
        f"dnsmasq --conf-file={shlex.quote(paths.DNSMASQ_LAN_CONF)} "
        f"--pid-file={shlex.quote(paths.DNSMASQ_LAN_PID)}",
        timeout=10,
    )
    append_log(paths.LAN_LOG, f"dnsmasq rc={code} :: {out}")
    time.sleep(0.5)
    _, ok = pid_alive(paths.DNSMASQ_LAN_PID, paths.DNSMASQ_LAN_CONF)
    if code != 0 or not ok:
        stop()
        return {"ok": False, "error": "dnsmasq failed", "output": out}

    state.save_lan_profile(
        {
            "iface": target,
            "subnet": subnet,
            "address": address,
            "started_at": time.time(),
            "enabled": True,
        }
    )
    return {"ok": True, "iface": target}


def stop() -> Dict:
    stop_pid(paths.DNSMASQ_LAN_PID, paths.DNSMASQ_LAN_CONF)
    profile = state.load_lan_profile()
    iface = profile.get("iface")
    if iface and interface_exists(iface):
        run(f"ip addr flush dev {shlex.quote(iface)}")
    if os.path.exists(paths.DNSMASQ_LAN_CONF):
        try:
            os.remove(paths.DNSMASQ_LAN_CONF)
        except OSError:
            pass
    state.save_lan_profile({"enabled": False})
    return {"ok": True}


def is_running() -> bool:
    _, ok = pid_alive(paths.DNSMASQ_LAN_PID, paths.DNSMASQ_LAN_CONF)
    return ok


def status() -> Dict:
    profile = state.load_lan_profile()
    iface = profile.get("iface")
    return {
        "running": is_running(),
        "enabled": bool(profile.get("enabled")),
        "iface": iface,
        "subnet": profile.get("subnet"),
        "address": profile.get("address"),
        "started_at": profile.get("started_at"),
    }


def clients() -> List[Dict]:
    if not os.path.exists(paths.DNSMASQ_LAN_LEASES):
        return []
    out: List[Dict] = []
    try:
        with open(paths.DNSMASQ_LAN_LEASES, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                out.append(
                    {
                        "expires": parts[0],
                        "mac": parts[1],
                        "ip": parts[2],
                        "hostname": parts[3] if parts[3] != "*" else "",
                    }
                )
    except OSError:
        return []
    return out
