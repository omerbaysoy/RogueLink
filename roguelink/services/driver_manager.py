"""Driver detection and install hooks for supported chipsets.

For Realtek chipsets (rtl8812au / rtl88x2bu / rtl8188eus) we plan a DKMS
install path; for MediaTek (mt7612u) the in-kernel mt76 stack is used and
firmware-misc-nonfree must be installed. Actual driver builds are gated by
running on a real Linux/Pi target — this module exposes detection and
reporting helpers, and a single helper to attempt the install for one
chipset (used by ``scripts/install.sh`` and the CLI).
"""

import os
import shutil
from typing import Dict, List

from ..utils import run, run_ok


REALTEK_RTL8812AU = {
    "name": "rtl8812au",
    "label": "Realtek RTL8812AU",
    "kind": "dkms",
    "modules": ["88XXau", "8812au", "rtw_8812au", "rtw88_8812au"],
    "git_url": "https://github.com/aircrack-ng/rtl8812au.git",
    "branch": "v5.6.4.2",
    "src_dir": "/usr/src/rtl8812au",
    "blacklist_modules": ["rtw_8812au", "rtw88_8812au", "rtl8xxxu"],
    "modprobe_options": "options 88XXau rtw_led_ctrl=0",
    "fallback_git_url": "https://github.com/morrownr/8812au-20210820.git",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl8812au.conf",
}

REALTEK_RTL88X2BU = {
    "name": "rtl88x2bu",
    "label": "Realtek RTL88x2BU",
    "kind": "dkms",
    "modules": ["88x2bu", "rtw_8822bu", "rtw88_8822bu"],
    "git_url": "https://github.com/morrownr/88x2bu-20210702.git",
    "branch": "main",
    "src_dir": "/usr/src/rtl88x2bu",
    "blacklist_modules": [],
    "modprobe_options": "",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl88x2bu.conf",
}

REALTEK_RTL8188EUS = {
    "name": "rtl8188eus",
    "label": "Realtek RTL8188EUS",
    "kind": "dkms",
    "modules": ["8188eu", "r8188eu"],
    "git_url": "https://github.com/aircrack-ng/rtl8188eus.git",
    "branch": "v5.7.6",
    "src_dir": "/usr/src/rtl8188eus",
    "blacklist_modules": ["r8188eu"],
    "modprobe_options": "",
    "modprobe_conf": "/etc/modprobe.d/roguelink-rtl8188eus.conf",
}

MEDIATEK_MT7612U = {
    "name": "mt7612u",
    "label": "MediaTek MT7612U",
    "kind": "in_kernel",
    "modules": ["mt76x2u", "mt76_usb", "mt76x2_common", "mt76"],
    "firmware_package": "firmware-misc-nonfree",
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
    if os.path.exists(makefile):
        run(f"sed -i 's/CONFIG_PLATFORM_I386_PC = y/CONFIG_PLATFORM_I386_PC = n/' {makefile}")
        run(f"sed -i 's/CONFIG_PLATFORM_ARM64_RPI = n/CONFIG_PLATFORM_ARM64_RPI = y/' {makefile}")

    out, code = run(f"cd {src} && make dkms_install", timeout=900)
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
    primary = spec["modules"][0]
    run(f"modprobe {primary}")
    return {"ok": True, "output": out}


def install_for(chipset: str) -> Dict:
    spec = DRIVERS.get(chipset)
    if not spec:
        return {"ok": False, "error": f"unknown chipset {chipset}"}
    if spec["kind"] == "in_kernel":
        return install_mediatek_firmware()
    return install_realtek_dkms(spec)
