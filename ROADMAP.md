# Roadmap

Features planned for future releases. This is a wishlist / direction, not a
promise — priorities may change, and some items depend on what individual Dell
monitors actually expose over DDC/CI (firmware varies between models).

*Currently shipping:* per-monitor Brightness, Contrast, Sharpness, RGB gain,
Colour Preset, Input Source, OSD Language, Power Mode, optional range calibration,
custom (app-side) input labels, system-tray quick controls, and a basic USB-C
Prioritization control on monitors that support it.

## Planned

- **Profiles — save & load settings across monitors.**
  Save a monitor's current settings as a named profile, then apply it to another
  monitor — handy when switching workplaces or swapping displays. If the target
  monitor doesn't support a particular setting, it's skipped and you're told which
  ones were skipped.

- **MST (Multi-Stream Transport) enable/disable.**
  Turn DisplayPort daisy-chaining on or off from the app, on monitors that
  support it. (Note: managing the resulting multi-monitor chain itself stays out
  of scope — this is just the on/off switch.)

- **PIP / PBP (Picture-in-Picture / Picture-by-Picture).**
  Control the second-window layout and its input source on monitors that offer
  it (e.g. large ultrawides).

- **Monitor-side input renaming.**
  Today you can give inputs friendly names *inside the app*. This would write the
  names into the monitor's own on-screen menu, like Dell's Windows software does.

- **Wider monitor coverage.**
  More tested Dell models, and support for community-reported monitors and any
  model-specific features they expose.

## Improvements

- Smoother detection feedback (so it never looks frozen while probing).
- Optional autostart / start-minimised-to-tray on login.
- Confirmation that write-only settings (like USB-C Prioritization) took effect.

## Out of scope

These are intentionally **not** planned, because they aren't monitor DDC/CI
features — KDE Plasma / your GPU driver already handle them:

- HDR enable/disable, VRR / Adaptive-Sync
- Resolution, refresh rate, screen rotation
- MST *multi-monitor topology* management (beyond the enable/disable switch)

Some settings a monitor shows in its own OSD menu also **can't** be controlled
over DDC/CI at all (a firmware limitation) — those can't be added regardless.

## Want something added?

Open an issue with your monitor model and the feature you'd like — or report a
monitor that works (or a feature that doesn't) so it can be added or investigated.
