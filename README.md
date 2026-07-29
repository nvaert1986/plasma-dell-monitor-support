# plasma-dell-monitor-support

A simple **PyQt6** desktop app for **KDE Plasma** that controls **Dell monitor**
settings over **DDC/CI**, using [`ddcutil`](https://www.ddcutil.com/) as the
backend. Adjust brightness, contrast, colour presets, input source, and more —
per monitor — from your desktop instead of fumbling with the monitor's buttons.

It talks to the monitor the same way Dell's own *Display & Peripheral Manager*
does on Windows, but natively on Linux.

**Version 1.3** — see the [Changelog](#changelog).

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
  supported), **Input Source, OSD Language, Power Mode**, and **speaker Volume /
  Mute** on monitors with built-in audio.
- **Set-then-verify** — every change is read back from the monitor to confirm it
  applied.
- Per-monitor **sub-tabs**: **Information** (read-only identity/status), Settings,
  Color / Picture, **PIP / PBP**, **MST**, and **KVM**.
- **PIP / PBP** — Picture-in-Picture / Picture-by-Picture: mode, sub-window input,
  and size/position toggles, on monitors that support it.
- **MST (Multi-Stream Transport)** — DisplayPort daisy-chaining enable/disable, on
  monitors that support it *(experimental — see limitations)*.
- **USB KVM** — on monitors with a built-in USB KVM, switch which computer gets the
  shared keyboard/mouse (the USB hub follows the active input), and choose which USB
  upstream port (e.g. USB-C / USB-B) feeds each input *(experimental — see
  limitations)*.
- Optional per-monitor **range calibration** (some panels clamp/quantise values
  over DDC) and **custom input labels**.
- **Factory reset** — restore a monitor to its factory defaults over DDC/CI.
- **Copy settings to other monitors** — mirror one monitor's image settings
  (brightness, contrast, sharpness, RGB gain, colour preset) to your other Dell
  monitors, clamped to each one's range and skipping anything it doesn't support.
- **Export / import settings** — save a monitor's image settings (and OSD
  language) to a JSON file, and import them back onto the same or a different Dell
  monitor (all settings, or just the image ones), skipping anything unsupported.
- **Command-line control for hotkeys** — a small `cli.py` lets you bind keys (KDE
  Custom Shortcuts) to adjust brightness/contrast/sharpness/RGB gain or cycle
  colour presets. It talks to the running app over D-Bus, so it's instant.
- **Profiles (10 per monitor)** — save a monitor's visual settings (brightness,
  contrast, sharpness, RGB gain, colour preset) into a numbered, labelled slot
  (e.g. "6. Gaming", "7. Movies") and load it instantly — from the Settings tab or
  a hotkey. Perfect for switching from web-browsing to a movie.
- **Retry detection** — re-scan for monitors without restarting (e.g. after loading
  `i2c-dev`, enabling DDC/CI, or plugging one in).
- Lives in the **system tray** with quick per-monitor controls.

Non-Dell monitors are detected but shown as *unsupported* (not touched).

## Command-line control & hotkeys

`cli.py` lets you drive the app from the command line — its main purpose is to be
bound to **keyboard shortcuts** so you can nudge brightness, cycle a colour preset,
or jump to a saved profile without opening a window.

### How it works

`cli.py` does **not** talk to the monitor itself. Instead it sends a request to the
**already-running GUI app** over **D-Bus**, and the GUI performs the change. That
design is deliberate and has three benefits:

- **Instant** — the GUI already has your monitors detected, so there's no slow
  per-press `ddcutil detect` (which can take a couple of minutes on an MST chain).
- **Live UI** — because the GUI makes the change, its sliders and dropdowns update
  immediately, and its normal *set-then-verify* read-back still runs.
- **No conflicts** — the GUI stays the single owner of DDC/CI access, so the CLI and
  GUI never fight over the I²C bus.

The trade-off: **the GUI app must be running** (keep it in the tray). If it isn't,
`cli.py` prints a message and exits with a non-zero status — it never falls back to
poking the hardware directly.

> `cli.py` only needs **PyQt6** (for its D-Bus client) and a **session bus** — both
> already present on a normal KDE session. It doesn't import the rest of the app.

### Command grammar

```
python3 cli.py <feature> <action> [value] [--monitor SEL | --all] [--step N] [--notify]
python3 cli.py list
```

| Feature | Actions | Notes |
|---|---|---|
| `brightness`, `contrast`, `sharpness` | `up`, `down`, `set <0-100>` | `up`/`down` use the monitor's own step; override with `--step`. |
| `gain-red`, `gain-green`, `gain-blue` | `up`, `down`, `set <0-100>` | RGB colour gain. |
| `preset` | `next`, `prev`, `set <name>` | Cycles / picks the merged Colour Preset ("Standard", "Movie"…). |
| `profile` | `load <0-9>`, `next`, `prev` | Applies a saved profile slot; `next`/`prev` cycle your filled slots. |
| `list` | — | Prints the detected monitors (model, serial, bus). |

- **Target** — every command (except `list`) needs a monitor: `--monitor` accepts a
  **serial**, **model**, or **I²C bus number**; or use `--all` for every monitor.
- **`--step N`** — step size for `up`/`down` (defaults to the monitor's calibrated
  step).
- **`--notify`** — pop a desktop notification with the result (e.g. "brightness = 60").
- **Exit codes** — `0` success · `2` no monitor selected · `3` the app isn't running
  · `1` the request was rejected (e.g. feature not supported on that monitor).

### Examples

```bash
python3 cli.py list                                  # what's connected
python3 cli.py brightness up    --monitor 3DMZZB4    # +1 step, by serial
python3 cli.py brightness down  --monitor P2425D --step 5   # -5, by model
python3 cli.py contrast set 50  --all                # exact value, all monitors
python3 cli.py preset next      --all --notify       # cycle colour presets + notify
python3 cli.py profile load 6   --monitor 3DMZZB4    # jump to profile "6. Gaming"
python3 cli.py profile next     --monitor 3DMZZB4    # cycle saved profiles
```

### Binding to a key in KDE Plasma

1. **System Settings ▸ Shortcuts ▸ Custom Shortcuts**.
2. **Edit ▸ New ▸ Global Shortcut ▸ Command/URL**.
3. Set the **Trigger** (the key combo) and the **Action** to the full command, e.g.
   `python3 /path/to/plasma-dell-monitor-support/cli.py brightness up --all`
   (use the **absolute path** to `cli.py`).

Tip: pair `brightness up` / `brightness down` on two keys for a hardware-style
brightness control, or map `profile load 6` / `profile load 7` to switch scenes.

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
| Dell **P2425H** | DisplayPort | Full HD; sharpness works. |
| Dell **P2222H** | DisplayPort | Full HD entry (VGA/DP/HDMI). |
| Dell **P2422H** | DisplayPort | 24″ Full HD (VGA/DP/HDMI); P2222H twin. |
| Dell **U2412M** | DisplayPort | UltraSharp 16:10 (1920×1200); older panel. |
| Dell **P2319H** | DisplayPort | Full HD; has ComfortView. |
| Dell **P2317H** | HDMI | Full HD; older/simpler panel. |
| Dell **P3424WE** | USB-C | 34″ ultrawide; **PIP / PBP** verified working. |
| Dell **P2725HE** | USB-C | Full HD USB-C hub; MST-capable. |

See [`TESTED-MONITORS.md`](TESTED-MONITORS.md) for per-model detail.

## ⚠️ Limitations & known quirks

- **This is early software and may contain bugs.**
- **MST support is experimental.** Enabling/disabling DisplayPort daisy-chaining
  only works on certain monitors, and when an MST chain is active, **monitor
  detection is very slow (~2 minutes)** while `ddcutil` negotiates the link. On
  many monitors MST can only be toggled from the on-screen menu (the app shows a
  note in that case).
- **Some features may not work on some monitors.** Different Dell models (and even
  firmware revisions) expose different DDC/CI features, and some monitors *accept*
  a DDC command but don't actually act on it (a firmware quirk — the setting only
  works from the monitor's own on-screen menu). Examples seen in testing include
  sharpness and aspect ratio on certain models.
- **USB KVM is experimental.** The switch is just a standard input change (the USB
  hub follows the active input); switching to another computer's input makes **this**
  computer lose the picture (a normal KVM switch-back), so it's best driven from the
  computer you're switching *to*. The keyboard/mouse only actually move when a **second**
  computer is connected to the other USB upstream — with one computer, switching input
  just changes the video. The per-input USB-upstream control is reverse-engineered from
  Dell's Windows software; it's verified working on the P3424WE (writes take effect and
  are read-back-checked) but hasn't been validated on other models yet.
- **A feature the monitor has in its OSD is not guaranteed to be controllable over
  DDC** — if the monitor doesn't advertise it, no software (including Dell's own)
  can set it remotely.
- Features KDE Plasma handles natively — HDR, VRR/Adaptive-Sync, resolution,
  refresh rate, rotation — are intentionally **out of scope**.

## Changelog

### 1.3
- **USB KVM** *(experimental)*. On monitors with a built-in USB KVM (advertising
  `0xE7`), a new **KVM** tab lets you switch the active input (the USB hub follows it)
  and **choose which USB upstream port (e.g. USB-C or USB-B) feeds each video input** —
  the `0xE7` bit-packed encoding was reverse-engineered and hardware-verified on the
  P3424WE. Inputs that carry USB themselves (USB-C DP-Alt / Thunderbolt) always use their
  own cable and aren't listed; the keyboard/mouse only actually move when a *second*
  computer is on the other upstream. On monitors using Dell's other USB-KVM scheme you
  can instead set the upstream to *Auto* / pin it to a computer.
- **Monitor audio** *(experimental — not yet verified on hardware)*. On monitors
  with built-in speakers, Volume and Mute controls appear on the Settings tab
  (shown only when the monitor advertises them).

### 1.2
- **Profiles — 10 saved slots per monitor.** Save a monitor's visual settings
  (brightness, contrast, sharpness, RGB gain, colour preset) into a numbered,
  labelled slot ("6. Gaming", "7. Movies") on the Settings tab, and load it from the
  UI or a hotkey. Visual settings only.
- **Command-line control (`cli.py`) for hotkeys.** Adjust brightness/contrast/
  sharpness/RGB gain, cycle colour presets, and load/cycle profiles from the command
  line — via D-Bus to the running app, so it's instant. Bind keys in KDE Custom
  Shortcuts (see the "Command-line control & hotkeys" section).
- **Copy settings to other monitors.** A per-monitor "Copy to other monitors…"
  button that mirrors image settings to your other Dell monitors, with a live
  preview of what will be applied and what's skipped (clamped to each monitor's
  range; unsupported settings and unavailable presets are skipped).
- **Export / import settings.** "Export settings from monitor…" / "Import settings
  to monitor…" on the Settings tab. Export writes a JSON file
  (`Dell-<model>-<serial>.json`); import lets you choose *all settings* (image + OSD
  language) or *image settings only*, and warns (skippably) about anything the target
  monitor doesn't support.
- **Information tab niceties.** A **Copy** button next to the serial number, and an
  **"Export information…"** button that saves everything shown on the Information tab
  to a `.txt` file (`Dell-<model>-<serial>.txt`).
- **Faster monitor detection.** Batches each monitor's reads into a single
  `ddcutil` call and reuses one shared `detect` pass for the Information tabs, cutting
  many redundant `ddcutil` invocations (startup ~5.8s → ~3.9s on a 2× P2425D setup,
  and much more on slow MST chains) — quicker, and gentler on the monitor.

### 1.1
- **PIP / PBP support** — mode, sub-window input, and size/position toggles
  (verified on the P3424WE).
- **MST (Multi-Stream Transport) support** — DisplayPort daisy-chaining
  enable/disable. **Experimental:** works on some monitors only, and detection is
  **very slow (~2 minutes)** while an MST chain is active; on many panels MST is
  OSD-only (the app says so).
- **Information tab** — read-only per-monitor identity/status (model, serial,
  firmware, panel technology, connection, etc.).
- **Retry detection** — re-scan for monitors from the app without restarting.
- **Factory reset** button — restore a monitor to factory defaults over DDC/CI.
- **More tested monitors** — added P2425H, P2222H, P2422H, U2412M (now 9 verified
  models).

### 1.0
- Initial release: per-monitor Brightness, Contrast, Sharpness, RGB gain, merged
  Colour Preset, Input Source, OSD Language, Power; set-then-verify; range
  calibration; custom input labels; system tray.

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
