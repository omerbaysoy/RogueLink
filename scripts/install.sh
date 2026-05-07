#!/usr/bin/env bash
# RogueLink installer for Raspberry Pi OS / Debian-based systems.
# Installs base packages, copies the project to /opt/roguelink, creates a
# Python venv, installs the systemd unit, enables the daemon, and prints the
# dashboard URL.

set -euo pipefail

INSTALL_PREFIX="/opt/roguelink"
ETC_DIR="/etc/roguelink"
LIB_DIR="/var/lib/roguelink"
LOG_DIR="/var/log/roguelink"
RUN_DIR="/run/roguelink"
SERVICE_NAME="roguelinkd.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
BIN_LINK="/usr/local/bin/roguelink"

if [[ "${EUID}" -ne 0 ]]; then
  echo "RogueLink install must be run as root (sudo)." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf "[roguelink-install] %s\n" "$*"; }

log "Repo: ${REPO_DIR}"
log "Creating directories"
mkdir -p "${INSTALL_PREFIX}" "${ETC_DIR}" "${LIB_DIR}" "${LOG_DIR}" "${RUN_DIR}"

BASE_PACKAGES=(
  python3 python3-venv python3-pip
  hostapd dnsmasq nftables
  iw wireless-tools wpasupplicant rfkill ethtool
  git curl
  build-essential dkms
)
HEADER_PACKAGES=(raspberrypi-kernel-headers "linux-headers-$(uname -r)")

log "Installing apt dependencies"
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${BASE_PACKAGES[@]}" || true
for pkg in "${HEADER_PACKAGES[@]}"; do
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkg}" 2>/dev/null || true
done

# hostapd and dnsmasq are managed by RogueLink, so disable the system unit.
systemctl disable --now hostapd 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true

log "Copying project to ${INSTALL_PREFIX}"
rsync -a --delete \
  --exclude '.git' --exclude 'external' --exclude '__pycache__' --exclude 'venv' \
  "${REPO_DIR}/" "${INSTALL_PREFIX}/"

log "Creating Python virtualenv"
python3 -m venv "${INSTALL_PREFIX}/venv"
"${INSTALL_PREFIX}/venv/bin/pip" install --upgrade pip
"${INSTALL_PREFIX}/venv/bin/pip" install \
  fastapi "uvicorn[standard]" jinja2 typer rich httpx python-multipart tomli

log "Installing example config"
if [[ ! -f "${ETC_DIR}/roguelink.toml" ]]; then
  install -m 0640 "${REPO_DIR}/config/roguelink.example.toml" "${ETC_DIR}/roguelink.toml"
fi

log "Installing CLI launcher to ${BIN_LINK}"
cat > "${BIN_LINK}" <<EOF
#!/usr/bin/env bash
exec "${INSTALL_PREFIX}/venv/bin/python" -m roguelink.cli "\$@"
EOF
chmod 0755 "${BIN_LINK}"

log "Installing systemd unit"
install -m 0644 "${REPO_DIR}/systemd/${SERVICE_NAME}" "${SERVICE_DST}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

log "Bootstrapping initial admin password"
"${INSTALL_PREFIX}/venv/bin/python" - <<'PY'
from roguelink import auth, paths
paths.ensure_dirs()
created, password = auth.ensure_initial_password()
if created:
    print(f"[roguelink-install] Initial dashboard credentials: admin / {password}")
    print(f"[roguelink-install] Stored at {paths.INITIAL_PASSWORD_PATH} (root-only)")
else:
    print("[roguelink-install] Auth already configured; keeping existing credentials.")
PY

log "Starting ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}" || true

log "Summary"
"${INSTALL_PREFIX}/venv/bin/python" -m roguelink.cli status || true

cat <<EOF

----------------------------------------------------------------
RogueLink installation complete.

Service:    sudo systemctl status roguelinkd.service
CLI:        roguelink
Dashboard:  see CLI banner above (http://<management-ip>:8080)

Next steps:
  - Configure WAN: roguelink wan scan --iface <iface>
                   roguelink wan connect --iface <iface> --ssid <SSID> --psk <PSK>
  - Start AP:      roguelink ap start --iface <iface> --ssid <SSID> --psk <PSK>
  - Wired LAN:     roguelink lan start --iface eth0
  - Pi 5 tuning:   sudo roguelink system apply-pi5  (then reboot)

Initial password is at /etc/roguelink/initial_password.txt — change it with
'sudo roguelink set-password' and remove the file.
----------------------------------------------------------------
EOF
