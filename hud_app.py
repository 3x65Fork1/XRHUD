#!/usr/bin/env python3
"""
xreal-hud - native fullscreen HUD app for the Xreal Air / Steam Deck.

Controls:
    c / gamepad-A = recenter
    u / gamepad-Y = mph / kmh
    m / gamepad-X = menu
    b / gamepad-B = quit
    esc           = quit
"""

import argparse
import json
import math
import os
import socket
import threading
import time
from datetime import datetime

import pygame

try:
    from imu import ImuReader
except ImportError:
    ImuReader = None


# ---------------------------------------------------------------------------
# HUD configuration
# ---------------------------------------------------------------------------

VW, VH = 640.0, 360.0
CX, CY = 320.0, 190.0

CYAN = (63, 217, 255)
AMBER = (255, 179, 64)
DIM = (138, 154, 165)
RED = (255, 93, 93)
BG = (0, 0, 0)

GPS_STALE_S = 6.0

LADDER_DEGS = range(-180, 181, 10)
PITCH_PX_PER_DEG = 3.0
LADDER_MARGIN = 120.0

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _find_font(bold=False):
    paths = FONT_PATHS_BOLD if bold else FONT_PATHS

    for path in paths:
        if os.path.exists(path):
            return path

    return None


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------

class GpsListener(threading.Thread):
    """Receive phone GPS as UDP JSON."""

    def __init__(self, port):
        super().__init__(daemon=True)

        self.data = None
        self.t = 0.0

        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(1.0)

    def run(self):
        while True:
            try:
                raw, _ = self.sock.recvfrom(2048)

                self.data = json.loads(
                    raw.decode("utf-8", "replace")
                )

                self.t = time.monotonic()

            except socket.timeout:
                continue

            except Exception:
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# IMU manager
# ---------------------------------------------------------------------------

class ImuManager:
    """
    Keep an ImuReader alive and automatically reconnect if the glasses
    disappear and reappear.
    """

    def __init__(self):
        self.reader = None

        self.thread = threading.Thread(
            target=self._work,
            daemon=True,
        )

        self.thread.start()

    def _work(self):
        while True:
            if ImuReader is None:
                print(
                    "[hud] imu.py unavailable",
                    flush=True,
                )

                time.sleep(3.0)
                continue

            try:
                reader = ImuReader()
                reader.start()

                deadline = time.monotonic() + 5.0

                while (
                    reader.is_alive()
                    and not reader.state.get("ok")
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.1)

                if reader.state.get("ok"):
                    print(
                        "[hud] imu stream live",
                        flush=True,
                    )

                    self.reader = reader
                    reader.join()

                else:
                    reader.stop()

                    try:
                        reader.close()
                    except Exception:
                        pass

                    time.sleep(2.0)

            except Exception as exc:
                print(
                    f"[hud] imu error: {type(exc).__name__}: {exc}",
                    flush=True,
                )

                time.sleep(2.0)

            self.reader = None

            print(
                "[hud] imu lost - retrying",
                flush=True,
            )

    @property
    def state(self):
        if self.reader is not None:
            return self.reader.state

        return {
            "ok": False,
            "hz": 0.0,
            "pitch": 0.0,
            "bank": 0.0,
            "yaw": 0.0,
            "g": 1.0,
        }

    def recenter(self):
        if self.reader is not None:
            self.reader.recenter()


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def xf(x, y, bank):
    """
    Rotate a HUD point around the center by the current bank angle.
    """

    angle = math.radians(-bank)

    dx = x - CX
    dy = y - CY

    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    return (
        CX + dx * cos_a - dy * sin_a,
        CY + dx * sin_a + dy * cos_a,
    )


def rotp(x, y, degrees):
    """Rotate a HUD point around the center."""

    angle = math.radians(degrees)

    dx = x - CX
    dy = y - CY

    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    return (
        CX + dx * cos_a - dy * sin_a,
        CY + dx * sin_a + dy * cos_a,
    )


def bank_ticks():
    segments = []

    for bank in range(-60, 61, 10):
        angle = math.radians(270 + bank)

        r1 = 102.0

        if bank % 30 == 0:
            r2 = 118.0
        else:
            r2 = 109.0

        segments.append(
            (
                (
                    CX + r1 * math.cos(angle),
                    CY + r1 * math.sin(angle),
                ),
                (
                    CX + r2 * math.cos(angle),
                    CY + r2 * math.sin(angle),
                ),
            )
        )

    return segments


BANK_TICKS = bank_ticks()


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------

class Hud:
    def __init__(
        self,
        demo=False,
        gps_port=8676,
        debug=False,
    ):
        pygame.init()
        pygame.joystick.init()

        self.pads = []

        for i in range(pygame.joystick.get_count()):
            joystick = pygame.joystick.Joystick(i)
            joystick.init()
            self.pads.append(joystick)

        info = pygame.display.Info()

        self.w = info.current_w
        self.h = info.current_h

        self.screen = pygame.display.set_mode(
            (0, 0),
            pygame.FULLSCREEN | pygame.NOFRAME,
        )

        pygame.display.set_caption("xreal hud")
        pygame.mouse.set_visible(False)

        self.k = 3.0

        self.ss = pygame.Surface(
            (
                int(VW * self.k),
                int(VH * self.k),
            ),
            pygame.SRCALPHA,
        )

        self.fonts = {}

        self.demo = demo
        self.debug = debug

        self.gps = GpsListener(gps_port)
        self.gps.start()

        if self.demo:
            self.imu = None
        else:
            self.imu = ImuManager()

        self.menu = False

        self.unit = self._load_unit()

        self.smooth_pitch = 0.0
        self.smooth_bank = 0.0
        self.smooth_yaw = 0.0

        self._last_t = time.monotonic()

        self.clock = pygame.time.Clock()
        self.t0 = time.monotonic()

    # -----------------------------------------------------------------------
    # Preferences
    # -----------------------------------------------------------------------

    def _cfg(self):
        return os.path.expanduser(
            "~/.config/xreal-hud/unit"
        )

    def _load_unit(self):
        try:
            with open(self._cfg(), "r") as f:
                value = f.read().strip()

            return value or "mph"

        except Exception:
            return "mph"

    def _save_unit(self):
        try:
            path = self._cfg()

            os.makedirs(
                os.path.dirname(path),
                exist_ok=True,
            )

            with open(path, "w") as f:
                f.write(self.unit)

        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Text
    # -----------------------------------------------------------------------

    def font(self, logical_px, bold=False):
        px = max(
            8,
            int(logical_px * self.k * 0.72),
        )

        key = (px, bold)

        if key not in self.fonts:
            path = _find_font(bold)

            try:
                if path:
                    self.fonts[key] = pygame.font.Font(
                        path,
                        px,
                    )
                else:
                    self.fonts[key] = pygame.font.Font(
                        None,
                        px,
                    )

            except Exception:
                self.fonts[key] = pygame.font.Font(
                    None,
                    px,
                )

        return self.fonts[key]

    def text(
        self,
        surface,
        txt,
        color,
        x,
        y,
        size=12,
        bold=False,
        anchor="la",
    ):
        rendered = self.font(
            size,
            bold,
        ).render(
            txt,
            True,
            color,
        )

        rect = rendered.get_rect()

        if anchor[0] == "r":
            rect.topright = (
                int(x * self.k),
                int(y * self.k),
            )

        elif anchor[0] == "c":
            rect.midtop = (
                int(x * self.k),
                int(y * self.k),
            )

        else:
            rect.topleft = (
                int(x * self.k),
                int(y * self.k),
            )

        surface.blit(
            rendered,
            rect,
        )

    # -----------------------------------------------------------------------
    # Input
    # -----------------------------------------------------------------------

    def handle(self, event, imu):
        if event.type == pygame.QUIT:
            return False

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return False

            if event.key == pygame.K_c:
                imu.recenter()

            elif event.key == pygame.K_u:
                self.unit = (
                    "kmh"
                    if self.unit == "mph"
                    else "mph"
                )

                self._save_unit()

            elif event.key == pygame.K_m:
                self.menu = not self.menu

        elif event.type == pygame.JOYBUTTONDOWN:
            button = event.button

            if button == 1:
                return False

            if button == 0:
                imu.recenter()

            elif button == 3:
                self.unit = (
                    "kmh"
                    if self.unit == "mph"
                    else "mph"
                )

                self._save_unit()

            elif button in (2, 7):
                self.menu = not self.menu

        return True

    # -----------------------------------------------------------------------
    # IMU
    # -----------------------------------------------------------------------

    def imu_state(self, t):
        if self.demo:
            return {
                "ok": True,
                "hz": 60.0,
                "pitch": math.sin(t / 2.1) * 9.0,
                "bank": math.sin(t / 1.3) * 22.0,
                "yaw": math.sin(t / 3.7) * 40.0,
                "g": 1.0 + math.sin(t / 0.9) * 0.12,
            }

        return self.imu.state

    # -----------------------------------------------------------------------
    # Demo GPS
    # -----------------------------------------------------------------------

    def demo_gps(self, t):
        return {
            "lat": 37.7749 + math.sin(t / 60.0) * 0.001,
            "lon": -122.4194 + math.cos(t / 60.0) * 0.001,
            "speed": 24.6 + math.sin(t / 4.0) * 3.0,
            "sats": 14,
            "hdop": 0.8,
        }

    # -----------------------------------------------------------------------
    # Drawing
    # -----------------------------------------------------------------------

    def draw(self):
        t = time.monotonic() - self.t0

        imu = self.imu_state(t)

        if self.demo:
            gps = self.demo_gps(t)
            gps_age = None
        else:
            gps = self.gps.data
            gps_age = (
                time.monotonic() - self.gps.t
                if self.gps.t
                else None
            )

            if (
                gps is None
                or gps_age is None
                or gps_age > GPS_STALE_S
            ):
                gps = None

        surface = self.ss

        surface.fill(
            (0, 0, 0, 0)
        )

        self.screen.fill(BG)

        k = self.k

        lw = max(
            2,
            int(1.6 * k),
        )

        lw2 = max(
            3,
            int(2.4 * k),
        )

        boldlw = max(
            2,
            int(1.2 * k),
        )

        # -------------------------------------------------------------------
        # Smooth orientation.
        #
        # Pitch and bank deliberately swapped compared to the values
        # published by imu.py.
        # -------------------------------------------------------------------

        pitch = float(
            imu.get("bank", 0.0)
        )

        bank = float(
            imu.get("pitch", 0.0)
        )

        yaw = float(
            imu.get("yaw", 0.0)
        )

        now = time.monotonic()

        dt = min(
            now - self._last_t,
            0.1,
        )

        self._last_t = now

        alpha = 1.0 - math.exp(
            -dt * 10.0
        )

        self.smooth_pitch += (
            pitch - self.smooth_pitch
        ) * alpha

        self.smooth_bank += (
            bank - self.smooth_bank
        ) * alpha

        yaw_delta = (
            yaw
            - self.smooth_yaw
            + 180.0
        ) % 360.0 - 180.0

        self.smooth_yaw = (
            self.smooth_yaw
            + yaw_delta * alpha
        ) % 360.0

        sp = self.smooth_pitch
        sb = self.smooth_bank
        sy = self.smooth_yaw

        if self.debug:
            print(
                "pitch=%.2f bank=%.2f yaw=%.2f"
                % (sp, sb, sy),
                flush=True,
            )

        # -------------------------------------------------------------------
        # Line helper
        # -------------------------------------------------------------------

        def line(
            x1,
            y1,
            x2,
            y2,
            color=CYAN,
            width=lw,
        ):
            pygame.draw.line(
                surface,
                color,
                (
                    int(x1 * k),
                    int(y1 * k),
                ),
                (
                    int(x2 * k),
                    int(y2 * k),
                ),
                width,
            )

                # -------------------------------------------------------------------
                # Attitude ladder
                #
                # The ladder is rendered WITHOUT alpha.
                #
                # Instead, each rung's colour is pre-blended against the black HUD
                # background. This makes the ladder fully opaque from Pygame's point
                # of view and avoids alpha/smoothscale issues on the Xreal display.
                #
                # Visual behaviour:
                #
                #   centre = 100% brightness
                #   outer  = 25% brightness
                #
                # Colour:
                #
                #   centre -> CYAN
                #   middle -> AMBER
                #   outer  -> RED
                # -------------------------------------------------------------------

                ladder_surface = pygame.Surface(
                    (
                        int(VW * k),
                        int(VH * k),
                ))

                ladder_surface.fill(BG)

                # Exactly 19 rungs:
                #
                #   -90 ... -10, 0, +10 ... +90

                for degrees in range(-90, 91, 10):

                    y = (
                        CY
                        - degrees * PITCH_PX_PER_DEG
                        + sp * PITCH_PX_PER_DEG
                    )

                    if (
                        y < -LADDER_MARGIN
                        or y > VH + LADDER_MARGIN
                    ):
                        continue

                    # Distance from the visual centre.
                    #
                    # This keeps transparency/brightness centred on the actual HUD
                    # centre as the ladder moves with pitch.

                    visual_distance = abs(y - CY)

                    max_distance = (
                        90.0 * PITCH_PX_PER_DEG
                    )

                    rung_distance = min(
                        1.0,
                        visual_distance / max_distance,
                    )

                    # Colour gradient:
                    #
                    # centre -> cyan
                    # middle -> amber
                    # outer  -> red

                    if rung_distance <= 0.5:

                        # Cyan -> Amber

                        color_t = (
                            rung_distance / 0.5
                        )

                        rgb = tuple(
                            int(
                                CYAN[i]
                                + (
                                    AMBER[i]
                                    - CYAN[i]
                                ) * color_t
                            )
                            for i in range(3)
                        )

                    else:

                        # Amber -> Red

                        color_t = (
                            rung_distance - 0.5
                        ) / 0.5

                        rgb = tuple(
                            int(
                                AMBER[i]
                                + (
                                    RED[i]
                                    - AMBER[i]
                                ) * color_t
                            )
                            for i in range(3)
                        )

                    # Brightness / transparency.
                    #
                    # We no longer use an alpha channel.
                    #
                    # Instead:
                    #
                    #   255 = full colour
                    #    64 = 25% colour
                    #
                    # Since BG is black, multiplying the colour by this factor gives
                    # exactly the same visual result as alpha compositing against
                    # black.

                   brightness = (
                        1.0
                        - 0.75 * rung_distance
                    )

                    color = tuple(
                        max(
                            0,
                            min(
                                255,
                                int(
                                    channel * brightness
                                ),
                            ),
                        )
                        for channel in rgb
                    )


                    # Rung dimensions.

                    if degrees == 0:
                        half = 110
                        line_width = lw2
                    else:
                        half = 92
                        line_width = lw


                    # Rotate rung with current bank.

                    x1, y1 = xf(
                        CX - half,
                        y,
                        sb,
                    )

                    x2, y2 = xf(
                        CX + half,
                        y,
                        sb,
                    )

                    pygame.draw.line(
                        ladder_surface,
                        color,
                        (
                            int(x1 * k),
                            int(y1 * k),
                        ),
                        (
                            int(x2 * k),
                            int(y2 * k),
                        ),
                        line_width,
                    )


                    # Inner segment.
                    #
                    # Previously this used alpha * 0.65. Since the ladder is now
                    # opaque, simply scale the already-computed RGB colour.


                    inner_color = tuple(
                        int(channel * 0.65)
                        for channel in color
                    )

                    ix1, iy1 = xf(
                        CX - 32,
                        y,
                        sb,
                    )

                    ix2, iy2 = xf(
                        CX + 32,
                        y,
                        sb,
                    )

                    pygame.draw.line(
                        ladder_surface,
                        inner_color,
                        (
                            int(ix1 * k),
                            int(iy1 * k),
                        ),
                        (
                            int(ix2 * k),
                            int(iy2 * k),
                        ),
                        lw,
                    )

                # Composite ladder.
                #
                # There is NO alpha here. The ladder is already blended against
                # black, so this is a normal opaque blit.


                surface.blit(
                    ladder_surface,
                    (0, 0),
                )



        # Composite the ladder onto the main HUD.


        surface.blit(
            ladder_surface,
            (
                0,
                0,
            ),
        )

        # -------------------------------------------------------------------
        # Bank scale
        # -------------------------------------------------------------------

        for p1, p2 in BANK_TICKS:
            line(
                p1[0],
                p1[1],
                p2[0],
                p2[1],
                DIM,
                lw,
            )

        triangle = [
            rotp(
                320,
                62,
                sb,
            ),
            rotp(
                312,
                77,
                sb,
            ),
            rotp(
                328,
                77,
                sb,
            ),
        ]

        pygame.draw.polygon(
            surface,
            AMBER,
            [
                (
                    x * k,
                    y * k,
                )
                for x, y in triangle
            ],
        )

        # -------------------------------------------------------------------
        # Center crosshair
        # -------------------------------------------------------------------

        pygame.draw.circle(
            surface,
            AMBER,
            (
                int(CX * k),
                int(CY * k),
            ),
            int(28 * k),
            lw2,
        )

        line(
            CX,
            CY - 28,
            CX,
            CY - 14,
            AMBER,
            lw2,
        )

        line(
            CX,
            CY + 14,
            CX,
            CY + 28,
            AMBER,
            lw2,
        )

        pygame.draw.circle(
            surface,
            AMBER,
            (
                int(CX * k),
                int(CY * k),
            ),
            int(2.4 * k),
        )

        # -------------------------------------------------------------------
        # Heading arrow
        # -------------------------------------------------------------------

        angle = math.radians(sy)

        arrow = [
            (
                CX + 46 * math.sin(angle),
                CY - 46 * math.cos(angle),
            ),
            (
                CX + 36 * math.sin(angle - 0.18),
                CY - 36 * math.cos(angle - 0.18),
            ),
            (
                CX + 36 * math.sin(angle + 0.18),
                CY - 36 * math.cos(angle + 0.18),
            ),
        ]

        pygame.draw.polygon(
            surface,
            AMBER,
            [
                (
                    x * k,
                    y * k,
                )
                for x, y in arrow
            ],
        )

        # -------------------------------------------------------------------
        # Corner brackets
        # -------------------------------------------------------------------

        margin = 12
        corner = 30

        line(
            margin,
            margin,
            margin + corner,
            margin,
            DIM,
            boldlw,
        )

        line(
            margin,
            margin,
            margin,
            margin + corner,
            DIM,
            boldlw,
        )

        line(
            VW - margin,
            margin,
            VW - margin - corner,
            margin,
            DIM,
            boldlw,
        )

        line(
            VW - margin,
            margin,
            VW - margin,
            margin + corner,
            DIM,
            boldlw,
        )

        line(
            margin,
            VH - margin,
            margin + corner,
            VH - margin,
            DIM,
            boldlw,
        )

        line(
            margin,
            VH - margin,
            margin,
            VH - margin - corner,
            DIM,
            boldlw,
        )

        line(
            VW - margin,
            VH - margin,
            VW - margin - corner,
            VH - margin,
            DIM,
            boldlw,
        )

        line(
            VW - margin,
            VH - margin,
            VW - margin,
            VH - margin - corner,
            DIM,
            boldlw,
        )

        # -------------------------------------------------------------------
        # Time / date
        # -------------------------------------------------------------------

        now_dt = datetime.now()

        self.text(
            surface,
            now_dt.strftime("%H:%M:%S"),
            CYAN,
            VW - 20,
            14,
            30,
            True,
            "ra",
        )

        self.text(
            surface,
            now_dt.strftime(
                "%a %d %b %Y"
            ).lower(),
            DIM,
            VW - 20,
            48,
            12,
            False,
            "ra",
        )

        # -------------------------------------------------------------------
        # Speed
        # -------------------------------------------------------------------

        speed = "--"

        if gps:
            multiplier = (
                2.23694
                if self.unit == "mph"
                else 3.6
            )

            speed = str(
                int(
                    round(
                        float(gps["speed"])
                        * multiplier
                    )
                )
            )

        self.text(
            surface,
            speed,
            AMBER,
            20,
            VH - 58,
            44,
            True,
            "la",
        )

        self.text(
            surface,
            self.unit,
            AMBER,
            20 + len(speed) * 26 + 14,
            VH - 40,
            14,
            False,
            "la",
        )

        self.text(
            surface,
            "ground speed",
            DIM,
            20,
            VH - 16,
            12,
            False,
            "la",
        )

        # -------------------------------------------------------------------
        # GPS
        # -------------------------------------------------------------------

        if gps:
            self.text(
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 66,
                12,
                False,
                "ra",
            )

            self.text(
                surface,
                "%.5f %s"
                % (
                    abs(float(gps["lat"])),
                    "N"
                    if float(gps["lat"]) >= 0
                    else "S",
                ),
                CYAN,
                VW - 20,
                VH - 52,
                13,
                False,
                "ra",
            )

            self.text(
                surface,
                "%.5f %s"
                % (
                    abs(float(gps["lon"])),
                    "E"
                    if float(gps["lon"]) >= 0
                    else "W",
                ),
                CYAN,
                VW - 20,
                VH - 34,
                13,
                False,
                "ra",
            )

            self.text(
                surface,
                "%d sats - hdop %s"
                % (
                    int(gps.get("sats", 0)),
                    gps.get("hdop", "-"),
                ),
                DIM,
                VW - 20,
                VH - 16,
                12,
                False,
                "ra",
            )

        else:
            self.text(
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 40,
                12,
                False,
                "ra",
            )

            self.text(
                surface,
                "awaiting phone link",
                DIM,
                VW - 20,
                VH - 24,
                12,
                False,
                "ra",
            )

        # -------------------------------------------------------------------
        # Orientation readout
        # -------------------------------------------------------------------

        readout = (
            "P %+d B %+d Y %03d G %.2f"
            % (
                round(sp),
                round(sb),
                int(round(sy)) % 360,
                imu.get("g", 1.0),
            )
        )

        self.text(
            surface,
            readout,
            DIM,
            CX,
            VH - 18,
            12,
            False,
            "ca",
        )

        # -------------------------------------------------------------------
        # Menu button
        # -------------------------------------------------------------------

        pygame.draw.rect(
            surface,
            DIM,
            (
                20 * k,
                16 * k,
                40 * k,
                40 * k,
            ),
            boldlw,
            2,
        )

        for yy in (26, 36, 46):
            line(
                28,
                yy,
                52,
                yy,
                CYAN,
                lw2,
            )

        if self.menu:
            self.draw_menu(
                surface,
                imu,
                gps,
                gps_age,
            )

        if not imu.get("ok"):
            self.text(
                surface,
                "awaiting glasses link",
                DIM,
                CX,
                170,
                15,
                False,
                "ca",
            )

        # -------------------------------------------------------------------
        # Present
        # -------------------------------------------------------------------

        pygame.transform.smoothscale(
            self.ss,
            (
                self.w,
                self.h,
            ),
            self.screen,
        )

        pygame.display.flip()

    # -----------------------------------------------------------------------
    # Menu
    # -----------------------------------------------------------------------

    def draw_menu(
        self,
        surface,
        imu,
        gps,
        gps_age,
    ):
        k = self.k

        x = 16
        y = 64
        w = 300
        h = 170

        panel = pygame.Surface(
            (
                int(w * k),
                int(h * k),
            ),
            pygame.SRCALPHA,
        )

        panel.fill(
            (0, 0, 0, 230)
        )

        surface.blit(
            panel,
            (
                x * k,
                y * k,
            ),
        )

        pygame.draw.rect(
            surface,
            DIM,
            (
                x * k,
                y * k,
                w * k,
                h * k,
            ),
            max(2, int(1.2 * k)),
            3,
        )

        self.text(
            surface,
            "data links",
            DIM,
            x + 14,
            y + 10,
            12,
        )

        self.text(
            surface,
            "glasses imu",
            CYAN,
            x + 14,
            y + 34,
            13,
        )

        self.text(
            surface,
            "online"
            if imu.get("ok")
            else "offline",
            AMBER
            if imu.get("ok")
            else RED,
            x + w - 14,
            y + 34,
            13,
            False,
            "ra",
        )

        self.text(
            surface,
            "phone gps",
            CYAN,
            x + 14,
            y + 56,
            13,
        )

        self.text(
            surface,
            "online"
            if gps
            else "offline",
            AMBER
            if gps
            else RED,
            x + w - 14,
            y + 56,
            13,
            False,
            "ra",
        )

        self.text(
            surface,
            "imu rate",
            CYAN,
            x + 14,
            y + 78,
            13,
        )

        self.text(
            surface,
            "%d hz"
            % imu.get("hz", 0),
            AMBER,
            x + w - 14,
            y + 78,
            13,
            False,
            "ra",
        )

        self.text(
            surface,
            "gps age",
            CYAN,
            x + 14,
            y + 100,
            13,
        )

        self.text(
            surface,
            "-"
            if gps_age is None
            else "%.1f s" % gps_age,
            AMBER,
            x + w - 14,
            y + 100,
            13,
            False,
            "ra",
        )

        self.text(
            surface,
            "a/c recenter  y/u units",
            DIM,
            x + 14,
            y + 128,
            12,
        )

        self.text(
            surface,
            "x/m menu  b/esc quit",
            DIM,
            x + 14,
            y + 146,
            12,
        )

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self):
        running = True

        if self.imu is not None:
            imu = self.imu
        else:
            class DemoImu:
                @staticmethod
                def recenter():
                    pass

            imu = DemoImu()

        while running:
            for event in pygame.event.get():
                running = self.handle(
                    event,
                    imu,
                )

            self.draw()

            self.clock.tick(60)

        pygame.quit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--demo",
        action="store_true",
        help="synthetic data, no glasses",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="print pitch/bank/yaw at 60 Hz",
    )

    parser.add_argument(
        "--gps-port",
        type=int,
        default=8676,
    )

    args = parser.parse_args()

    Hud(
        demo=args.demo,
        gps_port=args.gps_port,
        debug=args.debug,
    ).run()


if __name__ == "__main__":
    main()
