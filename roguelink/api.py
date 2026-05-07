"""FastAPI application powering the RogueLink dashboard and JSON API.

The same FastAPI app is run by ``roguelinkd`` (the systemd-managed daemon)
and is talked to by both the dashboard browser and the CLI. CLI access uses
the loopback bypass (no auth required for 127.0.0.1) so the operator does
not need to log in to issue local commands; remote dashboard access via the
management interface requires HTTP Basic auth.
"""

import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __subtitle__, __title__, __version__
from . import auth, config as roguelink_config, paths
from .services import (
    adapter_control,
    adapter_manager,
    ap_manager,
    driver_manager,
    fan_manager,
    firewall_manager,
    health_manager,
    lan_manager,
    logs as logs_service,
    management_manager,
    metrics,
    speedtest_manager,
    system_manager,
    wan_manager,
    wifi_scan_manager,
)


WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title=f"{__title__} — {__subtitle__}", version=__version__)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

basic_auth = HTTPBasic(auto_error=False)


def _is_local_request(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")


def require_auth(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(basic_auth),
) -> str:
    """Allow loopback requests through; require Basic auth for remote ones."""
    if _is_local_request(request):
        return "local"
    if not auth.is_configured():
        # Fail closed if no auth configured.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="dashboard auth not initialized",
        )
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": 'Basic realm="RogueLink"'},
        )
    if not auth.verify(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="RogueLink"'},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@app.get("/api/version")
def api_version():
    return {
        "name": __title__,
        "subtitle": __subtitle__,
        "version": __version__,
    }


@app.get("/api/overview")
def api_overview(_: str = Depends(require_auth)):
    return metrics.overview()


@app.get("/api/system")
def api_system(_: str = Depends(require_auth)):
    return system_manager.overview()


@app.get("/api/adapters")
def api_adapters(_: str = Depends(require_auth)):
    return {
        "adapters": adapter_manager.list_adapters(),
        "roles": adapter_manager.detect_roles(),
        "warnings": adapter_manager.warnings(),
        "drivers": driver_manager.status(),
    }


@app.post("/api/adapters/role")
def api_adapter_assign_role(
    role: str = Form(...),
    iface: str = Form(""),
    _: str = Depends(require_auth),
):
    ok = adapter_manager.assign_role(role, iface)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid role/interface")
    return {"ok": True}


@app.get("/api/management")
def api_management(_: str = Depends(require_auth)):
    return management_manager.status()


@app.post("/api/management/configure")
def api_management_configure(
    ssid: str = Form(...),
    psk: str = Form(""),
    country: str = Form(""),
    _: str = Depends(require_auth),
):
    return management_manager.configure(ssid, psk, country or None)


@app.post("/api/management/connect")
def api_management_connect(_: str = Depends(require_auth)):
    return management_manager.connect()


@app.get("/api/wan")
def api_wan(_: str = Depends(require_auth)):
    return wan_manager.status()


@app.post("/api/wan/scan")
def api_wan_scan(iface: str = Form(...), _: str = Depends(require_auth)):
    return {"networks": wan_manager.scan(iface)}


@app.post("/api/wan/connect")
def api_wan_connect(
    iface: str = Form(...),
    ssid: str = Form(...),
    psk: str = Form(""),
    country: str = Form(""),
    _: str = Depends(require_auth),
):
    cfg = roguelink_config.load()
    cc = country or cfg.get("general", {}).get("country_code", "US")
    result = wan_manager.connect(iface, ssid, psk, cc)
    _reapply_firewall()
    return result


@app.post("/api/wan/disconnect")
def api_wan_disconnect(_: str = Depends(require_auth)):
    result = wan_manager.disconnect()
    _reapply_firewall()
    return result


@app.get("/api/ap")
def api_ap(_: str = Depends(require_auth)):
    data = ap_manager.status()
    data["clients"] = ap_manager.clients()
    return data


@app.post("/api/ap/start")
def api_ap_start(
    iface: str = Form(...),
    ssid: str = Form(...),
    psk: str = Form(...),
    channel: int = Form(0),
    country: str = Form(""),
    _: str = Depends(require_auth),
):
    ch = channel or None
    cc = country or None
    result = ap_manager.start(iface, ssid, psk, ch, cc)
    _reapply_firewall()
    return result


@app.post("/api/ap/stop")
def api_ap_stop(_: str = Depends(require_auth)):
    result = ap_manager.stop()
    _reapply_firewall()
    return result


@app.get("/api/lan")
def api_lan(_: str = Depends(require_auth)):
    data = lan_manager.status()
    data["clients"] = lan_manager.clients()
    return data


@app.post("/api/lan/start")
def api_lan_start(iface: str = Form(""), _: str = Depends(require_auth)):
    result = lan_manager.start(iface or None)
    _reapply_firewall()
    return result


@app.post("/api/lan/stop")
def api_lan_stop(_: str = Depends(require_auth)):
    result = lan_manager.stop()
    _reapply_firewall()
    return result


@app.get("/api/clients")
def api_clients(_: str = Depends(require_auth)):
    return {
        "ap": ap_manager.clients(),
        "lan": lan_manager.clients(),
    }


@app.get("/api/firewall")
def api_firewall(_: str = Depends(require_auth)):
    return firewall_manager.status()


@app.post("/api/firewall/reapply")
def api_firewall_reapply(_: str = Depends(require_auth)):
    return _reapply_firewall()


@app.get("/api/logs")
def api_logs(_: str = Depends(require_auth)):
    return {"logs": logs_service.list_logs()}


@app.get("/api/logs/{name}")
def api_log_tail(name: str, lines: int = 200, _: str = Depends(require_auth)):
    return {"name": name, "lines": logs_service.tail(name, lines)}


# ---------------------------------------------------------------------------
# Networks (scan + saved)
# ---------------------------------------------------------------------------


@app.get("/api/networks/nearby")
def api_networks_nearby(_: str = Depends(require_auth)):
    return {"observations": wifi_scan_manager.history(limit=200)}


@app.get("/api/networks/scan")
def api_networks_scan(iface: str = "", _: str = Depends(require_auth)):
    target = iface or _default_scan_iface()
    if not target:
        raise HTTPException(status_code=400, detail="iface required (no WAN role assigned)")
    return {"iface": target, "networks": wifi_scan_manager.scan(target)}


@app.post("/api/networks/scan")
def api_networks_scan_post(iface: str = Form(""), _: str = Depends(require_auth)):
    target = iface or _default_scan_iface()
    if not target:
        raise HTTPException(status_code=400, detail="iface required")
    return {"iface": target, "networks": wifi_scan_manager.scan(target)}


@app.get("/api/networks/saved")
def api_networks_saved(_: str = Depends(require_auth)):
    return {"saved": wifi_scan_manager.list_saved()}


@app.post("/api/networks/saved")
def api_networks_saved_create(
    ssid: str = Form(...),
    psk: str = Form(""),
    note: str = Form(""),
    preferred_iface: str = Form(""),
    auto_connect: bool = Form(False),
    _: str = Depends(require_auth),
):
    res = wifi_scan_manager.save_network(ssid, psk, note, preferred_iface, auto_connect)
    return res


@app.get("/api/networks/saved/{network_id}")
def api_networks_saved_get(network_id: int, _: str = Depends(require_auth)):
    saved = wifi_scan_manager.get_saved(network_id)
    if not saved:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "saved": saved,
        "observations": wifi_scan_manager.observations_for(network_id),
        "attempts": wifi_scan_manager.connection_attempts(network_id=network_id),
    }


@app.patch("/api/networks/saved/{network_id}")
def api_networks_saved_update(
    network_id: int,
    ssid: Optional[str] = Form(None),
    psk: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    preferred_iface: Optional[str] = Form(None),
    auto_connect: Optional[bool] = Form(None),
    enabled: Optional[bool] = Form(None),
    _: str = Depends(require_auth),
):
    return wifi_scan_manager.update_saved(
        network_id,
        ssid=ssid,
        psk=psk,
        note=note,
        preferred_iface=preferred_iface,
        auto_connect=auto_connect,
        enabled=enabled,
    )


@app.delete("/api/networks/saved/{network_id}")
def api_networks_saved_delete(network_id: int, _: str = Depends(require_auth)):
    return wifi_scan_manager.delete_saved(network_id)


@app.post("/api/networks/saved/{network_id}/update")
def api_networks_saved_update_post(
    network_id: int,
    ssid: Optional[str] = Form(None),
    psk: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    preferred_iface: Optional[str] = Form(None),
    auto_connect: Optional[bool] = Form(None),
    enabled: Optional[bool] = Form(None),
    _: str = Depends(require_auth),
):
    return wifi_scan_manager.update_saved(
        network_id,
        ssid=ssid,
        psk=psk if psk else None,  # empty string = keep current
        note=note,
        preferred_iface=preferred_iface,
        auto_connect=auto_connect,
        enabled=enabled,
    )


@app.post("/api/networks/saved/{network_id}/delete")
def api_networks_saved_delete_post(network_id: int, _: str = Depends(require_auth)):
    return wifi_scan_manager.delete_saved(network_id)


@app.post("/api/networks/saved/{network_id}/connect")
def api_networks_saved_connect(
    network_id: int,
    iface: str = Form(""),
    _: str = Depends(require_auth),
):
    res = wifi_scan_manager.connect_saved(network_id, iface or None)
    _reapply_firewall()
    return res


@app.get("/api/networks/saved/{network_id}/observations")
def api_networks_saved_observations(network_id: int, _: str = Depends(require_auth)):
    return {"observations": wifi_scan_manager.observations_for(network_id)}


@app.get("/api/networks/history")
def api_networks_history(limit: int = 200, _: str = Depends(require_auth)):
    return {"history": wifi_scan_manager.history(limit=limit)}


@app.get("/api/networks/connection-attempts")
def api_networks_connection_attempts(limit: int = 100, _: str = Depends(require_auth)):
    return {"attempts": wifi_scan_manager.connection_attempts(limit=limit)}


# ---------------------------------------------------------------------------
# Speedtest
# ---------------------------------------------------------------------------


@app.post("/api/speedtest/run")
def api_speedtest_run(iface: str = Form(""), _: str = Depends(require_auth)):
    return speedtest_manager.run_test(iface or None)


@app.get("/api/speedtest/last")
def api_speedtest_last(_: str = Depends(require_auth)):
    last = speedtest_manager.last_result()
    return last or {"ok": False, "error": "no result on record"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
def api_health(_: str = Depends(require_auth)):
    last = health_manager.last()
    return last or {"ok": False, "status": "unknown", "error": "no health check on record"}


@app.post("/api/health/check")
def api_health_check(_: str = Depends(require_auth)):
    return health_manager.check()


# ---------------------------------------------------------------------------
# Adapter control
# ---------------------------------------------------------------------------


@app.get("/api/adapters/control/status")
def api_adapter_control_status(_: str = Depends(require_auth)):
    return {"adapters": adapter_control.status_all()}


@app.post("/api/adapters/{iface}/txpower")
def api_adapter_txpower(iface: str, dbm: float = Form(...), _: str = Depends(require_auth)):
    return adapter_control.set_txpower(iface, dbm)


@app.post("/api/adapters/{iface}/txpower-auto")
def api_adapter_txpower_auto(iface: str, _: str = Depends(require_auth)):
    return adapter_control.set_txpower_auto(iface)


@app.post("/api/adapters/{iface}/powersave")
def api_adapter_powersave(iface: str, on: bool = Form(...), _: str = Depends(require_auth)):
    return adapter_control.set_powersave(iface, on)


@app.post("/api/adapters/{iface}/reset")
def api_adapter_reset(iface: str, _: str = Depends(require_auth)):
    return adapter_control.soft_reset(iface)


@app.post("/api/adapters/{iface}/reset-usb")
def api_adapter_reset_usb(iface: str, _: str = Depends(require_auth)):
    return adapter_control.usb_reset(iface)


# ---------------------------------------------------------------------------
# Fan control + auth change
# ---------------------------------------------------------------------------


@app.get("/api/system/fan")
def api_fan_status(_: str = Depends(require_auth)):
    return fan_manager.status()


@app.post("/api/system/fan/profile")
def api_fan_profile(profile: str = Form(...), _: str = Depends(require_auth)):
    return fan_manager.apply_profile(profile)


@app.post("/api/system/fan/custom")
def api_fan_custom(
    t0: int = Form(...),
    s0: int = Form(...),
    t1: int = Form(...),
    s1: int = Form(...),
    t2: int = Form(...),
    s2: int = Form(...),
    t3: int = Form(...),
    s3: int = Form(...),
    _: str = Depends(require_auth),
):
    points = [
        {"temp": t0, "speed": s0},
        {"temp": t1, "speed": s1},
        {"temp": t2, "speed": s2},
        {"temp": t3, "speed": s3},
    ]
    return fan_manager.apply_profile("custom", points)


@app.post("/api/auth/change-password")
def api_auth_change_password(
    current: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    _: str = Depends(require_auth),
):
    if new_password != confirm:
        return {"ok": False, "error": "new password and confirmation do not match"}
    data = auth.load()
    username = data.get("username", auth.DEFAULT_USERNAME)
    ok, message = auth.change_password(username, current, new_password)
    return {"ok": ok, "message": message}


def _default_scan_iface() -> str:
    from .services import adapter_manager  # local import for clarity

    roles = adapter_manager.detect_roles()
    return roles.get("wan") or roles.get("management") or ""


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------


def _reapply_firewall() -> Dict[str, Any]:
    cfg = roguelink_config.load()
    api_port = cfg.get("general", {}).get("api_port", 8080)
    wan = wan_manager.status()
    ap = ap_manager.status()
    lan = lan_manager.status()
    mgmt = management_manager.status()
    return firewall_manager.reapply_from_state(
        wan_iface=wan.get("iface") if wan.get("connected") else None,
        ap_iface=ap.get("iface") if ap.get("running") else None,
        lan_iface=lan.get("iface") if lan.get("running") else None,
        mgmt_iface=mgmt.get("iface"),
        api_port=api_port,
    )


def _page_context(request: Request) -> Dict[str, Any]:
    cfg = roguelink_config.load()
    return {
        "title": __title__,
        "subtitle": __subtitle__,
        "version": __version__,
        "cfg": cfg,
    }


def _render(request: Request, template: str, context: Dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context)


@app.get("/", response_class=HTMLResponse)
def page_overview(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "overview"
    ctx["data"] = metrics.overview()
    return _render(request, "overview.html", ctx)


@app.get("/adapters", response_class=HTMLResponse)
def page_adapters(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "adapters"
    ctx["adapters"] = adapter_manager.list_adapters()
    ctx["roles"] = adapter_manager.detect_roles()
    ctx["warnings"] = adapter_manager.warnings()
    ctx["drivers"] = driver_manager.status()
    ctx["controls"] = adapter_control.status_all()
    return _render(request, "adapters.html", ctx)


@app.get("/networks", response_class=HTMLResponse)
def page_networks(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "networks"
    ctx["adapters"] = adapter_manager.list_adapters()
    ctx["roles"] = adapter_manager.detect_roles()
    ctx["recent"] = wifi_scan_manager.history(limit=80)
    saved = wifi_scan_manager.list_saved()
    saved_full = []
    for entry in saved:
        observations = wifi_scan_manager.observations_for(entry["id"])
        attempts = wifi_scan_manager.connection_attempts(network_id=entry["id"], limit=10)
        saved_full.append(
            {
                "saved": entry,
                "observations": observations,
                "attempts": attempts,
                "best_signal": max(
                    (o.get("strongest_signal_seen") or -999) for o in observations
                ) if observations else None,
                "latest_signal": observations[0].get("latest_signal") if observations else None,
                "last_seen": observations[0].get("last_seen") if observations else None,
            }
        )
    ctx["saved"] = saved_full
    ctx["scan_iface_default"] = _default_scan_iface()
    return _render(request, "networks.html", ctx)


@app.get("/management", response_class=HTMLResponse)
def page_management(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "management"
    ctx["mgmt"] = management_manager.status()
    return _render(request, "management.html", ctx)


@app.get("/wan", response_class=HTMLResponse)
def page_wan(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "wan"
    ctx["wan"] = wan_manager.status()
    ctx["roles"] = adapter_manager.detect_roles()
    ctx["adapters"] = adapter_manager.list_adapters()
    ctx["saved"] = wifi_scan_manager.list_saved()
    ctx["health"] = health_manager.last()
    return _render(request, "wan.html", ctx)


@app.get("/ap", response_class=HTMLResponse)
def page_ap(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "ap"
    ctx["ap"] = ap_manager.status()
    ctx["ap"]["clients"] = ap_manager.clients()
    ctx["adapters"] = adapter_manager.list_adapters()
    ctx["roles"] = adapter_manager.detect_roles()
    return _render(request, "ap.html", ctx)


@app.get("/lan", response_class=HTMLResponse)
def page_lan(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "lan"
    lan = lan_manager.status()
    lan["clients"] = lan_manager.clients()
    ctx["lan"] = lan
    return _render(request, "lan.html", ctx)


@app.get("/system", response_class=HTMLResponse)
def page_system(request: Request, _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "system"
    ctx["system"] = system_manager.overview()
    ctx["firewall"] = firewall_manager.status()
    ctx["drivers"] = driver_manager.status()
    ctx["fan"] = fan_manager.status()
    ctx["speedtest"] = speedtest_manager.last_result()
    ctx["health"] = health_manager.last()
    auth_data = auth.load()
    ctx["auth_username"] = auth_data.get("username", auth.DEFAULT_USERNAME)
    return _render(request, "system.html", ctx)


@app.get("/logs", response_class=HTMLResponse)
def page_logs(request: Request, name: str = "daemon", _: str = Depends(require_auth)):
    ctx = _page_context(request)
    ctx["page"] = "logs"
    ctx["logs"] = logs_service.list_logs()
    ctx["selected"] = name
    ctx["lines"] = logs_service.tail(name, 400)
    return _render(request, "logs.html", ctx)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
def on_startup() -> None:
    paths.ensure_dirs()
    auth.ensure_default_password()
    # Initial firewall apply so LAN/AP/WAN rules match current state.
    try:
        _reapply_firewall()
    except Exception:  # noqa: BLE001 — startup must never crash on firewall failure
        pass


def run_server(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Start uvicorn programmatically (used by ``roguelinkd``)."""
    import uvicorn

    cfg = roguelink_config.load()
    h = host or cfg.get("general", {}).get("host", "0.0.0.0")
    p = port or cfg.get("general", {}).get("api_port", 8080)
    uvicorn.run(app, host=h, port=int(p), log_level="info")
