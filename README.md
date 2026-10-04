XRHUD — Iron-Man HUD for Xreal Air on Steam Deck

A native Python/pygame HUD for Xreal Air glasses running from a Steam Deck.

XRHUD reads the glasses' IMU over USB, performs attitude fusion with a Madgwick filter, and renders a fighter-jet-style HUD with pitch, bank, speed, and GPS information.

System overview
Xreal Air
    │
    │ USB-C
    ▼
Steam Deck
    │
    ├── imu.py
    │     └── HID IMU reader + Madgwick fusion
    │
    └── hud_app.py
          ├── Pitch / bank
          ├── G-force
          ├── Speed
          └── GPS
               ▲
               │ UDP :8676
               │
          Android phone
               │
               └── XRHUDAPP
                    └── GPS + GPS-derived speed


The Android companion app is available here:

XRHUDAPP: https://github.com/3x65Fork1/XRHUDAPP

Features

Native pygame/SDL2 HUD

Xreal Air IMU support

Madgwick attitude fusion

Pitch ladder

Bank indicator

Accelerometer / G-force display

GPS position

GPS-derived speed

MPH / km/h display

Phone-to-Deck UDP GPS link

Steam Gaming Mode support

Gamepad controls

Demo mode for development without glasses

GPS and speed

GPS data comes from an Android phone running XRHUDAPP.

The phone obtains its location and speed directly from Android's GPS/location APIs and sends the data to the Steam Deck over UDP.

The Deck listens on:

UDP :8676


The packet format is JSON:

{
  "lat": 51.5073,
  "lon": -0.1277,
  "speed": 13.4,
  "sats": 18,
  "hdop": "-"
}


speed is supplied in m/s.

XRHUD converts the value to the currently selected display unit:

m/s → km/h
m/s → mph

Network setup

The phone and Steam Deck need to be able to communicate over the network.

A simple setup is:

Android phone
    │
    │ Wi-Fi hotspot
    ▼
Steam Deck


The Steam Deck's IP address is entered into XRHUDAPP.

The Android app sends packets specifically to:

<STEAM_DECK_IP>:8676


It does not broadcast GPS data to the network.

Testing the GPS connection

On the Steam Deck:

sudo tcpdump -ni any udp port 8676


Start XRHUDAPP on the phone and enable GPS.

You should see UDP packets arriving at the Deck.

You can also verify that the HUD is listening:

ss -lunp | grep 8676

IMU

The Xreal Air IMU is read directly from USB by imu.py.

Raw data can be inspected with:

python3 imu.py --dump


You should see a stream of accelerometer and gyroscope values.

Run the fused IMU:

python3 imu.py


Pitch and bank should respond to head movement.

G-force

The accelerometer data is used for the HUD's G-force display.

The IMU provides acceleration independently of the GPS speed system:

IMU
 └── acceleration → G-force display

GPS
 └── GPS speed → speed display


This keeps instantaneous acceleration/G-force measurement separate from GPS-derived vehicle speed.

Setup

Install dependencies:

sudo pacman -S python-hidapi python-pygame


Install the udev rule:

sudo cp 50-xreal-hud.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger


Replug the Xreal Air after installing the rule.

Then:

./install.sh

Demo mode

Development can be done without the glasses:

./install.sh --demo


or:

python3 hud_app.py --demo


This runs the HUD using synthetic IMU motion and fake GPS data.

Steam Gaming Mode

hud_app.py is a self-contained pygame/SDL2 application.

There is no browser, web server, or PyInstaller bundle required.

Run:

./install.sh


The installer:

Installs the required system dependencies

Installs the application under ~/.local/share/xreal-hud/

Creates the xreal-hud launcher

Installs the Xreal udev rule

Add the launcher as a Non-Steam Game:

~/.local/share/xreal-hud/xreal-hud


It can then be launched from Steam Gaming Mode.

Controls
Control	Action
Gamepad A	Recenter
Gamepad Y	Toggle mph / km/h
Gamepad X / Start	Menu
Gamepad B	Quit
c	Recenter
u	Toggle units
m	Toggle units
Esc	Quit

The selected speed unit is persisted in:

~/.config/xreal-hud/unit

First-run checklist

Start the HUD with your head reasonably still.

The IMU performs its initial 1g calibration from the first samples.

Check:

python3 imu.py --dump produces IMU data.

python3 imu.py produces sensible pitch/bank values.

Launch hud_app.py.

Put the glasses on and verify the pitch ladder.

Press c or Gamepad A to recenter.

Start XRHUDAPP on the phone.

Enter the Steam Deck's IP address.

Start GPS.

Confirm GPS/speed data appears on the HUD.

If pitch or bank axes are incorrect, the relevant corrections are in euler_deg() in imu.py.

Hardware

Xreal Air glasses

Steam Deck

Android phone with GPS

USB-C connection between Xreal Air and Steam Deck

Wi-Fi connection between phone and Steam Deck

Architecture

XRHUD intentionally separates the two major sensor systems:

                ┌──────────────────┐
                │   Xreal Air IMU  │
                └────────┬─────────┘
                         │ USB
                         ▼
                    ┌─────────┐
                    │ imu.py  │
                    └────┬────┘
                         │
                         ▼
                  Pitch / Bank / G
                         │
                         ▼
                    ┌─────────┐
                    │hud_app.py│
                    └────┬────┘
                         │
                         ▼
                     Xreal Air


┌──────────────────┐
│  Android Phone   │
│                  │
│ GPS + GPS speed  │
└────────┬─────────┘
         │ UDP :8676
         ▼
   ┌─────────────┐
   │ hud_app.py  │
   └──────┬──────┘
          │
          ▼
      Speed / GPS


This means GPS speed does not depend on integrating the IMU accelerometer, while the IMU remains responsible for the HUD's instantaneous acceleration/G-force information.

Notes

The Xreal Air provides 3DoF orientation, so yaw can drift.

Recenter when necessary.

Black pixels are effectively transparent on the optical see-through display.

The HUD is designed around the Xreal Air's 1080p display.

IMU fusion runs at the rate supplied by the glasses, while the pygame HUD renders at approximately 60 Hz.

The Android phone is only responsible for GPS/location data; it does not replace the Xreal IMU.

License

See the repository for licensing information.
