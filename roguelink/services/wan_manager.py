"""WAN uplink: an external Wi-Fi adapter joins an upstream Wi-Fi network.

We use wpa_supplicant directly (per spec) and run dhclient/dhcpcd/udhcpc on
the chosen interface. SSID scanning uses ``iw scan``.

The connect flow is staged with explicit error reporting at each step so the
dashboard and CLI can show *which stage* failed and *why*.
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


# ---------------------------------------------------------------------------
# wpa_supplicant config
# ---------------------------------------------------------------------------

def _wpa_conf_path(iface: str) -> str:
    return os.path.join(paths.RUN_DIR, f"wpa_supplicant-{iface}.conf")


def _wpa_pid_path(iface: str) -> str:
    return os.path.join(paths.RUN_DIR, f"wpa_supplicant-{iface}.pid")


def _build_wpa_conf(country: str, ssid: str, psk: str) -> str:
    """Build wpa_supplicant config. Uses wpa_passphrase when available."""
    header = (
        "ctrl_interface=/run/wpa_supplicant\n"
        "update_config=0\n"
        f"country={country}\n\n"
    )
    if not psk:
        # Open network
        net_block = (
            "network={\n"
            f'    ssid="{ssid}"\n'
            "    key_mgmt=NONE\n"
            "    priority=10\n"
            "}\n"
        )
    elif shutil.which("wpa_passphrase"):
        # Use wpa_passphrase for hashed PSK (never stores plaintext)
        out, code = run(f"wpa_passphrase {shlex.quote(ssid)} {shlex.quote(psk)}", timeout=5)
        if code == 0 and "network=" in out:
            # Strip the plaintext psk comment line
            lines = [l for l in out.splitlines() if not l.strip().startswith("#psk=")]
            net_block = "\n".join(lines) + "\n"
        else:
            net_block = (
                "network={\n"
                f'    ssid="{ssid}"\n'
                f'    psk="{psk}"\n'
                "    key_mgmt=WPA-PSK\n"
                "    priority=10\n"
                "}\n"
            )
    else:
        net_block = (
            "network={\n"
            f'    ssid="{ssid}"\n'
            f'    psk="{psk}"\n'
            "    key_mgmt=WPA-PSK\n"
            "    priority=10\n"
            "}\n"
        )
    return header + net_block


# ---------------------------------------------------------------------------
# Interface helpers
# ---------------------------------------------------------------------------

def _is_wireless(iface: str) -> bool:
    return os.path.exists(f"/sys/class/net/{iface}/wireless") or \
           os.path.isdir(f"/sys/class/net/{iface}/phy80211")


def _is_management_iface(iface: str) -> bool:
    """Check if iface is the locked management interface."""
    try:
        from ..services import management_manager
        mgmt = management_manager.status()
        return mgmt.get("iface") == iface
    except Exception:
        return False


def _check_rfkill(iface: str) -> Optional[str]:
    """Check if the interface is rfkill-blocked. Returns error string or None."""
    out, _ = run("rfkill list wifi")
    if "Soft blocked: yes" in out or "Hard blocked: yes" in out:
        # Try to unblock
        run("rfkill unblock wifi")
        time.sleep(0.5)
        out2, _ = run("rfkill list wifi")
        if "Hard blocked: yes" in out2:
            return "hard-blocked by rfkill (physical switch?)"
        if "Soft blocked: yes" in out2:
            return "soft-blocked by rfkill (rfkill unblock failed)"
    return None


def _kill_existing(iface: str) -> None:
    """Stop any RogueLink-managed wpa_supplicant and DHCP client for this iface."""
    # Stop RogueLink-managed PID files
    pid_path = _wpa_pid_path(iface)
    stop_pid(pid_path)

    # Also stop legacy single-file paths
    stop_pid(paths.WAN_WPA_PID, paths.WAN_WPA_CONF)

    # Kill any lingering per-iface processes
    run(f"pkill -f 'wpa_supplicant.*-i.*{shlex.quote(iface)}'")
    run(f"pkill -f 'dhclient.*{shlex.quote(iface)}'")
    run(f"pkill -f 'dhcpcd.*{shlex.quote(iface)}'")
    time.sleep(0.5)


def _bring_up(iface: str) -> tuple:
    """Bring the interface up. Returns (ok, error_msg)."""
    run("rfkill unblock wifi")
    out, code = run(f"ip link set {shlex.quote(iface)} up")
    if code != 0:
        return False, f"ip link set up failed: {out}"
    time.sleep(0.3)
    operstate = read_text(f"/sys/class/net/{iface}/operstate")
    if operstate == "down":
        # Some drivers need a moment
        time.sleep(1)
    return True, None


def _start_dhcp(iface: str) -> tuple:
    """Run DHCP client. Returns (ok, output, client_used)."""
    qi = shlex.quote(iface)

    # Try dhclient first
    if shutil.which("dhclient"):
        # Release any existing lease first
        run(f"dhclient -r {qi}", timeout=5)
        time.sleep(0.3)
        out, code = run(f"dhclient -v {qi}", timeout=30)
        append_log(paths.WAN_LOG, f"dhclient rc={code}")
        if code == 0:
            return True, out, "dhclient"

    # Try dhcpcd
    if shutil.which("dhcpcd"):
        out, code = run(f"dhcpcd -n {qi}", timeout=30)
        append_log(paths.WAN_LOG, f"dhcpcd rc={code}")
        if code == 0:
            return True, out, "dhcpcd"

    # Try udhcpc
    if shutil.which("udhcpc"):
        out, code = run(f"udhcpc -i {qi} -n -q", timeout=30)
        append_log(paths.WAN_LOG, f"udhcpc rc={code}")
        if code == 0:
            return True, out, "udhcpc"

    return False, "no DHCP client succeeded", "none"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Connect — staged with full error reporting
# ---------------------------------------------------------------------------

def connect(iface: str, ssid: str, psk: str, country: str = "US") -> Dict:
    """Connect to a Wi-Fi network. Returns a structured result dict.

    On failure, the result includes:
      - ok: False
      - stage: which step failed
      - error: human-readable error
      - reason: technical reason
      - recommended_fix: suggestion for the operator
      - output: raw command output (if applicable)
    """
    result: Dict = {
        "ok": False,
        "iface": iface,
        "ssid": ssid,
        "stage": "init",
    }

    # Stage 1: validate interface
    if not interface_exists(iface):
        result.update(stage="validate", error=f"interface {iface} not present",
                      reason="missing_iface",
                      recommended_fix=f"Check: ip link show {iface}")
        return result

    if not _is_wireless(iface):
        result.update(stage="validate", error=f"{iface} is not a wireless interface",
                      reason="not_wireless",
                      recommended_fix="Use a wireless adapter interface")
        return result

    if not ssid:
        result.update(stage="validate", error="SSID required",
                      reason="missing_ssid")
        return result

    if _is_management_iface(iface):
        result.update(stage="validate", error=f"{iface} is the management interface",
                      reason="management_conflict",
                      recommended_fix="Use a different adapter for WAN")
        return result

    # Stage 2: rfkill check
    rfkill_err = _check_rfkill(iface)
    if rfkill_err:
        result.update(stage="rfkill", error=f"rfkill: {rfkill_err}",
                      reason="rfkill_blocked",
                      recommended_fix="rfkill unblock wifi")
        return result

    # Stage 3: kill existing
    append_log(paths.WAN_LOG, f"connect start: iface={iface} ssid={ssid}")
    _kill_existing(iface)

    # Stage 4: bring interface up
    up_ok, up_err = _bring_up(iface)
    if not up_ok:
        result.update(stage="bring_up", error=up_err, reason="iface_up_failed",
                      recommended_fix=f"Check driver: ip link set {iface} up")
        return result

    # Stage 5: write config and start wpa_supplicant
    conf_path = _wpa_conf_path(iface)
    pid_path = _wpa_pid_path(iface)
    conf_text = _build_wpa_conf(country, ssid, psk)
    write_text(conf_path, conf_text, mode=0o600)

    wpa_cmd = (
        f"wpa_supplicant -B"
        f" -i {shlex.quote(iface)}"
        f" -c {shlex.quote(conf_path)}"
        f" -D nl80211"
        f" -P {shlex.quote(pid_path)}"
    )
    wpa_out, wpa_code = run(wpa_cmd, timeout=15)
    append_log(paths.WAN_LOG, f"wpa_supplicant rc={wpa_code}")

    if wpa_code != 0:
        # Try without -D nl80211 (some drivers need wext)
        wpa_cmd_wext = (
            f"wpa_supplicant -B"
            f" -i {shlex.quote(iface)}"
            f" -c {shlex.quote(conf_path)}"
            f" -D wext"
            f" -P {shlex.quote(pid_path)}"
        )
        wpa_out2, wpa_code2 = run(wpa_cmd_wext, timeout=15)
        append_log(paths.WAN_LOG, f"wpa_supplicant (wext fallback) rc={wpa_code2}")
        if wpa_code2 != 0:
            result.update(
                stage="wpa_supplicant",
                error="wpa_supplicant failed to start",
                reason="wpa_start_failed",
                output=wpa_out + "\n" + wpa_out2,
                command=wpa_cmd,
                recommended_fix=(
                    "Check: which wpa_supplicant, "
                    f"check driver supports station mode: iw phy $(iw dev {iface} info | grep wiphy | awk '{{print $2}}') info"
                ),
            )
            return result

    # Stage 6: wait for association
    associated = False
    for attempt in range(20):  # up to 20 seconds
        link_out, _ = run(f"iw dev {shlex.quote(iface)} link")
        if link_out and "Connected to" in link_out:
            associated = True
            break
        time.sleep(1)

    if not associated:
        append_log(paths.WAN_LOG, f"association timeout for ssid={ssid}")
        result.update(
            stage="association",
            error=f"association timeout (20s) for SSID: {ssid}",
            reason="association_timeout",
            recommended_fix=(
                "Check: wrong PSK, hidden SSID, out of range, "
                "channel mismatch, or driver does not support this network"
            ),
        )
        return result

    # Stage 7: DHCP
    dhcp_ok, dhcp_out, dhcp_client = _start_dhcp(iface)
    if not dhcp_ok:
        append_log(paths.WAN_LOG, f"DHCP failed: {dhcp_out}")
        result.update(
            stage="dhcp",
            error=f"DHCP failed ({dhcp_client})",
            reason="dhcp_failed",
            output=dhcp_out,
            recommended_fix=(
                "Check: upstream router DHCP server, "
                "install dhclient/dhcpcd: sudo apt install isc-dhcp-client"
            ),
        )
        return result

    # Stage 8: verify IP assignment
    time.sleep(1)
    ip_out, _ = run(f"ip -4 -o addr show dev {shlex.quote(iface)}")
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    assigned_ip = m.group(1) if m else None
    if not assigned_ip:
        result.update(
            stage="ip_verify",
            error="no IP address assigned after DHCP",
            reason="no_ip",
            recommended_fix="Check upstream DHCP server",
        )
        return result

    # Stage 9: check gateway
    gw_out, _ = run(f"ip route show default dev {shlex.quote(iface)}")
    gw_match = re.search(r"default via ([0-9.]+)", gw_out or "")
    gateway = gw_match.group(1) if gw_match else None

    # Get signal strength
    link_out, _ = run(f"iw dev {shlex.quote(iface)} link")
    signal: Optional[float] = None
    connected_ssid = ssid
    if link_out and "Connected to" in link_out:
        for l in link_out.splitlines():
            l = l.strip()
            if l.startswith("SSID:"):
                connected_ssid = l.split(":", 1)[1].strip()
            if l.startswith("signal:"):
                try:
                    signal = float(l.split(":", 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

    # Save WAN state
    state.save_wan_profile({
        "iface": iface,
        "ssid": ssid,
        "psk": psk,
        "country": country,
        "connected_at": time.time(),
    })

    append_log(paths.WAN_LOG,
               f"connected: iface={iface} ssid={connected_ssid} ip={assigned_ip} gw={gateway}")

    result.update(
        ok=True,
        stage="complete",
        associated=True,
        dhcp=True,
        dhcp_client=dhcp_client,
        ip=assigned_ip,
        gateway=gateway,
        signal_dbm=signal,
        connected_ssid=connected_ssid,
    )
    return result


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def diag(iface: str) -> Dict:
    """Run diagnostics on a WAN interface."""
    result: Dict = {"iface": iface}

    # Existence
    result["exists"] = interface_exists(iface)
    if not result["exists"]:
        result["error"] = f"Interface {iface} does not exist"
        return result

    # Wireless check
    result["wireless"] = _is_wireless(iface)

    # Operstate
    result["operstate"] = read_text(f"/sys/class/net/{iface}/operstate")

    # Driver
    driver_link = f"/sys/class/net/{iface}/device/driver"
    if os.path.islink(driver_link):
        result["driver"] = os.path.basename(os.readlink(driver_link))
    else:
        result["driver"] = None

    # rfkill
    rfkill_err = _check_rfkill(iface)
    result["rfkill_blocked"] = rfkill_err is not None
    if rfkill_err:
        result["rfkill_detail"] = rfkill_err

    # wpa_supplicant running?
    wpa_out, _ = run(f"pgrep -fa 'wpa_supplicant.*{shlex.quote(iface)}'")
    result["wpa_supplicant_running"] = bool(wpa_out.strip())

    # Link status
    link_out, _ = run(f"iw dev {shlex.quote(iface)} link")
    result["associated"] = "Connected to" in link_out if link_out else False
    if result["associated"]:
        for l in link_out.splitlines():
            l = l.strip()
            if l.startswith("SSID:"):
                result["ssid"] = l.split(":", 1)[1].strip()
            if l.startswith("signal:"):
                try:
                    result["signal_dbm"] = float(l.split(":", 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

    # IP
    ip_out, _ = run(f"ip -4 -o addr show dev {shlex.quote(iface)}")
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    result["ip"] = m.group(1) if m else None

    # Gateway
    gw_out, _ = run(f"ip route show default dev {shlex.quote(iface)}")
    gw_match = re.search(r"default via ([0-9.]+)", gw_out or "")
    result["gateway"] = gw_match.group(1) if gw_match else None

    # DHCP client available
    result["dhcp_clients"] = {
        "dhclient": shutil.which("dhclient") is not None,
        "dhcpcd": shutil.which("dhcpcd") is not None,
        "udhcpc": shutil.which("udhcpc") is not None,
    }

    # wpa_supplicant available
    result["wpa_supplicant_available"] = shutil.which("wpa_supplicant") is not None

    return result


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

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
