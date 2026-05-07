#!/usr/bin/env bash
# Start RogueLink in a local virtualenv with non-root paths so it can be
# exercised on a developer workstation. Most network/system actions will be
# no-ops on non-Linux hosts; the dashboard and CLI surface still works.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${REPO_DIR}/.venv"

export ROGUELINK_ETC="${REPO_DIR}/.dev/etc"
export ROGUELINK_LIB="${REPO_DIR}/.dev/lib"
export ROGUELINK_LOG="${REPO_DIR}/.dev/log"
export ROGUELINK_RUN="${REPO_DIR}/.dev/run"
mkdir -p "${ROGUELINK_ETC}" "${ROGUELINK_LIB}" "${ROGUELINK_LOG}" "${ROGUELINK_RUN}"

if [[ ! -d "${VENV}" ]]; then
  python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install --upgrade pip
  "${VENV}/bin/pip" install fastapi "uvicorn[standard]" jinja2 typer rich httpx python-multipart tomli
fi

cd "${REPO_DIR}"
exec "${VENV}/bin/python" -m roguelink.daemon
