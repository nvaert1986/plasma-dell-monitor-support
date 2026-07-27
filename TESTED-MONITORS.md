# Tested Monitors

These are the Dell monitors this app has been tried on, and what works on each.
Everything was tested on Linux (KDE Plasma) using `ddcutil` to talk to the monitor
over its DisplayPort/HDMI/USB-C cable (the DDC/CI channel).

The app is **capability-driven**: it asks each monitor what it supports and only
shows controls for those features. So most Dell monitors should work even if they
aren't in this list — this is just what's been confirmed by hand.

> Looking for the deep detail (exact opcodes, capability strings, RE notes)?
> See [`TESTED-MONITORS-TECHNICAL.md`](TESTED-MONITORS-TECHNICAL.md).

## At a glance

| Model | Connection | Resolution | Highlights |
|---|---|---|---|
| **P2425D** ×2 | DisplayPort | 2560×1440 | The baseline. Everything standard works. |
| **P2425H** | DisplayPort | 1920×1080 | 24″; sharpness works. |
| **P2222H** | DisplayPort | 1920×1080 | Entry 22″; VGA/DP/HDMI inputs. |
| **U2412M** | DisplayPort | 1920×1200 | Older UltraSharp, 16:10; VGA/DVI/DP. |
| **P2319H** | DisplayPort | 1920×1080 | Has the ComfortView blue-light preset. |
| **P2317H** | HDMI | 1920×1080 | Older/simpler panel. |
| **P3424WE** | USB-C | 3440×1440 | 34″ ultrawide; **PIP / PBP works**. |
| **P2725HE** | USB-C | 1920×1080 | USB-C hub; MST-capable. |

**Works on every model above:** brightness, contrast, RGB colour gain, colour
preset, input source, and power. Other features vary by model (see below).

## What "works" and "doesn't work" means here

A monitor can show a setting in its own on-screen menu (OSD) but still **not** let
software change it over the cable. That's a firmware choice by the monitor, not a
bug in the app — if the monitor doesn't offer a setting over DDC/CI, no software
(including Dell's own Windows tool) can change it remotely. Where we've seen this,
it's called out below.

## Per-model notes

### P2425D (the baseline, 27″ QHD)
All the standard controls work. It has no ComfortView (blue-light) preset. One
quirk: over the cable it won't go as low as the on-screen menu allows — contrast
stops at 25, colour gain at 30, and sharpness moves in steps of 10. The app's
optional **"Calibrate ranges"** feature learns these real limits so the sliders
match the hardware.

### P2425H (24″ Full HD)
Same as the P2222H below, **plus** working sharpness. No ComfortView. Writes
confirmed working.

### P2222H (entry 22″ Full HD)
Standard controls work (writes confirmed). Inputs are VGA, DisplayPort and HDMI.
It has an OSD-language control but **no sharpness** over the cable.

### U2412M (older 24″ UltraSharp, 16:10 / 1920×1200)
The oldest panel tested. Brightness, contrast, colour gain, colour preset, input
(VGA/DVI/DP) and power all work, and **factory reset works**. What *doesn't* work
over the cable on this one:
- **Sharpness** and **Gamma (PC/Mac)** are on-screen-menu only — changing them
  moves no cable setting at all.
- **OSD Language** can be *read* but not *changed* over the cable.

Everything it genuinely supports is handled automatically — no special code needed.

### P2319H (Full HD)
Has the **ComfortView** blue-light preset, folded into the Colour Preset dropdown.
Confirmed working (tested on Arch Linux).

### P2317H (older 23″)
A simpler, older panel: ComfortView yes, but **no sharpness** and **no
OSD-language** control over the cable.

### P3424WE (34″ USB-C ultrawide)
Standard controls work over its USB-C input, and **PIP / PBP (Picture-in-Picture /
Picture-by-Picture) is fully working** — you can set the mode, pick the second
window's input, and toggle its size/position. Note that switching PIP/PBP mode
briefly blanks the screen while the panel re-initialises (normal), and a second
picture only appears when a second input is actually plugged in. Sharpness and
aspect ratio on this model are on-screen-menu only (not controllable over the cable).

### P2725HE (USB-C hub monitor)
Standard controls work, including sharpness. It's **MST-capable** (DisplayPort
daisy-chaining) and has a USB-C bandwidth-priority setting. On this particular
model, **turning MST on/off can only be done from the monitor's own menu**, not
from the app — so the app shows a note explaining that instead of a toggle. (MST
on/off over the cable is only offered on monitors that actually support it that
way.)

## Want to add your monitor?

If your Dell model isn't listed, it will very likely still work. To help get it
confirmed and documented, run the included `collect-monitor-info.sh` script (it
only *reads* from the monitor) and share the report, or open an issue with your
model and what does / doesn't work.
