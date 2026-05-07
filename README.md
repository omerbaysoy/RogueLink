# RogueLink

**Rogue Network Tool** — Raspberry Pi 5 router/AP/WAN appliance with a
dark-mode operator dashboard, CLI, and full driver management.

> **Target:** Raspberry Pi 5, 2 GB RAM, NVMe SSD, Raspberry Pi OS Lite 64-bit (Trixie)

---

## Quick start

```bash
# Clone on the Pi (or copy from another machine)
git clone https://github.com/omerbaysoy/RogueLink.git /tmp/roguelink
cd /tmp/roguelink

# Install (requires root)
sudo bash scripts/install.sh

# Verify
roguelink status
```

The installer creates a virtualenv at `/opt/roguelink/venv`, copies code to
`/opt/roguelink`, installs the `roguelinkd.service` systemd unit, and starts
the daemon.

### Default dashboard login

| Field    | Value        |
|----------|--------------|
| URL      | `http://<management-ip>:8080` |
| Username | `admin`      |
| Password | `roguelink`  |

Change the password immediately via the dashboard **System → Security** section
or the CLI:

```bash
sudo roguelink set-password
```

---

## Dashboard

The dashboard opens at the management interface IP on port 8080.
All dashboard actions (scan, health check, speedtest, AP start/stop, etc.)
submit via JavaScript `fetch()` and display results inline — the browser
never navigates to raw JSON API endpoints.

### Pages

| Page        | Description |
|-------------|-------------|
| Overview    | Management/WAN/AP/LAN status, health, speedtest, networks summary |
| Adapters    | Detected adapters, role assignment, TX power, power save, reset |
| Networks    | Wi-Fi scan (parsed table), save/connect/edit/delete networks |
| Management  | Management interface config and reconnect |
| WAN         | Scan, connect, disconnect, saved network connect, health detail |
| AP          | Start/stop access point, connected clients |
| LAN         | Start/stop eth0 LAN bridge, DHCP clients |
| System      | Platform info, drivers, firewall, fan control, speedtest, password |
| Logs        | Tabbed log viewer: daemon, setup, WAN, AP, LAN, firewall, networks, speedtest, health |

### Networks page

- **Scan:** Select an adapter, click **Scan now** → nearby networks appear in a
  clean table (SSID, BSSID, signal dBm, quality, channel, band, security,
  interface, actions).
- **Save:** Click the Save button next to a scanned network or use the manual
  form below the table.
- **Saved networks:** Collapsible cards showing signal history, connection
  attempts, edit/connect/delete controls.

---

## CLI

```
roguelink                          # status banner
roguelink status                   # same as above
roguelink adapters                 # list adapters and roles
roguelink dashboard                # show dashboard URL

# Networks
roguelink networks scan --iface wlan3
roguelink networks scan --iface wlan2 --json
roguelink networks saved
roguelink networks save --ssid "MyNet" --psk "pass123" --note "home"
roguelink networks connect <id>

# WAN
roguelink wan connect --iface wlan2 --ssid "Uplink" --psk "secret"
roguelink wan disconnect

# AP
roguelink ap start --iface wlan3 --ssid "RogueLink-AP" --psk "password"
roguelink ap stop

# LAN
roguelink lan start --iface eth0
roguelink lan stop

# Health
roguelink health
roguelink health --json

# Speedtest
roguelink speedtest
roguelink speedtest --json
roguelink speedtest last

# Fan
roguelink fan status
roguelink fan set quiet
roguelink fan set balanced
roguelink fan set performance
roguelink fan set max

# Driver audit
roguelink system driver-audit
roguelink system driver-audit --json
roguelink system verify-driver rtl8812au
roguelink system verify-driver mt7612u
roguelink system verify-driver rtl88x2bu
roguelink system verify-driver rtl8188eus
roguelink system driver-diag
roguelink system driver-diag --iface wlan2

# Driver install
sudo roguelink system install-driver rtl8812au
sudo roguelink system install-driver mt7612u

# System
roguelink system info
sudo roguelink system apply-pi5

# Firewall
roguelink firewall status
sudo roguelink firewall reapply
sudo roguelink firewall flush

# Logs
roguelink logs daemon
roguelink logs wan
roguelink logs ap
```

---

## Health check

The health check uses a **split connectivity model**:

| Field                | Description |
|----------------------|-------------|
| **Overall**          | excellent / good / partial / weak / unstable / offline |
| **Management Internet** | Whether the management interface has internet |
| **WAN status**       | connected / not_configured |
| **DNS**              | OK / Failing |
| **Gateway**          | Default gateway IP and ping result |
| **RTT / Loss**       | Public ping latency and packet loss |
| **Signal**           | WAN Wi-Fi signal dBm (if applicable) |
| **Reason**           | Human-readable explanation |

The health check does **not** report `offline` when gateway ping, internet
ping, and DNS are all OK. If WAN is not configured but the management
interface has internet, it reports `good` or `partial` with
`wan: not_configured`.

---

## Driver strategies

### RTL8812AU (critical)

**Primary driver:** `aircrack-ng/rtl8812au` branch `v5.6.4.2`

```
Strategy:
1. Remove stale DKMS variants
2. Unload conflict modules: rtw_8812au, rtw88_8812au, rtl8xxxu, 8812au, 88XXau
3. Blacklist via /etc/modprobe.d/roguelink-rtl8812au.conf:
   - blacklist rtw_8812au
   - blacklist rtw88_8812au
   - blacklist rtl8xxxu
   - options 88XXau rtw_led_ctrl=0
4. Clone: git clone -b v5.6.4.2 --single-branch https://github.com/aircrack-ng/rtl8812au.git /usr/src/rtl8812au
5. Patch Makefile:
   - CONFIG_PLATFORM_I386_PC = n
   - CONFIG_PLATFORM_ARM64_RPI = y  (on arm64/aarch64)
   - CONFIG_PLATFORM_ARM_RPI = y    (on 32-bit ARM)
6. Build: ARCH=arm64 make dkms_install
7. Load: modprobe 88XXau
8. Verify: modinfo 88XXau, lsmod, USB ID 0bda:8812
```

**Fallback:** `morrownr/8812au-20210820` (module: `8812au`). Dashboard/CLI
will show a warning when the fallback is active.

### MT7612U

Uses the **in-kernel mt76 stack** — no third-party DKMS.

```
Required modules: mt76, mt76_usb, mt76x2_common, mt76x2u
Firmware: firmware-misc-nonfree (mt7662u.bin, mt7662u_rom_patch.bin)
USB IDs: 0e8d:7612, 0e8d:761a, 2001:3a02, 0b05:17d1, 148f:7612, 13b1:003e
Preferred role: AP
```

MT7612U setup does **not** touch Realtek configs or modules.

### RTL88x2BU

- Driver: `morrownr/88x2bu-20210702`
- Modules: `88x2bu`, `rtw_8822bu`, `rtw88_8822bu`
- USB ID: `0bda:b812`
- Preferred role: WAN (AP possible but MT7612U preferred for AP)

### RTL8188EUS

- Driver: `aircrack-ng/rtl8188eus`
- Modules: `8188eu`, `r8188eu`
- USB ID: `2357:010c`
- Role: fallback / test / 2.4GHz only

---

## Management IP binding

The daemon binds to the **management interface IP** by default (not `0.0.0.0`).

- Config key: `host = "auto"` in `/etc/roguelink/roguelink.toml`
- `"auto"` resolves to the management interface IP at startup
- If management IP cannot be detected, falls back to `127.0.0.1` (safe)
- Set `host = "0.0.0.0"` explicitly only for development/debug

```bash
# Verify bind address:
sudo ss -lntp | grep 8080
# Should show: 192.168.x.x:8080, NOT 0.0.0.0:8080
```

---

## Adapter controls

From the **Adapters** page or CLI:

- **TX power:** Read current dBm, set specific dBm, or set to auto
- **Power save:** Toggle on/off
- **Soft reset:** `ip link down/up`
- **USB reset:** Re-authorize the USB device (when supported)
- Unsupported operations show a clear message instead of failing silently

---

## Speedtest

```bash
roguelink speedtest             # run and show result
roguelink speedtest --json      # JSON output
roguelink speedtest last        # show last result without running
```

Results stored in `/var/lib/roguelink/speedtest_last.json`.
Dashboard speedtest button shows results inline.

---

## Fan control (Pi 5)

Profiles: `quiet`, `balanced`, `performance`, `max`, `custom`

```bash
roguelink fan status
roguelink fan set balanced
roguelink fan set performance
```

Config is written to `/boot/firmware/config.txt` with a backup. A reboot
is required to activate changes.

---

## File layout

```
/opt/roguelink/              # Installed code
/etc/roguelink/              # Configuration (roguelink.toml, auth.json)
/var/lib/roguelink/           # Runtime state (networks.db, profiles, speedtest)
/var/log/roguelink/           # Log files
/run/roguelink/               # PID files, runtime configs
/usr/local/bin/roguelink      # CLI launcher
```

---

## Known limitations

- RTL8812AU driver builds require kernel headers matching the running kernel.
  If headers are unavailable, the driver install will fail with a clear message.
- Fan control is Pi 5 specific; on other hardware it reports unsupported.
- WAN/AP/LAN management requires root. The daemon runs as root by design.
- Speedtest requires `speedtest-cli` (installed automatically).
- Management interface must be on the onboard Broadcom Wi-Fi (`brcmfmac`).

---

## Troubleshooting

### Dashboard not loading

```bash
sudo systemctl status roguelinkd --no-pager
sudo ss -lntp | grep 8080
roguelink dashboard
```

If `ss` shows `127.0.0.1:8080`, the management IP was not detected.
Set `host = "<your-ip>"` in `/etc/roguelink/roguelink.toml` and restart.

### Driver not loading

```bash
roguelink system driver-audit
roguelink system verify-driver rtl8812au
roguelink system driver-diag --iface wlan2
dmesg | tail -50
```

### Health says "offline" incorrectly

This was a known bug and has been fixed. The health check now uses a split
model and does not report offline when gateway/internet/DNS checks succeed.

### WAN not connecting

```bash
roguelink wan status
roguelink networks scan --iface <iface>
roguelink health
journalctl -u roguelinkd -n 50
```

---

## License

MIT
