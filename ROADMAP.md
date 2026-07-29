# Roadmap

Features planned for future releases. This is a wishlist / direction, not a
promise — priorities may change, and some items depend on what individual Dell
monitors actually expose over DDC/CI (firmware varies between models).

*Currently shipping (1.3):* per-monitor Brightness, Contrast, Sharpness, RGB gain,
Colour Preset, Input Source, OSD Language, Power Mode; per-monitor sub-tabs with an
**Information** tab; **PIP / PBP** and **MST** (experimental) support; optional range
calibration; custom (app-side) input labels; factory reset; retry detection;
**copy settings to other monitors**; **export / import settings** (save a monitor's
settings to a file and apply them to the same or another monitor); **profiles** (10
per-monitor slots); **command-line control** for hotkeys; system-tray quick controls;
a basic USB-C Prioritization control on monitors that support it; **monitor audio**
(volume / mute, experimental); and a **USB KVM** tab — input switch plus per-input
USB-upstream pairing (verified on the P3424WE), or Auto/per-computer on monitors using
Dell's other USB-KVM scheme *(experimental)*.

## Planned

- **Gaming controls** *(needs a gaming Dell to develop/test)*.
  Response time / overdrive, dark stabilizer, on-screen crosshair / timer / FPS
  counter, and Vision Engine — on Alienware / gaming monitors that expose them.

- **Monitor shortcut-button configuration** *(needs a monitor with configurable
  buttons + validation)*. Assign what the monitor's physical shortcut keys do.

- **Extra display options / auto-brightness** *(model-dependent; needs validation)*.
  Miscellaneous per-monitor toggles and, on monitors with an ambient-light sensor,
  automatic brightness.

- **Faster MST detection.**
  With an MST daisy-chain active, detection currently takes ~2 minutes. Batching
  the DDC reads should cut that down substantially.

- **Monitor-side input renaming.**
  Today you can give inputs friendly names *inside the app*. This would write the
  names into the monitor's own on-screen menu, like Dell's Windows software does.

- **Wider monitor coverage.**
  More tested Dell models, and support for community-reported monitors and any
  model-specific features they expose.

## Improvements

- Optional autostart / start-minimised-to-tray on login.
- Confirmation that write-only settings (like USB-C Prioritization) took effect.

## Out of scope

These are intentionally **not** planned, because they aren't monitor DDC/CI
features — KDE Plasma / your GPU driver already handle them:

- HDR enable/disable, VRR / Adaptive-Sync
- Resolution, refresh rate, screen rotation
- MST *multi-monitor topology* management (beyond the enable/disable switch)
- **Network KVM** (sharing keyboard/mouse across PCs over the LAN). This isn't a
  monitor/DDC feature — it's a network input-sharing service, already handled well
  by dedicated tools like **Input Leap** / **Deskflow**. The app may *detect* an
  NKVM-capable monitor and point you to those.

Some settings a monitor shows in its own OSD menu also **can't** be controlled
over DDC/CI at all (a firmware limitation) — those can't be added regardless.

## Want something added?

Open an issue with your monitor model and the feature you'd like — or report a
monitor that works (or a feature that doesn't) so it can be added or investigated.
