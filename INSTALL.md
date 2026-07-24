# Installation

Setup for `plasma-dell-monitor-support`. For the full list of prerequisites and
what's supported, see [REQUIREMENTS.md](REQUIREMENTS.md).

## 1. Install ddcutil

Use your distro's package manager, e.g.:

```bash
# Gentoo
sudo emerge -av app-misc/ddcutil
# Debian/Ubuntu:  sudo apt install ddcutil
# Fedora:         sudo dnf install ddcutil
# Arch:           sudo pacman -S ddcutil
```

## 2. Give your user DDC/CI access (no sudo)

`ddcutil` talks to monitors over the I²C buses at `/dev/i2c-*`. Enable access:

```bash
# load the kernel module now and on every boot
sudo modprobe i2c-dev
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf

# add yourself to the i2c group, then log out/in (or reboot)
sudo usermod -aG i2c "$USER"
```

Verify (should list your external monitors, without sudo):

```bash
ddcutil detect
```

If a monitor isn't listed, check that **DDC/CI is enabled in its OSD menu**.

## 3. Install PyQt6

Either via your distro (preferred on Gentoo/KDE), or pip:

```bash
# distro examples
# Gentoo:  sudo emerge -av dev-python/PyQt6
# Debian:  sudo apt install python3-pyqt6
# Fedora:  sudo dnf install python3-pyqt6

# or with pip (ideally in a virtualenv)
pip install -r requirements.txt
```

## 4. Run

```bash
cd /path/to/plasma-dell-monitor-support
python3 main.py
```

The app detects your monitors and opens a tab per display. Closing the window
minimises it to the system tray; quit via the tray's **Exit application** (or
*File ▸ Exit*).

## 5. (Optional) Desktop integration — icon + launcher

Installing the desktop file registers the app-id so Plasma shows the **monitor
icon** in the task switcher / launcher, and silences the harmless console warning
`Could not register app ID … App info not found`.

```bash
# run from the project directory so $PWD is substituted into Exec=
install -Dm644 plasma-dell-monitor-support.desktop \
  ~/.local/share/applications/plasma-dell-monitor-support.desktop
sed -i "s|/path/to/plasma-dell-monitor-support|$PWD|" \
  ~/.local/share/applications/plasma-dell-monitor-support.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

You can then launch it from the app menu, or add it to **System Settings ▸
Autostart** to start it (minimised to tray) on login.

## Troubleshooting

- **`ddcutil detect` needs sudo / shows nothing** — the `i2c-dev` module isn't
  loaded or you're not in the `i2c` group yet (log out/in after `usermod`).
- **A monitor is missing** — it may be on an MST hub / dock (unsupported), or
  DDC/CI is disabled in its OSD.
- **A slider snaps to a limit** (e.g. contrast won't go below 25) — that's the
  monitor's firmware; use *Calibrate ranges…* so the sliders match the real
  limits. See the README.
- **Console warning about app ID** — do step 5.
