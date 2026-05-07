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
    adapter_manager,
    ap_manager,
    driver_manager,
    firewall_manager,
    lan_manager,
    logs as logs_service,
    management_manager,
    metrics,
    system_manager,
    wan_manager,
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
    return _render(request, "adapters.html", ctx)


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
    auth.ensure_initial_password()
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
