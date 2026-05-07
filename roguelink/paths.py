"""Filesystem paths used by RogueLink.

The conventional locations are FHS-style. They can be overridden via env
variables to support development on non-root systems.
"""

import os

ETC_DIR = os.environ.get("ROGUELINK_ETC", "/etc/roguelink")
LIB_DIR = os.environ.get("ROGUELINK_LIB", "/var/lib/roguelink")
LOG_DIR = os.environ.get("ROGUELINK_LOG", "/var/log/roguelink")
RUN_DIR = os.environ.get("ROGUELINK_RUN", "/run/roguelink")

CONFIG_PATH = os.path.join(ETC_DIR, "roguelink.toml")
STATE_PATH = os.path.join(LIB_DIR, "state.json")
ADAPTER_MAP_PATH = os.path.join(LIB_DIR, "adapters.json")
AUTH_PATH = os.path.join(ETC_DIR, "auth.json")

WAN_PROFILE_PATH = os.path.join(LIB_DIR, "wan_profile.json")
AP_PROFILE_PATH = os.path.join(LIB_DIR, "ap_profile.json")
LAN_PROFILE_PATH = os.path.join(LIB_DIR, "lan_profile.json")
MGMT_PROFILE_PATH = os.path.join(LIB_DIR, "mgmt_profile.json")

WAN_WPA_CONF = os.path.join(RUN_DIR, "wpa_supplicant_wan.conf")
WAN_WPA_PID = os.path.join(RUN_DIR, "wpa_supplicant_wan.pid")
HOSTAPD_CONF = os.path.join(RUN_DIR, "hostapd.conf")
HOSTAPD_PID = os.path.join(RUN_DIR, "hostapd.pid")
DNSMASQ_AP_CONF = os.path.join(RUN_DIR, "dnsmasq_ap.conf")
DNSMASQ_AP_PID = os.path.join(RUN_DIR, "dnsmasq_ap.pid")
DNSMASQ_AP_LEASES = os.path.join(LIB_DIR, "dnsmasq_ap.leases")
DNSMASQ_LAN_CONF = os.path.join(RUN_DIR, "dnsmasq_lan.conf")
DNSMASQ_LAN_PID = os.path.join(RUN_DIR, "dnsmasq_lan.pid")
DNSMASQ_LAN_LEASES = os.path.join(LIB_DIR, "dnsmasq_lan.leases")
NFT_RULESET_PATH = os.path.join(RUN_DIR, "roguelink.nft")

DAEMON_LOG = os.path.join(LOG_DIR, "roguelinkd.log")
SETUP_LOG = os.path.join(LOG_DIR, "setup.log")
WAN_LOG = os.path.join(LOG_DIR, "wan.log")
AP_LOG = os.path.join(LOG_DIR, "ap.log")
LAN_LOG = os.path.join(LOG_DIR, "lan.log")
FIREWALL_LOG = os.path.join(LOG_DIR, "firewall.log")
NETWORKS_LOG = os.path.join(LOG_DIR, "networks.log")
SPEEDTEST_LOG = os.path.join(LOG_DIR, "speedtest.log")
HEALTH_LOG = os.path.join(LOG_DIR, "health.log")

NETWORKS_DB = os.path.join(LIB_DIR, "networks.db")
SPEEDTEST_LAST = os.path.join(LIB_DIR, "speedtest_last.json")
HEALTH_LAST = os.path.join(LIB_DIR, "health_last.json")
FAN_PROFILE_PATH = os.path.join(LIB_DIR, "fan_profile.json")

BOOT_CONFIG_CANDIDATES = ("/boot/firmware/config.txt", "/boot/config.txt")


def ensure_dirs():
    """Create RogueLink directories if missing. Returns list of error strings."""
    errors = []
    for path in (ETC_DIR, LIB_DIR, LOG_DIR, RUN_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors
