"""Wi-Fi scanning, observation history, and saved-network database.

The persistent state lives in SQLite at ``/var/lib/roguelink/networks.db``
with three tables: ``saved_networks``, ``network_observations``, and
``connection_attempts``. Every scan updates observation counts and links
back to a saved network when the SSID matches.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .. import paths, state
from ..utils import append_log, interface_exists, run


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ssid TEXT NOT NULL,
    psk TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    preferred_iface TEXT NOT NULL DEFAULT '',
    preferred_identity_profile TEXT,
    auto_connect INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_connected_at REAL,
    last_connection_status TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(ssid)
);

CREATE TABLE IF NOT EXISTS network_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_network_id INTEGER REFERENCES saved_networks(id) ON DELETE SET NULL,
    ssid TEXT NOT NULL,
    bssid TEXT NOT NULL DEFAULT '',
    iface TEXT NOT NULL DEFAULT '',
    signal_dbm REAL,
    quality TEXT,
    frequency_mhz INTEGER,
    channel INTEGER,
    band TEXT,
    security TEXT,
    capabilities TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    strongest_signal_seen REAL,
    latest_signal REAL,
    raw TEXT,
    UNIQUE(ssid, bssid)
);

CREATE TABLE IF NOT EXISTS connection_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_network_id INTEGER REFERENCES saved_networks(id) ON DELETE SET NULL,
    ssid TEXT NOT NULL,
    iface TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    finished_at REAL,
    success INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    assigned_ip TEXT,
    gateway TEXT,
    signal_dbm REAL
);

CREATE INDEX IF NOT EXISTS idx_obs_ssid ON network_observations(ssid);
CREATE INDEX IF NOT EXISTS idx_obs_saved ON network_observations(saved_network_id);
CREATE INDEX IF NOT EXISTS idx_attempts_saved ON connection_attempts(saved_network_id);
"""


def _ensure_db_dir() -> None:
    os.makedirs(os.path.dirname(paths.NETWORKS_DB), exist_ok=True)


@contextmanager
def _connect():
    _ensure_db_dir()
    conn = sqlite3.connect(paths.NETWORKS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    _ensure_db_dir()
    with _connect() as conn:
        conn.executescript(SCHEMA)
    try:
        os.chmod(paths.NETWORKS_DB, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Scan parsing
# ---------------------------------------------------------------------------


def quality_for(signal_dbm: Optional[float]) -> str:
    if signal_dbm is None:
        return "unknown"
    if signal_dbm >= -50:
        return "excellent"
    if signal_dbm >= -60:
        return "good"
    if signal_dbm >= -70:
        return "fair"
    if signal_dbm >= -80:
        return "weak"
    return "poor"


def _band_for(freq_mhz: Optional[int]) -> str:
    if freq_mhz is None:
        return "unknown"
    if 2400 <= freq_mhz <= 2500:
        return "2.4GHz"
    if 4900 <= freq_mhz <= 5900:
        return "5GHz"
    if 5925 <= freq_mhz <= 7125:
        return "6GHz"
    return "unknown"


def _channel_for(freq_mhz: Optional[int]) -> Optional[int]:
    if freq_mhz is None:
        return None
    if 2412 <= freq_mhz <= 2484:
        return (freq_mhz - 2407) // 5 if freq_mhz != 2484 else 14
    if 5000 <= freq_mhz <= 5900:
        return (freq_mhz - 5000) // 5
    return None


def _summarize_security(flags: List[str]) -> str:
    has_wpa3 = any("WPA3" in f or "SAE" in f for f in flags)
    has_wpa2 = any("RSN" in f or "WPA2" in f for f in flags)
    has_wpa = any("WPA" in f and "WPA2" not in f and "WPA3" not in f for f in flags)
    has_wep = any("WEP" in f for f in flags)
    has_priv = any("Privacy" in f for f in flags)
    if has_wpa3 and has_wpa2:
        return "WPA2/WPA3"
    if has_wpa3:
        return "WPA3"
    if has_wpa2 and has_wpa:
        return "WPA/WPA2"
    if has_wpa2:
        return "WPA2"
    if has_wpa:
        return "WPA"
    if has_wep:
        return "WEP"
    if has_priv:
        return "Privacy"
    return "Open"


def parse_iw_scan(output: str, iface: str) -> List[Dict[str, Any]]:
    """Parse ``iw dev <iface> scan`` output into a list of network dicts."""
    networks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    flags: List[str] = []
    raw_lines: List[str] = []

    def _commit():
        nonlocal current, flags, raw_lines
        if current is None:
            return
        if current.get("ssid"):
            current["security"] = _summarize_security(flags)
            current["capabilities"] = ", ".join(sorted(set(flags)))
            current["raw"] = "\n".join(raw_lines)[-2000:]
            current["band"] = _band_for(current.get("frequency_mhz"))
            networks.append(current)
        current = None
        flags = []
        raw_lines = []

    for line in output.splitlines():
        if line.startswith("BSS "):
            _commit()
            m = re.match(r"BSS\s+([0-9a-f:]{17})", line)
            current = {
                "bssid": m.group(1) if m else "",
                "ssid": "",
                "signal_dbm": None,
                "frequency_mhz": None,
                "channel": None,
                "band": "unknown",
                "security": "Open",
                "capabilities": "",
                "iface": iface,
            }
            raw_lines = [line]
            continue
        if current is None:
            continue
        raw_lines.append(line)
        stripped = line.strip()
        if stripped.startswith("SSID:"):
            current["ssid"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("signal:"):
            try:
                current["signal_dbm"] = float(stripped.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        elif stripped.startswith("freq:"):
            try:
                freq = int(float(stripped.split(":", 1)[1].strip()))
                current["frequency_mhz"] = freq
                current.setdefault("channel", _channel_for(freq))
            except ValueError:
                pass
        elif stripped.startswith("DS Parameter set: channel"):
            try:
                current["channel"] = int(stripped.rsplit(" ", 1)[-1])
            except ValueError:
                pass
        elif stripped.startswith("RSN:") and "not present" not in stripped:
            flags.append("RSN")
            flags.append("WPA2")
        elif stripped.startswith("WPA:") and "not present" not in stripped:
            flags.append("WPA")
        elif "Authentication suites:" in stripped:
            if "SAE" in stripped:
                flags.append("WPA3")
                flags.append("SAE")
            if "PSK" in stripped:
                flags.append("PSK")
        elif "capability:" in stripped:
            if "Privacy" in stripped:
                flags.append("Privacy")
        elif "WEP" in stripped:
            flags.append("WEP")

    _commit()
    return networks


def _scan_with_iw(iface: str) -> List[Dict[str, Any]]:
    if not interface_exists(iface):
        return []
    run(f"rfkill unblock wifi")
    run(f"ip link set {shlex.quote(iface)} up")
    out, code = run(f"iw dev {shlex.quote(iface)} scan", timeout=25)
    if code != 0 or not out:
        append_log(paths.NETWORKS_LOG, f"scan failed iface={iface} rc={code} :: {out}")
        return []
    networks = parse_iw_scan(out, iface)
    for n in networks:
        n["quality"] = quality_for(n.get("signal_dbm"))
    # Deduplicate by (SSID, BSSID) keeping strongest signal.
    by_key: Dict[str, Dict[str, Any]] = {}
    for n in networks:
        key = f"{n['ssid']}|{n['bssid']}"
        existing = by_key.get(key)
        if existing is None or (n.get("signal_dbm") or -999) > (existing.get("signal_dbm") or -999):
            by_key[key] = n
    return sorted(by_key.values(), key=lambda x: x.get("signal_dbm") or -999, reverse=True)


def scan(iface: str) -> List[Dict[str, Any]]:
    """Run a Wi-Fi scan on ``iface`` and persist observations."""
    init_db()
    networks = _scan_with_iw(iface)
    now = time.time()
    state.update_state({"last_scan_at": now, "last_scan_iface": iface})
    if not networks:
        return []
    with _connect() as conn:
        for n in networks:
            saved_id = _saved_id_for_ssid(conn, n["ssid"])
            existing = conn.execute(
                "SELECT id, seen_count, strongest_signal_seen FROM network_observations "
                "WHERE ssid = ? AND bssid = ?",
                (n["ssid"], n["bssid"]),
            ).fetchone()
            signal = n.get("signal_dbm")
            if existing:
                strongest = existing["strongest_signal_seen"]
                if signal is not None and (strongest is None or signal > strongest):
                    strongest = signal
                conn.execute(
                    "UPDATE network_observations SET "
                    "saved_network_id = COALESCE(?, saved_network_id), "
                    "iface = ?, signal_dbm = ?, quality = ?, frequency_mhz = ?, "
                    "channel = ?, band = ?, security = ?, capabilities = ?, "
                    "last_seen = ?, seen_count = seen_count + 1, "
                    "strongest_signal_seen = ?, latest_signal = ?, raw = ? "
                    "WHERE id = ?",
                    (
                        saved_id,
                        n["iface"],
                        signal,
                        n["quality"],
                        n.get("frequency_mhz"),
                        n.get("channel"),
                        n.get("band"),
                        n.get("security"),
                        n.get("capabilities"),
                        now,
                        strongest,
                        signal,
                        n.get("raw"),
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO network_observations ("
                    "saved_network_id, ssid, bssid, iface, signal_dbm, quality, "
                    "frequency_mhz, channel, band, security, capabilities, "
                    "first_seen, last_seen, seen_count, strongest_signal_seen, "
                    "latest_signal, raw) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        saved_id,
                        n["ssid"],
                        n["bssid"],
                        n["iface"],
                        signal,
                        n["quality"],
                        n.get("frequency_mhz"),
                        n.get("channel"),
                        n.get("band"),
                        n.get("security"),
                        n.get("capabilities"),
                        now,
                        now,
                        signal,
                        signal,
                        n.get("raw"),
                    ),
                )
    append_log(paths.NETWORKS_LOG, f"scan ok iface={iface} count={len(networks)}")
    return networks


# ---------------------------------------------------------------------------
# Saved networks
# ---------------------------------------------------------------------------


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _saved_id_for_ssid(conn: sqlite3.Connection, ssid: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM saved_networks WHERE ssid = ?", (ssid,)
    ).fetchone()
    return row["id"] if row else None


def list_saved(masked: bool = True) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM saved_networks ORDER BY ssid COLLATE NOCASE"
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            data = _row(row) or {}
            if masked:
                data["psk"] = "***" if data.get("psk") else ""
            out.append(data)
        return out


def get_saved(network_id: int, masked: bool = True) -> Optional[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM saved_networks WHERE id = ?", (network_id,)
        ).fetchone()
        data = _row(row)
        if data and masked:
            data["psk"] = "***" if data.get("psk") else ""
        return data


def get_saved_psk(network_id: int) -> Optional[str]:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT psk FROM saved_networks WHERE id = ?", (network_id,)
        ).fetchone()
        return row["psk"] if row else None


def save_network(
    ssid: str,
    psk: str = "",
    note: str = "",
    preferred_iface: str = "",
    auto_connect: bool = False,
) -> Dict[str, Any]:
    init_db()
    if not ssid:
        return {"ok": False, "error": "ssid required"}
    now = time.time()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM saved_networks WHERE ssid = ?", (ssid,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE saved_networks SET psk = ?, note = ?, "
                "preferred_iface = ?, auto_connect = ?, updated_at = ? WHERE id = ?",
                (psk, note, preferred_iface, 1 if auto_connect else 0, now, existing["id"]),
            )
            saved_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO saved_networks ("
                "ssid, psk, note, preferred_iface, auto_connect, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ssid, psk, note, preferred_iface, 1 if auto_connect else 0, now, now),
            )
            saved_id = cur.lastrowid
        conn.execute(
            "UPDATE network_observations SET saved_network_id = ? WHERE ssid = ?",
            (saved_id, ssid),
        )
    append_log(paths.NETWORKS_LOG, f"saved network ssid={ssid} id={saved_id}")
    return {"ok": True, "id": saved_id}


def update_saved(
    network_id: int,
    ssid: Optional[str] = None,
    psk: Optional[str] = None,
    note: Optional[str] = None,
    preferred_iface: Optional[str] = None,
    auto_connect: Optional[bool] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    init_db()
    fields: List[str] = []
    values: List[Any] = []
    if ssid is not None:
        fields.append("ssid = ?"); values.append(ssid)
    if psk is not None:
        fields.append("psk = ?"); values.append(psk)
    if note is not None:
        fields.append("note = ?"); values.append(note)
    if preferred_iface is not None:
        fields.append("preferred_iface = ?"); values.append(preferred_iface)
    if auto_connect is not None:
        fields.append("auto_connect = ?"); values.append(1 if auto_connect else 0)
    if enabled is not None:
        fields.append("enabled = ?"); values.append(1 if enabled else 0)
    if not fields:
        return {"ok": False, "error": "no changes"}
    fields.append("updated_at = ?"); values.append(time.time())
    values.append(network_id)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE saved_networks SET {', '.join(fields)} WHERE id = ?",
            values,
        )
    append_log(paths.NETWORKS_LOG, f"updated network id={network_id}")
    return {"ok": cur.rowcount > 0}


def delete_saved(network_id: int) -> Dict[str, Any]:
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM saved_networks WHERE id = ?", (network_id,))
    append_log(paths.NETWORKS_LOG, f"deleted network id={network_id}")
    return {"ok": cur.rowcount > 0}


def observations_for(network_id: int) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM network_observations WHERE saved_network_id = ? "
            "ORDER BY last_seen DESC",
            (network_id,),
        ).fetchall()
    return [_row(r) or {} for r in rows]


def observations_for_ssid(ssid: str) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM network_observations WHERE ssid = ? ORDER BY last_seen DESC",
            (ssid,),
        ).fetchall()
    return [_row(r) or {} for r in rows]


def history(limit: int = 200) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM network_observations ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row(r) or {} for r in rows]


def connection_attempts(limit: int = 100, network_id: Optional[int] = None) -> List[Dict[str, Any]]:
    init_db()
    with _connect() as conn:
        if network_id is None:
            rows = conn.execute(
                "SELECT * FROM connection_attempts ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM connection_attempts WHERE saved_network_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (network_id, limit),
            ).fetchall()
    return [_row(r) or {} for r in rows]


def record_attempt(
    saved_network_id: Optional[int],
    ssid: str,
    iface: str,
    success: bool,
    error: Optional[str] = None,
    assigned_ip: Optional[str] = None,
    gateway: Optional[str] = None,
    signal_dbm: Optional[float] = None,
) -> int:
    init_db()
    started = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO connection_attempts ("
            "saved_network_id, ssid, iface, started_at, finished_at, "
            "success, error, assigned_ip, gateway, signal_dbm) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                saved_network_id,
                ssid,
                iface,
                started,
                started,
                1 if success else 0,
                error,
                assigned_ip,
                gateway,
                signal_dbm,
            ),
        )
        if saved_network_id:
            conn.execute(
                "UPDATE saved_networks SET last_connected_at = ?, "
                "last_connection_status = ?, updated_at = ? WHERE id = ?",
                (
                    started,
                    "success" if success else (error or "failed"),
                    started,
                    saved_network_id,
                ),
            )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Connect from saved network
# ---------------------------------------------------------------------------


def connect_saved(
    network_id: int,
    iface_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Connect to a saved network using the existing wan_manager."""
    from . import wan_manager  # local import to avoid cycles

    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, ssid, psk, preferred_iface FROM saved_networks WHERE id = ?",
            (network_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "saved network not found"}
    iface = iface_override or row["preferred_iface"]
    if not iface:
        roles = state.load_adapter_map() or {}
        iface = roles.get("wan") or ""
    if not iface:
        return {"ok": False, "error": "no interface (set preferred_iface or assign wan role)"}

    result = wan_manager.connect(iface, row["ssid"], row["psk"] or "")
    success = bool(result.get("ok"))
    error = None if success else result.get("error") or "wan connect failed"
    wan_status = wan_manager.status()
    record_attempt(
        saved_network_id=row["id"],
        ssid=row["ssid"],
        iface=iface,
        success=success,
        error=error,
        assigned_ip=wan_status.get("ip"),
        gateway=wan_status.get("gateway"),
        signal_dbm=wan_status.get("signal"),
    )
    append_log(
        paths.NETWORKS_LOG,
        f"connect saved id={network_id} ssid={row['ssid']} iface={iface} "
        f"ok={success} err={error}",
    )
    return {
        "ok": success,
        "iface": iface,
        "ssid": row["ssid"],
        "wan": wan_status,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Aggregations for dashboard cards
# ---------------------------------------------------------------------------


def overview_summary() -> Dict[str, Any]:
    init_db()
    with _connect() as conn:
        saved = conn.execute("SELECT COUNT(*) AS n FROM saved_networks").fetchone()["n"]
        strongest = conn.execute(
            "SELECT no.ssid, no.latest_signal, no.last_seen "
            "FROM network_observations no JOIN saved_networks sn "
            "ON sn.id = no.saved_network_id "
            "ORDER BY COALESCE(no.latest_signal, -999) DESC LIMIT 1"
        ).fetchone()
    snapshot = state.load_state()
    return {
        "saved_count": saved,
        "last_scan_at": snapshot.get("last_scan_at"),
        "last_scan_iface": snapshot.get("last_scan_iface"),
        "strongest_saved": _row(strongest) if strongest else None,
    }


def export_json(ssid: Optional[str] = None) -> str:
    """Return a JSON snapshot of saved networks (with PSKs)."""
    init_db()
    with _connect() as conn:
        if ssid:
            rows = conn.execute(
                "SELECT * FROM saved_networks WHERE ssid = ?", (ssid,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM saved_networks").fetchall()
    return json.dumps([_row(r) for r in rows], indent=2, default=str)
