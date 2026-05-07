"""Log tailing helpers for the dashboard/CLI."""

import os
from typing import Dict, List

from .. import paths


LOG_FILES: Dict[str, str] = {
    "daemon": paths.DAEMON_LOG,
    "setup": paths.SETUP_LOG,
    "wan": paths.WAN_LOG,
    "ap": paths.AP_LOG,
    "lan": paths.LAN_LOG,
    "firewall": paths.FIREWALL_LOG,
    "networks": paths.NETWORKS_LOG,
    "speedtest": paths.SPEEDTEST_LOG,
    "health": paths.HEALTH_LOG,
}


def tail(name: str, lines: int = 200) -> List[str]:
    path = LOG_FILES.get(name)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def list_logs() -> Dict[str, bool]:
    return {name: os.path.exists(path) for name, path in LOG_FILES.items()}
