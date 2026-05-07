"""Aggregated dashboard metrics."""

from typing import Dict

from . import (
    adapter_manager,
    ap_manager,
    firewall_manager,
    lan_manager,
    management_manager,
    system_manager,
    wan_manager,
)
from .. import config as roguelink_config


def overview() -> Dict:
    cfg = roguelink_config.load()
    api_port = cfg.get("general", {}).get("api_port", 8080)
    sys = system_manager.overview()
    wan = wan_manager.status()
    ap = ap_manager.status()
    lan = lan_manager.status()
    mgmt = management_manager.status()
    fw = firewall_manager.status()
    warns = adapter_manager.warnings()
    internet_status = "Connected" if wan.get("ip") and wan.get("gateway") else "Unknown"
    return {
        "system": sys,
        "wan": wan,
        "ap": ap,
        "lan": lan,
        "management": mgmt,
        "firewall": fw,
        "adapter_warnings": warns,
        "dashboard_url": management_manager.dashboard_url(api_port),
        "api_port": api_port,
        "internet": internet_status,
    }
