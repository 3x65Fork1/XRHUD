#!/usr/bin/env python3
"""
xreal-hud — self-contained native HUD app for the Xreal Air / Steam Deck.

Single binary (pyinstaller), launched as a non-Steam game from gaming mode.
Renders the fighter-jet HUD fullscreen; reads the glasses IMU directly over
USB HID (imu.py), listens for phone GPS on UDP :8676.

Controls:  c / gamepad-A = recenter    u / gamepad-Y = mph-kmh
           m / gamepad-X = menu        b / gamepad-B = quit
           esc = quit
"""

import argparse
import json
import math
import os
import socket
import sys
import threading
import time
from datetime import datetime

import pygame

try:
    from imu import ImuReader
except Exception:
    ImuReader = None

VW, VH = 640.0, 360.0          # logical HUD coordinate space (16:9)
CX, CY = 320.0, 190.0
CYAN = (63, 217, 255)
AMBER = (255, 179, 64)
DIM = (138, 154, 165)
BG = (0, 0, 0)
RED = (255, 93, 93)
GPS_STALE_S = 6.0


# --------------------------------------------------------------------- data

class GpsListener(threading.Thread):
    """Phone GPS arrives as UDP JSON: {"lat","lon","speed","sats","hdop"}."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.data, self.t = None, 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(1.0)

    def run(self):
        while True:
            try:
                raw, _ = self.sock.recvfrom(2048)
                self.data = json.loads(raw.decode("utf-8", "replace"))
                self.t = time.monotonic()
            except socket.timeout:
                continue
            except Exception:
                time.sleep(1.0)


class ImuManager:
    """Creates an ImuReader once the glasses answer; retries until then."""

    def __init__(self):
        self.reader = None
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def _work(self):
        while True:
            if ImuReader is None:
                print("[hud] imu.py unavailable", flush=True)
                time.sleep(3)
                continue
            r = ImuReader()
            r.start()
            deadline = time.monotonic() + 5.0
            while r.is_alive() and not r.state.get("ok") and time.monotonic() < deadline:
                time.sleep(0.25)
            if r.state.get("ok"):
                print("[hud] imu stream live", flush=True)
                self.reader = r
                r.join()          # runs until stop() or error
            else:
                r.stop()
                time.sleep(2.0)
            self.reader = None
            print("[hud] imu lost - retrying", flush=True)

    @property
    def state(self):
        if self.reader is not None:
            return self.reader.state
        return {"ok": False, "hz": 0.0, "pitch": 0.0, "bank": 0.0,
                "yaw": 0.0, "g": 1.0}

    def recenter(self):
        if self.reader is not None:
            self.reader.recenter()


# ---------------------------------------------------------------- geometry

LADDER_DEGS = [-30, -20, -10, 0, 10, 20, 30]


def xf(x, y, pitch, bank):
    """pitch/bank (deg) -> screen transform for world-fixed ladder lines."""
    y += pitch * 3.0
    a = math.radians(-bank)
    dx, dy = x - CX, y - CY
    return (CX + dx * math.cos(a) - dy * math.sin(a),
            CY + dx * math.sin(a) + dy * math.cos(a))


def rotp(x, y, deg):
    a = math.radians(deg)
    dx, dy = x - CX, y - CY
    return (CX + dx * math.cos(a) - dy * math.sin(a),
            CY + dx * math.sin(a) + dy * math.cos(a))


def bank_ticks():
    segs = []
    for b in range(-60, 61, 10):
        a = math.radians(270 + b)
        r1 = 102.0
        r2 = 118.0 if b % 30 == 0 else 109.0
        segs.append(((CX + r1 * math.cos(a), CY + r1 * math.sin(a)),
                     (CX + r2 * math.cos(a), CY + r2 * math.sin(a))))
    return segs


BANK_TICKS = bank_ticks()


# --------------------------------------------------------------------- app

class Hud:
    def __init__(self, demo=False, gps_port=8676):
        pygame.init()
        pygame.joystick.init()
        self.pads = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self.pads.append(j)

        info = pygame.display.Info()
        self.w, self.h = info.current_w, info.current_h
        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.display.set_caption("xreal hud")
        pygame.mouse.set_visible(False)

        self.SS = 2 if self.w >= 1920 else 1
        self.ss = pygame.Surface((int(VW * 3), int(VH * 3)), pygame.SRCALPHA)
        self.k = (VW * 3) / VW            # logical -> supersample px

        self.fonts = {}
        self.demo = demo
        self.gps = GpsListener(gps_port)
        self.gps.start()
        self.imu = None if demo else ImuManager()
        self.menu = True                  # visible at start so controls are discoverable
        self.unit = self._load_unit()
        self.smooth = {"pitch": 0.0, "bank": 0.0}
        self.clock = pygame.time.Clock()
        self.t0 = time.monotonic()

    # -- tiny prefs --------------------------------------------------------
    def _cfg(self):
        return os.path.expanduser("~/.config/xreal-hud/unit")

    def _load_unit(self):
        try:
            return open(self._cfg()).read().strip() or "mph"
        except Exception:
            return "mph"

    def _save_unit(self):
        try:
            os.makedirs(os.path.dirname(self._cfg()), exist_ok=True)
            open(self._cfg(), "w").write(self.unit)
        except Exception:
            pass

    # -- text helper ---------------------------------------------------------
    def font(self, logical_px, bold=False):
        px = int(logical_px * self.k * 0.72)
        key = (px, bold)
        if key not in self.fonts:
            self.fonts[key] = pygame.font.SysFont(["dejavusans", "liberationsans", None],
                                                  px, bold=bold)
        return self.fonts[key]

    def text(self, s, txt, color, x, y, size=12, bold=False, anchor="la"):
        surf = self.font(size, bold).render(txt, True, color)
        rect = surf.get_rect()
        if anchor[0] == "r":
            rect.topright = (int(x * self.k), int(y * self.k))
        elif anchor[0] == "c":
            rect.midtop = (int(x * self.k), int(y * self.k))
        else:
            rect.topleft = (int(x * self.k), int(y * self.k))
        s.blit(surf, rect)

    # -- input ----------------------------------------------------------------
    def handle(self, ev, imu):
        if ev.type == pygame.QUIT:
            return False
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_ESCAPE:
                return False
            if ev.key == pygame.K_c:
                imu.recenter()
            elif ev.key == pygame.K_u:
                self.unit = "kmh" if self.unit == "mph" else "mph"
                self._save_unit()
            elif ev.key == pygame.K_m:
                self.menu = not self.menu
        if ev.type == pygame.JOYBUTTONDOWN:
            b = ev.button
            if b in (1,):
                return False
            if b in (0,):
                imu.recenter()
            elif b in (3,):
                self.unit = "kmh" if self.unit == "mph" else "mph"
                self._save_unit()
            elif b in (2, 7):
                self.menu = not self.menu
        return True

    # -- frame ----------------------------------------------------------------
    def imu_state(self, t):
        if self.demo:
            return {"ok": True, "hz": 60.0,
                    "pitch": math.sin(t / 2.1) * 9.0,
                    "bank": math.sin(t / 1.3) * 22.0,
                    "yaw": math.sin(t / 3.7) * 40.0,
                    "g": 1.0 + math.sin(t / 0.9) * 0.12}
        return self.imu.state

    def demo_gps(self, t):
        return {"lat": 37.7749 + math.sin(t / 60) * 0.001,
                "lon": -122.4194 + math.cos(t / 60) * 0.001,
                "speed": 24.6 + math.sin(t / 4) * 3.0,
                "sats": 14, "hdop": 0.8}

    def draw(self):
        t = time.monotonic() - self.t0
        imu = self.imu_state(t)
        gps = self.demo_gps(t) if self.demo else self.gps.data
        gps_age = None if self.demo else (time.monotonic() - self.gps.t)
        if not self.demo and (gps is None or gps_age > GPS_STALE_S):
            gps = None

        s = self.ss
        s.fill((0, 0, 0, 0))
        self.screen.fill(BG)
        k = self.k
        lw = max(2, int(1.6 * k))         # hairline on the microdisplay
        lw2 = max(3, int(2.4 * k))
        boldlw = max(2, int(1.2 * k))

        pitch, bank = imu["pitch"], imu["bank"]
        self.smooth["pitch"] += (pitch - self.smooth["pitch"]) * 0.6
        self.smooth["bank"] += (bank - self.smooth["bank"]) * 0.6
        sp, sb = self.smooth["pitch"], self.smooth["bank"]

        def L(x1, y1, x2, y2, color=CYAN, w=lw):
            pygame.draw.line(s, color, (x1 * k, y1 * k), (x2 * k, y2 * k), w)

        # pitch ladder + bank marker
        for d in LADDER_DEGS:
            y = CY - d * 3
            half = 110 if d == 0 else 92
            (x1, yy1), (x2, yy2) = xf(CX - half, y, sp, sb), xf(CX + half, y, sp, sb)
            L(x1, yy1, x2, yy2, CYAN, lw2 if d == 0 else lw)
            (x1, yy1), (x2, yy2) = xf(CX - 32, y, sp, sb), xf(CX + 32, y, sp, sb)
            L(x1, yy1, x2, yy2, DIM, lw)
        for (p1, p2) in BANK_TICKS:
            L(p1[0], p1[1], p2[0], p2[1], DIM, lw)
        tri = [rotp(320, 62, sb), rotp(312, 77, sb), rotp(328, 77, sb)]
        pygame.draw.polygon(s, AMBER, [(x * k, y * k) for x, y in tri])

        # crosshair
        pygame.draw.circle(s, AMBER, (int(CX * k), int(CY * k)), int(28 * k), lw2)
        L(CX, CY - 28, CX, CY - 14, AMBER, lw2)
        L(CX, CY + 14, CX, CY + 28, AMBER, lw2)
        pygame.draw.circle(s, AMBER, (int(CX * k), int(CY * k)), int(2.4 * k))

        # corner brackets
        m, c = 12, 30
        L(m, m, m + c, m, DIM, boldlw), L(m, m, m, m + c, DIM, boldlw)
        L(VW - m, m, VW - m - c, m, DIM, boldlw), L(VW - m, m, VW - m, m + c, DIM, boldlw)
        L(m, VH - m, m + c, VH - m, DIM, boldlw), L(m, VH - m, m, VH - m - c, DIM, boldlw)
        L(VW - m, VH - m, VW - m - c, VH - m, DIM, boldlw), L(VW - m, VH - m, VW - m, VH - m - c, DIM, boldlw)

        # time / date (top right)
        now = datetime.now()
        self.text(s, now.strftime("%H:%M:%S"), CYAN, VW - 20, 14, 30, True, "ra")
        self.text(s, now.strftime("%a %d %b %Y").lower(), DIM, VW - 20, 48, 12, False, "ra")

        # speed (bottom left)
        spd = "--"
        if gps:
            spd = str(int(round(gps["speed"] * (2.23694 if self.unit == "mph" else 3.6))))
        self.text(s, spd, AMBER, 20, VH - 58, 44, True, "la")
        self.text(s, self.unit, AMBER, 20 + len(spd) * 26 + 14, VH - 40, 14, False, "la")
        self.text(s, "ground speed", DIM, 20, VH - 16, 12, False, "la")

        # gps (bottom right)
        if gps:
            self.text(s, "gps", DIM, VW - 20, VH - 66, 12, False, "ra")
            self.text(s, "%.5f° %s" % (abs(gps["lat"]), "N" if gps["lat"] >= 0 else "S"),
                      CYAN, VW - 20, VH - 52, 13, False, "ra")
            self.text(s, "%.5f° %s" % (abs(gps["lon"]), "E" if gps["lon"] >= 0 else "W"),
                      CYAN, VW - 20, VH - 34, 13, False, "ra")
            self.text(s, "%d sats · hdop %s" % (gps.get("sats", 0), gps.get("hdop", "-")),
                      DIM, VW - 20, VH - 16, 12, False, "ra")
        else:
            self.text(s, "gps", DIM, VW - 20, VH - 40, 12, False, "ra")
            self.text(s, "awaiting phone link", DIM, VW - 20, VH - 24, 12, False, "ra")

        # bottom center readouts
        yaw = ((imu.get("yaw", 0) % 360) + 360) % 360
        mid = ("pitch %+d° · bank %+d° · %.2f g · hdg %03d°" %
               (round(sp), round(sb), imu.get("g", 1.0), round(yaw)))
        self.text(s, mid, DIM, CX, VH - 18, 12, False, "ca")

        # menu button (top left)
        pygame.draw.rect(s, DIM, (20 * k, 16 * k, 40 * k, 40 * k), boldlw, 2)
        for i, yy in enumerate((26, 36, 46)):
            L(28, yy, 52, yy, CYAN, lw2)

        if self.menu:
            self.draw_menu(s, imu, gps, gps_age)

        if not imu.get("ok"):
            self.text(s, "awaiting glasses link", DIM, CX, 170, 15, False, "ca")

        pygame.transform.smoothscale(self.ss, (self.w, self.h), self.screen)
        pygame.display.flip()

    def draw_menu(self, s, imu, gps, gps_age):
        k = self.k
        x, y, w, h = 16, 64, 300, 170
        panel = pygame.Surface((int(w * k), int(h * k)), pygame.SRCALPHA)
        panel.fill((0, 0, 0, 230))
        s.blit(panel, (x * k, y * k))
        pygame.draw.rect(s, DIM, (x * k, y * k, w * k, h * k), max(2, int(1.2 * k)), 3)
        self.text(s, "data links", DIM, x + 14, y + 10, 12)
        self.text(s, "glasses imu", CYAN, x + 14, y + 34, 13)
        self.text(s, "online" if imu.get("ok") else "offline",
                  AMBER if imu.get("ok") else RED, x + w - 14, y + 34, 13, False, "ra")
        self.text(s, "phone gps", CYAN, x + 14, y + 56, 13)
        self.text(s, "online" if gps else "offline",
                  AMBER if gps else RED, x + w - 14, y + 56, 13, False, "ra")
        self.text(s, "imu rate", CYAN, x + 14, y + 78, 13)
        self.text(s, "%d hz" % imu.get("hz", 0), AMBER, x + w - 14, y + 78, 13, False, "ra")
        self.text(s, "gps age", CYAN, x + 14, y + 100, 13)
        self.text(s, "-" if gps_age is None else "%.1f s" % gps_age, AMBER, x + w - 14, y + 100, 13, False, "ra")
        self.text(s, "a/c recenter  y/u units", DIM, x + 14, y + 128, 12)
        self.text(s, "x/m menu  b/esc quit", DIM, x + 14, y + 146, 12)

    def run(self):
        running = True
        imu = self.imu if self.imu is not None else type("D", (), {"recenter": lambda self: None})()
        while running:
            for ev in pygame.event.get():
                running = self.handle(ev, imu)
            self.draw()
            self.clock.tick(60)
        pygame.quit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data, no glasses")
    ap.add_argument("--gps-port", type=int, default=8676)
    args = ap.parse_args()
    Hud(demo=args.demo, gps_port=args.gps_port).run()


if __name__ == "__main__":
    main()
