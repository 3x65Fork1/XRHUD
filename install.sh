#!/usr/bin/env bash
# xreal-hud one-time setup for CachyOS/Steam Deck.
# No pip, no venv, no PyInstaller (AUR-only on Arch and unnecessary here).
# Note on hid bindings: Arch has TWO packages providing the 'hid' python
# module - python-hidapi and python-hid - and they conflict with each other.
# This script does not care which you have: it installs python-hidapi only
# if NO 'hid' module is importable. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

DEST="$HOME/.local/share/xreal-hud"

echo "== 1/3 system packages (pacman) =="
need=()
pacman -Q python-pygame >/dev/null 2>&1 || need+=(python-pygame)
python3 -c "import hid" 2>/dev/null || need+=(python-hidapi hidapi)
if ((${#need[@]})); then
  echo "installing: ${need[*]}"
  sudo pacman -S --needed --noconfirm "${need[@]}"
fi

echo "== 2/3 install app to $DEST =="
python3 - <<'PYEOF'
import importlib, sys
for m in ("pygame", "hid"):
    try:
        importlib.import_module(m)
    except ImportError:
        sys.exit(f"missing python module: {m} - install python-pygame and python-hidapi (or python-hid)")
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
