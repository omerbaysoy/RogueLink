"""roguelink CLI — Typer + Rich.

The CLI talks to the local daemon's JSON API over loopback. When the daemon
is not running (e.g. during install before the systemd unit starts), the
CLI falls back to invoking the same service-layer functions directly so a
fresh install can still bootstrap the device.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __subtitle__, __title__, __version__
from . import auth, config as roguelink_config, state
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


app = typer.Typer(add_completion=False, no_args_is_help=False)
mgmt_app = typer.Typer(help="Management interface controls")
wan_app = typer.Typer(help="WAN uplink controls")
ap_app = typer.Typer(help="AP mode controls")
lan_app = typer.Typer(help="Wired LAN (eth0) controls")
sys_app = typer.Typer(help="System helpers (Pi 5 setup, status)")
networks_app = typer.Typer(help="Wi-Fi scan and saved network management")
adapter_app = typer.Typer(help="Adapter power/reset controls (singular)")
speedtest_app = typer.Typer(help="Internet speed tests", invoke_without_command=True)
health_app = typer.Typer(help="Connection health checks", invoke_without_command=True)
fan_app = typer.Typer(help="Pi 5 fan profile control")

app.add_typer(mgmt_app, name="mgmt")
app.add_typer(wan_app, name="wan")
app.add_typer(ap_app, name="ap")
app.add_typer(lan_app, name="lan")
app.add_typer(sys_app, name="system")
app.add_typer(networks_app, name="networks")
app.add_typer(adapter_app, name="adapter")
app.add_typer(speedtest_app, name="speedtest")
app.add_typer(health_app, name="health")
app.add_typer(fan_app, name="fan")

console = Console()

ASCII_BANNER = r"""
██████╗  ██████╗  ██████╗ ██╗   ██╗███████╗██╗     ██╗███╗   ██╗██╗  ██╗
██╔══██╗██╔═══██╗██╔════╝ ██║   ██║██╔════╝██║     ██║████╗  ██║██║ ██╔╝
██████╔╝██║   ██║██║  ███╗██║   ██║█████╗  ██║     ██║██╔██╗ ██║█████╔╝
██╔══██╗██║   ██║██║   ██║██║   ██║██╔══╝  ██║     ██║██║╚██╗██║██╔═██╗
██║  ██║╚██████╔╝╚██████╔╝╚██████╔╝███████╗███████╗██║██║ ╚████║██║  ██╗
╚═╝  ╚═╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝╚══════╝╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝
"""


def _print_banner() -> None:
    console.print(Text(ASCII_BANNER, style="bold green"))
    console.print(f"[bold green]{__title__}[/bold green]  [dim]{__subtitle__}[/dim]  v{__version__}")
    overview = metrics.overview()
    sys = overview["system"]
    mgmt = overview["management"]
    wan = overview["wan"]
    ap = overview["ap"]
    lan = overview["lan"]
    fw = overview["firewall"]

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Management IP", str(mgmt.get("ip") or "—"))
    table.add_row("Dashboard URL", overview["dashboard_url"])
    table.add_row(
        "WAN",
        _fmt_role(
            wan.get("connected"),
            f"{wan.get('iface') or '—'} · {wan.get('ssid') or '—'} · {wan.get('ip') or '—'}",
            "disconnected",
        ),
    )
    table.add_row(
        "AP",
        _fmt_role(
            ap.get("running"),
            f"{ap.get('iface') or '—'} · {ap.get('ssid') or '—'} · {ap.get('subnet') or '—'}",
            "stopped",
        ),
    )
    table.add_row(
        "LAN (eth0)",
        _fmt_role(
            lan.get("running"),
            f"{lan.get('iface') or '—'} · {lan.get('subnet') or '—'}",
            "stopped",
        ),
    )
    table.add_row(
        "Firewall",
        "[green]active[/green]" if fw.get("active") else "[yellow]inactive[/yellow]",
    )
    table.add_row(
        "Daemon",
        "[green]active[/green]" if sys["daemon"]["active"] else f"[yellow]{sys['daemon']['raw']}[/yellow]",
    )
    table.add_row(
        "Temperature",
        f"{sys['temperature_c']} °C" if sys.get("temperature_c") is not None else "—",
    )
    console.print(table)

    warnings = []
    warnings.extend(overview.get("adapter_warnings") or [])
    if not sys["daemon"]["active"]:
        warnings.append("roguelinkd is not active; run: sudo systemctl start roguelinkd")
    if not fw.get("active"):
        warnings.append("Firewall ruleset is not loaded; run: sudo roguelink firewall reapply")
    if not wan.get("connected"):
        warnings.append("No WAN uplink. AP/LAN clients will not have internet.")
    if ap.get("running") and not wan.get("connected"):
        warnings.append("AP is running but WAN is down — clients cannot reach the internet.")
    if warnings:
        console.print("\n[bold yellow]Warnings[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]![/yellow] {w}")


def _fmt_role(active, text_active: str, text_inactive: str) -> str:
    if active:
        return f"[green]{text_active}[/green]"
    return f"[dim]{text_inactive}[/dim]"


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _print_banner()


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show RogueLink overall status."""
    _print_banner()


@app.command()
def dashboard() -> None:
    """Print the dashboard URL and login info."""
    cfg = roguelink_config.load()
    port = cfg.get("general", {}).get("api_port", 8080)
    url = management_manager.dashboard_url(port)
    console.print(f"Dashboard URL: [bold]{url}[/bold]")
    console.print(
        "Default login: [cyan]admin[/cyan] / [cyan]roguelink[/cyan]  "
        "(change with [bold]roguelink set-password[/bold])"
    )


@app.command()
def adapters() -> None:
    """List wireless adapters and detected roles."""
    roles = adapter_manager.detect_roles()
    table = Table(title="Wireless adapters", show_lines=False)
    for col in ("iface", "mac", "driver", "chipset", "usb_id", "phy", "operstate", "role"):
        table.add_column(col)
    for ad in adapter_manager.list_adapters():
        role = next((r for r, i in roles.items() if i == ad["iface"]), "—")
        table.add_row(
            ad["iface"], ad["mac"], ad["driver"], ad["chipset"],
            ad["usb_id"] or "—", ad["phy"] or "—", ad["operstate"], role,
        )
    console.print(table)
    warns = adapter_manager.warnings()
    if warns:
        console.print("\n[yellow]Warnings:[/yellow]")
        for w in warns:
            console.print(f"  [yellow]![/yellow] {w}")


@app.command()
def clients() -> None:
    """Show connected AP and LAN clients."""
    ap_clients = ap_manager.clients()
    lan_clients = lan_manager.clients()
    for label, items in (("AP clients", ap_clients), ("LAN clients", lan_clients)):
        table = Table(title=label)
        for col in ("hostname", "ip", "mac", "expires"):
            table.add_column(col)
        for c in items:
            table.add_row(c.get("hostname") or "—", c["ip"], c["mac"], c["expires"])
        if not items:
            table.add_row("—", "—", "—", "no leases")
        console.print(table)


@app.command()
def logs(name: str = "daemon", lines: int = 80) -> None:
    """Tail a RogueLink log file."""
    output = logs_service.tail(name, lines)
    if not output:
        console.print(f"[dim]Log '{name}' is empty or missing.[/dim]")
        return
    for line in output:
        console.print(line)


# ---------------------------------------------------------------------------
# mgmt
# ---------------------------------------------------------------------------


@mgmt_app.command("status")
def mgmt_status() -> None:
    info = management_manager.status()
    console.print(json.dumps(info, indent=2))


@mgmt_app.command("connect")
def mgmt_connect(
    ssid: str = typer.Option(..., "--ssid", help="Management Wi-Fi SSID"),
    psk: str = typer.Option("", "--psk", help="Management Wi-Fi password"),
    country: str = typer.Option("", "--country", help="Country code (e.g. US)"),
) -> None:
    management_manager.configure(ssid, psk, country or None)
    res = management_manager.connect()
    console.print(json.dumps(res, indent=2))


# ---------------------------------------------------------------------------
# wan
# ---------------------------------------------------------------------------


@wan_app.command("status")
def wan_status() -> None:
    console.print(json.dumps(wan_manager.status(), indent=2))


@wan_app.command("scan")
def wan_scan(iface: str = typer.Option(..., "--iface")) -> None:
    networks = wan_manager.scan(iface)
    table = Table(title=f"Scan on {iface}")
    for col in ("ssid", "bssid", "channel", "signal", "encryption"):
        table.add_column(col)
    for n in networks:
        table.add_row(
            n.get("ssid") or "",
            n.get("bssid") or "",
            str(n.get("channel") or "—"),
            str(n.get("signal") or "—"),
            n.get("encryption") or "—",
        )
    console.print(table)


@wan_app.command("connect")
def wan_connect(
    iface: str = typer.Option(..., "--iface"),
    ssid: str = typer.Option(..., "--ssid"),
    psk: str = typer.Option("", "--psk"),
    country: str = typer.Option("", "--country"),
) -> None:
    cfg = roguelink_config.load()
    cc = country or cfg.get("general", {}).get("country_code", "US")
    res = wan_manager.connect(iface, ssid, psk, cc)
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


@wan_app.command("disconnect")
def wan_disconnect() -> None:
    res = wan_manager.disconnect()
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


# ---------------------------------------------------------------------------
# ap
# ---------------------------------------------------------------------------


@ap_app.command("start")
def ap_start(
    iface: str = typer.Option(..., "--iface"),
    ssid: str = typer.Option(..., "--ssid"),
    psk: str = typer.Option(..., "--psk"),
    channel: int = typer.Option(0, "--channel"),
    country: str = typer.Option("", "--country"),
) -> None:
    res = ap_manager.start(iface, ssid, psk, channel or None, country or None)
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


@ap_app.command("stop")
def ap_stop() -> None:
    res = ap_manager.stop()
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


@ap_app.command("status")
def ap_status() -> None:
    info = ap_manager.status()
    info["clients"] = ap_manager.clients()
    console.print(json.dumps(info, indent=2))


# ---------------------------------------------------------------------------
# lan
# ---------------------------------------------------------------------------


@lan_app.command("status")
def lan_status() -> None:
    info = lan_manager.status()
    info["clients"] = lan_manager.clients()
    console.print(json.dumps(info, indent=2))


@lan_app.command("start")
def lan_start(iface: str = typer.Option("eth0", "--iface")) -> None:
    res = lan_manager.start(iface or None)
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


@lan_app.command("stop")
def lan_stop() -> None:
    res = lan_manager.stop()
    _reapply_firewall()
    console.print(json.dumps(res, indent=2))


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


@sys_app.command("info")
def system_info() -> None:
    console.print(json.dumps(system_manager.overview(), indent=2))


@sys_app.command("apply-pi5")
def system_apply_pi5() -> None:
    """Apply Pi 5 fan, PCIe Gen 3, and light overclock blocks to /boot config."""
    results = system_manager.apply_pi5_boot_config()
    zram = system_manager.apply_zram()
    for r in results + [zram]:
        console.print(json.dumps(r, indent=2))
    if system_manager.reboot_required():
        console.print("[bold yellow]Reboot required to activate boot config changes.[/bold yellow]")


@sys_app.command("install-driver")
def system_install_driver(chipset: str = typer.Argument(..., help="rtl8812au|rtl88x2bu|rtl8188eus|mt7612u")) -> None:
    res = driver_manager.install_for(chipset)
    console.print(json.dumps(res, indent=2))


@sys_app.command("verify-driver")
def system_verify_driver(chipset: str = typer.Argument(..., help="rtl8812au|rtl88x2bu|rtl8188eus|mt7612u")) -> None:
    """Verify driver installation status for a specific chipset."""
    res = driver_manager.verify_driver(chipset)
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        raise typer.Exit(2)
    ok = res.get("ok", False)
    color = "green" if ok else "yellow"
    console.print(f"[{color}]{res['label']}[/{color}] — {res.get('capabilities', '')}")
    console.print(f"  Kind: {res.get('kind')}")
    for m in res.get("modules", []):
        avail = "[green]✓[/green]" if m["available"] else "[red]✗[/red]"
        loaded = "[green]loaded[/green]" if m["loaded"] else "[dim]not loaded[/dim]"
        console.print(f"  {m['module']}: {avail} {loaded}")
    if res.get("loaded_module"):
        console.print(f"  Active module: [bold]{res['loaded_module']}[/bold]")
    if res.get("bound_ifaces"):
        console.print(f"  Bound interfaces: {', '.join(res['bound_ifaces'])}")
    if res.get("using_fallback"):
        console.print(f"  [yellow]⚠ {res.get('fallback_warning', 'Using fallback driver')}[/yellow]")
    if res.get("blacklist"):
        bl = res["blacklist"]
        if bl.get("exists"):
            if bl.get("missing"):
                console.print(f"  [yellow]Blacklist missing: {bl['missing']}[/yellow]")
            else:
                console.print(f"  Blacklist: [green]OK[/green]")
        else:
            console.print(f"  [yellow]Blacklist conf not found[/yellow]")
    if res.get("firmware_files"):
        for fw, ok_fw in res["firmware_files"].items():
            st = "[green]✓[/green]" if ok_fw else "[red]✗ missing[/red]"
            console.print(f"  Firmware: {fw} {st}")
    if res.get("dkms_status"):
        console.print(f"  DKMS: {res['dkms_status']}")
    if res.get("makefile_arm"):
        ma = res["makefile_arm"]
        if ma.get("exists"):
            console.print(f"  Makefile: i386=n:{ma['i386_disabled']} arm64=y:{ma['arm64_enabled']} arm=y:{ma['arm_enabled']}")
    for fix in res.get("recommended_fixes", []):
        console.print(f"  [yellow]→ {fix}[/yellow]")


@sys_app.command("driver-audit")
def system_driver_audit(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Audit all supported drivers."""
    audit = driver_manager.driver_audit()
    if json_output:
        console.print(json.dumps(audit, indent=2, default=str))
        return
    for entry in audit:
        ok = entry.get("ok", False)
        color = "green" if ok else "yellow"
        status_icon = "✓" if ok else "⚠"
        loaded = entry.get("loaded_module") or "none"
        console.print(
            f"[{color}]{status_icon} {entry['label']}[/{color}] "
            f"— loaded: {loaded}, bound: {', '.join(entry.get('bound_ifaces', [])) or 'none'}"
        )
        for fix in entry.get("recommended_fixes", []):
            console.print(f"    [yellow]→ {fix}[/yellow]")


@sys_app.command("driver-diag")
def system_driver_diag(
    iface: str = typer.Option("", "--iface", help="Diagnose a specific interface"),
) -> None:
    """Driver diagnostics for one or all interfaces."""
    res = driver_manager.driver_diag(iface or None)
    console.print(json.dumps(res, indent=2, default=str))


@app.command("firewall")
def firewall_cmd(action: str = typer.Argument("status", help="status|reapply|flush")) -> None:
    """Inspect or reapply nftables ruleset."""
    if action == "status":
        console.print(json.dumps(firewall_manager.status(), indent=2))
    elif action == "reapply":
        console.print(json.dumps(_reapply_firewall(), indent=2))
    elif action == "flush":
        console.print(json.dumps(firewall_manager.flush(), indent=2))
    else:
        console.print(f"[red]Unknown firewall action: {action}[/red]")
        raise typer.Exit(2)


@app.command("set-password")
def set_password(
    username: str = typer.Option("admin", "--username"),
    password: str = typer.Option(..., "--password", prompt=True, hide_input=True, confirmation_prompt=True),
) -> None:
    auth.set_password(username, password)
    console.print("[green]Password updated.[/green]")


@app.command()
def daemon() -> None:
    """Run roguelinkd in the foreground (used by systemd ExecStart)."""
    from . import daemon as daemon_module

    raise typer.Exit(daemon_module.main())


# ---------------------------------------------------------------------------
# networks
# ---------------------------------------------------------------------------


def _scan_table(networks):
    table = Table(title="Wi-Fi scan")
    for col in ("SSID", "BSSID", "Signal dBm", "Quality", "Channel", "Band", "Security", "Interface"):
        table.add_column(col)
    for n in networks:
        signal = n.get("signal_dbm")
        table.add_row(
            n.get("ssid") or "",
            n.get("bssid") or "",
            f"{signal:.0f}" if signal is not None else "—",
            n.get("quality") or "—",
            str(n.get("channel") or "—"),
            n.get("band") or "—",
            n.get("security") or "—",
            n.get("iface") or "—",
        )
    return table


@networks_app.command("scan")
def networks_scan(
    iface: str = typer.Option("", "--iface"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Scan nearby Wi-Fi networks and persist observations."""
    target = iface
    if not target:
        roles = state.load_adapter_map() or {}
        target = roles.get("wan") or roles.get("management") or ""
    if not target:
        console.print("[red]No interface to scan; pass --iface or assign WAN role.[/red]")
        raise typer.Exit(2)
    networks = wifi_scan_manager.scan(target)
    if json_output:
        console.print(json.dumps(networks, indent=2, default=str))
        return
    console.print(_scan_table(networks))


@networks_app.command("list")
def networks_list() -> None:
    """List most recent observations across all SSIDs."""
    history = wifi_scan_manager.history(limit=200)
    table = Table(title="Recent Wi-Fi observations")
    for col in ("SSID", "BSSID", "Signal", "Quality", "Channel", "Band", "Security", "Last seen"):
        table.add_column(col)
    for h in history:
        signal = h.get("latest_signal")
        table.add_row(
            h.get("ssid") or "",
            h.get("bssid") or "",
            f"{signal:.0f}" if signal is not None else "—",
            h.get("quality") or "—",
            str(h.get("channel") or "—"),
            h.get("band") or "—",
            h.get("security") or "—",
            str(h.get("last_seen") or "—"),
        )
    console.print(table)


@networks_app.command("saved")
def networks_saved() -> None:
    """List saved networks."""
    rows = wifi_scan_manager.list_saved()
    table = Table(title="Saved networks")
    for col in ("ID", "SSID", "Note", "Last seen", "Last connected", "Status", "Enabled"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r.get("id")),
            r.get("ssid") or "",
            r.get("note") or "",
            str(r.get("updated_at") or "—"),
            str(r.get("last_connected_at") or "—"),
            r.get("last_connection_status") or "—",
            "yes" if r.get("enabled") else "no",
        )
    console.print(table)


@networks_app.command("show")
def networks_show(network_id: int) -> None:
    """Show a saved network with observations and history."""
    saved = wifi_scan_manager.get_saved(network_id)
    if not saved:
        console.print(f"[red]Saved network {network_id} not found.[/red]")
        raise typer.Exit(2)
    console.print(json.dumps(saved, indent=2, default=str))
    console.print("\nObservations:")
    console.print(json.dumps(wifi_scan_manager.observations_for(network_id), indent=2, default=str))
    console.print("\nConnection attempts:")
    console.print(
        json.dumps(wifi_scan_manager.connection_attempts(network_id=network_id), indent=2, default=str)
    )


@networks_app.command("save")
def networks_save(
    ssid: str = typer.Option(..., "--ssid"),
    psk: str = typer.Option("", "--psk"),
    note: str = typer.Option("", "--note"),
    iface: str = typer.Option("", "--iface", help="Preferred interface for WAN connect"),
) -> None:
    """Save a Wi-Fi network with PSK and note."""
    res = wifi_scan_manager.save_network(ssid, psk, note, iface)
    console.print(json.dumps(res, indent=2))


@networks_app.command("update")
def networks_update(
    network_id: int,
    ssid: str = typer.Option("", "--ssid"),
    psk: str = typer.Option("", "--psk"),
    note: str = typer.Option("", "--note"),
    iface: str = typer.Option("", "--iface"),
) -> None:
    """Update a saved network."""
    kwargs: Dict[str, Any] = {}
    if ssid:
        kwargs["ssid"] = ssid
    if psk:
        kwargs["psk"] = psk
    if note:
        kwargs["note"] = note
    if iface:
        kwargs["preferred_iface"] = iface
    res = wifi_scan_manager.update_saved(network_id, **kwargs)
    console.print(json.dumps(res, indent=2))


@networks_app.command("delete")
def networks_delete(network_id: int) -> None:
    res = wifi_scan_manager.delete_saved(network_id)
    console.print(json.dumps(res, indent=2))


@networks_app.command("connect")
def networks_connect(network_id: int, iface: str = typer.Option("", "--iface")) -> None:
    """Connect to a saved network. Optional --iface overrides preferred_iface."""
    res = wifi_scan_manager.connect_saved(network_id, iface or None)
    _reapply_firewall()
    console.print(json.dumps(res, indent=2, default=str))


@networks_app.command("history")
def networks_history(limit: int = 100) -> None:
    """Show recent Wi-Fi observations."""
    rows = wifi_scan_manager.history(limit=limit)
    console.print(json.dumps(rows, indent=2, default=str))


@networks_app.command("observations")
def networks_observations(network_id: int) -> None:
    rows = wifi_scan_manager.observations_for(network_id)
    console.print(json.dumps(rows, indent=2, default=str))


# ---------------------------------------------------------------------------
# speedtest
# ---------------------------------------------------------------------------


@speedtest_app.callback(invoke_without_command=True)
def speedtest_default(
    ctx: typer.Context,
    iface: str = typer.Option("", "--iface"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a speed test (default action when no subcommand given)."""
    if ctx.invoked_subcommand is not None:
        return
    res = speedtest_manager.run_test(iface or None)
    if json_output:
        console.print(json.dumps(res, indent=2, default=str))
        return
    if res.get("ok"):
        console.print(
            f"[green]Speed test:[/green] "
            f"down=[bold]{res.get('download_mbps')}[/bold] Mbps, "
            f"up=[bold]{res.get('upload_mbps')}[/bold] Mbps, "
            f"ping=[bold]{res.get('ping_ms')}[/bold] ms "
            f"({res.get('server_name') or '—'})"
        )
    else:
        console.print(f"[red]Speed test failed:[/red] {res.get('error')}")


@speedtest_app.command("last")
def speedtest_last() -> None:
    """Show the last speed test result."""
    last = speedtest_manager.last_result()
    if not last:
        console.print("[dim]No speed test result on record.[/dim]")
        return
    console.print(json.dumps(last, indent=2, default=str))


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@health_app.callback(invoke_without_command=True)
def health_default(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a one-shot connection health check."""
    if ctx.invoked_subcommand is not None:
        return
    res = health_manager.check()
    if json_output:
        console.print(json.dumps(res, indent=2, default=str))
        return
    summary = res.get("summary", {})
    color = {
        "excellent": "green",
        "good": "green",
        "partial": "yellow",
        "weak": "yellow",
        "unstable": "yellow",
        "offline": "red",
    }.get(res.get("status", ""), "white")
    console.print(f"[{color}]Overall:[/{color}] {res.get('status')}")
    if res.get("reason"):
        console.print(f"  Reason: {res['reason']}")
    console.print(f"  Management Internet: {'OK' if res.get('management_internet') else 'No'}")
    console.print(f"  WAN: {res.get('wan_status', '—')}")
    console.print(f"  DNS: {'OK' if res.get('dns_ok') else 'Failing'}")
    console.print(
        f"  Gateway: {summary.get('gateway', '—')} · "
        f"RTT: {summary.get('rtt_ms', '—')} ms · "
        f"Loss: {summary.get('packet_loss_pct', '—')}%"
    )
    console.print(
        f"  Internet iface: {summary.get('wan_iface', '—')} · "
        f"Signal: {summary.get('signal_dbm', '—')} dBm"
    )


@health_app.command("watch")
def health_watch(interval: int = 10, count: int = 0) -> None:
    """Repeat health checks. count=0 means run forever."""
    seen = 0
    while count == 0 or seen < count:
        res = health_manager.check()
        summary = res.get("summary", {})
        console.print(
            f"[{res.get('status')}] rtt={summary.get('rtt_ms')} loss={summary.get('packet_loss_pct')}% "
            f"signal={summary.get('signal_dbm')}"
        )
        seen += 1
        if count and seen >= count:
            break
        import time as _t
        _t.sleep(interval)


# ---------------------------------------------------------------------------
# adapter (singular) — power/reset controls
# ---------------------------------------------------------------------------


@adapter_app.command("power")
def adapter_power(iface: str) -> None:
    """Show TX power, power-save, and capability status for an adapter."""
    console.print(json.dumps(adapter_control.status_for(iface), indent=2))


@adapter_app.command("txpower")
def adapter_txpower(iface: str, dbm: float = typer.Option(..., "--dbm")) -> None:
    console.print(json.dumps(adapter_control.set_txpower(iface, dbm), indent=2))


@adapter_app.command("txpower-auto")
def adapter_txpower_auto(iface: str) -> None:
    console.print(json.dumps(adapter_control.set_txpower_auto(iface), indent=2))


@adapter_app.command("powersave")
def adapter_powersave(iface: str, mode: str = typer.Argument(..., help="on|off")) -> None:
    if mode not in ("on", "off"):
        console.print("[red]mode must be 'on' or 'off'[/red]")
        raise typer.Exit(2)
    console.print(json.dumps(adapter_control.set_powersave(iface, mode == "on"), indent=2))


@adapter_app.command("reset")
def adapter_reset(iface: str) -> None:
    """Soft-reset the adapter (ip link down/up)."""
    console.print(json.dumps(adapter_control.soft_reset(iface), indent=2))


@adapter_app.command("reset-usb")
def adapter_reset_usb(iface: str) -> None:
    """Re-authorize the underlying USB device when safely detected."""
    console.print(json.dumps(adapter_control.usb_reset(iface), indent=2))


# ---------------------------------------------------------------------------
# fan
# ---------------------------------------------------------------------------


@fan_app.command("status")
def fan_status() -> None:
    console.print(json.dumps(fan_manager.status(), indent=2, default=str))


@fan_app.command("set")
def fan_set(
    profile: str = typer.Argument(..., help="quiet|balanced|performance|max|custom"),
    t0: int = typer.Option(0, "--t0"),
    s0: int = typer.Option(0, "--s0"),
    t1: int = typer.Option(0, "--t1"),
    s1: int = typer.Option(0, "--s1"),
    t2: int = typer.Option(0, "--t2"),
    s2: int = typer.Option(0, "--s2"),
    t3: int = typer.Option(0, "--t3"),
    s3: int = typer.Option(0, "--s3"),
) -> None:
    custom = None
    if profile == "custom":
        custom = [
            {"temp": t0, "speed": s0},
            {"temp": t1, "speed": s1},
            {"temp": t2, "speed": s2},
            {"temp": t3, "speed": s3},
        ]
    res = fan_manager.apply_profile(profile, custom)
    console.print(json.dumps(res, indent=2, default=str))


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


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
