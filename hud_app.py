#!/usr/bin/env python3
"""
xreal-hud - native fullscreen HUD app for the Xreal Air / Steam Deck.

Controls:
    c / gamepad-A = recenter
    u / gamepad-Y = mph / kmh
    m             = menu
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
    import pygame.freetype
except ImportError:
    pygame.freetype = None

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

PITCH_PX_PER_DEG = 3.0
LADDER_MARGIN = 120.0

# ---------------------------------------------------------------------------
# Large transparent attitude sphere
# ---------------------------------------------------------------------------

ATTITUDE_RADIUS = 235.0
ATTITUDE_CENTER_Y = 142.0

ATTITUDE_TICK_MIN_BRIGHTNESS = 0.08
ATTITUDE_TICK_MAX_BRIGHTNESS = 0.42

ATTITUDE_LADDER_MIN_BRIGHTNESS = 0.10
ATTITUDE_LADDER_MAX_BRIGHTNESS = 0.50

# ---------------------------------------------------------------------------
# Heading arc
# ---------------------------------------------------------------------------

HEADING_RADIUS = 155.0
HEADING_CENTER_Y = 205.0
HEADING_TICKS = 72
HEADING_CLIP_Y = CY

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def _find_font(bold=False):
    paths = FONT_PATHS_BOLD if bold else FONT_PATHS

    for path in paths:
        if os.path.isfile(path):
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

        if pygame.freetype is not None:
            pygame.freetype.init()

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
        self.text_fonts = {}

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
    #
    # Text is rendered directly onto the physical display.
    #
    # This deliberately keeps text OUT of the 3x backing surface so that
    # glyphs are never subjected to the HUD smoothscale operation.
    # -----------------------------------------------------------------------

    def text_font(self, logical_px, bold=False):
        scale_x = self.w / VW
        scale_y = self.h / VH

        size = max(
            8,
            int(
                round(
                    logical_px
                    * min(
                        scale_x,
                        scale_y,
                    )
                )
            ),
        )

        key = (
            size,
            bool(bold),
        )

        if key in self.text_fonts:
            return self.text_fonts[key]

        path = _find_font(bold)

        if pygame.freetype is not None:
            if path:
                font = pygame.freetype.Font(
                    path,
                    size,
                )
            else:
                font = pygame.freetype.SysFont(
                    "dejavusans",
                    size,
                    bold=bold,
                )

            font.antialiased = True

        else:
            if path:
                font = pygame.font.Font(
                    path,
                    size,
                )
            else:
                font = pygame.font.SysFont(
                    "dejavusans",
                    size,
                    bold=bold,
                )

        self.text_fonts[key] = font

        return font

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
        """
        Render text at physical display resolution.

        x/y remain in the 640x360 logical HUD coordinate system.
        """

        scale_x = self.w / VW
        scale_y = self.h / VH

        px = int(
            round(x * scale_x)
        )

        py = int(
            round(y * scale_y)
        )

        font = self.text_font(
            size,
            bold,
        )

        text_value = str(txt)

        if (
            pygame.freetype is not None
            and isinstance(
                font,
                pygame.freetype.Font,
            )
        ):
            rect = font.get_rect(
                text_value
            )

            if anchor and anchor[0] == "r":
                px -= rect.width

            elif anchor and anchor[0] == "c":
                px -= rect.width // 2

            font.render_to(
                surface,
                (
                    px,
                    py,
                ),
                text_value,
                fgcolor=color,
            )

            return

        rendered = font.render(
            text_value,
            True,
            color,
        )

        rect = rendered.get_rect()

        if anchor and anchor[0] == "r":
            rect.topright = (
                px,
                py,
            )

        elif anchor and anchor[0] == "c":
            rect.midtop = (
                px,
                py,
            )

        else:
            rect.topleft = (
                px,
                py,
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
    # Large transparent spherical attitude display
    # -----------------------------------------------------------------------

    def draw_attitude_sphere(
        self,
        surface,
        pitch,
        bank,
        yaw,
        k,
    ):
        """
        Render a large, translucent spherical attitude instrument.

        The sphere is intentionally oversized and subtle so that it feels
        like one continuous 3D object rather than a ladder sitting beside
        a heading instrument.

        The center of the sphere is kept above the middle of the HUD so
        that the primary telemetry remains readable.
        """

        center_x = CX
        center_y = ATTITUDE_CENTER_Y
        radius = ATTITUDE_RADIUS

        # ---------------------------------------------------------------
        # Outer spherical latitude/longitude guide.
        # ---------------------------------------------------------------

        sphere = pygame.Surface(
            (
                int(VW * k),
                int(VH * k),
            ),
            pygame.SRCALPHA,
        )

        # Very faint outer sphere.
        pygame.draw.circle(
            sphere,
            (
                DIM[0],
                DIM[1],
                DIM[2],
                20,
            ),
            (
                int(center_x * k),
                int(center_y * k),
            ),
            int(radius * k),
            max(
                1,
                int(1.0 * k),
            ),
        )

        # ---------------------------------------------------------------
        # Horizon / latitude bands.
        # ---------------------------------------------------------------

        for degrees in range(-80, 81, 10):
            vertical = (
                degrees
                * PITCH_PX_PER_DEG
            )

            cy = (
                center_y
                + vertical
                + pitch * PITCH_PX_PER_DEG
            )

            normalized = (
                degrees / 90.0
            )

            half_width = radius * math.sqrt(
                max(
                    0.0,
                    1.0
                    - normalized
                    * normalized,
                )
            )

            if half_width < 2:
                continue

            x1 = center_x - half_width
            x2 = center_x + half_width

            # Fade bands toward the poles.
            strength = (
                ATTITUDE_LADDER_MAX_BRIGHTNESS
                - (
                    abs(normalized)
                    * (
                        ATTITUDE_LADDER_MAX_BRIGHTNESS
                        - ATTITUDE_LADDER_MIN_BRIGHTNESS
                    )
                )
            )

            color = tuple(
                int(
                    channel * strength
                )
                for channel in CYAN
            )

            # Major horizon.
            if degrees == 0:
                color = tuple(
                    int(
                        channel
                        * (
                            strength
                            + 0.12
                        )
                    )
                    for channel in AMBER
                )

            # Apply bank rotation.
            x1r, y1r = xf(
                x1,
                cy,
                bank,
            )

            x2r, y2r = xf(
                x2,
                cy,
                bank,
            )

            # Clip to sphere bounds.
            if (
                y1r < center_y - radius
                or y1r > center_y + radius
            ):
                continue

            pygame.draw.line(
                sphere,
                color + (105,),
                (
                    int(x1r * k),
                    int(y1r * k),
                ),
                (
                    int(x2r * k),
                    int(y2r * k),
                ),
                max(
                    1,
                    int(
                        (
                            1.0
                            if degrees
                            else 2.0
                        )
                        * k
                    ),
                ),
            )

        # ---------------------------------------------------------------
        # Longitude curves.
        #
        # These are subtle curved meridians that make the ladder read
        # as a sphere rather than a flat artificial horizon.
        # ---------------------------------------------------------------

        for longitude in range(-60, 61, 20):
            lon = math.radians(
                longitude + yaw * 0.35
            )

            points = []

            for i in range(61):
                latitude = (
                    -math.pi / 2
                    + math.pi
                    * i
                    / 60.0
                )

                x = (
                    center_x
                    + radius
                    * math.cos(latitude)
                    * math.sin(lon)
                )

                y = (
                    center_y
                    - radius
                    * math.sin(latitude)
                )

                # Compress longitude toward the side of the sphere.
                x = (
                    center_x
                    + (
                        x
                        - center_x
                    )
                    * 0.42
                )

                xr, yr = xf(
                    x,
                    y,
                    bank,
                )

                points.append(
                    (
                        int(xr * k),
                        int(yr * k),
                    )
                )

            strength = (
                ATTITUDE_TICK_MAX_BRIGHTNESS
                * (
                    1.0
                    - abs(
                        longitude
                    ) / 120.0
                )
            )

            color = tuple(
                int(
                    channel
                    * max(
                        0.0,
                        strength,
                    )
                )
                for channel in DIM
            )

            pygame.draw.lines(
                sphere,
                color + (85,),
                False,
                points,
                max(
                    1,
                    int(
                        0.8 * k
                    ),
                ),
            )

        # ---------------------------------------------------------------
        # Spherical pitch ladder.
        # ---------------------------------------------------------------

        for degrees in range(
            -60,
            61,
            10,
        ):
            offset = (
                -degrees
                * PITCH_PX_PER_DEG
            )

            cy = (
                center_y
                + offset
                + pitch * PITCH_PX_PER_DEG
            )

            normalized = (
                abs(
                    degrees
                )
                / 70.0
            )

            half = (
                radius
                * math.sqrt(
                    max(
                        0.0,
                        1.0
                        - min(
                            1.0,
                            normalized,
                        ) ** 2,
                    )
                )
                * 0.62
            )

            if half < 5:
                continue

            if degrees == 0:
                color_base = AMBER
                width = 2.2
            else:
                color_base = CYAN
                width = 1.1

            fade = (
                ATTITUDE_LADDER_MAX_BRIGHTNESS
                - normalized
                * (
                    ATTITUDE_LADDER_MAX_BRIGHTNESS
                    - ATTITUDE_LADDER_MIN_BRIGHTNESS
                )
            )

            color = tuple(
                int(
                    channel
                    * fade
                )
                for channel in color_base
            )

            x1, y1 = xf(
                center_x - half,
                cy,
                bank,
            )

            x2, y2 = xf(
                center_x + half,
                cy,
                bank,
            )

            pygame.draw.line(
                sphere,
                color + (115,),
                (
                    int(x1 * k),
                    int(y1 * k),
                ),
                (
                    int(x2 * k),
                    int(y2 * k),
                ),
                max(
                    1,
                    int(
                        width * k
                    ),
                ),
            )

            # Short center segment.
            inner = half * 0.30

            ix1, iy1 = xf(
                center_x - inner,
                cy,
                bank,
            )

            ix2, iy2 = xf(
                center_x + inner,
                cy,
                bank,
            )

            pygame.draw.line(
                sphere,
                color + (150,),
                (
                    int(ix1 * k),
                    int(iy1 * k),
                ),
                (
                    int(ix2 * k),
                    int(iy2 * k),
                ),
                max(
                    1,
                    int(
                        width * 1.4 * k
                    ),
                ),
            )

        # ---------------------------------------------------------------
        # Faint spherical outline.
        # ---------------------------------------------------------------

        pygame.draw.circle(
            sphere,
            (
                DIM[0],
                DIM[1],
                DIM[2],
                28,
            ),
            (
                int(center_x * k),
                int(center_y * k),
            ),
            int(radius * k),
            max(
                1,
                int(
                    1.0 * k
                ),
            ),
        )

        surface.blit(
            sphere,
            (0, 0),
        )

    # -----------------------------------------------------------------------
    # Heading arc
    # -----------------------------------------------------------------------

    def draw_heading_arc(
        self,
        surface,
        sy,
        k,
        width,
    ):
        """
        Draw the upper visible portion of the heading ring.

        This is intentionally subtle so it reads as part of the same
        spherical instrument rather than a separate dashboard element.
        """

        for i in range(
            HEADING_TICKS
        ):
            heading = (
                i
                * (
                    360.0
                    / HEADING_TICKS
                )
                + sy
            )

            angle = math.radians(
                heading - 90.0
            )

            x = (
                CX
                + HEADING_RADIUS
                * math.cos(angle)
            )

            y = (
                HEADING_CENTER_Y
                + HEADING_RADIUS
                * math.sin(angle)
            )

            if y >= HEADING_CLIP_Y:
                continue

            depth = max(
                0.0,
                math.cos(angle),
            )

            perspective = (
                0.35
                + depth * 0.85
            )

            tick_height = (
                7.0
                + 20.0 * perspective
            )

            tick_width = max(
                1,
                int(
                    (
                        0.8
                        + 1.6
                        * perspective
                    )
                    * k
                ),
            )

            radial_x = math.cos(angle)
            radial_y = math.sin(angle)

            x1 = x
            y1 = y

            x2 = (
                x
                - radial_x
                * tick_height
            )

            y2 = (
                y
                - radial_y
                * tick_height
            )

            if y2 > HEADING_CLIP_Y:
                y2 = HEADING_CLIP_Y

            brightness = (
                0.28
                + 0.32
                * perspective
            )

            color = tuple(
                max(
                    0,
                    min(
                        255,
                        int(
                            channel
                            * brightness
                        ),
                    ),
                )
                for channel in DIM
            )

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
                tick_width,
            )

    # -----------------------------------------------------------------------
    # Drawing
    # -----------------------------------------------------------------------

    def draw(self):
        t = (
            time.monotonic()
            - self.t0
        )

        imu = self.imu_state(t)

        if self.demo:
            gps = self.demo_gps(t)
            gps_age = None

        else:
            gps = self.gps.data

            gps_age = (
                time.monotonic()
                - self.gps.t
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
            int(
                1.6 * k
            ),
        )

        lw2 = max(
            3,
            int(
                2.4 * k
            ),
        )

        boldlw = max(
            2,
            int(
                1.2 * k
            ),
        )

        # -------------------------------------------------------------------
        # Smooth orientation.
        #
        # Pitch and bank remain deliberately swapped compared to imu.py.
        # -------------------------------------------------------------------

        pitch = float(
            imu.get(
                "bank",
                0.0,
            )
        )

        bank = float(
            imu.get(
                "pitch",
                0.0,
            )
        )

        yaw = float(
            imu.get(
                "yaw",
                0.0,
            )
        )

        now = time.monotonic()

        dt = min(
            now - self._last_t,
            0.1,
        )

        self._last_t = now

        alpha = (
            1.0
            - math.exp(
                -dt * 10.0
            )
        )

        self.smooth_pitch += (
            pitch
            - self.smooth_pitch
        ) * alpha

        self.smooth_bank += (
            bank
            - self.smooth_bank
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
                % (
                    sp,
                    sb,
                    sy,
                ),
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
        # Large spherical attitude display
        # -------------------------------------------------------------------

        self.draw_attitude_sphere(
            surface,
            sp,
            sb,
            sy,
            k,
        )

        # -------------------------------------------------------------------
        # Heading ring integrated with sphere
        # -------------------------------------------------------------------

        self.draw_heading_arc(
            surface,
            sy,
            k,
            lw,
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
        # Present graphics first.
        #
        # Text is deliberately NOT rendered to self.ss.
        # -------------------------------------------------------------------

        pygame.transform.smoothscale(
            self.ss,
            (
                self.w,
                self.h,
            ),
            self.screen,
        )

        # -------------------------------------------------------------------
        # Final-resolution text layer
        # -------------------------------------------------------------------

        self.draw_text_layer(
            imu,
            gps,
            gps_age,
        )

        pygame.display.flip()

    # -----------------------------------------------------------------------
    # Final-resolution text
    # -----------------------------------------------------------------------

    def draw_text_layer(
        self,
        imu,
        gps,
        gps_age,
    ):
        # -------------------------------------------------------------------
        # G force
        # -------------------------------------------------------------------

        g_value = float(
            imu.get(
                "g",
                1.0,
            )
        )

        self.text(
            self.screen,
            "G %.2f" % g_value,
            AMBER,
            20,
            18,
            18,
            True,
            "la",
        )

        # -------------------------------------------------------------------
        # Time / date
        # -------------------------------------------------------------------

        now_dt = datetime.now()

        self.text(
            self.screen,
            now_dt.strftime(
                "%H:%M:%S"
            ),
            CYAN,
            VW - 20,
            14,
            30,
            True,
            "ra",
        )

        self.text(
            self.screen,
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
                        float(
                            gps["speed"]
                        )
                        * multiplier
                    )
                )
            )

        self.text(
            self.screen,
            "ground speed",
            DIM,
            20,
            VH - 78,
            12,
            False,
            "la",
        )

        self.text(
            self.screen,
            speed,
            AMBER,
            20,
            VH - 58,
            44,
            True,
            "la",
        )

        font = self.text_font(
            44,
            True,
        )

        if (
            pygame.freetype is not None
            and isinstance(
                font,
                pygame.freetype.Font,
            )
        ):
            speed_rect = font.get_rect(
                speed
            )

            speed_width = (
                speed_rect.width
                / (
                    self.w
                    / VW
                )
            )

        else:
            speed_surface = font.render(
                speed,
                True,
                AMBER,
            )

            speed_width = (
                speed_surface.get_width()
                / (
                    self.w
                    / VW
                )
            )

        self.text(
            self.screen,
            self.unit,
            AMBER,
            20
            + speed_width
            + 14,
            VH - 40,
            14,
            False,
            "la",
        )

        # -------------------------------------------------------------------
        # GPS
        # -------------------------------------------------------------------

        if gps:
            self.text(
                self.screen,
                "gps",
                DIM,
                VW - 20,
                VH - 82,
                12,
                False,
                "ra",
            )

            self.text(
                self.screen,
                "%.5f %s"
                % (
                    abs(
                        float(
                            gps["lat"]
                        )
                    ),
                    "N"
                    if float(
                        gps["lat"]
                    ) >= 0
                    else "S",
                ),
                CYAN,
                VW - 20,
                VH - 64,
                13,
                False,
                "ra",
            )

            self.text(
                self.screen,
                "%.5f %s"
                % (
                    abs(
                        float(
                            gps["lon"]
                        )
                    ),
                    "E"
                    if float(
                        gps["lon"]
                    ) >= 0
                    else "W",
                ),
                CYAN,
                VW - 20,
                VH - 46,
                13,
                False,
                "ra",
            )

            self.text(
                self.screen,
                "%d sats - hdop %s"
                % (
                    int(
                        gps.get(
                            "sats",
                            0,
                        )
                    ),
                    gps.get(
                        "hdop",
                        "-",
                    ),
                ),
                DIM,
                VW - 20,
                VH - 28,
                12,
                False,
                "ra",
            )

        else:
            self.text(
                self.screen,
                "gps",
                DIM,
                VW - 20,
                VH - 60,
                12,
                False,
                "ra",
            )

            self.text(
                self.screen,
                "awaiting phone link",
                DIM,
                VW - 20,
                VH - 42,
                12,
                False,
                "ra",
            )

        # -------------------------------------------------------------------
        # IMU status
        # -------------------------------------------------------------------

        if not imu.get("ok"):
            self.text(
                self.screen,
                "awaiting glasses link",
                DIM,
                CX,
                170,
                15,
                False,
                "ca",
            )

        # -------------------------------------------------------------------
        # Menu
        # -------------------------------------------------------------------

        if self.menu:
            self.draw_menu(
                imu,
                gps,
                gps_age,
            )

    # -----------------------------------------------------------------------
    # Menu
    # -----------------------------------------------------------------------

    def draw_menu(
        self,
        imu,
        gps,
        gps_age,
    ):
        """
        Menu panel is rendered directly to the physical display.

        This keeps all text in the same crisp rendering path.
        """

        scale_x = self.w / VW
        scale_y = self.h / VH

        x = 16
        y = 64
        w = 300
        h = 170

        panel = pygame.Surface(
            (
                int(
                    w * scale_x
                ),
                int(
                    h * scale_y
                ),
            ),
            pygame.SRCALPHA,
        )

        panel.fill(
            (
                0,
                0,
                0,
                230,
            )
        )

        self.screen.blit(
            panel,
            (
                int(x * scale_x),
                int(y * scale_y),
            ),
        )

        pygame.draw.rect(
            self.screen,
            DIM,
            (
                int(x * scale_x),
                int(y * scale_y),
                int(w * scale_x),
                int(h * scale_y),
            ),
            max(
                2,
                int(
                    1.2
                    * min(
                        scale_x,
                        scale_y,
                    )
                ),
            ),
            3,
        )

        self.text(
            self.screen,
            "data links",
            DIM,
            x + 14,
            y + 10,
            12,
        )

        self.text(
            self.screen,
            "glasses imu",
            CYAN,
            x + 14,
            y + 34,
            13,
        )

        self.text(
            self.screen,
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
            self.screen,
            "phone gps",
            CYAN,
            x + 14,
            y + 56,
            13,
        )

        self.text(
            self.screen,
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
            self.screen,
            "imu rate",
            CYAN,
            x + 14,
            y + 78,
            13,
        )

        self.text(
            self.screen,
            "%d hz"
            % imu.get(
                "hz",
                0,
            ),
            AMBER,
            x + w - 14,
            y + 78,
            13,
            False,
            "ra",
        )

        self.text(
            self.screen,
            "gps age",
            CYAN,
            x + 14,
            y + 100,
            13,
        )

        self.text(
            self.screen,
            "-"
            if gps_age is None
            else "%.1f s"
            % gps_age,
            AMBER,
            x + w - 14,
            y + 100,
            13,
            False,
            "ra",
        )

        self.text(
            self.screen,
            "a/c recenter  y/u units",
            DIM,
            x + 14,
            y + 128,
            12,
        )

        self.text(
            self.screen,
            "m menu  b/esc quit",
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
