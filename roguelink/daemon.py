"""roguelinkd — long-running daemon entry point.

Hosts the FastAPI app via uvicorn. Started by systemd and reachable from
the management interface.
"""

import sys

from . import api, auth, paths
from . import config as roguelink_config
from .services import management_manager
from .utils import append_log


def _resolve_bind_host() -> str:
    """Determine the bind host for uvicorn.

    When host is ``"auto"`` (default), resolve the management interface IP.
    Falls back to ``127.0.0.1`` when the management IP cannot be detected
    (safe — never falls back to ``0.0.0.0``).
    """
    cfg = roguelink_config.load()
    host = cfg.get("general", {}).get("host", "auto")
    if host and host != "auto":
        # Explicit override (e.g. "0.0.0.0" for debug, "127.0.0.1", etc.)
        return host
    mgmt_ip = management_manager.get_management_ip()
    if mgmt_ip:
        append_log(paths.DAEMON_LOG, f"bind auto-resolved to management IP {mgmt_ip}")
        return mgmt_ip
    # Management IP not available — safe fallback
    append_log(
        paths.DAEMON_LOG,
        "WARNING: management IP not detected, binding to 127.0.0.1 (safe fallback). "
        "Set host explicitly in /etc/roguelink/roguelink.toml if needed.",
    )
    return "127.0.0.1"


def main(argv=None) -> int:
    paths.ensure_dirs()
    created, username, password = auth.ensure_default_password()
    if created:
        msg = f"[roguelinkd] Created default dashboard login: {username} / {password}"
        print(msg, flush=True)
        append_log(paths.DAEMON_LOG, msg)

    bind_host = _resolve_bind_host()
    cfg = roguelink_config.load()
    bind_port = int(cfg.get("general", {}).get("api_port", 8080))
    append_log(paths.DAEMON_LOG, f"roguelinkd starting on {bind_host}:{bind_port}")

    try:
        api.run_server(host=bind_host, port=bind_port)
    except KeyboardInterrupt:
        append_log(paths.DAEMON_LOG, "roguelinkd interrupted")
        return 0
    except Exception as exc:  # noqa: BLE001 — log the crash before exiting.
        append_log(paths.DAEMON_LOG, f"roguelinkd crashed: {exc!r}")
        raise
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
