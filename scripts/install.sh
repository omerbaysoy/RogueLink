#!/usr/bin/env bash
# RogueLink installer for Raspberry Pi OS / Debian-based systems.
#
# Usage:
#   sudo bash scripts/install.sh
#
# This single command handles everything:
#   - apt dependencies (including kernel headers, firmware, DHCP clients)
#   - Python virtualenv + pip packages
#   - Driver install/verify for RTL8812AU, MT7612U, etc.
#   - systemd service
#   - CLI launchers (roguelink + ghostlink)
#   - default auth
#   - permissions hardening
#   - install summary

set -euo pipefail

INSTALL_PREFIX="/opt/roguelink"
ETC_DIR="/etc/roguelink"
LIB_DIR="/var/lib/roguelink"
LOG_DIR="/var/log/roguelink"
RUN_DIR="/run/roguelink"
SERVICE_NAME="roguelinkd.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
BIN_LINK="/usr/local/bin/roguelink"
BIN_ALIAS="/usr/local/bin/ghostlink"

if [[ "${EUID}" -ne 0 ]]; then
  echo "RogueLink install must be run as root (sudo)." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log()  { printf "\n\033[1;32m[roguelink]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[roguelink]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[roguelink]\033[0m %s\n" "$*"; }

# -----------------------------------------------------------------------
# 1. Directories
# -----------------------------------------------------------------------

log "Creating directories"
mkdir -p "${INSTALL_PREFIX}" "${ETC_DIR}" "${LIB_DIR}" "${LOG_DIR}" "${RUN_DIR}"

# -----------------------------------------------------------------------
# 2. Apt packages
# -----------------------------------------------------------------------

BASE_PACKAGES=(
  python3 python3-venv python3-pip
  hostapd dnsmasq nftables
  iw wireless-tools wpasupplicant rfkill ethtool
  git curl
  build-essential dkms
  usbutils iproute2 procps
)

# Optional packages — install best-effort
OPTIONAL_PACKAGES=(
  pciutils
  firmware-misc-nonfree
  isc-dhcp-client
  dhcpcd5
)

# Kernel headers — try multiple candidates
HEADER_CANDIDATES=(
  raspberrypi-kernel-headers
  "linux-headers-$(uname -r)"
  linux-headers-arm64
)

log "Updating apt and installing base packages"
DEBIAN_FRONTEND=noninteractive apt-get update -qq

# Install base packages (fail the install if critical ones are missing)
DEBIAN_FRONTEND=noninteractive apt-get install -y "${BASE_PACKAGES[@]}" || {
  err "Some base packages failed to install. Continuing..."
}

# Install optional packages silently
for pkg in "${OPTIONAL_PACKAGES[@]}"; do
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkg}" 2>/dev/null || true
done

# Install kernel headers (try each candidate)
HEADERS_OK=0
for pkg in "${HEADER_CANDIDATES[@]}"; do
  if DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkg}" 2>/dev/null; then
    HEADERS_OK=1
    log "Kernel headers installed: ${pkg}"
    break
  fi
done
if [[ ${HEADERS_OK} -eq 0 ]]; then
  warn "No kernel headers found. DKMS driver builds will fail."
  warn "Try: sudo apt install raspberrypi-kernel-headers"
fi

# Disable system-managed hostapd/dnsmasq (RogueLink manages these)
systemctl disable --now hostapd 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true
systemctl unmask hostapd 2>/dev/null || true

# -----------------------------------------------------------------------
# 3. Copy project
# -----------------------------------------------------------------------

log "Copying project to ${INSTALL_PREFIX}"
rsync -a --delete \
  --exclude '.git' --exclude 'external' --exclude '__pycache__' --exclude 'venv' \
  "${REPO_DIR}/" "${INSTALL_PREFIX}/"

# -----------------------------------------------------------------------
# 4. Python virtualenv
# -----------------------------------------------------------------------

log "Creating Python virtualenv"
python3 -m venv "${INSTALL_PREFIX}/venv"
"${INSTALL_PREFIX}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_PREFIX}/venv/bin/pip" install -q \
  fastapi "uvicorn[standard]" jinja2 typer rich httpx python-multipart tomli speedtest-cli

# -----------------------------------------------------------------------
# 5. Config
# -----------------------------------------------------------------------

log "Installing configuration"
if [[ ! -f "${ETC_DIR}/roguelink.toml" ]]; then
  install -m 0640 "${REPO_DIR}/config/roguelink.example.toml" "${ETC_DIR}/roguelink.toml"
fi

# -----------------------------------------------------------------------
# 6. CLI launchers
# -----------------------------------------------------------------------

log "Installing CLI launchers"

# Primary CLI: roguelink
cat > "${BIN_LINK}" <<'LAUNCHER'
#!/usr/bin/env bash
export PYTHONPATH="/opt/roguelink${PYTHONPATH:+:$PYTHONPATH}"
cd /opt/roguelink
exec /opt/roguelink/venv/bin/python -m roguelink.cli "$@"
LAUNCHER
chmod 0755 "${BIN_LINK}"

# Compatibility alias: ghostlink
cat > "${BIN_ALIAS}" <<'LAUNCHER'
#!/usr/bin/env bash
export PYTHONPATH="/opt/roguelink${PYTHONPATH:+:$PYTHONPATH}"
export ROGUELINK_ALIAS="ghostlink"
cd /opt/roguelink
exec /opt/roguelink/venv/bin/python -m roguelink.cli "$@"
LAUNCHER
chmod 0755 "${BIN_ALIAS}"

log "CLI: roguelink and ghostlink installed to /usr/local/bin/"

# -----------------------------------------------------------------------
# 7. Systemd service
# -----------------------------------------------------------------------

log "Installing systemd unit"
install -m 0644 "${REPO_DIR}/systemd/${SERVICE_NAME}" "${SERVICE_DST}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# -----------------------------------------------------------------------
# 8. Auth bootstrap
# -----------------------------------------------------------------------

log "Bootstrapping default admin credentials"
PYTHONPATH="${INSTALL_PREFIX}" "${INSTALL_PREFIX}/venv/bin/python" - <<'PY'
from roguelink import auth, paths
paths.ensure_dirs()
created, username, password = auth.ensure_default_password()
if created:
    print(f"[roguelink] Dashboard login created: {username} / {password}")
else:
    print("[roguelink] Auth already configured; keeping existing credentials.")
PY

# -----------------------------------------------------------------------
# 9. Permissions hardening
# -----------------------------------------------------------------------

log "Setting permissions"
chmod 0750 "${ETC_DIR}"
chmod 0750 "${LIB_DIR}"
chmod 0755 "${LOG_DIR}"
# Protect sensitive files
[[ -f "${ETC_DIR}/auth.json" ]]          && chmod 0600 "${ETC_DIR}/auth.json"
[[ -f "${ETC_DIR}/roguelink.toml" ]]     && chmod 0640 "${ETC_DIR}/roguelink.toml"
[[ -f "${LIB_DIR}/networks.db" ]]        && chmod 0600 "${LIB_DIR}/networks.db"
[[ -f "${LIB_DIR}/wan_profile.json" ]]   && chmod 0600 "${LIB_DIR}/wan_profile.json"
[[ -f "${LIB_DIR}/ap_profile.json" ]]    && chmod 0600 "${LIB_DIR}/ap_profile.json"
[[ -f "${LIB_DIR}/mgmt_profile.json" ]]  && chmod 0600 "${LIB_DIR}/mgmt_profile.json"

# -----------------------------------------------------------------------
# 10. Driver install / verify
# -----------------------------------------------------------------------

install_rtl8812au() {
  local SRC="/usr/src/rtl8812au"
  local MODULE="88XXau"
  local GIT_URL="https://github.com/aircrack-ng/rtl8812au.git"
  local BRANCH="v5.6.4.2"
  local FALLBACK_URL="https://github.com/morrownr/8812au-20210820.git"
  local BLACKLIST_CONF="/etc/modprobe.d/roguelink-rtl8812au.conf"
  local ARCH
  ARCH="$(uname -m)"

  log "RTL8812AU driver install/verify"

  # Check if already loaded
  if lsmod | grep -q "88XXau"; then
    log "RTL8812AU: module 88XXau already loaded"
    return 0
  fi

  # Check if module is available
  if modinfo "${MODULE}" &>/dev/null; then
    log "RTL8812AU: module available, loading..."
    modprobe "${MODULE}" 2>/dev/null || true
    return 0
  fi

  # Need to build — check headers
  if [[ ! -d "/lib/modules/$(uname -r)/build" ]]; then
    warn "RTL8812AU: kernel headers missing, cannot build. Install headers and retry."
    return 1
  fi

  # Clean stale DKMS entries
  for mod in 8812au rtl8812au 88XXau; do
    local dkms_ver
    dkms_ver=$(dkms status "${mod}" 2>/dev/null | head -1 | awk -F'[, ]+' '{print $2}' || true)
    if [[ -n "${dkms_ver}" ]]; then
      log "Removing stale DKMS: ${mod}/${dkms_ver}"
      dkms remove -m "${mod}" -v "${dkms_ver}" --all 2>/dev/null || true
    fi
  done

  # Unload conflict modules
  for mod in rtw_8812au rtw88_8812au rtl8xxxu 8812au 88XXau; do
    if lsmod | grep -q "^${mod}"; then
      log "Unloading conflict module: ${mod}"
      rmmod "${mod}" 2>/dev/null || true
    fi
  done

  # Clone source
  if [[ -d "${SRC}" ]]; then
    log "Removing old source ${SRC}"
    rm -rf "${SRC}"
  fi

  log "Cloning aircrack-ng/rtl8812au ${BRANCH}"
  if ! git clone -b "${BRANCH}" --single-branch --depth 1 "${GIT_URL}" "${SRC}" 2>/dev/null; then
    warn "Primary clone failed, trying fallback..."
    if ! git clone --depth 1 "${FALLBACK_URL}" "${SRC}" 2>/dev/null; then
      err "RTL8812AU: git clone failed"
      return 1
    fi
  fi

  # Patch Makefile for ARM
  local MAKEFILE="${SRC}/Makefile"
  if [[ -f "${MAKEFILE}" ]]; then
    sed -i 's/CONFIG_PLATFORM_I386_PC = y/CONFIG_PLATFORM_I386_PC = n/' "${MAKEFILE}"
    case "${ARCH}" in
      aarch64|arm64)
        sed -i 's/CONFIG_PLATFORM_ARM64_RPI = n/CONFIG_PLATFORM_ARM64_RPI = y/' "${MAKEFILE}"
        ;;
      arm*)
        sed -i 's/CONFIG_PLATFORM_ARM_RPI = n/CONFIG_PLATFORM_ARM_RPI = y/' "${MAKEFILE}"
        ;;
    esac
    log "Makefile patched for ${ARCH}"
  fi

  # Build
  local MAKE_ARCH=""
  case "${ARCH}" in
    aarch64|arm64) MAKE_ARCH="ARCH=arm64" ;;
    arm*)          MAKE_ARCH="ARCH=arm" ;;
  esac

  log "Building with DKMS (this may take several minutes)..."
  if (cd "${SRC}" && ${MAKE_ARCH} make dkms_install) 2>&1 | tail -5; then
    log "RTL8812AU DKMS install succeeded"
  else
    err "RTL8812AU DKMS install failed. Check kernel headers."
    return 1
  fi

  # Write blacklist and module options
  cat > "${BLACKLIST_CONF}" <<BLCONF
# Generated by RogueLink for RTL8812AU
blacklist rtw_8812au
blacklist rtw88_8812au
blacklist rtl8xxxu
options 88XXau rtw_led_ctrl=0
BLCONF

  # Load
  depmod -a
  modprobe "${MODULE}" 2>/dev/null || true

  if lsmod | grep -q "${MODULE}"; then
    log "RTL8812AU: module ${MODULE} loaded successfully"
  else
    warn "RTL8812AU: module built but not loaded (device may not be plugged in)"
  fi
  return 0
}

install_mt7612u_firmware() {
  log "MT7612U firmware verify"
  local FW1="/lib/firmware/mediatek/mt7662u.bin"
  local FW2="/lib/firmware/mediatek/mt7662u_rom_patch.bin"

  if [[ -f "${FW1}" && -f "${FW2}" ]]; then
    log "MT7612U: firmware files present"
  else
    log "MT7612U: installing firmware-misc-nonfree..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y firmware-misc-nonfree 2>/dev/null || true
  fi

  # Load kernel module if not loaded
  if ! lsmod | grep -q "mt76x2u"; then
    modprobe mt76x2u 2>/dev/null || true
  fi

  if lsmod | grep -q "mt76x2u"; then
    log "MT7612U: mt76x2u module loaded"
  else
    warn "MT7612U: mt76x2u not loaded (device may not be plugged in)"
  fi
}

# Run driver installs (failures don't abort the installer)
install_rtl8812au || warn "RTL8812AU driver install had issues (see above)"
install_mt7612u_firmware || true

# -----------------------------------------------------------------------
# 11. Start service
# -----------------------------------------------------------------------

log "Starting ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}" || true
sleep 2

# -----------------------------------------------------------------------
# 12. Install summary
# -----------------------------------------------------------------------

echo ""
echo "================================================================"
echo ""
echo "  ██████╗  ██████╗  ██████╗ ██╗   ██╗███████╗██╗     ██╗███╗   ██╗██╗  ██╗"
echo "  ██╔══██╗██╔═══██╗██╔════╝ ██║   ██║██╔════╝██║     ██║████╗  ██║██║ ██╔╝"
echo "  ██████╔╝██║   ██║██║  ███╗██║   ██║█████╗  ██║     ██║██╔██╗ ██║█████╔╝ "
echo "  ██╔══██╗██║   ██║██║   ██║██║   ██║██╔══╝  ██║     ██║██║╚██╗██║██╔═██╗ "
echo "  ██║  ██║╚██████╔╝╚██████╔╝╚██████╔╝███████╗███████╗██║██║ ╚████║██║  ██╗"
echo "  ╚═╝  ╚═╝ ╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝╚══════╝╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝"
echo ""
echo "  Rogue Network Tool — Installation Complete"
echo ""
echo "================================================================"
echo ""

# Show status via CLI
PYTHONPATH="${INSTALL_PREFIX}" "${INSTALL_PREFIX}/venv/bin/python" -m roguelink.cli status 2>/dev/null || true

echo ""
echo "----------------------------------------------------------------"
echo "  Service:          sudo systemctl status roguelinkd"
echo "  CLI:              roguelink"
echo "  Alias:            ghostlink"
echo "  Dashboard login:  admin / roguelink"
echo "----------------------------------------------------------------"
echo ""
echo "  Quick start:"
echo "    roguelink status"
echo "    roguelink adapters"
echo "    roguelink networks scan --iface <iface>"
echo "    roguelink wan connect --iface <iface> --ssid \"<SSID>\" --psk \"<PSK>\""
echo "    roguelink ap start --iface <iface> --ssid \"<SSID>\" --psk \"<PSK>\""
echo "    roguelink lan start --iface eth0"
echo "    roguelink health"
echo "    roguelink speedtest"
echo "    roguelink system driver-audit"
echo "    roguelink fan status"
echo ""
echo "  Change default password:"
echo "    sudo roguelink set-password"
echo "    or via dashboard: System → Security"
echo ""
echo "  Driver audit:"
echo "    roguelink system driver-audit"
echo "    roguelink system verify-driver rtl8812au"
echo "    roguelink system verify-driver mt7612u"
echo "----------------------------------------------------------------"
echo ""

# Show driver audit summary
log "Driver audit:"
PYTHONPATH="${INSTALL_PREFIX}" "${INSTALL_PREFIX}/venv/bin/python" -m roguelink.cli system driver-audit 2>/dev/null || true

echo ""
log "Service status:"
systemctl is-active "${SERVICE_NAME}" 2>/dev/null || true

echo ""
log "Installation complete."
