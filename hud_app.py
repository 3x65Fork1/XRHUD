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

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)

# ---------------------------------------------------------------------------
# Unified attitude / heading instrument
# ---------------------------------------------------------------------------

# The entire attitude instrument is biased upward.
# The center of the display remains comparatively open.
ATTITUDE_CENTER_X = CX
ATTITUDE_CENTER_Y = 122.0

# Heading ring.
ATTITUDE_RADIUS = 132.0

# Number of heading ticks around the full circle.
HEADING_TICKS = 72

# Only the upper portion of the spherical instrument is visible.
ATTITUDE_CLIP_Y = 182.0

# Width of the pitch surface.
ATTITUDE_HALF_WIDTH = 108.0

# Perspective amount applied to the pitch ladder.
ATTITUDE_PERSPECTIVE = 0.32


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
    # Unified attitude / heading instrument
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
        Draw the heading scale and pitch ladder as one unified,
        three-dimensional attitude reference.

        The heading scale forms the outer spherical rim.

        The pitch ladder is projected onto that same implied surface,
        rather than behaving like an independent flat ladder.
        """

        # -------------------------------------------------------------------
        # Shared instrument transform
        # -------------------------------------------------------------------

        center_x = ATTITUDE_CENTER_X
        center_y = ATTITUDE_CENTER_Y
        radius = ATTITUDE_RADIUS

        # Bank rotates the entire attitude surface.
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
        # Heading rim
        # -------------------------------------------------------------------

        # Yaw shifts the heading positions around the same sphere.
        heading_step = 360.0 / HEADING_TICKS

        for i in range(HEADING_TICKS):

            heading = (
                i * heading_step
                + yaw
            )

            angle = math.radians(
                heading - 90.0
            )

            # Circular base position.
            local_x = (
                center_x
                + radius * math.cos(angle)
            )

            local_y = (
                center_y
                + radius * math.sin(angle)
            )

            # The circle is the outer boundary of the virtual sphere.
            #
            # Keep only the forward/upper portion.
            if local_y >= ATTITUDE_CLIP_Y:
                continue

            # ---------------------------------------------------------------
            # Depth.
            #
            # The top-center is closest to the viewer.
            # The sides fall away into depth.
            # ---------------------------------------------------------------

            front = max(
                0.0,
                math.cos(angle),
            )

            depth = (
                0.30
                + front * 0.70
            )

            # Major ticks every 15 degrees.
            major = (
                i % 3 == 0
            )

            # Cardinal ticks every 45 degrees.
            cardinal = (
                i % 9 == 0
            )

            if cardinal:
                tick_height = 18.0
                base_width = 2.4

            elif major:
                tick_height = 13.0
                base_width = 1.8

            else:
                tick_height = 7.0
                base_width = 1.1

            tick_height *= (
                0.65
                + depth * 0.65
            )

            tick_width = max(
                1,
                int(
                    base_width
                    * (
                        0.65
                        + depth * 0.55
                    )
                    * k
                ),
            )

            # Radial direction.
            radial_x = math.cos(angle)
            radial_y = math.sin(angle)

            outer_x = local_x
            outer_y = local_y

            inner_x = (
                local_x
                - radial_x * tick_height
            )

            inner_y = (
                local_y
                - radial_y * tick_height
            )

            # Rotate the whole spherical reference with bank.
            x1, y1 = rotate_point(
                outer_x,
                outer_y,
            )

            x2, y2 = rotate_point(
                inner_x,
                inner_y,
            )

            # Clip below the visible horizon.
            if (
                y1 >= ATTITUDE_CLIP_Y
                and y2 >= ATTITUDE_CLIP_Y
            ):
                continue

            brightness = (
                0.40
                + depth * 0.60
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

        # -------------------------------------------------------------------
        # Pitch ladder
        # -------------------------------------------------------------------

        # The pitch ladder is projected onto the same circular surface.
        #
        # Positive pitch moves the attitude surface downward, matching
        # the existing attitude convention.
        pitch_offset = (
            pitch * PITCH_PX_PER_DEG
        )

        for degrees in range(
            -90,
            91,
            10,
        ):

            # Vertical position on the shared spherical surface.
            local_y = (
                center_y
                - degrees * PITCH_PX_PER_DEG
                + pitch_offset
            )

            # Distance from the center of the sphere.
            vertical = (
                local_y
                - center_y
            )

            normalized = (
                vertical / radius
            )

            # Outside the spherical surface.
            if abs(normalized) > 1.0:
                continue

            # Spherical cross-section.
            sphere_width = math.sqrt(
                max(
                    0.0,
                    1.0 - normalized * normalized,
                )
            )

            # Perspective deliberately compresses the outer portions.
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

            # Keep the center section stronger.
            if degrees == 0:
                half_width *= 1.08

            # Pitch lines become slightly more dimensional toward the
            # center/front of the virtual sphere.
            depth = (
                1.0
                - abs(normalized)
            )

            brightness = (
                0.48
                + 0.52 * depth
            )

            if degrees == 0:
                base_color = CYAN
                line_width = lw2

            elif abs(degrees) <= 30:
                base_color = AMBER
                line_width = lw

            else:
                base_color = RED
                line_width = lw

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

            # ---------------------------------------------------------------
            # Main projected pitch line.
            # ---------------------------------------------------------------

            x1 = (
                center_x
                - half_width
            )

            x2 = (
                center_x
                + half_width
            )

            y = local_y

            p1 = rotate_point(
                x1,
                y,
            )

            p2 = rotate_point(
                x2,
                y,
            )

            # Don't allow the attitude element to invade the lower
            # sightline.
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

            # ---------------------------------------------------------------
            # Short inner line.
            #
            # This gives the ladder a layered / dimensional appearance
            # without introducing a separate visual element.
            # ---------------------------------------------------------------

            inner_half = (
                half_width
                * 0.34
            )

            inner_color = tuple(
                int(channel * 0.55)
                for channel in color
            )

            ip1 = rotate_point(
                center_x - inner_half,
                y,
            )

            ip2 = rotate_point(
                center_x + inner_half,
                y,
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
                    lw,
                ),
            )

        # -------------------------------------------------------------------
        # Central attitude reference.
        #
        # This is deliberately very small and sits above the actual
        # sightline so the center remains open.
        # -------------------------------------------------------------------

        marker_y = center_y + 38.0

        marker_half = 15.0

        p1 = rotate_point(
            center_x - marker_half,
            marker_y,
        )

        p2 = rotate_point(
            center_x - 4.0,
            marker_y,
        )

        p3 = rotate_point(
            center_x + 4.0,
            marker_y,
        )

        p4 = rotate_point(
            center_x + marker_half,
            marker_y,
        )

        pygame.draw.line(
            surface,
            CYAN,
            (
                int(p1[0] * k),
                int(p1[1] * k),
            ),
            (
                int(p2[0] * k),
                int(p2[1] * k),
            ),
            lw2,
        )

        pygame.draw.line(
            surface,
            CYAN,
            (
                int(p3[0] * k),
                int(p3[1] * k),
            ),
            (
                int(p4[0] * k),
                int(p4[1] * k),
            ),
            lw2,
        )

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
        # Unified attitude / heading element
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
                    abs(float(gps["lat"])),
                    "N"
                    if float(gps["lat"]) >= 0
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
                    abs(float(gps["lon"])),
                    "E"
                    if float(gps["lon"]) >= 0
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
                    int(gps.get("sats", 0)),
                    gps.get("hdop", "-"),
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
