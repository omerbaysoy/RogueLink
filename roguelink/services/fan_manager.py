"""Pi 5 fan profile control. Writes a managed block to config.txt."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import paths
from ..utils import append_log, load_json, read_text, save_json
from . import system_manager


# A profile is a list of (temp_c, speed_byte) pairs for fan_temp0..3.
PROFILES: Dict[str, List[Dict[str, int]]] = {
    "quiet": [
        {"temp": 60, "speed": 75},
        {"temp": 67, "speed": 125},
        {"temp": 75, "speed": 192},
        {"temp": 82, "speed": 255},
    ],
    "balanced": [
        {"temp": 50, "speed": 75},
        {"temp": 60, "speed": 125},
        {"temp": 67, "speed": 192},
        {"temp": 75, "speed": 255},
    ],
    "performance": [
        {"temp": 45, "speed": 100},
        {"temp": 55, "speed": 175},
        {"temp": 62, "speed": 220},
        {"temp": 70, "speed": 255},
    ],
    "max": [
        {"temp": 40, "speed": 150},
        {"temp": 45, "speed": 200},
        {"temp": 50, "speed": 240},
        {"temp": 55, "speed": 255},
    ],
}

BLOCK_BEGIN = "# RogueLink fan profile begin"
BLOCK_END = "# RogueLink fan profile end"


def _render_block(profile: str, points: List[Dict[str, int]]) -> str:
    lines = [BLOCK_BEGIN, f"# profile={profile}", "[all]"]
    for idx, point in enumerate(points):
        lines.append(f"dtparam=fan_temp{idx}={point['temp']}")
        lines.append(f"dtparam=fan_temp{idx}_hyst=5")
        lines.append(f"dtparam=fan_temp{idx}_speed={point['speed']}")
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def _backup(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.roguelink-fan-bak-{stamp}"
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


def _strip_existing_block(text: str) -> str:
    if BLOCK_BEGIN not in text:
        return text
    out_lines: List[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith(BLOCK_BEGIN):
            skipping = True
            continue
        if line.startswith(BLOCK_END):
            skipping = False
            continue
        if not skipping:
            out_lines.append(line)
    return "\n".join(out_lines).rstrip() + "\n"


def apply_profile(profile: str, custom_points: Optional[List[Dict[str, int]]] = None) -> Dict[str, Any]:
    if profile == "custom":
        points = custom_points or []
        if len(points) != 4:
            return {"ok": False, "error": "custom profile requires 4 (temp, speed) points"}
        for p in points:
            if not (20 <= p["temp"] <= 95):
                return {"ok": False, "error": "custom temp must be 20..95 °C"}
            if not (0 <= p["speed"] <= 255):
                return {"ok": False, "error": "custom speed must be 0..255"}
    else:
        points = PROFILES.get(profile)
        if not points:
            return {"ok": False, "error": f"unknown profile {profile}"}

    config_path = system_manager.boot_config_path()
    if not config_path:
        return {"ok": False, "error": "boot config.txt not found"}

    backup = _backup(config_path)
    text = read_text(config_path)
    cleaned = _strip_existing_block(text)
    block = _render_block(profile, points)
    new_text = cleaned.rstrip() + ("\n\n" if cleaned.strip() else "") + block
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}", "backup": backup}

    save_json(
        paths.FAN_PROFILE_PATH,
        {
            "profile": profile,
            "points": points,
            "applied_at": time.time(),
            "config_path": config_path,
            "backup": backup,
        },
        mode=0o644,
    )
    append_log(paths.DAEMON_LOG, f"fan profile applied: {profile} -> {config_path} (backup={backup})")
    return {
        "ok": True,
        "profile": profile,
        "points": points,
        "config_path": config_path,
        "backup": backup,
        "block": block,
        "reboot_required": True,
    }


def status() -> Dict[str, Any]:
    saved = load_json(paths.FAN_PROFILE_PATH, default={}) or {}
    config_path = system_manager.boot_config_path()
    text = read_text(config_path) if config_path else ""
    block_present = BLOCK_BEGIN in text
    return {
        "profile": saved.get("profile", "balanced" if block_present else "none"),
        "points": saved.get("points") or PROFILES.get("balanced"),
        "applied_at": saved.get("applied_at"),
        "config_path": config_path,
        "backup": saved.get("backup"),
        "block_present": block_present,
        "available_profiles": list(PROFILES.keys()) + ["custom"],
        "current_temperature_c": system_manager.get_temperature_c(),
        "reboot_required": system_manager.reboot_required(),
    }
