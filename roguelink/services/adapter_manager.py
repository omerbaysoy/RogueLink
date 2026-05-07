"""Adapter detection, chipset/driver mapping, role assignment.

Adapter identity uses USB vendor:product IDs read from sysfs so role
assignments survive interface-name reshuffles between reboots.
"""

import os
import re
from typing import Dict, List, Optional

from .. import state
from ..utils import interface_exists, read_text, run


# USB IDs grouped by chipset family. Sourced from Ghostlink-Mini reference.
RTL8812AU_USB_IDS = frozenset({"0bda:8812", "0bda:881a", "0e66:0023", "2357:0101"})
RTL88X2BU_USB_IDS = frozenset({"0bda:b812", "0bda:b82c", "0bda:b81b"})
RTL8188EUS_USB_IDS = frozenset({"2357:010c", "0bda:8179", "7392:b811"})
MT7612U_USB_IDS = frozenset(
    {
        "0e8d:7612",
        "0e8d:761a",
        "2001:3a02",
        "0b05:17d1",
        "148f:7612",
        "13b1:003e",
    }
)

CHIPSET_LABELS = {
    "rtl8812au": "Realtek RTL8812AU (802.11ac, USB)",
    "rtl88x2bu": "Realtek RTL88x2BU (802.11ac, USB)",
    "rtl8188eus": "Realtek RTL8188EUS (802.11n 2.4GHz, USB)",
    "mt7612u": "MediaTek MT7612U (802.11ac, USB)",
    "brcmfmac": "Broadcom (Raspberry Pi onboard)",
}

DRIVER_TO_CHIPSET = {
    "88XXau": "rtl8812au",
    "8812au": "rtl8812au",
    "rtw_8812au": "rtl8812au",
    "rtw88_8812au": "rtl8812au",
    "88x2bu": "rtl88x2bu",
    "rtw_8822bu": "rtl88x2bu",
    "rtw88_8822bu": "rtl88x2bu",
    "8188eu": "rtl8188eus",
    "r8188eu": "rtl8188eus",
    "mt76x2u": "mt7612u",
    "mt76usb": "mt7612u",
    "brcmfmac": "brcmfmac",
    "brcmsmac": "brcmfmac",
}

# Per-chipset capability hints for role suggestion.
CHIPSET_CAPABILITY = {
    "rtl8812au": {"ap": True, "wan": True, "monitor": True, "preferred_role": "wan"},
    "rtl88x2bu": {"ap": True, "wan": True, "monitor": False, "preferred_role": "wan"},
    "mt7612u": {"ap": True, "wan": True, "monitor": True, "preferred_role": "ap"},
    "rtl8188eus": {"ap": True, "wan": True, "monitor": True, "preferred_role": "fallback"},
    "brcmfmac": {"ap": False, "wan": True, "monitor": False, "preferred_role": "management"},
}


def list_wireless_interfaces() -> List[str]:
    out, code = run("iw dev")
    if code != 0 or not out:
        # Fallback: scan /sys/class/net for entries with a wireless directory.
        try:
            ifaces = []
            for name in os.listdir("/sys/class/net"):
                if os.path.isdir(f"/sys/class/net/{name}/wireless"):
                    ifaces.append(name)
            return ifaces
        except OSError:
            return []
    return [
        line.split()[1]
        for line in out.splitlines()
        if line.strip().startswith("Interface ")
    ]


def get_driver(iface: str) -> str:
    safe = os.path.basename(iface)
    path = f"/sys/class/net/{safe}/device/driver"
    target = os.path.realpath(path) if os.path.exists(path) else ""
    return os.path.basename(target) if target else "unknown"


def get_mac(iface: str) -> str:
    safe = os.path.basename(iface)
    return read_text(f"/sys/class/net/{safe}/address") or "00:00:00:00:00:00"


def get_phy(iface: str) -> str:
    out, code = run(f"iw dev {iface} info")
    if code != 0:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("wiphy "):
            return f"phy{line.split()[1]}"
    return ""


def get_usb_path(iface: str) -> str:
    safe = os.path.basename(iface)
    real = os.path.realpath(f"/sys/class/net/{safe}/device")
    return real if real else ""


def get_usb_id(iface: str) -> Optional[str]:
    safe = os.path.basename(iface)
    current = f"/sys/class/net/{safe}/device"
    for _ in range(4):
        vendor = read_text(os.path.join(current, "idVendor"))
        product = read_text(os.path.join(current, "idProduct"))
        if vendor and product:
            return f"{vendor.lower()}:{product.lower()}"
        parent = os.path.dirname(os.path.realpath(current))
        if parent == current:
            break
        current = parent
    uevent = read_text(f"/sys/class/net/{safe}/device/uevent")
    for line in uevent.splitlines():
        if line.startswith("PRODUCT="):
            parts = line.split("=", 1)[1].split("/")
            if len(parts) >= 2:
                try:
                    return f"{int(parts[0], 16):04x}:{int(parts[1], 16):04x}"
                except ValueError:
                    pass
    modalias = read_text(f"/sys/class/net/{safe}/device/modalias")
    match = re.search(r"v([0-9A-Fa-f]{4})p([0-9A-Fa-f]{4})", modalias)
    if match:
        return f"{match.group(1).lower()}:{match.group(2).lower()}"
    return None


def chipset_for(iface: str) -> Optional[str]:
    usb_id = get_usb_id(iface) or ""
    if usb_id in RTL8812AU_USB_IDS:
        return "rtl8812au"
    if usb_id in RTL88X2BU_USB_IDS:
        return "rtl88x2bu"
    if usb_id in RTL8188EUS_USB_IDS:
        return "rtl8188eus"
    if usb_id in MT7612U_USB_IDS:
        return "mt7612u"
    return DRIVER_TO_CHIPSET.get(get_driver(iface))


def get_operstate(iface: str) -> str:
    if not interface_exists(iface):
        return "missing"
    return read_text(f"/sys/class/net/{os.path.basename(iface)}/operstate") or "unknown"


def is_onboard(iface: str) -> bool:
    return get_driver(iface) in ("brcmfmac", "brcmsmac")


def describe(iface: str) -> Dict:
    chipset = chipset_for(iface)
    capability = CHIPSET_CAPABILITY.get(chipset, {})
    return {
        "iface": iface,
        "mac": get_mac(iface),
        "driver": get_driver(iface),
        "chipset": chipset or "unknown",
        "chipset_label": CHIPSET_LABELS.get(chipset, "Unknown / unsupported"),
        "usb_id": get_usb_id(iface),
        "usb_path": get_usb_path(iface),
        "phy": get_phy(iface),
        "operstate": get_operstate(iface),
        "onboard": is_onboard(iface),
        "ap_capable": bool(capability.get("ap")),
        "wan_capable": bool(capability.get("wan")),
        "monitor_capable": bool(capability.get("monitor")),
        "preferred_role": capability.get("preferred_role"),
    }


def list_adapters() -> List[Dict]:
    return [describe(iface) for iface in list_wireless_interfaces()]


def detect_roles() -> Dict[str, Optional[str]]:
    """Return the suggested role->iface map and persist it.

    Roles:
      - management: the onboard Pi Wi-Fi (brcmfmac) — never reassigned.
      - wan:        external adapter best suited as WAN uplink.
      - ap:         external adapter best suited as AP.

    Saved assignments take priority unless the interface no longer exists.
    """
    saved = state.load_adapter_map()
    adapters = list_adapters()
    by_iface = {ad["iface"]: ad for ad in adapters}

    roles: Dict[str, Optional[str]] = {"management": None, "wan": None, "ap": None}

    # Honor saved management iface only if it's still present and onboard.
    saved_mgmt = saved.get("management")
    if saved_mgmt and saved_mgmt in by_iface and by_iface[saved_mgmt]["onboard"]:
        roles["management"] = saved_mgmt

    if not roles["management"]:
        for ad in adapters:
            if ad["onboard"]:
                roles["management"] = ad["iface"]
                break

    used = {roles["management"]}

    for role in ("wan", "ap"):
        saved_iface = saved.get(role)
        if (
            saved_iface
            and saved_iface in by_iface
            and saved_iface not in used
            and not by_iface[saved_iface]["onboard"]
        ):
            roles[role] = saved_iface
            used.add(saved_iface)

    # Auto-pick by preferred_role for unfilled slots.
    candidates = [ad for ad in adapters if not ad["onboard"] and ad["iface"] not in used]
    # Prefer mt7612u for AP.
    if not roles["ap"]:
        for ad in candidates:
            if ad["chipset"] == "mt7612u":
                roles["ap"] = ad["iface"]
                used.add(ad["iface"])
                break
    # Prefer rtl8812au or rtl88x2bu for WAN.
    if not roles["wan"]:
        for chip in ("rtl8812au", "rtl88x2bu"):
            for ad in candidates:
                if ad["chipset"] == chip and ad["iface"] not in used:
                    roles["wan"] = ad["iface"]
                    used.add(ad["iface"])
                    break
            if roles["wan"]:
                break
    # Fill remaining slots with anything left (rtl8188eus is fallback).
    leftovers = [ad for ad in candidates if ad["iface"] not in used]
    for role in ("wan", "ap"):
        if roles[role] or not leftovers:
            continue
        roles[role] = leftovers.pop(0)["iface"]

    state.save_adapter_map(roles)
    return roles


def assign_role(role: str, iface: str) -> bool:
    if role not in {"management", "wan", "ap"}:
        return False
    if iface and not interface_exists(iface):
        return False
    roles = state.load_adapter_map() or {"management": None, "wan": None, "ap": None}
    # Don't allow management role to be reassigned to a non-onboard adapter.
    if role == "management" and iface and not is_onboard(iface):
        return False
    roles[role] = iface or None
    state.save_adapter_map(roles)
    return True


def warnings() -> List[str]:
    """Adapter-level warnings shown to operators."""
    out: List[str] = []
    saved = state.load_adapter_map()
    by_iface = {ad["iface"]: ad for ad in list_adapters()}
    for role in ("management", "wan", "ap"):
        iface = saved.get(role)
        if iface and iface not in by_iface:
            out.append(f"{role} interface '{iface}' is not currently visible.")
        elif iface:
            ad = by_iface[iface]
            if ad["chipset"] == "unknown":
                out.append(
                    f"{role} interface '{iface}' has unrecognized chipset (driver={ad['driver']})."
                )
    if saved.get("management") and not by_iface.get(saved["management"], {}).get("onboard", False):
        out.append("Management role is not bound to onboard Pi Wi-Fi (brcmfmac).")
    return out
