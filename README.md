# plasma-dell-monitor-support

A simple **PyQt6** desktop app for **KDE Plasma** that controls **Dell monitor**
settings over **DDC/CI**, using [`ddcutil`](https://www.ddcutil.com/) as the
backend. Adjust brightness, contrast, colour presets, input source, and more —
per monitor — from your desktop instead of fumbling with the monitor's buttons.

It talks to the monitor the same way Dell's own *Display & Peripheral Manager*
does on Windows, but natively on Linux.

---

> # ❗ USE AT YOUR OWN RISK
> **This software talks directly to your monitor hardware over DDC/CI. It is
> provided "as is", with NO WARRANTY of any kind. The author is NOT responsible
> for any damage, data loss, misconfiguration, or other problems that may result
> from using it. By using this software you accept full responsibility.**

---

## What it does

- Detects connected **Dell** monitors (DisplayPort / HDMI / USB-C DP-Alt).
- Per-monitor controls: **Brightness, Contrast, Sharpness, RGB gain, Colour
  Preset** (Standard / Movie / Game / ComfortView / Cool / Warm / Custom, where
  supported), **Input Source, OSD Language, Power Mode**.
- **Set-then-verify** — every change is read back from the monitor to confirm it
  applied.
- Optional per-monitor **range calibration** (some panels clamp/quantise values
  over DDC) and **custom input labels**.
- Lives in the **system tray** with quick per-monitor controls.

Non-Dell monitors are detected but shown as *unsupported* (not touched).

## Requirements

| Requirement | Notes |
|---|---|
| **OS** | Linux with **KDE Plasma** (X11 or Wayland). |
| **[ddcutil](https://www.ddcutil.com/)** | 2.x, usable **without `sudo`** (see below). |
| **Python** | 3.10 or newer. |
| **PyQt6** | via your distro package or `pip`. |
| **A Dell monitor** | on DisplayPort, HDMI, or USB-C (DP-Alt), connected **directly** to the GPU. |

**Not supported:** laptop internal panels (no DDC/CI), MST daisy-chains, docking
hubs, and DisplayLink adapters.

## Installation

### 1. Install ddcutil
```bash
# Arch:    sudo pacman -S ddcutil
# Gentoo:  sudo emerge -av app-misc/ddcutil
# Debian/Ubuntu: sudo apt install ddcutil
# Fedora:  sudo dnf install ddcutil
```

### 2. Give your user DDC/CI access (no sudo)
```bash
sudo modprobe i2c-dev
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
sudo usermod -aG i2c "$USER"      # then log out / back in
ddcutil detect                    # should list your monitor, without sudo
```
If your monitor isn't listed, make sure **DDC/CI is enabled in its OSD menu**.

### 3. Install PyQt6
```bash
# Arch:    sudo pacman -S python-pyqt6
# Gentoo:  sudo emerge -av dev-python/PyQt6
# or:      pip install -r requirements.txt   (ideally in a virtualenv)
```

### 4. Run
```bash
python3 main.py
```

Optional desktop integration (icon + launcher entry) is described in
[`INSTALL.md`](INSTALL.md).

## Platform support

Developed and tested on **Arch Linux** and **Gentoo Linux** (KDE Plasma,
Wayland). Other distributions with Plasma and a working `ddcutil` should work
too, but are untested.

## Tested monitors

The app is capability-driven, so most Dell monitors should work without changes.
These have been verified:

| Model | Connection | Notes |
|---|---|---|
| Dell **P2425D** | DisplayPort | Baseline (QHD). |
| Dell **P2319H** | DisplayPort | Full HD; has ComfortView. |
| Dell **P2317H** | HDMI | Full HD; older/simpler panel. |
| Dell **P3424WE** | USB-C | 34″ ultrawide; PIP/PBP present (not yet exposed). |
| Dell **P2725HE** | USB-C | Full HD USB-C hub. |

See [`TESTED-MONITORS.md`](TESTED-MONITORS.md) for per-model detail.

## ⚠️ Limitations & known quirks

- **This is early software and may contain bugs.**
- **Some features may not work on some monitors.** Different Dell models (and even
  firmware revisions) expose different DDC/CI features, and some monitors *accept*
  a DDC command but don't actually act on it (a firmware quirk — the setting only
  works from the monitor's own on-screen menu). Examples seen in testing include
  sharpness and aspect ratio on certain models.
- **A feature the monitor has in its OSD is not guaranteed to be controllable over
  DDC** — if the monitor doesn't advertise it, no software (including Dell's own)
  can set it remotely.
- Features KDE Plasma handles natively — HDR, VRR/Adaptive-Sync, resolution,
  refresh rate, rotation — are intentionally **out of scope**.

## Contributing / reporting

Help make the monitor coverage better:

- **Report a working Dell monitor** so it can be added to the tested list —
  include the model and the output of:
  ```bash
  ddcutil detect
  ddcutil --bus N capabilities --terse    # N = your monitor's I2C bus
  ```
- **Report a feature that doesn't work** on your monitor (which model, which
  control, what happens) so it can be investigated. There's a helper,
  `collect-monitor-info.sh`, that gathers a full (read-only) capability report you
  can attach.

Open an issue with the details and we'll try to add support or troubleshoot.

## Development note

Most of this project was written with **heavy assistance from Claude**
(Anthropic's AI assistant), with some minor manual modifications. The code has
been **checked and verified against real hardware where possible** — but, as with
any software (and especially AI-assisted code that talks to hardware), it may
still contain bugs. See the risk notice above.

## License

No license has been chosen yet — add a `LICENSE` file before publishing if you
want to set usage terms. Regardless, the software is provided **as-is, without
warranty** (see the risk notice above).
