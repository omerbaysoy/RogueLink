"""Driver detection and install hooks for supported chipsets.

For Realtek chipsets (rtl8812au / rtl88x2bu / rtl8188eus) we plan a DKMS
install path; for MediaTek (mt7612u) the in-kernel mt76 stack is used and
firmware-misc-nonfree must be installed. Actual driver builds are gated by
running on a real Linux/Pi target — this module exposes detection and
reporting helpers, and a single helper to attempt the install for one
chipset (used by ``scripts/install.sh`` and the CLI).
"""

import os
import platform
import shutil
from typing import Any, Dict, List, Optional

from ..utils import read_text, run, run_ok


REALTEK_RTL8812AU = {
    "name": "rtl8812au",
    "label": "Realtek RTL8812AU",
    "kind": "dkms",
    "primary_module": "88XXau",
    "modules": ["88XXau", "8812au", "rtw_8812au", "rtw88_8812au"],
    "git_url": "https://github.com/aircrack-ng/rtl8812au.git",
    "branch": "v5.6.4.2",
    "src_dir": "/usr/src/rtl8812au",
    "blacklist_modules": ["rtw_8812au", "rtw88_8812au", "rtl8xxxu"],
    "conflict_modules": ["rtw_8812au", "rtw88_8812au", "rtl8xxxu", "8812au", "88XXau"],
    "modprobe_options": "options 88XXau rtw_led_ctrl=0",
    "fallback_git_url": "https://github.com/morrownr/8812au-20210820.git",
    "fallback_module": "8812au",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl8812au.conf",
    "usb_ids": ["0bda:8812", "0bda:881a", "0e66:0023", "2357:0101"],
    "firmware_files": [],
    "preferred_role": "wan",
    "capabilities": "AP, WAN, MON (802.11ac, USB)",
}

REALTEK_RTL88X2BU = {
    "name": "rtl88x2bu",
    "label": "Realtek RTL88x2BU",
    "kind": "dkms",
    "primary_module": "88x2bu",
    "modules": ["88x2bu", "rtw_8822bu", "rtw88_8822bu"],
    "git_url": "https://github.com/morrownr/88x2bu-20210702.git",
    "branch": "main",
    "src_dir": "/usr/src/rtl88x2bu",
    "blacklist_modules": [],
    "conflict_modules": [],
    "modprobe_options": "",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl88x2bu.conf",
    "usb_ids": ["0bda:b812", "0bda:b82c", "0bda:b81b"],
    "firmware_files": [],
    "preferred_role": "wan",
    "capabilities": "AP (limited), WAN (802.11ac, USB)",
}

REALTEK_RTL8188EUS = {
    "name": "rtl8188eus",
    "label": "Realtek RTL8188EUS",
    "kind": "dkms",
    "primary_module": "8188eu",
    "modules": ["8188eu", "r8188eu"],
    "git_url": "https://github.com/aircrack-ng/rtl8188eus.git",
    "branch": "v5.7.6",
    "src_dir": "/usr/src/rtl8188eus",
    "blacklist_modules": ["r8188eu"],
    "conflict_modules": ["r8188eu"],
    "modprobe_options": "",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl8188eus.conf",
    "usb_ids": ["2357:010c", "0bda:8179", "7392:b811"],
    "firmware_files": [],
    "preferred_role": "fallback",
    "capabilities": "Fallback / test (802.11n 2.4GHz, USB)",
}

MEDIATEK_MT7612U = {
    "name": "mt7612u",
    "label": "MediaTek MT7612U",
    "kind": "in_kernel",
    "primary_module": "mt76x2u",
    "modules": ["mt76x2u", "mt76_usb", "mt76x2_common", "mt76"],
    "firmware_package": "firmware-misc-nonfree",
    "firmware_files": [
        "/lib/firmware/mediatek/mt7662u.bin",
        "/lib/firmware/mediatek/mt7662u_rom_patch.bin",
    ],
    "usb_ids": ["0e8d:7612", "0e8d:761a", "2001:3a02", "0b05:17d1", "148f:7612", "13b1:003e"],
    "preferred_role": "ap",
    "capabilities": "AP (recommended), WAN, MON (802.11ac, USB)",
}

DRIVERS = {
    "rtl8812au": REALTEK_RTL8812AU,
    "rtl88x2bu": REALTEK_RTL88X2BU,
    "rtl8188eus": REALTEK_RTL8188EUS,
    "mt7612u": MEDIATEK_MT7612U,
}

BASE_PACKAGES = [
    "python3",
    "python3-venv",
    "python3-pip",
    "hostapd",
    "dnsmasq",
    "nftables",
    "iw",
    "wireless-tools",
    "wpasupplicant",
    "rfkill",
    "ethtool",
    "git",
    "curl",
    "build-essential",
    "dkms",
]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def module_available(module: str) -> bool:
    return run_ok(f"modinfo {module}")


def module_loaded(module: str) -> bool:
    out, _ = run("lsmod")
    for line in out.splitlines():
        if line.split()[:1] == [module]:
            return True
    return False


def kernel_release() -> str:
    out, _ = run("uname -r")
    return out.strip().splitlines()[0] if out else ""


def kernel_headers_present() -> bool:
    rel = kernel_release()
    if not rel:
        return False
    return os.path.isdir(f"/lib/modules/{rel}/build")


def header_package_candidates() -> List[str]:
    """Best-effort list of header packages to try installing."""
    rel = kernel_release()
    candidates = []
    if rel:
        candidates.append(f"linux-headers-{rel}")
    candidates.extend(["raspberrypi-kernel-headers", "linux-headers-arm64", "linux-headers-amd64"])
    return candidates


def _get_arch() -> str:
    """Detect ARM architecture for Makefile patching."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("arm") or machine.startswith("armv"):
        return "arm"
    return machine


def _dkms_status(module_name: str) -> str:
    """Get DKMS status string for a module (e.g. 'installed', 'built', '')."""
    out, code = run(f"dkms status {module_name}")
    if code != 0 or not out.strip():
        return ""
    return out.strip()


def _blacklist_status(conf_path: str, blacklist_modules: List[str]) -> Dict[str, Any]:
    """Check if the blacklist configuration file exists and has the right entries."""
    if not os.path.exists(conf_path):
        return {"exists": False, "missing": blacklist_modules, "content": ""}
    content = read_text(conf_path)
    missing = []
    for mod in blacklist_modules:
        if f"blacklist {mod}" not in content:
            missing.append(mod)
    return {"exists": True, "missing": missing, "content": content}


def _bound_iface_for_usb_ids(usb_ids: List[str]) -> List[str]:
    """Find interfaces bound to any of the given USB vendor:product IDs."""
    ifaces = []
    try:
        for name in os.listdir("/sys/class/net"):
            net_dir = f"/sys/class/net/{name}/device"
            if not os.path.exists(net_dir):
                continue
            # Walk up sysfs to find idVendor/idProduct
            current = net_dir
            for _ in range(4):
                vendor = read_text(os.path.join(current, "idVendor")).strip().lower()
                product = read_text(os.path.join(current, "idProduct")).strip().lower()
                if vendor and product:
                    usb_id = f"{vendor}:{product}"
                    if usb_id in usb_ids:
                        ifaces.append(name)
                    break
                parent = os.path.dirname(os.path.realpath(current))
                if parent == current:
                    break
                current = parent
    except OSError:
        pass
    return ifaces


def _makefile_arm_patched(src_dir: str) -> Dict[str, Any]:
    """Check if the Makefile ARM patches have been applied."""
    makefile = os.path.join(src_dir, "Makefile")
    if not os.path.exists(makefile):
        return {"exists": False, "i386_disabled": False, "arm64_enabled": False, "arm_enabled": False}
    content = read_text(makefile)
    i386_disabled = "CONFIG_PLATFORM_I386_PC = n" in content
    arm64_enabled = "CONFIG_PLATFORM_ARM64_RPI = y" in content
    arm_enabled = "CONFIG_PLATFORM_ARM_RPI = y" in content
    return {
        "exists": True,
        "i386_disabled": i386_disabled,
        "arm64_enabled": arm64_enabled,
        "arm_enabled": arm_enabled,
    }


# ---------------------------------------------------------------------------
# Status / verify / audit
# ---------------------------------------------------------------------------


def status() -> List[Dict]:
    out: List[Dict] = []
    for name, spec in DRIVERS.items():
        loaded = any(module_loaded(m) for m in spec["modules"])
        available = any(module_available(m) for m in spec["modules"])
        out.append(
            {
                "name": name,
                "label": spec["label"],
                "kind": spec["kind"],
                "modules": spec["modules"],
                "available": available,
                "loaded": loaded,
            }
        )
    return out


def verify_driver(chipset: str) -> Dict[str, Any]:
    """Detailed verification of a specific driver/chipset.

    Returns a rich dict with module status, DKMS status, conflicts,
    blacklist, firmware, bound interfaces, recommended fix, etc.
    """
    spec = DRIVERS.get(chipset)
    if not spec:
        return {"ok": False, "error": f"unknown chipset: {chipset}"}

    result: Dict[str, Any] = {
        "chipset": chipset,
        "label": spec["label"],
        "kind": spec["kind"],
        "preferred_role": spec.get("preferred_role", ""),
        "capabilities": spec.get("capabilities", ""),
    }

    # Module availability and load status
    module_info: List[Dict[str, Any]] = []
    any_available = False
    any_loaded = False
    loaded_module = None
    for mod in spec["modules"]:
        avail = module_available(mod)
        loaded = module_loaded(mod)
        module_info.append({"module": mod, "available": avail, "loaded": loaded})
        if avail:
            any_available = True
        if loaded:
            any_loaded = True
            loaded_module = mod
    result["modules"] = module_info
    result["any_available"] = any_available
    result["any_loaded"] = any_loaded
    result["loaded_module"] = loaded_module

    # USB ID bound interfaces
    usb_ids = spec.get("usb_ids", [])
    bound_ifaces = _bound_iface_for_usb_ids(usb_ids)
    result["usb_ids"] = usb_ids
    result["bound_ifaces"] = bound_ifaces

    # DKMS-specific checks
    if spec["kind"] == "dkms":
        primary = spec.get("primary_module", spec["modules"][0])
        result["primary_module"] = primary
        result["dkms_status"] = _dkms_status(chipset)

        # Conflict modules
        conflict_mods = spec.get("conflict_modules", [])
        conflicts_loaded = [m for m in conflict_mods if module_loaded(m) and m != loaded_module]
        result["conflict_modules"] = conflict_mods
        result["conflicts_loaded"] = conflicts_loaded

        # Blacklist
        bl = spec.get("blacklist_modules", [])
        conf = spec.get("modprobe_conf", "")
        result["blacklist"] = _blacklist_status(conf, bl)

        # Source dir
        src = spec.get("src_dir", "")
        result["source_path"] = src
        result["source_exists"] = os.path.isdir(src)

        # Makefile ARM patches (RTL8812AU specific)
        if chipset == "rtl8812au":
            result["makefile_arm"] = _makefile_arm_patched(src)

        # Fallback detection
        if chipset == "rtl8812au":
            fallback_mod = spec.get("fallback_module", "8812au")
            using_fallback = loaded_module == fallback_mod
            result["using_fallback"] = using_fallback
            if using_fallback:
                result["fallback_warning"] = (
                    f"Loaded module is '{fallback_mod}' (fallback). "
                    f"Primary strategy uses '{primary}' from aircrack-ng/rtl8812au v5.6.4.2."
                )

    # In-kernel driver checks
    elif spec["kind"] == "in_kernel":
        fw_files = spec.get("firmware_files", [])
        fw_status = {}
        for fw in fw_files:
            fw_status[fw] = os.path.exists(fw)
        result["firmware_files"] = fw_status
        result["firmware_ok"] = all(fw_status.values()) if fw_status else True

    # Recommended fix
    fixes = []
    if not any_available:
        if spec["kind"] == "dkms":
            fixes.append(f"Install driver: roguelink system install-driver {chipset}")
        elif spec["kind"] == "in_kernel":
            fixes.append(f"Install firmware: sudo apt install {spec.get('firmware_package', 'firmware-misc-nonfree')}")
            fixes.append(f"Then: sudo modprobe {spec.get('primary_module', spec['modules'][0])}")
    elif not any_loaded:
        primary = spec.get("primary_module", spec["modules"][0])
        fixes.append(f"Load module: sudo modprobe {primary}")
    if spec["kind"] == "dkms":
        conflicts = [m for m in spec.get("conflict_modules", []) if module_loaded(m) and m != loaded_module]
        if conflicts:
            fixes.append(f"Unload conflict modules: sudo rmmod {' '.join(conflicts)}")
        bl_info = result.get("blacklist", {})
        if bl_info.get("missing"):
            fixes.append(f"Missing blacklist entries in {spec.get('modprobe_conf', '')}: {bl_info['missing']}")
    if spec["kind"] == "in_kernel":
        fw_status = result.get("firmware_files", {})
        missing_fw = [f for f, ok in fw_status.items() if not ok]
        if missing_fw:
            fixes.append(f"Missing firmware: {', '.join(missing_fw)}")

    result["recommended_fixes"] = fixes
    result["ok"] = any_available and (any_loaded or not bound_ifaces)
    return result


def driver_audit() -> List[Dict[str, Any]]:
    """Run verify_driver for all supported chipsets."""
    return [verify_driver(name) for name in DRIVERS]


def driver_diag(iface: Optional[str] = None) -> Dict[str, Any]:
    """Per-interface driver diagnostics or overall diagnostics."""
    from . import adapter_manager

    result: Dict[str, Any] = {
        "kernel": kernel_release(),
        "headers_present": kernel_headers_present(),
        "architecture": _get_arch(),
    }

    if iface:
        ad = adapter_manager.describe(iface)
        chipset = ad.get("chipset")
        result["iface"] = ad
        if chipset and chipset in DRIVERS:
            result["driver_verify"] = verify_driver(chipset)
        else:
            result["driver_verify"] = {"error": f"chipset '{chipset}' not in supported drivers"}
    else:
        result["adapters"] = adapter_manager.list_adapters()
        result["drivers"] = driver_audit()

    return result


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_base_packages() -> Dict:
    """Run apt-get install for base packages plus header candidates."""
    if shutil.which("apt-get") is None:
        return {"ok": False, "error": "apt-get not available"}
    pkgs = list(BASE_PACKAGES) + header_package_candidates()
    cmd = "DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(pkgs)
    out, code = run(f"apt-get update && {cmd}", timeout=600)
    return {"ok": code == 0, "output": out}


def install_mediatek_firmware() -> Dict:
    if shutil.which("apt-get") is None:
        return {"ok": False, "error": "apt-get not available"}
    out, code = run(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y firmware-misc-nonfree",
        timeout=180,
    )
    # Try loading the module
    if code == 0:
        run("modprobe mt76x2u")
    return {"ok": code == 0, "output": out}


def install_realtek_dkms(spec: Dict) -> Dict:
    """Clone, configure, and DKMS-install a Realtek out-of-tree driver."""
    if shutil.which("git") is None or shutil.which("dkms") is None:
        return {"ok": False, "error": "git or dkms not available"}
    if not kernel_headers_present():
        return {"ok": False, "error": f"kernel headers missing for {kernel_release()}"}

    src = spec["src_dir"]
    git_url = spec["git_url"]
    branch = spec["branch"]

    # Unload conflict modules before install
    for mod in spec.get("conflict_modules", []):
        if module_loaded(mod):
            run(f"rmmod {mod}")

    if not os.path.isdir(src):
        out, code = run(
            f"git clone -b {branch} --single-branch {git_url} {src}", timeout=300
        )
        if code != 0 and spec.get("fallback_git_url"):
            out, code = run(
                f"git clone {spec['fallback_git_url']} {src}", timeout=300
            )
        if code != 0:
            return {"ok": False, "error": f"git clone failed: {out}"}

    # Patch Makefile for ARM64/Pi if present.
    makefile = os.path.join(src, "Makefile")
    arch = _get_arch()
    if os.path.exists(makefile):
        run(f"sed -i 's/CONFIG_PLATFORM_I386_PC = y/CONFIG_PLATFORM_I386_PC = n/' {makefile}")
        if arch == "arm64":
            run(f"sed -i 's/CONFIG_PLATFORM_ARM64_RPI = n/CONFIG_PLATFORM_ARM64_RPI = y/' {makefile}")
        elif arch == "arm":
            run(f"sed -i 's/CONFIG_PLATFORM_ARM_RPI = n/CONFIG_PLATFORM_ARM_RPI = y/' {makefile}")

    # Build with correct ARCH
    arch_env = ""
    if arch == "arm64":
        arch_env = "ARCH=arm64 "
    elif arch == "arm":
        arch_env = "ARCH=arm "

    out, code = run(f"cd {src} && {arch_env}make dkms_install", timeout=900)
    if code != 0:
        return {"ok": False, "error": f"dkms_install failed: {out}"}

    # Write blacklist and module options.
    conf_lines = [f"# Generated by RogueLink for {spec['label']}"]
    for mod in spec.get("blacklist_modules", []):
        conf_lines.append(f"blacklist {mod}")
    if spec.get("modprobe_options"):
        conf_lines.append(spec["modprobe_options"])
    if conf_lines:
        try:
            with open(spec["modprobe_conf"], "w", encoding="utf-8") as f:
                f.write("\n".join(conf_lines) + "\n")
        except OSError as exc:
            return {"ok": False, "error": f"write modprobe.d failed: {exc}"}

    # Try loading the primary module.
    primary = spec.get("primary_module", spec["modules"][0])
    run(f"modprobe {primary}")
    return {"ok": True, "output": out, "module": primary}


def install_for(chipset: str) -> Dict:
    spec = DRIVERS.get(chipset)
    if not spec:
        return {"ok": False, "error": f"unknown chipset {chipset}"}
    if spec["kind"] == "in_kernel":
        return install_mediatek_firmware()
    return install_realtek_dkms(spec)
