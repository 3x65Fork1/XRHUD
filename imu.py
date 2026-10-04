#!/usr/bin/env python3
"""
Xreal Air IMU reader + quaternion AHRS.

Supports:
  - python-hidapi: hid.device()
  - python-hid:    hid.Device()

The Xreal Air uses:
  VID 0x3318
  PID 0x0424
  IMU HID interface 3

Protocol:
  [0xAA][crc32 u32le][len u16le][msgid u8][data...]

Orientation:
  - Gyroscope provides fast rotational response.
  - Accelerometer provides gravity reference for roll/pitch.
  - Accelerometer correction is reduced during strong linear motion.
  - Yaw is gyro-integrated and will drift because the magnetometer
    is decoded but intentionally not used for heading correction.
  - Recenter stores a quaternion reference rather than subtracting
    Euler angles.

The public ImuReader.state interface remains:

    {
        "ok": bool,
        "hz": float,
        "pitch": float,
        "bank": float,
        "yaw": float,
        "g": float,
        "temp_c": float,
    }

No axis swapping is performed here. If the HUD deliberately maps
pitch/bank differently, that remains the HUD's responsibility.
"""

import math
import os
import struct
import sys
import threading
import time
import zlib

try:
    import hid
except ImportError:
    sys.exit(
        "missing dependency: sudo pacman -S python-hidapi "
        "(or python-hid)"
    )


# ---------------------------------------------------------------------------
# Xreal device / protocol
# ---------------------------------------------------------------------------

VID = 0x3318

KNOWN_PIDS = {
    0x0424: 3,  # Xreal Air
    0x0428: 3,  # Xreal Air 2
    0x0432: 3,  # Xreal Air 2 Pro
    0x0426: 2,  # Xreal Air 2 Ultra
}

PKT = 0x40

MSG_CAL_LENGTH = 0x14
MSG_CAL_SEGMENT = 0x15
MSG_STREAM = 0x19
MSG_STATIC_ID = 0x1A


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

# Mahony proportional feedback.
#
# Larger = stronger gravity correction, but more sensitivity to
# acceleration/vibration.
MAHONY_KP = 1.8

# Integral gyro-bias correction.
#
# Kept deliberately small because the explicit startup gyro calibration
# already removes most of the static bias.
MAHONY_KI = 0.015

# Number of samples used to estimate gyro bias at startup.
GYRO_CAL_SAMPLES = 150

# Number of samples used to establish the initial 1g reference.
ACCEL_CAL_SAMPLES = 150

# Nominal gravity magnitude is whatever the sensor reports during startup.
# This makes the G meter independent of the sensor's raw scaling.
ACCEL_MIN_G = 0.70
ACCEL_MAX_G = 1.30

# Below this interval, the accelerometer is considered trustworthy.
# Between MIN and MAX the correction is gradually reduced.
ACCEL_FULL_WEIGHT = 0.97
ACCEL_ZERO_WEIGHT = 0.20

# Final orientation smoothing inside the sensor thread.
#
# The HUD already performs additional frame-rate-independent smoothing,
# so this is intentionally mild.
ORIENTATION_SMOOTH_TAU = 0.025

GYRO_DPS_TO_RAD = math.pi / 180.0


# ---------------------------------------------------------------------------
# Packet codec
# ---------------------------------------------------------------------------

def build_msg(msgid: int, data: bytes = b"") -> bytes:
    """Build an Xreal framed command."""
    body = struct.pack("<HB", 3 + len(data), msgid) + data
    return b"\xAA" + struct.pack(
        "<I",
        zlib.crc32(body) & 0xFFFFFFFF,
    ) + body


def _recv_msg(rd, msgid: int, want_len: int, timeout_ms: int):
    """Read framed response packets until one matches msgid."""
    end = time.monotonic() + timeout_ms / 1000.0

    while time.monotonic() < end:
        pkt = rd(PKT, 100)

        if not pkt:
            continue

        pkt = bytes(pkt)

        if (
            len(pkt) >= 8
            and pkt[0] == 0xAA
            and pkt[7] == msgid
        ):
            return pkt[8:8 + want_len]

    return None


def _s24(b: bytes) -> int:
    """3-byte little-endian signed integer."""
    v = b[0] | (b[1] << 8) | (b[2] << 16)

    return (
        v - (1 << 24)
        if v & 0x800000
        else v
    )


def _mag_s16(b0: int, b1: int) -> int:
    """Magnetometer reading with inverted sign bit."""
    v = b0 | ((b1 ^ 0x80) << 8)

    return (
        v - (1 << 16)
        if v & 0x8000
        else v
    )


def decode(pkt: bytes):
    """Decode a 64-byte Xreal IMU event packet."""

    if (
        len(pkt) < PKT
        or pkt[0] != 0x01
        or pkt[1] != 0x02
    ):
        return None

    temp = struct.unpack_from("<h", pkt, 0x02)[0]
    ts = struct.unpack_from("<Q", pkt, 0x04)[0]

    gmul, gdiv = struct.unpack_from("<hi", pkt, 0x0C)

    gx, gy, gz = (
        _s24(pkt[o:o + 3])
        for o in (0x12, 0x15, 0x18)
    )

    amul, adiv = struct.unpack_from("<hi", pkt, 0x1B)

    ax, ay, az = (
        _s24(pkt[o:o + 3])
        for o in (0x21, 0x24, 0x27)
    )

    mmul, mdiv = struct.unpack_from(">hi", pkt, 0x2A)

    mx = _mag_s16(pkt[0x30], pkt[0x31])
    my = _mag_s16(pkt[0x32], pkt[0x33])
    mz = _mag_s16(pkt[0x34], pkt[0x35])

    def scaled(raw, mul, div):
        return (
            raw * mul / div
            if div
            else 0.0
        )

    return {
        "ts_ns": ts,
        "temp_c": temp / 132.48 + 25.0,

        "gx": scaled(gx, gmul, gdiv),
        "gy": scaled(gy, gmul, gdiv),
        "gz": scaled(gz, gmul, gdiv),

        "ax": scaled(ax, amul, adiv),
        "ay": scaled(ay, amul, adiv),
        "az": scaled(az, amul, adiv),

        # Kept for future compass fusion.
        "mx": scaled(mx, mmul, mdiv),
        "my": scaled(my, mmul, mdiv),
        "mz": scaled(mz, mmul, mdiv),
    }


# ---------------------------------------------------------------------------
# Quaternion utilities
# ---------------------------------------------------------------------------

def quat_normalize(q):
    n = math.sqrt(
        q[0] * q[0]
        + q[1] * q[1]
        + q[2] * q[2]
        + q[3] * q[3]
    )

    if n < 1e-12:
        return [1.0, 0.0, 0.0, 0.0]

    return [
        q[0] / n,
        q[1] / n,
        q[2] / n,
        q[3] / n,
    ]


def quat_conjugate(q):
    return [
        q[0],
        -q[1],
        -q[2],
        -q[3],
    ]


def quat_multiply(a, b):
    """Hamilton quaternion multiplication."""

    aw, ax, ay, az = a
    bw, bx, by, bz = b

    return [
        aw * bw - ax * bx - ay * by - az * bz,

        aw * bx + ax * bw + ay * bz - az * by,

        aw * by - ax * bz + ay * bw + az * bx,

        aw * bz + ax * by - ay * bx + az * bw,
    ]


def quat_relative(reference, current):
    """
    Orientation of current relative to reference.

    q_relative = inverse(reference) * current
    """

    return quat_normalize(
        quat_multiply(
            quat_conjugate(reference),
            current,
        )
    )


def quat_slerp(a, b, t):
    """Small-angle-safe quaternion interpolation."""

    t = max(0.0, min(1.0, t))

    dot = (
        a[0] * b[0]
        + a[1] * b[1]
        + a[2] * b[2]
        + a[3] * b[3]
    )

    # Same orientation represented by opposite quaternion signs.
    if dot < 0.0:
        b = [-x for x in b]
        dot = -dot

    dot = max(-1.0, min(1.0, dot))

    if dot > 0.9995:
        q = [
            a[i] + t * (b[i] - a[i])
            for i in range(4)
        ]

        return quat_normalize(q)

    theta = math.acos(dot)
    sin_theta = math.sin(theta)

    if abs(sin_theta) < 1e-8:
        return list(a)

    wa = math.sin((1.0 - t) * theta) / sin_theta
    wb = math.sin(t * theta) / sin_theta

    return [
        wa * a[i] + wb * b[i]
        for i in range(4)
    ]


def quaternion_to_euler(q):
    """
    Quaternion -> roll, pitch, yaw.

    Roll and yaw use the standard atan2 representation.

    Pitch uses atan2 rather than asin so it retains the existing
    project's -180..+180 behaviour.
    """

    w, x, y, z = q

    roll = math.degrees(
        math.atan2(
            2.0 * (w * x + y * z),
            1.0 - 2.0 * (x * x + y * y),
        )
    )

    pitch = math.degrees(
        math.atan2(
            2.0 * (w * y - z * x),
            1.0 - 2.0 * (y * y + x * x),
        )
    )

    yaw = math.degrees(
        math.atan2(
            2.0 * (w * z + x * y),
            1.0 - 2.0 * (y * y + z * z),
        )
    )

    return roll, pitch, yaw


# ---------------------------------------------------------------------------
# Mahony IMU filter
# ---------------------------------------------------------------------------

class MahonyIMU:
    """
    Quaternion Mahony AHRS using gyro + accelerometer.

    This is intentionally a 6-axis filter.

    Accelerometer:
        stabilizes roll/pitch against gravity.

    Gyroscope:
        provides fast motion response and yaw.

    Magnetometer:
        not currently used because there is no magnetic calibration/
        hard-iron/soft-iron compensation in this application.
    """

    def __init__(
        self,
        kp=MAHONY_KP,
        ki=MAHONY_KI,
    ):
        self.q = [1.0, 0.0, 0.0, 0.0]

        self.kp = kp
        self.ki = ki

        # Integral error used to estimate residual gyro bias.
        self.integral = [0.0, 0.0, 0.0]

    def reset(self):
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.integral = [0.0, 0.0, 0.0]

    def update(
        self,
        gx,
        gy,
        gz,
        ax,
        ay,
        az,
        dt,
        accel_weight=1.0,
    ):
        """
        Update orientation.

        gx/gy/gz:
            rad/s

        ax/ay/az:
            arbitrary accelerometer units

        dt:
            seconds

        accel_weight:
            0..1, controls how strongly gravity corrects orientation.
        """

        if dt <= 0.0:
            return

        # ------------------------------------------------------------------
        # Normalize accelerometer.
        # ------------------------------------------------------------------

        an = math.sqrt(
            ax * ax
            + ay * ay
            + az * az
        )

        if an < 1e-9:
            accel_weight = 0.0
        else:
            ax /= an
            ay /= an
            az /= an

        accel_weight = max(
            0.0,
            min(1.0, accel_weight),
        )

        # ------------------------------------------------------------------
        # Estimated gravity direction from quaternion.
        #
        # This is the direction gravity should appear in body coordinates
        # for the current orientation.
        # ------------------------------------------------------------------

        q0, q1, q2, q3 = self.q

        vx = 2.0 * (
            q1 * q3 - q0 * q2
        )

        vy = 2.0 * (
            q0 * q1 + q2 * q3
        )

        vz = (
            q0 * q0
            - q1 * q1
            - q2 * q2
            + q3 * q3
        )

        # ------------------------------------------------------------------
        # Gravity error.
        #
        # Cross product:
        #
        #     measured_gravity × estimated_gravity
        #
        # gives the rotation direction required to correct the estimate.
        # ------------------------------------------------------------------

        ex = (
            ay * vz
            - az * vy
        )

        ey = (
            az * vx
            - ax * vz
        )

        ez = (
            ax * vy
            - ay * vx
        )

        # ------------------------------------------------------------------
        # Integral feedback.
        #
        # Only accumulate it when acceleration is considered trustworthy.
        # This prevents head movement from becoming "stored gyro bias".
        # ------------------------------------------------------------------

        if (
            self.ki > 0.0
            and accel_weight > 0.5
        ):
            self.integral[0] += (
                self.ki * ex * accel_weight * dt
            )

            self.integral[1] += (
                self.ki * ey * accel_weight * dt
            )

            self.integral[2] += (
                self.ki * ez * accel_weight * dt
            )

            # Prevent runaway integral correction.
            for i in range(3):
                self.integral[i] = max(
                    -0.15,
                    min(0.15, self.integral[i]),
                )

        else:
            # Slowly bleed old integral correction when acceleration
            # becomes unreliable.
            decay = max(
                0.0,
                1.0 - 2.0 * dt,
            )

            self.integral = [
                x * decay
                for x in self.integral
            ]

        # ------------------------------------------------------------------
        # Proportional gravity correction.
        # ------------------------------------------------------------------

        gx += (
            self.kp
            * ex
            * accel_weight
            + self.integral[0]
        )

        gy += (
            self.kp
            * ey
            * accel_weight
            + self.integral[1]
        )

        gz += (
            self.kp
            * ez
            * accel_weight
            + self.integral[2]
        )

        # ------------------------------------------------------------------
        # Quaternion derivative.
        # ------------------------------------------------------------------

        half_dt = 0.5 * dt

        qa = q0
        qb = q1
        qc = q2
        qd = q3

        q0 += (
            -qb * gx
            - qc * gy
            - qd * gz
        ) * half_dt

        q1 += (
            qa * gx
            + qc * gz
            - qd * gy
        ) * half_dt

        q2 += (
            qa * gy
            - qb * gz
            + qd * gx
        ) * half_dt

        q3 += (
            qa * gz
            + qb * gy
            - qc * gx
        ) * half_dt

        self.q = quat_normalize(
            [q0, q1, q2, q3]
        )


# ---------------------------------------------------------------------------
# HID glue
# ---------------------------------------------------------------------------

def _connect(path):
    """
    Open an HID device using either supported Python HID binding.

    python-hidapi:
        hid.device()
        dev.open_path(...)

    python-hid:
        hid.Device(path=...)

    Returns:
        (device, read_function)
    """

    p_str = (
        path.decode()
        if isinstance(path, (bytes, bytearray))
        else path
    )

    p_bytes = os.fsencode(p_str)

    # python-hidapi
    if hasattr(hid, "device"):
        dev = hid.device()

        try:
            dev.open_path(p_bytes)
        except TypeError:
            dev.open_path(p_str)

        if hasattr(dev, "set_nonblocking"):
            try:
                dev.set_nonblocking(0)
            except Exception:
                pass

        def rd(n, timeout_ms):
            try:
                return dev.read(
                    n,
                    timeout_ms=timeout_ms,
                )

            except TypeError:
                try:
                    return dev.read(
                        n,
                        timeout_ms,
                    )

                except TypeError:
                    end = (
                        time.monotonic()
                        + timeout_ms / 1000.0
                    )

                    while time.monotonic() < end:
                        pkt = dev.read(n)

                        if pkt:
                            return pkt

                        time.sleep(0.004)

                    return None

        return dev, rd

    # python-hid
    if hasattr(hid, "Device"):
        dev = hid.Device(path=p_bytes)

        def rd(n, timeout_ms):
            for args, kwargs in (
                ((n, timeout_ms), {}),
                ((n,), {"timeout": timeout_ms}),
                ((n,), {}),
            ):
                try:
                    return dev.read(
                        *args,
                        **kwargs,
                    )

                except TypeError:
                    continue

            return None

        return dev, rd

    raise RuntimeError(
        "Unsupported Python HID library: "
        "expected hid.device or hid.Device"
    )


# ---------------------------------------------------------------------------
# IMU reader
# ---------------------------------------------------------------------------

class ImuReader(threading.Thread):
    """
    Open the Xreal IMU, fuse samples, and publish orientation.
    """

    def __init__(self, dump=False):
        super().__init__(daemon=True)

        self.dump = dump

        self.filter = MahonyIMU()

        self.dev = None
        self._rd = None

        self.state = {
            "ok": False,
            "hz": 0.0,
            "pitch": 0.0,
            "bank": 0.0,
            "yaw": 0.0,
            "g": 1.0,
            "temp_c": 0.0,
        }

        self._stop = threading.Event()
        self._recenter = threading.Event()

        # Gyro bias in rad/s.
        self._gyro_bias = [0.0, 0.0, 0.0]

        # Startup calibration.
        self._gyro_samples = []
        self._accel_samples = []

        self._calibrated = False
        self._one_g = None

        # Quaternion at the moment of recenter.
        self._reference_q = [
            1.0,
            0.0,
            0.0,
            0.0,
        ]

        # Light internal smoothing.
        self._display_q = [
            1.0,
            0.0,
            0.0,
            0.0,
        ]

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    def _candidates(self):
        try:
            devs = hid.enumerate(VID, 0)

        except TypeError:
            devs = [
                d
                for d in hid.enumerate()
                if d.get("vendor_id") == VID
            ]

        if not devs:
            try:
                devs = [
                    d
                    for d in hid.enumerate()
                    if d.get("vendor_id") == VID
                ]

            except Exception:
                devs = []

        def rank(d):
            pid = d.get("product_id")
            want_if = KNOWN_PIDS.get(pid)

            if (
                want_if is not None
                and d.get("interface_number") == want_if
            ):
                return 0

            if pid in KNOWN_PIDS:
                return 1

            return 2

        devs.sort(key=rank)

        return [
            d["path"]
            for d in devs
            if d.get("path")
        ]

    # ------------------------------------------------------------------
    # Xreal initialization handshake
    # ------------------------------------------------------------------

    def _init_stream(self, dev, rd) -> bool:
        # Stop any active stream.
        dev.write(
            build_msg(
                MSG_STREAM,
                b"\x00",
            )
        )

        # Drain stale packets.
        for _ in range(10):
            if not rd(PKT, 20):
                break

        # Optional static ID.
        _recv_msg(
            rd,
            MSG_STATIC_ID,
            4,
            300,
        )

        # Calibration length.
        dev.write(
            build_msg(MSG_CAL_LENGTH)
        )

        cal_len_b = _recv_msg(
            rd,
            MSG_CAL_LENGTH,
            4,
            300,
        )

        if cal_len_b:
            cal_len = struct.unpack(
                "<I",
                cal_len_b,
            )[0]

            drained = 0

            for _ in range(512):
                if drained >= cal_len:
                    break

                dev.write(
                    build_msg(MSG_CAL_SEGMENT)
                )

                seg = _recv_msg(
                    rd,
                    MSG_CAL_SEGMENT,
                    min(
                        56,
                        cal_len - drained,
                    ),
                    300,
                )

                if not seg:
                    break

                drained += len(seg)

        # Start IMU streaming.
        dev.write(
            build_msg(
                MSG_STREAM,
                b"\x01",
            )
        )

        # Confirm actual IMU packets arrive.
        end = time.monotonic() + 1.0

        while time.monotonic() < end:
            pkt = rd(PKT, 200)

            if pkt and decode(bytes(pkt)):
                return True

        return False

    def _try_open(self, path):
        path_display = (
            path.decode(errors="replace")
            if isinstance(path, bytes)
            else str(path)
        )

        print(
            f"[imu] trying {path_display}",
            flush=True,
        )

        try:
            dev, rd = _connect(path)

        except Exception as e:
            print(
                f"[imu] connect failed {path_display}: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )
            return None

        print(
            f"[imu] HID opened {path_display}",
            flush=True,
        )

        try:
            if self._init_stream(dev, rd):
                self._rd = rd

                print(
                    f"[imu] handshake OK {path_display}",
                    flush=True,
                )

                return dev

            print(
                f"[imu] handshake failed {path_display}",
                flush=True,
            )

            dev.close()

        except Exception as e:
            print(
                f"[imu] handshake exception {path_display}: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )

            try:
                dev.close()
            except Exception:
                pass

        return None

    def open(self):
        paths = self._candidates()

        if not paths:
            raise RuntimeError(
                "no 0x3318 USB devices found - is the Air "
                "plugged into the Deck's USB-C port and awake?"
            )

        print(
            "[imu] candidates:",
            ", ".join(
                p.decode(errors="replace")
                if isinstance(p, bytes)
                else str(p)
                for p in paths
            ),
            flush=True,
        )

        for p in paths:
            dev = self._try_open(p)

            if dev:
                self.dev = dev

                print(
                    "[imu] stream open",
                    flush=True,
                )

                return

        raise RuntimeError(
            "Xreal devices present but the IMU handshake "
            "got no data. See the [imu] diagnostics above."
        )

    # ------------------------------------------------------------------
    # Calibration helpers
    # ------------------------------------------------------------------

    def _reset_calibration(self):
        self._gyro_samples = []
        self._accel_samples = []

        self._calibrated = False
        self._one_g = None

        self._gyro_bias = [
            0.0,
            0.0,
            0.0,
        ]

    def _collect_calibration(self, s):
        """
        Collect startup stationary samples.

        The glasses should be still during this phase.
        """

        gx = s["gx"] * GYRO_DPS_TO_RAD
        gy = s["gy"] * GYRO_DPS_TO_RAD
        gz = s["gz"] * GYRO_DPS_TO_RAD

        an = math.sqrt(
            s["ax"] ** 2
            + s["ay"] ** 2
            + s["az"] ** 2
        )

        self._gyro_samples.append(
            (gx, gy, gz)
        )

        if an > 0.0:
            self._accel_samples.append(an)

        if (
            len(self._gyro_samples)
            < GYRO_CAL_SAMPLES
        ):
            return False

        # Average gyro while stationary.
        self._gyro_bias = [
            sum(v[i] for v in self._gyro_samples)
            / len(self._gyro_samples)
            for i in range(3)
        ]

        # Average accelerometer magnitude.
        if self._accel_samples:
            self._one_g = (
                sum(self._accel_samples)
                / len(self._accel_samples)
            )

        if not self._one_g or self._one_g <= 1e-9:
            self._one_g = 1.0

        print(
            "[imu] gyro bias = "
            f"{self._gyro_bias[0] / GYRO_DPS_TO_RAD:.4f}, "
            f"{self._gyro_bias[1] / GYRO_DPS_TO_RAD:.4f}, "
            f"{self._gyro_bias[2] / GYRO_DPS_TO_RAD:.4f} dps",
            flush=True,
        )

        print(
            f"[imu] 1g reference = {self._one_g:.4f}",
            flush=True,
        )

        self._calibrated = True

        # Reset filter after calibration so the initial state starts
        # cleanly rather than integrating calibration samples.
        self.filter.reset()

        self._reference_q = [
            1.0,
            0.0,
            0.0,
            0.0,
        ]

        self._display_q = [
            1.0,
            0.0,
            0.0,
            0.0,
        ]

        print(
            "[imu] calibration complete - hold still",
            flush=True,
        )

        return True

    def _accel_weight(self, acceleration):
        """
        Determine how trustworthy the accelerometer is as a gravity
        reference.

        Near 1g:
            full correction.

        During strong acceleration:
            reduced correction.

        This is important for a head-mounted device because moving
        the glasses produces linear acceleration in addition to gravity.
        """

        if self._one_g is None:
            return 1.0

        ratio = acceleration / self._one_g

        error = abs(ratio - 1.0)

        if error <= (1.0 - ACCEL_FULL_WEIGHT):
            return 1.0

        if error >= (1.0 - ACCEL_ZERO_WEIGHT):
            return ACCEL_ZERO_WEIGHT

        # Smoothly transition between full and reduced correction.
        x = (
            error - (1.0 - ACCEL_FULL_WEIGHT)
        ) / (
            (1.0 - ACCEL_ZERO_WEIGHT)
            - (1.0 - ACCEL_FULL_WEIGHT)
        )

        # Smoothstep.
        x = x * x * (3.0 - 2.0 * x)

        return (
            1.0
            + (
                ACCEL_ZERO_WEIGHT - 1.0
            ) * x
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self):
        self._stop.set()

    def recenter(self):
        self._recenter.set()

    # ------------------------------------------------------------------
    # Main sensor thread
    # ------------------------------------------------------------------

    def run(self):
        try:
            self.open()

        except Exception as e:
            print(
                f"[imu] open failed: {e}",
                flush=True,
            )

            self.state["ok"] = False
            return

        self._reset_calibration()

        last_ts = None
        last_wall = time.monotonic()

        count = 0

        while not self._stop.is_set():
            try:
                pkt = self._rd(PKT, 100)

            except Exception as e:
                print(
                    f"[imu] read error: {e}",
                    flush=True,
                )

                self.state["ok"] = False

                time.sleep(0.5)
                continue

            if not pkt:
                self.state["hz"] = 0.0
                continue

            s = decode(bytes(pkt))

            if s is None:
                continue

            if self.dump:
                print(
                    " ".join(
                        f"{k}={v:.4f}"
                        if isinstance(v, float)
                        else f"{k}={v}"
                        for k, v in s.items()
                    ),
                    flush=True,
                )
                continue

            # ----------------------------------------------------------
            # Sensor timestamp
            # ----------------------------------------------------------

            dt = 0.0

            if (
                last_ts is not None
                and s["ts_ns"] > last_ts
            ):
                dt = (
                    s["ts_ns"] - last_ts
                ) / 1e9

            last_ts = s["ts_ns"]

            if dt <= 0.0 or dt > 0.05:
                continue

            # ----------------------------------------------------------
            # Startup calibration
            # ----------------------------------------------------------

            if not self._calibrated:
                self._collect_calibration(s)

                # Don't publish orientation until calibration is done.
                self.state["ok"] = False

                continue

            # ----------------------------------------------------------
            # Raw accelerometer
            # ----------------------------------------------------------

            ax = s["ax"]
            ay = s["ay"]
            az = s["az"]

            acceleration = math.sqrt(
                ax * ax
                + ay * ay
                + az * az
            )

            # ----------------------------------------------------------
            # Raw gyro, converted to rad/s and corrected for bias.
            # ----------------------------------------------------------

            gx = (
                s["gx"] * GYRO_DPS_TO_RAD
                - self._gyro_bias[0]
            )

            gy = (
                s["gy"] * GYRO_DPS_TO_RAD
                - self._gyro_bias[1]
            )

            gz = (
                s["gz"] * GYRO_DPS_TO_RAD
                - self._gyro_bias[2]
            )

            # ----------------------------------------------------------
            # Adaptive gravity correction.
            # ----------------------------------------------------------

            accel_weight = self._accel_weight(
                acceleration
            )

            # ----------------------------------------------------------
            # Quaternion fusion.
            # ----------------------------------------------------------

            self.filter.update(
                gx,
                gy,
                gz,
                ax,
                ay,
                az,
                dt,
                accel_weight,
            )

            q = self.filter.q

            # ----------------------------------------------------------
            # Recenter.
            #
            # Store the entire quaternion. This avoids problems caused
            # by independently subtracting Euler angles.
            # ----------------------------------------------------------

            if self._recenter.is_set():
                self._recenter.clear()

                self._reference_q = list(q)

                # Reset display interpolation so recenter happens
                # immediately rather than visibly easing through it.
                self._display_q = [
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                ]

                print(
                    "[imu] recentered",
                    flush=True,
                )

            # ----------------------------------------------------------
            # Convert current orientation into the recentered frame.
            # ----------------------------------------------------------

            relative_q = quat_relative(
                self._reference_q,
                q,
            )

            # ----------------------------------------------------------
            # Very light orientation smoothing.
            #
            # The HUD has its own stronger smoothing, so this only
            # removes sensor-level single-frame noise.
            # ----------------------------------------------------------

            alpha = 1.0 - math.exp(
                -dt / ORIENTATION_SMOOTH_TAU
            )

            self._display_q = quat_slerp(
                self._display_q,
                relative_q,
                alpha,
            )

            roll, pitch, yaw = quaternion_to_euler(
                self._display_q
            )

            # ----------------------------------------------------------
            # G meter.
            # ----------------------------------------------------------

            if self._one_g:
                g_force = (
                    acceleration
                    / self._one_g
                )
            else:
                g_force = 1.0

            # ----------------------------------------------------------
            # Update rate.
            # ----------------------------------------------------------

            now = time.monotonic()

            count += 1

            if now - last_wall >= 1.0:
                self.state["hz"] = (
                    count / (now - last_wall)
                )

                count = 0
                last_wall = now

            # ----------------------------------------------------------
            # Publish.
            # ----------------------------------------------------------

            self.state.update(
                ok=True,
                bank=roll,
                pitch=pitch,
                yaw=yaw,
                g=g_force,
                temp_c=s["temp_c"],
            )

        self.close()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self):
        self.state["ok"] = False

        if self.dev:
            try:
                self.dev.write(
                    build_msg(
                        MSG_STREAM,
                        b"\x00",
                    )
                )

            except Exception:
                pass

            try:
                self.dev.close()

            except Exception:
                pass

            self.dev = None


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    if "--dump" in sys.argv:
        r = ImuReader(dump=True)

        r.open()
        r.start()

        try:
            while True:
                time.sleep(1)

        except KeyboardInterrupt:
            r.stop()
            r.close()

    else:
        r = ImuReader()
        r.start()

        try:
            while True:
                time.sleep(0.5)

                print(
                    {
                        k: (
                            round(v, 2)
                            if isinstance(v, float)
                            else v
                        )
                        for k, v in r.state.items()
                    },
                    flush=True,
                )

        except KeyboardInterrupt:
            r.stop()
            r.close()
