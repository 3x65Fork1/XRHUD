import pygame


CYAN = (63, 217, 255)
AMBER = (255, 179, 64)
DIM = (138, 154, 165)
RED = (255, 93, 93)


class MenuRenderer:
    def __init__(self, text_renderer):
        self.text = text_renderer

    def draw(
        self,
        surface,
        imu,
        gps,
        gps_age,
        k,
    ):
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

        self.text.text(
            surface,
            "data links",
            DIM,
            x + 14,
            y + 10,
            12,
        )

        self.text.text(
            surface,
            "glasses imu",
            CYAN,
            x + 14,
            y + 34,
            13,
        )

        self.text.text(
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

        self.text.text(
            surface,
            "phone gps",
            CYAN,
            x + 14,
            y + 56,
            13,
        )

        self.text.text(
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

        self.text.text(
            surface,
            "imu rate",
            CYAN,
            x + 14,
            y + 78,
            13,
        )

        self.text.text(
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

        self.text.text(
            surface,
            "gps age",
            CYAN,
            x + 14,
            y + 100,
            13,
        )

        self.text.text(
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

        self.text.text(
            surface,
            "a/c recenter  y/u units",
            DIM,
            x + 14,
            y + 128,
            12,
        )

        self.text.text(
            surface,
            "m menu  b/esc quit",
            DIM,
            x + 14,
            y + 146,
            12,
        )
