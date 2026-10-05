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
# Fonts
#
# Prefer real TrueType fonts.  pygame's default bitmap font is intentionally
# avoided because it can appear as blocky text on the scaled HUD.
# ---------------------------------------------------------------------------

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


# ---------------------------------------------------------------------------
# Unified attitude / heading instrument
# ---------------------------------------------------------------------------

# Large transparent spherical reference.
ATTITUDE_CENTER_X = CX
ATTITUDE_CENTER_Y = 145.0

# Large enough to become the dominant visual instrument.
ATTITUDE_RADIUS = 205.0

HEADING_TICKS = 72

# Allow the large sphere to occupy the upper/middle HUD while leaving
# the bottom telemetry unobstructed.
ATTITUDE_CLIP_Y = 238.0

# Width of the projected pitch surface.
ATTITUDE_HALF_WIDTH = 165.0

# Amount of perspective compression toward the edges.
ATTITUDE_PERSPECTIVE = 0.42

# Transparency is achieved by reducing RGB intensity rather than drawing
# a filled translucent sphere.
ATTITUDE_TICK_MIN_BRIGHTNESS = 0.14
ATTITUDE_TICK_MAX_BRIGHTNESS = 0.52

ATTITUDE_LADDER_MIN_BRIGHTNESS = 0.16
ATTITUDE_LADDER_MAX_BRIGHTNESS = 0.60


# ---------------------------------------------------------------------------
# Font discovery
# ---------------------------------------------------------------------------

def _find_font(bold=False):
    paths = (
        FONT_PATHS_BOLD
        if bold
        else FONT_PATHS
    )

    for path in paths:
        if os.path.isfile(path):
            return path

    # Last-resort system font lookup.
    try:
        name = (
            "DejaVu Sans"
            if not bold
            else "DejaVu Sans"
        )

        path = pygame.font.match_font(
            name,
            bold=bold,
        )

        if path:
            return path

    except Exception:
        pass

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

        self.sock.bind(
            ("0.0.0.0", port)
        )

        self.sock.settimeout(1.0)

    def run(self):
        while True:
            try:
                raw, _ = self.sock.recvfrom(2048)

                self.data = json.loads(
                    raw.decode(
                        "utf-8",
                        "replace",
                    )
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

                deadline = (
                    time.monotonic()
                    + 5.0
                )

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
                    "[hud] imu error: "
                    f"{type(exc).__name__}: {exc}",
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

        self.pads = []

        for i in range(
            pygame.joystick.get_count()
        ):
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

        pygame.display.set_caption(
            "xreal hud"
        )

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
            with open(
                self._cfg(),
                "r",
                encoding="utf-8",
            ) as f:
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

            with open(
                path,
                "w",
                encoding="utf-8",
            ) as f:
                f.write(self.unit)

        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Text
    # -----------------------------------------------------------------------

    def font(self, logical_px, bold=False):
        """
        Return a real antialiased TrueType font.

        Render fonts at logical HUD resolution. The entire HUD is then
        scaled to the display.
        """

        px = max(8, int(logical_px * self.k))

        key = (px, bool(bold))

        if key in self.fonts:
            return self.fonts[key]

        path = _find_font(bold)

        if path:
            font = pygame.font.Font(path, px)
        else:
            font = pygame.font.SysFont(
                "dejavusans",
                px,
                bold=bool(bold),
            )

        self.fonts[key] = font
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
        Render antialiased TrueType text onto the HUD.
        """

        font = self.font(size, bold)

        rendered = font.render(
            str(txt),
            True,
            color,
        )

        # IMPORTANT:
        # Do not manipulate the font surface's pixel format.
        # pygame handles the alpha channel correctly when blitting
        # an antialiased font surface onto an SRCALPHA surface.

        rect = rendered.get_rect()

        px = int(round(x * self.k))
        py = int(round(y * self.k))

        if anchor == "ra":
            rect.topright = (px, py)

        elif anchor == "ca":
            rect.midtop = (px, py)

        else:
            rect.topleft = (px, py)

        surface.blit(rendered, rect)

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
                "g": 1.0
                + math.sin(t / 0.9) * 0.12,
            }

        return self.imu.state

    # -----------------------------------------------------------------------
    # Demo GPS
    # -----------------------------------------------------------------------

    def demo_gps(self, t):
        return {
            "lat": (
                37.7749
                + math.sin(t / 60.0) * 0.001
            ),
            "lon": (
                -122.4194
                + math.cos(t / 60.0) * 0.001
            ),
            "speed": (
                24.6
                + math.sin(t / 4.0) * 3.0
            ),
            "sats": 14,
            "hdop": 0.8,
        }

    # -----------------------------------------------------------------------
    # Unified spherical attitude instrument
    # -----------------------------------------------------------------------

    def draw_attitude_instrument(
        self,
        surface,
        pitch,
        bank,
        yaw,
        k,
        lw,
        lw2,
    ):
        """
        Large, transparent, unified spherical attitude reference.

        Heading and pitch are projected onto the same implied sphere.

        No opaque background is drawn.
        No labels are drawn inside the instrument.
        """

        center_x = ATTITUDE_CENTER_X
        center_y = ATTITUDE_CENTER_Y
        radius = ATTITUDE_RADIUS

        # -------------------------------------------------------------------
        # Shared bank transform
        # -------------------------------------------------------------------

        bank_r = math.radians(-bank)

        cos_b = math.cos(bank_r)
        sin_b = math.sin(bank_r)

        def rotate_point(x, y):
            dx = x - center_x
            dy = y - center_y

            return (
                center_x
                + dx * cos_b
                - dy * sin_b,
                center_y
                + dx * sin_b
                + dy * cos_b,
            )

        # -------------------------------------------------------------------
        # Heading ring
        # -------------------------------------------------------------------

        heading_step = (
            360.0 / HEADING_TICKS
        )

        for i in range(
            HEADING_TICKS
        ):
            heading = (
                i * heading_step
                + yaw
            )

            angle = math.radians(
                heading - 90.0
            )

            outer_x = (
                center_x
                + radius
                * math.cos(angle)
            )

            outer_y = (
                center_y
                + radius
                * math.sin(angle)
            )

            if outer_y >= ATTITUDE_CLIP_Y:
                continue

            # ---------------------------------------------------------------
            # Spherical depth.
            #
            # The top/front of the sphere is stronger.
            # The sides recede into transparency.
            # ---------------------------------------------------------------

            front = max(
                0.0,
                math.cos(angle),
            )

            depth = (
                0.12
                + front * 0.88
            )

            side_fade = (
                0.30
                + 0.70 * front
            )

            brightness = (
                ATTITUDE_TICK_MIN_BRIGHTNESS
                + (
                    ATTITUDE_TICK_MAX_BRIGHTNESS
                    - ATTITUDE_TICK_MIN_BRIGHTNESS
                )
                * depth
                * side_fade
            )

            # ---------------------------------------------------------------
            # Tick hierarchy
            # ---------------------------------------------------------------

            cardinal = (
                i % 9 == 0
            )

            major = (
                i % 3 == 0
            )

            if cardinal:
                tick_height = 22.0
                tick_base_width = 2.2

            elif major:
                tick_height = 15.0
                tick_base_width = 1.5

            else:
                tick_height = 8.0
                tick_base_width = 0.9

            tick_height *= (
                0.45
                + depth * 0.75
            )

            tick_width = max(
                1,
                int(
                    tick_base_width
                    * (
                        0.50
                        + depth * 0.70
                    )
                    * k
                ),
            )

            radial_x = math.cos(angle)
            radial_y = math.sin(angle)

            inner_x = (
                outer_x
                - radial_x
                * tick_height
            )

            inner_y = (
                outer_y
                - radial_y
                * tick_height
            )

            p1 = rotate_point(
                outer_x,
                outer_y,
            )

            p2 = rotate_point(
                inner_x,
                inner_y,
            )

            if (
                p1[1] >= ATTITUDE_CLIP_Y
                and p2[1] >= ATTITUDE_CLIP_Y
            ):
                continue

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
                    int(p1[0] * k),
                    int(p1[1] * k),
                ),
                (
                    int(p2[0] * k),
                    int(p2[1] * k),
                ),
                tick_width,
            )

        # -------------------------------------------------------------------
        # Pitch ladder projected onto the sphere
        # -------------------------------------------------------------------

        pitch_offset = (
            pitch
            * PITCH_PX_PER_DEG
        )

        for degrees in range(
            -90,
            91,
            10,
        ):
            local_y = (
                center_y
                - degrees
                * PITCH_PX_PER_DEG
                + pitch_offset
            )

            vertical = (
                local_y
                - center_y
            )

            normalized = (
                vertical
                / radius
            )

            if abs(normalized) > 1.0:
                continue

            # Width of the circular cross-section.
            sphere_width = math.sqrt(
                max(
                    0.0,
                    1.0
                    - normalized
                    * normalized,
                )
            )

            perspective = (
                1.0
                - ATTITUDE_PERSPECTIVE
                * abs(normalized)
            )

            half_width = (
                ATTITUDE_HALF_WIDTH
                * sphere_width
                * perspective
            )

            if degrees == 0:
                half_width *= 1.05

            # ----------------------------------------------------------------
            # Depth / transparency
            # ----------------------------------------------------------------

            center_depth = (
                1.0
                - abs(normalized)
            )

            brightness = (
                ATTITUDE_LADDER_MIN_BRIGHTNESS
                + (
                    ATTITUDE_LADDER_MAX_BRIGHTNESS
                    - ATTITUDE_LADDER_MIN_BRIGHTNESS
                )
                * center_depth
            )

            # ----------------------------------------------------------------
            # Existing cyan -> amber -> red language
            # ----------------------------------------------------------------

            distance = abs(degrees)

            if distance <= 20:
                base_color = CYAN

            elif distance <= 50:
                blend = (
                    distance - 20.0
                ) / 30.0

                base_color = tuple(
                    int(
                        CYAN[i]
                        + (
                            AMBER[i]
                            - CYAN[i]
                        )
                        * blend
                    )
                    for i in range(3)
                )

            else:
                blend = min(
                    1.0,
                    (
                        distance - 50.0
                    ) / 40.0,
                )

                base_color = tuple(
                    int(
                        AMBER[i]
                        + (
                            RED[i]
                            - AMBER[i]
                        )
                        * blend
                    )
                    for i in range(3)
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
                for channel in base_color
            )

            if degrees == 0:
                line_width = lw2

            elif distance <= 30:
                line_width = lw

            else:
                line_width = max(
                    1,
                    int(
                        lw * 0.72
                    ),
                )

            # ----------------------------------------------------------------
            # Main pitch line
            # ----------------------------------------------------------------

            x1 = (
                center_x
                - half_width
            )

            x2 = (
                center_x
                + half_width
            )

            p1 = rotate_point(
                x1,
                local_y,
            )

            p2 = rotate_point(
                x2,
                local_y,
            )

            if (
                p1[1] > ATTITUDE_CLIP_Y
                and p2[1] > ATTITUDE_CLIP_Y
            ):
                continue

            pygame.draw.line(
                surface,
                color,
                (
                    int(p1[0] * k),
                    int(p1[1] * k),
                ),
                (
                    int(p2[0] * k),
                    int(p2[1] * k),
                ),
                line_width,
            )

            # ----------------------------------------------------------------
            # Very faint inner detail
            # ----------------------------------------------------------------

            inner_half = (
                half_width * 0.28
            )

            inner_color = tuple(
                int(
                    channel * 0.30
                )
                for channel in color
            )

            ip1 = rotate_point(
                center_x - inner_half,
                local_y,
            )

            ip2 = rotate_point(
                center_x + inner_half,
                local_y,
            )

            pygame.draw.line(
                surface,
                inner_color,
                (
                    int(ip1[0] * k),
                    int(ip1[1] * k),
                ),
                (
                    int(ip2[0] * k),
                    int(ip2[1] * k),
                ),
                max(
                    1,
                    int(
                        lw * 0.60
                    ),
                ),
            )

        # -------------------------------------------------------------------
        # Segmented spherical rim
        #
        # No filled circle. Just a very faint boundary.
        # -------------------------------------------------------------------

        rim_points = []

        for i in range(73):
            angle = math.radians(
                -180.0
                + i * 5.0
            )

            x = (
                center_x
                + radius
                * math.cos(angle)
            )

            y = (
                center_y
                + radius
                * math.sin(angle)
            )

            if y < ATTITUDE_CLIP_Y:
                rim_points.append(
                    rotate_point(x, y)
                )

        for i in range(
            len(rim_points) - 1
        ):
            # Gaps make the sphere feel transparent rather than outlined.
            if i % 2:
                continue

            p1 = rim_points[i]
            p2 = rim_points[i + 1]

            pygame.draw.line(
                surface,
                (
                    int(DIM[0] * 0.18),
                    int(DIM[1] * 0.18),
                    int(DIM[2] * 0.18),
                ),
                (
                    int(p1[0] * k),
                    int(p1[1] * k),
                ),
                (
                    int(p2[0] * k),
                    int(p2[1] * k),
                ),
                max(
                    1,
                    int(0.75 * k),
                ),
            )

        # -------------------------------------------------------------------
        # Tiny central reference
        # -------------------------------------------------------------------

        marker_y = (
            center_y + 32.0
        )

        marker_half = 13.0

        p1 = rotate_point(
            center_x - marker_half,
            marker_y,
        )

        p2 = rotate_point(
            center_x - 3.0,
            marker_y,
        )

        p3 = rotate_point(
            center_x + 3.0,
            marker_y,
        )

        p4 = rotate_point(
            center_x + marker_half,
            marker_y,
        )

        marker_color = tuple(
            int(channel * 0.62)
            for channel in CYAN
        )

        pygame.draw.line(
            surface,
            marker_color,
            (
                int(p1[0] * k),
                int(p1[1] * k),
            ),
            (
                int(p2[0] * k),
                int(p2[1] * k),
            ),
            lw,
        )

        pygame.draw.line(
            surface,
            marker_color,
            (
                int(p3[0] * k),
                int(p3[1] * k),
            ),
            (
                int(p4[0] * k),
                int(p4[1] * k),
            ),
            lw,
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
        # Pitch and bank deliberately swapped compared to imu.py.
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
        # Unified spherical attitude element
        # -------------------------------------------------------------------

        self.draw_attitude_instrument(
            surface,
            sp,
            sb,
            sy,
            k,
            lw,
            lw2,
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
        # G-force - top left
        # -------------------------------------------------------------------

        g_value = float(
            imu.get("g", 1.0)
        )

        self.text(
            surface,
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
            surface,
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
                        float(
                            gps["speed"]
                        )
                        * multiplier
                    )
                )
            )

        self.text(
            surface,
            "ground speed",
            DIM,
            20,
            VH - 78,
            12,
            False,
            "la",
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

        # Position unit based on rendered width instead of assuming
        # a fixed character width.
        speed_font = self.font(
            44,
            True,
        )

        speed_surface = speed_font.render(
            speed,
            True,
            AMBER,
        )

        speed_width = (
            speed_surface.get_width()
            / k
        )

        self.text(
            surface,
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
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 82,
                12,
                False,
                "ra",
            )

            self.text(
                surface,
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
                surface,
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
                surface,
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
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 60,
                12,
                False,
                "ra",
            )

            self.text(
                surface,
                "awaiting phone link",
                DIM,
                VW - 20,
                VH - 42,
                12,
                False,
                "ra",
            )

        # -------------------------------------------------------------------
        # Menu
        # -------------------------------------------------------------------

        if self.menu:
            self.draw_menu(
                surface,
                imu,
                gps,
                gps_age,
            )

        # -------------------------------------------------------------------
        # IMU disconnected indicator
        # -------------------------------------------------------------------

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
                int(x * k),
                int(y * k),
            ),
        )

        pygame.draw.rect(
            surface,
            DIM,
            (
                int(x * k),
                int(y * k),
                int(w * k),
                int(h * k),
            ),
            max(
                2,
                int(1.2 * k),
            ),
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
