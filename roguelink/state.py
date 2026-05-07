"""Runtime state files: adapter map, role assignments, AP/WAN/LAN profiles."""

from typing import Any, Dict, Optional

from . import paths
from .utils import load_json, save_json


def load_adapter_map() -> Dict[str, Optional[str]]:
    return load_json(paths.ADAPTER_MAP_PATH, default={})


def save_adapter_map(data: Dict[str, Optional[str]]) -> bool:
    return save_json(paths.ADAPTER_MAP_PATH, data, mode=0o644)


def load_state() -> Dict[str, Any]:
    return load_json(paths.STATE_PATH, default={})


def save_state(state: Dict[str, Any]) -> bool:
    return save_json(paths.STATE_PATH, state, mode=0o644)


def update_state(updates: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    state.update(updates)
    save_state(state)
    return state


def load_wan_profile() -> Dict[str, Any]:
    return load_json(paths.WAN_PROFILE_PATH, default={})


def save_wan_profile(profile: Dict[str, Any]) -> bool:
    return save_json(paths.WAN_PROFILE_PATH, profile, mode=0o600)


def load_ap_profile() -> Dict[str, Any]:
    return load_json(paths.AP_PROFILE_PATH, default={})


def save_ap_profile(profile: Dict[str, Any]) -> bool:
    return save_json(paths.AP_PROFILE_PATH, profile, mode=0o600)


def load_lan_profile() -> Dict[str, Any]:
    return load_json(paths.LAN_PROFILE_PATH, default={})


def save_lan_profile(profile: Dict[str, Any]) -> bool:
    return save_json(paths.LAN_PROFILE_PATH, profile, mode=0o644)


def load_mgmt_profile() -> Dict[str, Any]:
    return load_json(paths.MGMT_PROFILE_PATH, default={})


def save_mgmt_profile(profile: Dict[str, Any]) -> bool:
    return save_json(paths.MGMT_PROFILE_PATH, profile, mode=0o600)
