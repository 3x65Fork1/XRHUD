import math
import os
import time
from datetime import datetime

import pygame

from attitude import (
    AttitudeRenderer,
    CYAN,
    AMBER,
    DIM,
    RED,
    VW,
    VH,
    CX,
)

from gps import GpsListener
from imulogic import ImuManager
from menu import MenuRenderer
from text import TextRenderer


BG = (0, 0, 0)

GPS_STALE_S = 6.0


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

        self.k = min(
            self.w / VW,
            self.h / VH,
        )

        self.ss = pygame.Surface(
            (
                int(VW * self.k),
                int(VH * self.k),
            ),
            pygame.SRCALPHA,
        )

        self.text = TextRenderer(
            self.k
        )

        self.attitude = AttitudeRenderer()

        self.menu_renderer = MenuRenderer(
            self.text
        )

        self.demo = demo
        self.debug = debug

        self.gps = GpsListener(
            gps_port
        )

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

        self.attitude.draw(
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

        self.text.text(
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

        self.text.text(
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

        self.text.text(
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

        self.text.text(
            surface,
            "ground speed",
            DIM,
            20,
            VH - 78,
            12,
            False,
            "la",
        )

        self.text.text(
            surface,
            speed,
            AMBER,
            20,
            VH - 58,
            44,
            True,
            "la",
        )

        speed_font = self.text.font(
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

        self.text.text(
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
            self.text.text(
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 82,
                12,
                False,
                "ra",
            )

            self.text.text(
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

            self.text.text(
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

            self.text.text(
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
            self.text.text(
                surface,
                "gps",
                DIM,
                VW - 20,
                VH - 60,
                12,
                False,
                "ra",
            )

            self.text.text(
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
            self.menu_renderer.draw(
                surface,
                imu,
                gps,
                gps_age,
                k,
            )

        # -------------------------------------------------------------------
        # IMU disconnected indicator
        # -------------------------------------------------------------------

        if not imu.get("ok"):
            self.text.text(
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

        self.screen.blit(
            self.ss,
            (
                (
                    self.w
                    - self.ss.get_width()
                ) // 2,
                (
                    self.h
                    - self.ss.get_height()
                ) // 2,
            ),
        )

        pygame.display.flip()

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
