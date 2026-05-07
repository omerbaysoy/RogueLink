"""roguelinkd — long-running daemon entry point.

Hosts the FastAPI app via uvicorn. Started by systemd and reachable from
the management interface.
"""

import sys

from . import api, auth, paths
from .utils import append_log


def main(argv=None) -> int:
    paths.ensure_dirs()
    created, password = auth.ensure_initial_password()
    if created and password:
        # Print to stdout/log so the operator can read it from the journal once.
        msg = (
            f"[roguelinkd] Initial admin password generated. "
            f"username=admin password={password}"
        )
        print(msg, flush=True)
        append_log(paths.DAEMON_LOG, msg)

    append_log(paths.DAEMON_LOG, "roguelinkd starting")
    try:
        api.run_server()
    except KeyboardInterrupt:
        append_log(paths.DAEMON_LOG, "roguelinkd interrupted")
        return 0
    except Exception as exc:  # noqa: BLE001 — log the crash before exiting.
        append_log(paths.DAEMON_LOG, f"roguelinkd crashed: {exc!r}")
        raise
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
