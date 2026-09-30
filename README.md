# deck-hud — Iron-Man HUD for Xreal Air on Steam Deck (CachyOS)

Deck-side of the HUD: reads the glasses' IMU over USB, fuses attitude with a
Madgwick filter, and renders a fighter-jet HUD (pitch ladder + bank arc,
speed, clock, GPS tile) in a fullscreen browser on the glasses' display.

```
Xreal Air ──USB-C──► Steam Deck (this code)
  IMU @ ~200-1000 Hz     imu.py   (HID reader + Madgwick fusion)
  Video @ 1080p          server.py (WebSocket broadcaster + static + GPS UDP)
                         firefox --kiosk -> http://127.0.0.1:8675/

GrapheneOS phone ──Wi-Fi──► Deck UDP :8676   (built next phase)
  GPS @ 1 Hz              {"lat","lon","speed"(m/s),"sats","hdop"}
```

## Setup

```bash
sudo pacman -S python-hidapi python-websockets firefox
sudo cp 50-xreal-hud.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# replug the glasses once so the rule applies
./run.sh
```

No glasses handy, or developing at a desk? `./run.sh --demo` runs the full
pipeline on synthetic motion + fake GPS.

If you have wheaney's `xr-driver` installed, stop it first — it owns the HID
device: `sudo systemctl stop xr-driver`.

## First-run checks (do these in order)

1. **Raw stream:** `python3 imu.py --dump` — you should see a flood of
   `gx=… gy=… gz=…` lines. Nothing? Check the udev rule and that the glasses
   are plugged into the Deck's USB-C port (the top port — the bottom one is
   for the dock/charging only).
2. **Fused angles:** `python3 imu.py` — tilt your head forward; `pitch` should
   track. Roll your head; `bank` should track. If axes are swapped or
   inverted, adjust `AXIS` (and optionally add sign flips) at the top of the
   `<script>` block in `hud.html`, and `euler_deg()` in `imu.py` if the raw
   angles themselves are wrong.
3. **Units:** if pitch/bank numerically look ~57x too small or large, the gyro
   scaling changed — check `GYRO_DPS_TO_RAD` in `imu.py`. The 1g reference is
   auto-calibrated at startup from the first ~100 samples, so start the HUD
   with your head still.

## Notes

- **Recenter:** press `c` on the Deck (or the menu button) — the Air has 3DoF
  only, so yaw drifts slowly; recenter whenever it annoys you.
- **GPS tile** stays in "awaiting phone link" until the GrapheneOS streamer
  (next phase) pushes JSON to UDP `8676`. Speed arrives in m/s; the HUD
  converts to mph/kmh.
- **Protocol source:** the packet codec mirrors the reference C driver
  (thejackimonster's xreal-imu, vendored in DannyDesert/XReal-Ultrawide):
  standard CRC32 framing, full init handshake, magnetometer decoded but
  unused (it degrades fusion on this hardware).
- **CPU:** fusion runs at whatever rate the glasses stream (up to ~1 kHz) and
  costs a few percent of one core; the browser gets 60 Hz updates.
- **Black background:** on the Air's optical see-through panels, black pixels
  are transparent — the HUD floats over the real world. Lines are drawn at
  1.5–2.4 px to survive the microdisplay's pixel grid.

## Steam / gaming mode (native app, no browser)

`hud_app.py` is a self-contained pygame/SDL2 app: same IMU + fusion code,
HUD drawn natively, gamepad support, no Firefox and no `server.py` needed.
It listens for phone GPS on UDP :8676 exactly as before.

Install once (no pip, no venv, no PyInstaller):

```bash
./install.sh
```

What it does: makes sure `python-pygame` is installed and that SOME `hid`
binding is importable (Arch ships two conflicting ones - `python-hidapi` and
`python-hid` - either works, and it will not force one if the other is
present), copies the app to `~/.local/share/xreal-hud/`, creates the
`xreal-hud` launcher Steam runs, and installs the udev rule. Re-run it any
time after pulling changes.

Then: Steam (desktop mode) -> Games -> Add a Non-Steam Game -> browse to
`~/.local/share/xreal-hud/xreal-hud` (if the picker hides it, set the file
dialog to "All Files"). It appears in gaming mode from then on. Do NOT also
run wheaney's xr-driver/Breezy - same HID-device conflict as before; with
this app you don't need them at all.

Why a launcher script instead of a bundled binary: the dependencies are
pacman-managed system packages (updated with the system, nothing to rot),
so PyInstaller would only add a build step and a failure mode. The launcher
is the game as far as Steam is concerned. Uninstall: delete
`~/.local/share/xreal-hud/` and the Steam shortcut.

Gaming mode display notes:
- With the Air plugged in, the gamescope session renders on the external
  (glasses) output. If a launch ever comes up on the Deck panel instead,
  replug the glasses before launching, or ask and we'll pin the output.
- Controls: gamepad A = recenter, Y = mph/kmh, X/Start = menu, B = quit;
  keyboard c/u/m/esc mirror them. Unit preference persists in
  ~/.config/xreal-hud/unit.
- For tinkering without reinstalling: `python3 hud_app.py --demo` from the
  repo folder still works.

First-run checklist (app mode): launch with the glasses on your head and head
still (1g auto-calibration samples the first ~100 packets), check the ladder
tracks pitch/bank, press A to recenter if heading drift bugs you. Axis fixes,
if needed, live in `euler_deg()` in imu.py as before.
