"""WAN uplink: an external Wi-Fi adapter joins an upstream Wi-Fi network.

Uses wpa_supplicant directly and runs dhclient/dhcpcd/udhcpc on the chosen
interface. The connect flow is staged with explicit error reporting.
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

WPA_LOG_DIR = paths.LOG_DIR


def _wpa_conf_path(iface: str) -> str:
    return os.path.join(paths.RUN_DIR, f"wpa_supplicant-{iface}.conf")


def _wpa_pid_path(iface: str) -> str:
    return os.path.join(paths.RUN_DIR, f"wpa_supplicant-{iface}.pid")


def _wpa_log_path(iface: str) -> str:
    return os.path.join(WPA_LOG_DIR, f"wpa_supplicant-{iface}.log")


# ---------------------------------------------------------------------------
# wpa config builder
# ---------------------------------------------------------------------------

def _build_wpa_conf(country: str, ssid: str, psk: str) -> str:
    header = (
        "ctrl_interface=/run/wpa_supplicant\n"
        "update_config=0\n"
        f"country={country}\n\n"
    )
    if not psk:
        net_block = (
            "network={\n"
            f'    ssid="{ssid}"\n'
            "    key_mgmt=NONE\n"
            "    priority=10\n"
            "}\n"
        )
    elif shutil.which("wpa_passphrase"):
        out, code = run(f"wpa_passphrase {shlex.quote(ssid)} {shlex.quote(psk)}", timeout=5)
        if code == 0 and "network=" in out:
            lines = [l for l in out.splitlines() if not l.strip().startswith("#psk=")]
            net_block = "\n".join(lines) + "\n"
        else:
            net_block = _plain_net_block(ssid, psk)
    else:
        net_block = _plain_net_block(ssid, psk)
    return header + net_block


def _plain_net_block(ssid: str, psk: str) -> str:
    return (
        "network={\n"
        f'    ssid="{ssid}"\n'
        f'    psk="{psk}"\n'
        "    key_mgmt=WPA-PSK\n"
        "    priority=10\n"
        "}\n"
    )


# ---------------------------------------------------------------------------
# Interface helpers
# ---------------------------------------------------------------------------

def _is_wireless(iface: str) -> bool:
    return os.path.exists(f"/sys/class/net/{iface}/wireless") or \
           os.path.isdir(f"/sys/class/net/{iface}/phy80211")


def _is_management_iface(iface: str) -> bool:
    try:
        from ..services import management_manager
        mgmt = management_manager.status()
        return mgmt.get("iface") == iface
    except Exception:
        return False


def _get_driver(iface: str) -> Optional[str]:
    link = f"/sys/class/net/{iface}/device/driver"
    if os.path.islink(link):
        return os.path.basename(os.readlink(link))
    return None


def _check_rfkill(iface: str) -> Optional[str]:
    out, _ = run("rfkill list wifi")
    if "Soft blocked: yes" in out or "Hard blocked: yes" in out:
        run("rfkill unblock wifi")
        time.sleep(0.5)
        out2, _ = run("rfkill list wifi")
        if "Hard blocked: yes" in out2:
            return "hard-blocked by rfkill"
        if "Soft blocked: yes" in out2:
            return "soft-blocked by rfkill"
    return None


def _kill_existing(iface: str) -> None:
    """Stop RogueLink-managed wpa_supplicant and DHCP for this iface."""
    stop_pid(_wpa_pid_path(iface))
    stop_pid(paths.WAN_WPA_PID, paths.WAN_WPA_CONF)
    qi = shlex.quote(iface)
    run(f"pkill -f 'wpa_supplicant.*-i *{qi}'")
    run(f"pkill -f 'dhclient.*{qi}'")
    run(f"pkill -f 'dhcpcd.*{qi}'")
    # Remove stale control socket
    ctrl = f"/run/wpa_supplicant/{iface}"
    if os.path.exists(ctrl):
        try:
            os.remove(ctrl)
        except OSError:
            pass
    time.sleep(0.5)


def _prepare_iface(iface: str, country: str) -> tuple:
    """Full interface preparation. Returns (ok, error_msg)."""
    run("rfkill unblock wifi")
    qi = shlex.quote(iface)
    # Flush and down
    run(f"ip addr flush dev {qi}")
    run(f"ip link set {qi} down")
    time.sleep(0.3)
    # Set managed mode
    run(f"iw dev {qi} set type managed")
    time.sleep(0.2)
    # Up
    out, code = run(f"ip link set {qi} up")
    if code != 0:
        return False, f"ip link set up failed: {out}"
    time.sleep(0.5)
    # Power save off
    run(f"iw dev {qi} set power_save off")
    # Regulatory domain
    if country:
        run(f"iw reg set {shlex.quote(country)}")
    return True, None


def _start_dhcp(iface: str) -> tuple:
    qi = shlex.quote(iface)
    if shutil.which("dhclient"):
        run(f"dhclient -r {qi}", timeout=5)
        time.sleep(0.3)
        out, code = run(f"dhclient -v {qi}", timeout=30)
        append_log(paths.WAN_LOG, f"dhclient rc={code}")
        if code == 0:
            return True, out, "dhclient"
    if shutil.which("dhcpcd"):
        out, code = run(f"dhcpcd -n {qi}", timeout=30)
        append_log(paths.WAN_LOG, f"dhcpcd rc={code}")
        if code == 0:
            return True, out, "dhcpcd"
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
    run("rfkill unblock wifi")
    run(f"ip link set {shlex.quote(iface)} up")
    time.sleep(0.3)
    out, code = run(f"iw dev {shlex.quote(iface)} scan", timeout=20)
    if code != 0 or not out:
        return []
    networks: List[Dict] = []
    current: Dict = {}
    for line in out.splitlines():
        if line.startswith("BSS "):
            if current.get("ssid"):
                networks.append(current)
            bssid_match = re.match(r"BSS\s+([0-9a-f:]{17})", line)
            current = {
                "bssid": bssid_match.group(1) if bssid_match else "",
                "ssid": "", "signal": None, "channel": None, "encryption": "Open",
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
        networks.append(current)
    by_ssid: Dict[str, Dict] = {}
    for net in networks:
        key = str(net.get("ssid"))
        if key in by_ssid:
            if (net.get("signal") or -999) > (by_ssid[key].get("signal") or -999):
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
# Connect
# ---------------------------------------------------------------------------

def connect(iface: str, ssid: str, psk: str, country: str = "TR") -> Dict:
    """Connect to Wi-Fi. Returns structured result with stage/reason/fix."""
    driver = _get_driver(iface)
    result: Dict = {
        "ok": False, "iface": iface, "ssid": ssid, "stage": "init",
        "driver": driver, "wpa_backend_used": None,
    }

    # Stage 1: validate
    if not interface_exists(iface):
        result.update(stage="validate", error=f"interface {iface} not found",
                      reason="missing_iface",
                      recommended_fix=f"ip link show {iface}")
        return result
    if not _is_wireless(iface):
        result.update(stage="validate", error=f"{iface} is not wireless",
                      reason="not_wireless",
                      recommended_fix="Use a wireless adapter")
        return result
    if not ssid:
        result.update(stage="validate", error="SSID required", reason="missing_ssid")
        return result
    if _is_management_iface(iface):
        result.update(stage="validate", error=f"{iface} is the management interface",
                      reason="management_conflict",
                      recommended_fix="Use a different adapter for WAN")
        return result

    # Stage 2: rfkill
    rfkill_err = _check_rfkill(iface)
    if rfkill_err:
        result.update(stage="rfkill", error=rfkill_err, reason="rfkill_blocked",
                      recommended_fix="rfkill unblock wifi")
        return result

    append_log(paths.WAN_LOG, f"connect: iface={iface} ssid={ssid} driver={driver}")

    # Stage 3: kill stale processes
    _kill_existing(iface)

    # Stage 4: prepare interface (down, managed mode, up, powersave off, reg)
    up_ok, up_err = _prepare_iface(iface, country)
    if not up_ok:
        result.update(stage="prepare", error=up_err, reason="iface_prepare_failed",
                      recommended_fix=f"Check driver: ip link set {iface} up")
        return result

    # Stage 5: write config
    conf_path = _wpa_conf_path(iface)
    pid_path = _wpa_pid_path(iface)
    log_path = _wpa_log_path(iface)
    write_text(conf_path, _build_wpa_conf(country, ssid, psk), mode=0o600)
    result["log_path"] = log_path

    # Stage 6: start wpa_supplicant
    qi = shlex.quote(iface)
    qc = shlex.quote(conf_path)
    qp = shlex.quote(pid_path)
    ql = shlex.quote(log_path)

    # Try nl80211 first with log file
    wpa_cmd = f"wpa_supplicant -B -i {qi} -c {qc} -D nl80211 -P {qp} -f {ql}"
    wpa_out, wpa_code = run(wpa_cmd, timeout=15)
    append_log(paths.WAN_LOG, f"wpa nl80211 rc={wpa_code} out={wpa_out[:200]}")
    wpa_backend = "nl80211"

    if wpa_code != 0:
        # Run foreground diagnostic to capture real error
        diag_cmd = f"timeout 8s wpa_supplicant -dd -i {qi} -c {qc} -D nl80211 2>&1"
        diag_out, _ = run(diag_cmd, timeout=12)
        append_log(paths.WAN_LOG, f"wpa diag (nl80211): {diag_out[:500]}")

        # Try wext fallback
        wpa_cmd2 = f"wpa_supplicant -B -i {qi} -c {qc} -D wext -P {qp} -f {ql}"
        wpa_out2, wpa_code2 = run(wpa_cmd2, timeout=15)
        append_log(paths.WAN_LOG, f"wpa wext rc={wpa_code2} out={wpa_out2[:200]}")
        wpa_backend = "wext"

        if wpa_code2 != 0:
            # Run foreground diagnostic for wext too
            diag_cmd2 = f"timeout 8s wpa_supplicant -dd -i {qi} -c {qc} -D wext 2>&1"
            diag_out2, _ = run(diag_cmd2, timeout=12)
            append_log(paths.WAN_LOG, f"wpa diag (wext): {diag_out2[:500]}")

            # Both failed
            combined_diag = diag_out[-800:] + "\n--- wext ---\n" + diag_out2[-800:]
            result.update(
                stage="wpa_supplicant", wpa_backend_used="nl80211+wext",
                error="wpa_supplicant failed to start with both nl80211 and wext",
                reason="wpa_start_failed",
                command=wpa_cmd,
                stdout_tail=wpa_out[-300:] + "\n" + wpa_out2[-300:],
                stderr_tail=combined_diag[-600:],
                recommended_fix=(
                    f"1) Check wpa_supplicant is installed: which wpa_supplicant\n"
                    f"2) Check {iface} supports station mode: iw phy\n"
                    f"3) Check for stale processes: pgrep -a wpa_supplicant\n"
                    f"4) Check dmesg: dmesg | grep -i {iface}\n"
                    f"5) Check log: cat {log_path}"
                ),
            )
            return result

    result["wpa_backend_used"] = wpa_backend
    result["wpa_started"] = True
    time.sleep(1)

    # Stage 7: wait for association (up to 25s)
    associated = False
    for _ in range(25):
        link_out, _ = run(f"iw dev {qi} link")
        if link_out and "Connected to" in link_out:
            associated = True
            break
        # Also check wpa_cli if available
        wpa_status, _ = run(f"wpa_cli -i {qi} status 2>/dev/null")
        if "wpa_state=COMPLETED" in wpa_status:
            associated = True
            break
        if "WRONG_KEY" in wpa_status or "ASSOC_REJECT" in wpa_status:
            break
        time.sleep(1)

    if not associated:
        # Check why
        wpa_status, _ = run(f"wpa_cli -i {qi} status 2>/dev/null")
        wpa_log_tail = read_text(log_path)[-500:] if os.path.exists(log_path) else ""
        reason = "association_timeout"
        fix = "Check: wrong PSK, out of range, hidden SSID, channel mismatch"
        if "WRONG_KEY" in wpa_status:
            reason = "wrong_psk"
            fix = "PSK/password is incorrect"
        elif "ASSOC_REJECT" in wpa_status:
            reason = "assoc_rejected"
            fix = "AP rejected association — check MAC filter or max clients"
        append_log(paths.WAN_LOG, f"association failed: {reason}")
        result.update(
            stage="association", associated=False,
            error=f"Association failed: {reason}",
            reason=reason, recommended_fix=fix,
            stdout_tail=wpa_status[-300:],
            stderr_tail=wpa_log_tail,
        )
        return result

    result["associated"] = True

    # Stage 8: DHCP
    dhcp_ok, dhcp_out, dhcp_client = _start_dhcp(iface)
    if not dhcp_ok:
        append_log(paths.WAN_LOG, f"DHCP failed: {dhcp_out[:200]}")
        result.update(
            stage="dhcp", error=f"DHCP failed ({dhcp_client})",
            reason="dhcp_failed", stdout_tail=dhcp_out[-300:],
            recommended_fix="Check upstream DHCP server; sudo apt install isc-dhcp-client",
        )
        return result

    result["dhcp_client"] = dhcp_client
    time.sleep(1)

    # Stage 9: verify IP
    ip_out, _ = run(f"ip -4 -o addr show dev {qi}")
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    assigned_ip = m.group(1) if m else None
    if not assigned_ip:
        result.update(stage="ip_verify", error="no IP assigned after DHCP",
                      reason="no_ip", recommended_fix="Check upstream DHCP server")
        return result

    # Stage 10: gateway + signal
    gw_out, _ = run(f"ip route show default dev {qi}")
    gw_match = re.search(r"default via ([0-9.]+)", gw_out or "")
    gateway = gw_match.group(1) if gw_match else None

    link_out, _ = run(f"iw dev {qi} link")
    signal: Optional[float] = None
    connected_ssid = ssid
    if link_out and "Connected to" in link_out:
        for ln in link_out.splitlines():
            ln = ln.strip()
            if ln.startswith("SSID:"):
                connected_ssid = ln.split(":", 1)[1].strip()
            if ln.startswith("signal:"):
                try:
                    signal = float(ln.split(":", 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass

    # DNS
    dns_servers: List[str] = []
    resolv = read_text("/etc/resolv.conf")
    for ln in resolv.splitlines():
        if ln.startswith("nameserver"):
            parts = ln.split()
            if len(parts) >= 2:
                dns_servers.append(parts[1])

    # Save state
    state.save_wan_profile({
        "iface": iface, "ssid": ssid, "psk": psk,
        "country": country, "connected_at": time.time(),
    })

    append_log(paths.WAN_LOG,
               f"connected: iface={iface} ssid={connected_ssid} ip={assigned_ip} gw={gateway}")

    result.update(
        ok=True, stage="complete", associated=True,
        dhcp=True, ip=assigned_ip, gateway=gateway,
        signal_dbm=signal, connected_ssid=connected_ssid,
        dns=dns_servers,
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
    """Comprehensive WAN diagnostics."""
    result: Dict = {"iface": iface}
    result["exists"] = interface_exists(iface)
    if not result["exists"]:
        result["error"] = f"Interface {iface} does not exist"
        return result

    qi = shlex.quote(iface)
    result["wireless"] = _is_wireless(iface)
    result["operstate"] = read_text(f"/sys/class/net/{iface}/operstate")
    result["driver"] = _get_driver(iface)

    # USB identity
    for attr in ("idVendor", "idProduct"):
        val = read_text(f"/sys/class/net/{iface}/device/{attr}")
        if val:
            result[attr] = val

    # rfkill
    rfkill_err = _check_rfkill(iface)
    result["rfkill_blocked"] = rfkill_err is not None

    # ip link
    ip_link, _ = run(f"ip link show {qi}")
    result["ip_link"] = ip_link[:300]

    # iw dev info
    iw_info, _ = run(f"iw dev {qi} info")
    result["iw_info"] = iw_info[:400]

    # iw link
    link_out, _ = run(f"iw dev {qi} link")
    result["associated"] = "Connected to" in link_out if link_out else False
    result["iw_link"] = link_out[:300]

    # power save
    ps_out, _ = run(f"iw dev {qi} get power_save")
    result["power_save"] = ps_out.strip()

    # wpa_supplicant running?
    wpa_out, _ = run(f"pgrep -fa 'wpa_supplicant.*{qi}'")
    result["wpa_supplicant_running"] = bool(wpa_out.strip())
    result["wpa_processes"] = wpa_out.strip()[:200]

    # pidfile
    pid_path = _wpa_pid_path(iface)
    result["pidfile_exists"] = os.path.exists(pid_path)

    # wpa log tail
    log_path = _wpa_log_path(iface)
    if os.path.exists(log_path):
        result["wpa_log_tail"] = read_text(log_path)[-600:]
        result["wpa_log_path"] = log_path

    # IP
    ip_out, _ = run(f"ip -4 -o addr show dev {qi}")
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    result["ip"] = m.group(1) if m else None

    # Gateway
    gw_out, _ = run(f"ip route show default dev {qi}")
    gw_match = re.search(r"default via ([0-9.]+)", gw_out or "")
    result["gateway"] = gw_match.group(1) if gw_match else None

    # DHCP clients
    result["dhcp_clients"] = {
        "dhclient": shutil.which("dhclient") is not None,
        "dhcpcd": shutil.which("dhcpcd") is not None,
        "udhcpc": shutil.which("udhcpc") is not None,
    }
    result["wpa_supplicant_available"] = shutil.which("wpa_supplicant") is not None

    # dmesg tail for driver
    driver = result.get("driver") or ""
    dmesg_out, _ = run(f"dmesg | grep -iE '{iface}|{driver}' | tail -20")
    result["dmesg_tail"] = dmesg_out[:500]

    # rfkill list
    rfkill_out, _ = run("rfkill list wifi")
    result["rfkill_list"] = rfkill_out[:300]

    return result


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> Dict:
    profile = state.load_wan_profile()
    iface = profile.get("iface")
    if not iface or not interface_exists(iface):
        return {
            "connected": False, "iface": iface,
            "ssid": profile.get("ssid"), "ip": None,
            "gateway": None, "dns": [], "signal": None,
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
    m = re.search(r"inet\s+([0-9.]+)/", ip_out or "")
    ip_addr = m.group(1) if m else None
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
        "iface": iface, "operstate": operstate,
        "ssid": ssid or profile.get("ssid"),
        "ip": ip_addr, "gateway": gateway,
        "dns": dns_servers, "signal": signal,
    }
