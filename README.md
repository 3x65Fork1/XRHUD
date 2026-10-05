# XRHUD — Iron-Man HUD for Xreal Air on Steam Deck

A native Python/pygame HUD for Xreal Air glasses running from a Steam Deck.

XRHUD reads the glasses' IMU over USB, performs attitude fusion with a Madgwick filter, and renders a fighter-jet-style HUD with pitch, bank, speed, and GPS information.

---

## System Overview

The system consists of three main components:

- **Xreal Air glasses** — provides IMU (inertial measurement unit) data via USB-C
- **Steam Deck** — runs the HUD application and fuses IMU data; listens for GPS packets on UDP port 8676
- **Android phone** — supplies GPS location and speed data via Wi-Fi UDP link

Data flows from the glasses to the Deck for orientation, and from the phone to the Deck for GPS/speed information. The HUD renders on the Xreal Air's optical display.

---

## Features

- **Native pygame/SDL2 HUD** — no web server, no browser required
- **Xreal Air IMU support** — direct USB HID access to 9-axis sensors
- **Madgwick attitude fusion** — robust orientation estimation
- **Pitch ladder** — visual pitch reference
- **Bank indicator** — roll angle display
- **Accelerometer / G-force display** — instantaneous acceleration readout
- **GPS position** — latitude/longitude from Android companion app
- **GPS-derived speed** — m/s converted to mph or km/h
- **Phone-to-Deck UDP GPS link** — wireless GPS data transfer
- **Steam Gaming Mode support** — runs as a native Steam game
- **Gamepad controls** — full controller support
- **Demo mode** — test without glasses using synthetic data

---

## GPS and Speed

### Data Source

GPS data comes from an Android phone running **XRHUDAPP** (available at [https://github.com/3x65Fork1/XRHUDAPP](https://github.com/3x65Fork1/XRHUDAPP)).

The phone obtains its location and speed directly from Android's GPS/location APIs and sends the data to the Steam Deck over UDP.

### UDP Listener

The Deck listens on **UDP port 8676**.

### Packet Format

The phone sends JSON packets:

```json
{
  "lat": 51.5073,
  "lon": -0.1277,
  "speed": 13.4,
  "sats": 18,
  "hdop": "-"
}
```

**Note:** `speed` is supplied in **m/s**.

XRHUD converts the value to the currently selected display unit (km/h or mph).

### Network Setup

The phone and Steam Deck must be able to communicate over the network. A simple setup is:

- **Android phone** — creates a Wi-Fi hotspot
- **Steam Deck** — connects to the hotspot
- **XRHUDAPP** — is configured with the Steam Deck's IP address
- **UDP packets** — sent from phone to `<STEAM_DECK_IP>:8676` (unicast, not broadcast)

### Testing the GPS Connection

On the Steam Deck, monitor incoming GPS packets:

```bash
sudo tcpdump -ni any udp port 8676
```

Start XRHUDAPP on the phone and enable GPS. You should see UDP packets arriving at the Deck.

Verify the HUD is listening:

```bash
ss -lunp | grep 8676
```

---

## IMU

The Xreal Air IMU is read directly from USB by `imu.py`.

### Inspecting Raw Data

```bash
python3 imu.py --dump
```

You should see a stream of accelerometer and gyroscope values.

### Running the Fused IMU

```bash
python3 imu.py
```

Pitch and bank should respond to head movement.

### G-Force Display

The accelerometer data is used for the HUD's G-force display. The IMU provides acceleration independently of the GPS speed system:

- **IMU accelerometer** — fed to the G-force display (instantaneous acceleration)
- **GPS speed** — fed to the speed display (vehicle velocity from phone)

This separation keeps instantaneous acceleration measurement distinct from GPS-derived vehicle speed.

---

## Setup

### Install Dependencies

On Arch Linux / SteamOS:

```bash
sudo pacman -S python-hidapi python-pygame
```

### Install the udev Rule

```bash
sudo cp 50-xreal-hud.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Replug the Xreal Air after installing the rule.

### Install the Application

```bash
./install.sh
```

---

## Demo Mode

Development can be done without the glasses using synthetic IMU motion and fake GPS data:

```bash
./install.sh --demo
```

Or run directly:

```bash
python3 hud_app.py --demo
```

---

## Steam Gaming Mode

`hud_app.py` is a self-contained pygame/SDL2 application with no browser, web server, or PyInstaller bundle required.

After running `./install.sh`, add the launcher as a Non-Steam Game:

```
~/.local/share/xreal-hud/xreal-hud
```

The installer automatically:

- Installs system dependencies
- Installs the application under `~/.local/share/xreal-hud/`
- Creates the `xreal-hud` launcher
- Installs the Xreal udev rule

The HUD can then be launched directly from Steam Gaming Mode.

---

## Controls

| Control | Action |
|---------|--------|
| Gamepad A | Recenter |
| Gamepad Y | Toggle mph / km/h |
| Gamepad X / Start | Menu |
| Gamepad B | Quit |
| c | Recenter |
| u | Toggle units |
| m | Toggle units |
| Esc | Quit |

The selected speed unit is persisted in `~/.config/xreal-hud/unit`.

---

## First-Run Checklist

1. Start the HUD with your head reasonably still (IMU performs initial 1g calibration from first samples)
2. Run `python3 imu.py --dump` and verify IMU data streams
3. Run `python3 imu.py` and verify sensible pitch/bank values
4. Launch `hud_app.py`
5. Put the glasses on and verify the pitch ladder responds
6. Press **c** or **Gamepad A** to recenter
7. Start **XRHUDAPP** on the phone
8. Enter the Steam Deck's IP address into XRHUDAPP
9. Start GPS on the phone
10. Confirm GPS/speed data appears on the HUD
11. If pitch or bank axes are incorrect, adjust the relevant corrections in `euler_deg()` in `imu.py`

---

## Hardware Requirements

- **Xreal Air glasses**
- **Steam Deck**
- **Android phone with GPS**
- **USB-C cable** — Xreal Air to Steam Deck
- **Wi-Fi connection** — phone to Steam Deck

---

## Architecture

XRHUD intentionally separates the two major sensor systems to keep orientation independent from GPS velocity.

### IMU / Orientation Pipeline

Xreal Air IMU → `imu.py` (HID reader + Madgwick fusion) → Pitch / Bank / G-force → `hud_app.py` → Xreal Air display

### GPS / Speed Pipeline

Android Phone (GPS + GPS speed) → UDP :8676 → `hud_app.py` → Speed / GPS display on Xreal Air

This architecture ensures:

- **GPS speed does not depend on IMU integration** — velocity comes directly from the phone's GPS receiver
- **IMU handles instantaneous acceleration** — G-force is always fresh and independent of GPS
- **Separation of concerns** — orientation and speed are decoupled systems

---

## Notes

- **3DoF limitation** — The Xreal Air provides 3DoF (pitch, roll, yaw) orientation, so yaw can drift over time. Recenter when necessary.
- **Transparent display** — Black pixels are effectively transparent on the optical see-through display
- **Resolution** — The HUD is designed around the Xreal Air's 1080p display
- **Frame rates** — IMU fusion runs at the rate supplied by the glasses; the pygame HUD renders at approximately 60 Hz
- **Phone role** — The Android phone is only responsible for GPS/location data; it does not replace the Xreal IMU

---

## License

This project is licensed under the **MIT License**.

You are free to use, copy, modify, merge, publish, distribute, sublicense, and sell this software, provided that the original copyright notice and license are retained.
