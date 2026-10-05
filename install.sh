#!/usr/bin/env bash
# xreal-hud one-time setup for CachyOS/Steam Deck.
#
# No pip, no venv, no PyInstaller.
#
# Runtime structure:
#
#   main.py       -> application entrypoint
#   hud.py        -> HUD/rendering
#   text.py       -> text rendering
#   menu.py       -> menu/UI
#   attitude.py   -> attitude/orientation presentation
#   gps.py        -> GPS handling
#   imulogic.py   -> IMU lifecycle/reconnect logic
#   imu.py        -> low-level Xreal IMU/HID reader
#
# imu.py and imulogic.py are both installed because:
#   imulogic.py imports ImuReader from imu.py.
#
# Arch has TWO packages providing the Python 'hid' module:
#   python-hidapi
#   python-hid
#
# They conflict with each other.
# This script only installs python-hidapi if no usable 'hid'
# Python module is currently importable.

set -euo pipefail

cd "$(dirname "$0")"

DEST="$HOME/.local/share/xreal-hud"

echo "== 1/3 system packages (pacman) =="

need=()

# pygame is required by the HUD.
if ! pacman -Q python-pygame >/dev/null 2>&1; then
    need+=(python-pygame)
fi

# Only install python-hidapi if Python cannot currently import hid.
if ! python3 -c "import hid" >/dev/null 2>&1; then
    need+=(python-hidapi hidapi)
fi

if ((${#need[@]})); then
    echo "installing: ${need[*]}"
    sudo pacman -S --needed --noconfirm "${need[@]}"
fi

echo "== 2/3 install app to $DEST =="

# Verify Python dependencies before installing the application.
python3 - <<'PYEOF'
import importlib
import sys

for module in ("pygame", "hid"):
    try:
        importlib.import_module(module)
    except ImportError:
        sys.exit(
            f"missing Python module: {module} - "
            "install python-pygame and python-hidapi "
            "(or python-hid)"
        )
PYEOF

mkdir -p "$DEST"

# Install the actual application components.
#
# hud_app.py is intentionally NOT installed.
cp \
    main.py \
    hud.py \
    text.py \
    menu.py \
    attitude.py \
    gps.py \
    imu.py \
    imulogic.py \
    "$DEST/"

# Create the executable launcher.
cat > "$DEST/xreal-hud" <<'LAUNCHEOF'
#!/usr/bin/env bash

cd "$HOME/.local/share/xreal-hud"

exec python3 main.py "$@"
LAUNCHEOF

chmod +x "$DEST/xreal-hud"

echo "== 3/3 udev rule (glasses IMU access) =="

sudo cp 50-xreal-hud.rules /etc/udev/rules.d/

sudo udevadm control --reload-rules
sudo udevadm trigger

echo
echo "========================================"
echo "xreal-hud installation complete."
echo "========================================"
echo
echo "Quick test:"
echo
echo "  $DEST/xreal-hud"
echo
echo "Then add to Steam (desktop mode):"
echo
echo "  Games -> Add a Non-Steam Game -> Browse"
echo
echo "Select:"
echo
echo "  $DEST/xreal-hud"
echo
echo "If the file picker hides it, switch the dialog"
echo "to 'All Files'."
echo
echo "Replug the glasses once so the udev rule applies."
echo
