# Requirements

What `plasma-dell-monitor-support` needs to run, and what it does / does not
support. For step-by-step setup see [INSTALL.md](INSTALL.md).

## System

| Requirement | Notes |
|---|---|
| **OS** | Linux. Developed on Gentoo; verified on Arch and Gentoo. |
| **Desktop** | KDE Plasma recommended (tray, icons, and out-of-scope features assume Plasma). Other desktops work but system-tray behaviour depends on the tray implementation. |
| **Session** | X11 or Wayland. On Wayland the app can't anchor a popup to the tray (compositor restriction) — left-click opens the main window instead. |
| **Python** | 3.10 or newer (tested on 3.14). |

## Software dependencies

| Dependency | Min version | Purpose |
|---|---|---|
| **[ddcutil](https://www.ddcutil.com/)** | 2.x (tested 2.2.6 / 2.2.7) | All monitor I/O (detect, capabilities, get/set VCP). Must be on `PATH`. |
| **PyQt6** | 6.5+ (tested 6.11) | GUI toolkit (incl. **QtDBus**, used by the CLI). Installed via `pip` (`requirements.txt`) or your distro package. |
| Qt 6 | comes with PyQt6 | — |
| **D-Bus** (session bus) | standard on KDE / Linux | Only needed for the `cli.py` command-line control (hotkeys): it talks to the running app over the session bus. QtDBus ships with PyQt6 — **no extra pip package**. The GUI itself works without it. |

No other Python packages are used (standard library only beyond PyQt6).

## Permissions

- **ddcutil must work without `sudo`.** This means the `i2c-dev` kernel module is
  loaded (or built in) and your user can access `/dev/i2c-*` — typically by being
  in the **`i2c`** group, or via ddcutil's udev rules.
- Verify with: `ddcutil detect` (should list your external monitors, not error).

## Hardware / connection

- **Supported:** **Dell** external monitors (EDID manufacturer `DEL`) on
  **DisplayPort, HDMI, or USB-C DP-Alt**, connected **directly** to the GPU.
  Non-Dell monitors are detected but not controlled — they get an "unsupported"
  tab (or a blocked screen if no Dell monitor is present).
- **Not supported:** laptop internal panels (eDP — no DDC/CI), and **MST
  daisy-chains, docking stations, and USB DisplayLink** adapters. These often
  appear as "invalid" displays to ddcutil and are skipped.
- The monitor must implement **DDC/CI** and have it **enabled in its OSD** (some
  Dell panels have a "DDC/CI" on/off item).
- **Verified models:** Dell **P2425D**, **P2319H**, **P2317H**, **P3424WE**,
  **P2725HE**. Other Dell models work via the same capability-driven logic but are
  unverified; `collect-monitor-info.sh` gathers what's needed to confirm one.

## Feature scope

**In scope (controlled over DDC/CI):** brightness, contrast, sharpness, RGB gain,
colour preset (merged Dell picture-mode + colour-temperature, incl. ComfortView
where present), input source, OSD language, power mode — per monitor, with
set-then-verify and optional range calibration. Plus **app-side custom input
names** and, on supported USB-C hub monitors, **USB-C Prioritization**.

**Out of scope (handled natively by KDE Plasma / the GPU driver, not DDC):**
HDR enable/disable, VRR / Adaptive-Sync, resolution, refresh rate, framebuffer
rotation.

**Not yet implemented:** HDR-mode / gaming / KVM / PIP-PBP controls, MST
enable/disable (opcode known — planned), profiles (save/load across monitors —
planned), and **monitor-side** input renaming (writing the names into the
monitor's own OSD — needs a Dell opcode we don't have). See
[DDC_ROADMAP.md](DDC_ROADMAP.md) and [ROADMAP.md](ROADMAP.md).
