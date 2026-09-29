#!/usr/bin/env bash
# xreal-hud one-time setup for CachyOS/Steam Deck.
# No pip, no venv, no PyInstaller (AUR-only on Arch and unnecessary here):
# system deps via pacman + a launcher script that Steam runs directly.
# Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

DEST="$HOME/.local/share/xreal-hud"

echo "== 1/3 system packages (pacman) =="
sudo pacman -S --needed --noconfirm python-pygame python-hidapi hidapi

echo "== 2/3 install app to $DEST =="
python3 - <<'PYEOF'
import importlib, sys
for m in ("pygame", "hid"):
    try:
        importlib.import_module(m)
    except ImportError:
        sys.exit(f"missing python module: {m} - pacman should have provided it")
PYEOF
mkdir -p "$DEST"
cp hud_app.py imu.py "$DEST/"
cat > "$DEST/xreal-hud" <<'LAUNCHEOF'
#!/usr/bin/env bash
cd "$HOME/.local/share/xreal-hud"
exec python3 hud_app.py "$@"
LAUNCHEOF
chmod +x "$DEST/xreal-hud"

echo "== 3/3 udev rule (glasses IMU access) =="
sudo cp 50-xreal-hud.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

echo
echo "done. Quick check (Ctrl-C to exit):"
echo "  $DEST/xreal-hud --demo"
echo
echo "Then add to Steam (desktop mode):"
echo "  Games -> Add a Non-Steam Game -> Browse -> $DEST/xreal-hud"
echo "(if the picker hides it, switch the file dialog to 'All Files')"
echo "Replug the glasses once so the udev rule applies."
