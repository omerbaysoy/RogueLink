"""TOML-backed runtime configuration for RogueLink.

The TOML file is the persistent, human-editable configuration. Mutable runtime
state (e.g. AP/WAN profiles, lease maps) lives in JSON files under LIB_DIR
and is handled by ``state.py``.
"""

import os
from typing import Any, Dict

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from . import paths


DEFAULT_CONFIG: Dict[str, Any] = {
    "general": {
        "host": "0.0.0.0",
        "api_port": 8080,
        "country_code": "US",
        "log_level": "INFO",
    },
    "management": {
        "iface": "wlan0",
        "ssid": "",
        "psk": "",
        "static_ip": "",
        "fallback_ap_ssid": "RogueLink-Setup",
        "fallback_ap_psk": "",
    },
    "wan": {
        "iface": "",
        "ssid": "",
        "psk": "",
    },
    "ap": {
        "iface": "",
        "ssid": "RogueLink-AP",
        "psk": "",
        "channel": 6,
        "subnet": "10.42.0.0/24",
        "address": "10.42.0.1",
        "dhcp_start": "10.42.0.10",
        "dhcp_end": "10.42.0.200",
        "country_code": "US",
    },
    "lan": {
        "iface": "eth0",
        "subnet": "10.42.1.0/24",
        "address": "10.42.1.1",
        "dhcp_start": "10.42.1.10",
        "dhcp_end": "10.42.1.200",
        "enabled": False,
    },
    "auth": {
        "username": "admin",
    },
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> Dict[str, Any]:
    if not os.path.exists(paths.CONFIG_PATH):
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    try:
        with open(paths.CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    return _deep_merge(DEFAULT_CONFIG, data)


def _toml_dump(data: Dict[str, Any]) -> str:
    """Minimal TOML writer for our flat-section config layout."""
    lines = []
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def save(config: Dict[str, Any]) -> bool:
    paths.ensure_dirs()
    try:
        with open(paths.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(_toml_dump(config))
        try:
            os.chmod(paths.CONFIG_PATH, 0o640)
        except OSError:
            pass
        return True
    except OSError:
        return False


def update_section(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load()
    cfg.setdefault(section, {})
    cfg[section].update(values)
    save(cfg)
    return cfg
