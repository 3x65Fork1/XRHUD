import math

import pygame


# ---------------------------------------------------------------------------
# HUD configuration
# ---------------------------------------------------------------------------

VW, VH = 640.0, 360.0
CX, CY = 320.0, 190.0

CYAN = (63, 217, 255)
AMBER = (255, 179, 64)
DIM = (138, 154, 165)
RED = (255, 93, 93)


# ---------------------------------------------------------------------------
# Unified attitude / heading instrument
# ---------------------------------------------------------------------------

ATTITUDE_CENTER_X = CX
ATTITUDE_CENTER_Y = 145.0

ATTITUDE_RADIUS = 205.0

HEADING_TICKS = 72

ATTITUDE_CLIP_Y = 238.0

ATTITUDE_HALF_WIDTH = 165.0

ATTITUDE_PERSPECTIVE = 0.42

ATTITUDE_TICK_MIN_BRIGHTNESS = 0.14
ATTITUDE_TICK_MAX_BRIGHTNESS = 0.52

ATTITUDE_LADDER_MIN_BRIGHTNESS = 0.16
ATTITUDE_LADDER_MAX_BRIGHTNESS = 0.60

PITCH_PX_PER_DEG = 3.0


class AttitudeRenderer:
    def draw(
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
