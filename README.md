# RogueLink

**Rogue Network Tool** — a Raspberry Pi router/AP appliance.

RogueLink turns a Raspberry Pi 5 (with one onboard Wi-Fi chip and two USB
Wi-Fi adapters) into a self-contained router that:

- exposes a fixed **management interface** on the onboard Pi Wi-Fi,
- accepts a **WAN uplink** over an external USB Wi-Fi adapter,
- broadcasts an **AP** on a second external USB Wi-Fi adapter,
- can also share the WAN over **eth0 as wired LAN**,
- is controlled by a CLI (`roguelink`) and a lightweight web dashboard,
- runs as a persistent systemd service (`roguelinkd.service`).

## Project layout

```
roguelink/             Python package: API, CLI, services, web assets
scripts/               install.sh / uninstall.sh / dev_run.sh
systemd/               roguelinkd.service unit
config/                roguelink.example.toml
external/              Reference projects (not committed)
```

## Supported platform

- **Hardware:** Raspberry Pi 5 (2 GB) with NVMe SSD.
- **OS:** Raspberry Pi OS Lite 64-bit Trixie (Bookworm also works).
- **Wireless:** internal Broadcom (`brcmfmac`) for management; one or two
  USB adapters from the supported list below.

## Supported USB Wi-Fi adapters

| Chipset    | Role hint                       | Driver path                       |
|------------|---------------------------------|------------------------------------|
| MT7612U    | Preferred for AP                 | In-kernel `mt76` + firmware-misc-nonfree |
| RTL8812AU  | Preferred for WAN/uplink         | aircrack-ng/rtl8812au (`v5.6.4.2`) DKMS, fallback morrownr |
| RTL88x2BU  | Alternate WAN/AP                 | morrownr/88x2bu DKMS               |
| RTL8188EUS | 2.4 GHz fallback                 | aircrack-ng/rtl8188eus DKMS        |

Adapter identity is bound to the USB vendor:product ID (read from sysfs)
so role assignments survive interface-name reshuffles between reboots.

## Installation

```bash
sudo ./scripts/install.sh
```

The installer:

1. installs apt dependencies (python3, hostapd, dnsmasq, nftables, iw,
   wpasupplicant, dkms, kernel headers, …),
2. copies the project to `/opt/roguelink`,
3. creates a Python virtualenv at `/opt/roguelink/venv`,
4. installs the example config at `/etc/roguelink/roguelink.toml`,
5. registers `roguelink` (CLI) at `/usr/local/bin/roguelink`,
6. installs and enables `roguelinkd.service`,
7. generates the initial admin password (printed on stdout and stored at
   `/etc/roguelink/initial_password.txt` — root-only),
8. starts the daemon and prints the dashboard URL.

To remove RogueLink:

```bash
sudo ./scripts/uninstall.sh           # keep config/state
sudo ./scripts/uninstall.sh --purge   # also wipe /etc/roguelink, /var/lib/roguelink, /var/log/roguelink
```

## Service management

```bash
sudo systemctl start  roguelinkd
sudo systemctl stop   roguelinkd
sudo systemctl status roguelinkd
journalctl -u roguelinkd -f
```

## CLI

```text
roguelink                         # banner: mgmt IP, dashboard URL, WAN/AP/LAN status, temperature
roguelink status
roguelink dashboard
roguelink adapters
roguelink mgmt status
roguelink mgmt connect --ssid "..." --psk "..."
roguelink wan scan --iface wlan1
roguelink wan connect --iface wlan1 --ssid "..." --psk "..."
roguelink wan disconnect
roguelink ap start  --iface wlan2 --ssid "..." --psk "..."
roguelink ap stop
roguelink lan status
roguelink lan start --iface eth0
roguelink lan stop
roguelink clients
roguelink logs [name]
roguelink firewall {status|reapply|flush}
roguelink set-password
roguelink system apply-pi5         # apply Pi 5 boot config + zram (then reboot)
roguelink system install-driver mt7612u
```

## Dashboard

The dashboard is served by the daemon on the management interface
(`http://<management-ip>:8080`). Pages: Overview, Adapters, Management,
WAN, AP, LAN, System, Logs.

- **Authentication:** HTTP Basic. Loopback (`127.0.0.1`) requests are
  allowed without auth so the local CLI can talk to the API.
- **Initial credentials:** generated at install time; printed to stdout and
  saved to `/etc/roguelink/initial_password.txt` (root-only).
- **Change password:** `sudo roguelink set-password`. The initial password
  file is removed automatically after a successful change.

## Default network topology

```
upstream Wi-Fi  ─── USB Wi-Fi #1 (WAN, RTL8812AU/88x2BU)
                      │
                  [ RogueLink ]──── USB Wi-Fi #2 (AP, MT7612U)  → 10.42.0.0/24
                      │                                            (DHCP via dnsmasq)
                      ├─ onboard Pi Wi-Fi (management) → dashboard at :8080
                      └─ eth0 (LAN)                    → 10.42.1.0/24 (optional)
```

NAT/forwarding is implemented with **nftables** (`inet roguelink` for
filter, `ip roguelink_nat` for postrouting). The ruleset is rendered each
time WAN/AP/LAN state changes so the firewall always matches reality.

## Management interface behavior

- Only the onboard Pi Wi-Fi may hold the `management` role.
- Reassignment to a non-onboard interface is rejected.
- `roguelink mgmt connect` configures and brings up the management Wi-Fi
  using `wpa_supplicant`.
- The dashboard binds on `0.0.0.0` but the firewall only opens the API
  port on the management interface.

## Raspberry Pi 5 setup

`roguelink system apply-pi5` writes three blocks to `/boot/firmware/config.txt`
(or `/boot/config.txt`), each preceded by a backup copy:

- **Active Cooler thresholds** (`fan_temp0..3`) for safe sustained load.
- **PCIe Gen 3** for NVMe (`dtparam=pciex1`, `dtparam=pciex1_gen=3`).
- **Light overclock** (`arm_freq=2600`, `over_voltage_delta=20000`).

It also writes `/etc/default/zramswap` for **2 GB zram** (zstd, 50% target,
priority 100).

GPU memory is **not** written on Pi 5 — Pi 5 manages it dynamically
(see `external/Ghostlink-Mini/docs/raspberry_pi_compatibility.md`).

After applying, the CLI tells you whether a reboot is required.

## Driver strategy

| Chipset    | Approach |
|------------|----------|
| MT7612U    | In-kernel `mt76x2u` stack. We install `firmware-misc-nonfree`; no DKMS clone. |
| RTL8812AU  | DKMS install of aircrack-ng `v5.6.4.2`; fallback to morrownr `8812au-20210820`. Conflicting in-tree modules are blacklisted via `/etc/modprobe.d/roguelink-rtl8812au.conf`. |
| RTL88x2BU  | DKMS install of morrownr `88x2bu-20210702`. |
| RTL8188EUS | DKMS install of aircrack-ng `rtl8188eus`. |

Run `sudo roguelink system install-driver <chipset>` to attempt an install
on demand. Driver detection (which modules are visible/loaded) is shown on
the System page.

## Ghostlink-Mini reference summary

The reference project at `external/Ghostlink-Mini/docs/` informed several
RogueLink decisions:

- **`mt7612u_strategy.md`** — confirms the in-kernel `mt76x2u` path,
  required firmware package, and stable USB-ID adapter detection.
- **`rtl8812au_strategy.md`** — DKMS install pipeline, ARM64 Makefile
  patches, blacklist of conflicting in-tree modules, USB-ID binding.
- **`raspberry_pi_compatibility.md`** — Pi 5 specifics: 2 GB zram,
  `arm_freq=2600`, Active Cooler thresholds, PCIe Gen 3, GPU memory
  firmware-managed on Pi 5, target Trixie/Bookworm.

USB ID lists, role priorities, and the management-interface protection
model are adapted directly from Ghostlink-Mini (`src/core/network.py`,
`src/core/config.py`).

## File locations

| Path | Purpose |
|------|---------|
| `/etc/roguelink/roguelink.toml` | Operator-editable config |
| `/etc/roguelink/auth.json`      | Salted password hash |
| `/etc/roguelink/initial_password.txt` | One-time install password |
| `/var/lib/roguelink/`           | Adapter map, AP/WAN/LAN profiles, leases |
| `/var/log/roguelink/`           | Daemon, WAN, AP, LAN, firewall logs |
| `/run/roguelink/`               | hostapd/dnsmasq/wpa_supplicant configs and pidfiles |
| `/etc/systemd/system/roguelinkd.service` | systemd unit |
| `/opt/roguelink/`               | Installed package and venv |

## Known limitations

- **Hardware-only checks** (driver build, hostapd start, dnsmasq DHCP) can
  only be validated on a real Pi. Local syntax/import checks pass on any
  Python 3.11+ host.
- The dashboard uses HTTP Basic. For production-grade access, terminate
  TLS with a reverse proxy or enable a stronger auth layer.
- nftables is mandatory; iptables-only systems are not supported.
- Realtek out-of-tree drivers require kernel headers matching the running
  kernel. The installer attempts `raspberrypi-kernel-headers` and
  `linux-headers-$(uname -r)`.

## Troubleshooting

- **Daemon won't start:** `journalctl -u roguelinkd -e` and
  `roguelink logs daemon`.
- **No management IP:** `iw dev`, `ip addr show`, then
  `roguelink mgmt connect --ssid ... --psk ...`.
- **AP fails to start:** `roguelink logs ap` shows the hostapd/dnsmasq
  output. Confirm the AP adapter chipset supports AP (`iw phy phyN info`).
- **No internet on AP/LAN clients:** check `roguelink wan status`, then
  `roguelink firewall status` and `roguelink firewall reapply`.
- **Adapter role flipped after reboot:** RogueLink stores roles by USB
  vendor:product ID under `/var/lib/roguelink/adapters.json`. If a
  warning appears, run `roguelink adapters` to re-detect.
