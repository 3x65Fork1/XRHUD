import os

import pygame


FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)

FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def _find_font(bold=False):
    paths = (
        FONT_PATHS_BOLD
        if bold
        else FONT_PATHS
    )

    for path in paths:
        if os.path.isfile(path):
            return path

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


class TextRenderer:
    def __init__(self, scale):
        self.k = scale
        self.fonts = {}

    def font(self, logical_px, bold=False):
        """
        Return a real antialiased TrueType font.

        Render fonts at logical HUD resolution. The entire HUD is then
        scaled to the display.
        """

        px = max(
            8,
            int(logical_px * self.k),
        )

        key = (
            px,
            bool(bold),
        )

        if key in self.fonts:
            return self.fonts[key]

        path = _find_font(bold)

        if path:
            font = pygame.font.Font(
                path,
                px,
            )

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

        font = self.font(
            size,
            bold,
        )

        rendered = font.render(
            str(txt),
            True,
            color,
        )

        rect = rendered.get_rect()

        px = int(
            round(x * self.k)
        )

        py = int(
            round(y * self.k)
        )

        if anchor == "ra":
            rect.topright = (
                px,
                py,
            )

        elif anchor == "ca":
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
