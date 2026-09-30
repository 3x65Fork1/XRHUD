#!/usr/bin/env python3
"""
Xreal Air IMU reader + Madgwick fusion.

Protocol implemented from the reference C driver (thejackimonster's
xreal-imu, vendored by DannyDesert/XReal-Ultrawide), which is derived from
wheaney's XRLinuxDriver:

  - USB vendor 0x3318; products 0x0424 (Air), 0x0428 (Air 2), 0x0432 (Air 2
    Pro), 0x0426 (Air 2 Ultra). IMU HID interface: 3 (2 on Ultra).
  - Framed messages: [0xAA][crc32 u32le][len u16le][msgid u8][data...]
    len = 3 + len(data); crc32 (standard, reflected) covers len..end-of-data.
    Sent exactly as 8+len bytes via hid write - no padding, no report prefix.
  - Msgids: 0x14 cal length, 0x15 cal segment, 0x19 stream on/off, 0x1A id.
  - Init handshake: stop stream -> drain -> static id -> cal length ->
    drain cal segments -> start stream.
  - IMU event: 64-byte packet, signature 0x01 0x02, fields per SPEC below.
  - gyro = raw * mult / div in deg/s; accel same; magnetometer mult/div are
    big-endian and its s16 readings carry an inverted sign bit - the
    reference driver found fusion works BETTER without mag, so it is unused.

NOTE: if wheaney's xr-driver systemd service is installed it owns the device:
  sudo systemctl stop xr-driver   # before running this
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
    sys.exit("missing dependency: sudo pacman -S python-hidapi (or python-hid)")

VID = 0x3318
KNOWN_PIDS = {0x0424: 3, 0x0428: 3, 0x0432: 3, 0x0426: 2}  # pid -> imu interface
PKT = 0x40

MSG_CAL_LENGTH = 0x14
MSG_CAL_SEGMENT = 0x15
MSG_STREAM = 0x19
MSG_STATIC_ID = 0x1A

# ---------------------------------------------------------------- packet codec

def build_msg(msgid: int, data: bytes = b"") -> bytes:
    """Frame a command message exactly as the reference driver does."""
    body = struct.pack("<HB", 3 + len(data), msgid) + data
    return b"\xAA" + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF) + body


def _recv_msg(rd, msgid: int, want_len: int, timeout_ms: int):
    """Read framed response packets until one matches msgid. -> data or None."""
    end = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < end:
        pkt = rd(PKT, 100)
        if not pkt:
            continue
        pkt = bytes(pkt)
        if len(pkt) >= 8 and pkt[0] == 0xAA and pkt[7] == msgid:
            return pkt[8:8 + want_len]
    return None


def _s24(b: bytes) -> int:
    """3-byte little-endian signed integer (two's complement)."""
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    return v - (1 << 24) if v & 0x800000 else v


def _mag_s16(b0: int, b1: int) -> int:
    """Magnetometer reading: sign bit inverted (per reference driver)."""
    v = b0 | ((b1 ^ 0x80) << 8)
    return v - (1 << 16) if v & 0x8000 else v


def decode(pkt: bytes):
    """Parse one 64-byte IMU event packet -> dict, or None if not an IMU packet."""
    if len(pkt) < PKT or pkt[0] != 0x01 or pkt[1] != 0x02:
        return None
    temp = struct.unpack_from("<h", pkt, 0x02)[0]
    ts = struct.unpack_from("<Q", pkt, 0x04)[0]          # nanoseconds (device clock)
    gmul, gdiv = struct.unpack_from("<hi", pkt, 0x0C)
    gx, gy, gz = (_s24(pkt[o:o + 3]) for o in (0x12, 0x15, 0x18))
    amul, adiv = struct.unpack_from("<hi", pkt, 0x1B)
    ax, ay, az = (_s24(pkt[o:o + 3]) for o in (0x21, 0x24, 0x27))
    mmul, mdiv = struct.unpack_from(">hi", pkt, 0x2A)    # magnetometer: big-endian
    mx = _mag_s16(pkt[0x30], pkt[0x31])
    my = _mag_s16(pkt[0x32], pkt[0x33])
    mz = _mag_s16(pkt[0x34], pkt[0x35])

    def scaled(raw, mul, div):
        return (raw * mul / div) if div else 0.0

    return {
        "ts_ns": ts,
        # ICM-42688-P datasheet: offset 25 degC, sensitivity 132.48 LSB/degC
        "temp_c": temp / 132.48 + 25.0,
        "gx": scaled(gx, gmul, gdiv), "gy": scaled(gy, gmul, gdiv), "gz": scaled(gz, gmul, gdiv),
        "ax": scaled(ax, amul, adiv), "ay": scaled(ay, amul, adiv), "az": scaled(az, amul, adiv),
        # decoded for completeness; fusion deliberately ignores magnetometer
        "mx": scaled(mx, mmul, mdiv), "my": scaled(my, mmul, mdiv), "mz": scaled(mz, mmul, mdiv),
    }


# ------------------------------------------------------------------- fusion

class Madgwick:
    """Madgwick IMU(AHRS) filter - gyro in rad/s; accel any consistent units.
    Magnetometer input is accepted but ignored: the reference driver found
    the Air's mag data degrades fusion quality."""

    def __init__(self, beta=0.08):
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.beta = beta

    def update(self, gx, gy, gz, ax, ay, az, dt):
        q0, q1, q2, q3 = self.q
        an = math.sqrt(ax * ax + ay * ay + az * az)
        if an < 1e-6:
            self._integrate(gx, gy, gz, dt)
            return
        ax, ay, az = ax / an, ay / an, az / an

        _2q0q2, _2q2q3 = 2 * q0 * q2, 2 * q2 * q3
        q0q0, q0q1, q0q2, q0q3 = q0 * q0, q0 * q1, q0 * q2, q0 * q3
        q1q1, q1q2, q1q3 = q1 * q1, q1 * q2, q1 * q3
        q2q2, q2q3, q3q3 = q2 * q2, q2 * q3, q3 * q3

        s0 = -_2q2 * (2 * (q1q3 - q0q2) - ax) + _2q0q2 * 0 + _2q1 * (2 * (q0q1 + q2q3) - ay)
        s1 = (_2q2q3 * 0 + _2q3 * (2 * (q1q3 - q0q2) - ax) + _2q0 * (2 * (q0q1 + q2q3) - ay)
              - 4 * q1 * (2 * (q2q2 + q3q3) - az))
        s2 = (-_2q0 * (2 * (q1q3 - q0q2) - ax) + _2q3 * (2 * (q0q1 + q2q3) - ay)
              - 4 * q2 * (2 * (q1q1 + q3q3) - az))
        s3 = _2q1 * (2 * (q1q3 - q0q2) - ax) + _2q2 * (2 * (q0q1 + q2q3) - ay)

        sn = math.sqrt(s0 * s0 + s1 * s1 + s2 * s2 + s3 * s3)
        if sn > 1e-6:
            s0, s1, s2, s3 = s0 / sn, s1 / sn, s2 / sn, s3 / sn
            q0 -= self.beta * s0
            q1 -= self.beta * s1
            q2 -= self.beta * s2
            q3 -= self.beta * s3
        self.q = [q0, q1, q2, q3]
        self._integrate(gx, gy, gz, dt)

    def _integrate(self, gx, gy, gz, dt):
        q0, q1, q2, q3 = self.q
        q0 += dt * 0.5 * (-q1 * gx - q2 * gy - q3 * gz)
        q1 += dt * 0.5 * (q0 * gx + q2 * gz - q3 * gy)
        q2 += dt * 0.5 * (q0 * gy - q1 * gz + q3 * gx)
        q3 += dt * 0.5 * (q0 * gz + q1 * gy - q2 * gx)
        n = math.sqrt(q0 * q0 + q1 * q1 + q2 * q2 + q3 * q3) or 1.0
        self.q = [q0 / n, q1 / n, q2 / n, q3 / n]


def euler_deg(q):
    """quaternion -> (roll, pitch, yaw) in degrees."""
    w, x, y, z = q
    roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
    yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    return roll, pitch, yaw


# ------------------------------------------------------------------- hid glue

def _connect(path):
    """Open an HID device with whichever 'hid' binding is installed
    (python-hidapi: C ext, hid.device / open_path; python-hid: pure ctypes,
    hid.Device(path=...)). Returns (dev, read_fn); read_fn(n, timeout_ms)
    yields a packet (bytes/list) or None."""
    p_str = path.decode() if isinstance(path, (bytes, bytearray)) else path
    p_bytes = os.fsencode(p_str)
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
                return dev.read(n, timeout_ms=timeout_ms)
            except TypeError:
                try:
                    return dev.read(n, timeout_ms)
                except TypeError:  # no timeout support: poll
                    end = time.monotonic() + timeout_ms / 1000.0
                    while time.monotonic() < end:
                        pkt = dev.read(n)
                        if pkt:
                            return pkt
                        time.sleep(0.004)
                    return None
        return dev, rd

    dev = hid.Device(path=p_str)

    def rd(n, timeout_ms):
        for args, kwargs in (((n, timeout_ms), {}), ((n,), {"timeout": timeout_ms}),
                             ((n,), {})):
            try:
                return dev.read(*args, **kwargs)
            except TypeError:
                continue
        return None
    return dev, rd


# ------------------------------------------------------------------- reader

class ImuReader(threading.Thread):
    """Opens the glasses' IMU HID interface, fuses samples, publishes euler state."""

    # packet's mult/div fields put gyro in deg/s (confirmed against the
    # reference driver, which feeds Fusion in degrees).
    GYRO_DPS_TO_RAD = math.pi / 180.0

    def __init__(self, dump=False):
        super().__init__(daemon=True)
        self.dump = dump
        self.filter = Madgwick()
        self.dev = None
        self._rd = None
        self.state = {"ok": False, "hz": 0.0, "pitch": 0.0, "bank": 0.0,
                      "yaw": 0.0, "g": 1.0, "temp_c": 0.0}
        self._stop = threading.Event()
        self._recenter = threading.Event()
        self._offsets = [0.0, 0.0, 0.0]
        self._one_g = None
        self._rest_vals = []

    # -- device discovery ------------------------------------------------
    def _candidates(self):
        try:
            devs = hid.enumerate(VID, 0)
        except TypeError:  # python-hid has a different enumerate signature
            devs = [d for d in hid.enumerate() if d.get("vendor_id") == VID]
        if not devs:
            try:
                devs = [d for d in hid.enumerate() if d.get("vendor_id") == VID]
            except Exception:
                devs = []

        def rank(d):
            pid = d.get("product_id")
            want_if = KNOWN_PIDS.get(pid)
            if want_if is not None and d.get("interface_number") == want_if:
                return 0                      # known product, known IMU iface
            if pid in KNOWN_PIDS:
                return 1
            return 2                          # unknown: probe anyway

        devs.sort(key=rank)
        return [d["path"] for d in devs if d.get("path")]

    # -- reference driver's init handshake --------------------------------
    def _init_stream(self, dev, rd) -> bool:
        dev.write(build_msg(MSG_STREAM, b"\x00"))       # stop any active stream
        for _ in range(10):                             # drain stale packets
            if not rd(PKT, 20):
                break
        _recv_msg(rd, MSG_STATIC_ID, 4, 300)            # optional: static id
        dev.write(build_msg(MSG_CAL_LENGTH))
        cal_len_b = _recv_msg(rd, MSG_CAL_LENGTH, 4, 300)
        if cal_len_b:
            cal_len = struct.unpack("<I", cal_len_b)[0]
            drained = 0
            for _ in range(512):                        # cap: 512*56 = 28 KiB
                if drained >= cal_len:
                    break
                dev.write(build_msg(MSG_CAL_SEGMENT))
                seg = _recv_msg(rd, MSG_CAL_SEGMENT, min(56, cal_len - drained), 300)
                if not seg:
                    break
                drained += len(seg)
        dev.write(build_msg(MSG_STREAM, b"\x01"))       # start stream
        end = time.monotonic() + 1.0
        while time.monotonic() < end:                   # confirm packets flow
            pkt = rd(PKT, 200)
            if pkt and decode(bytes(pkt)):
                return True
        return False

    def _try_open(self, path):
        try:
            dev, rd = _connect(path)
        except Exception:
            return None
        try:
            if self._init_stream(dev, rd):
                self._rd = rd
                return dev
            dev.close()
        except Exception:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def open(self):
        paths = self._candidates()
        if not paths:
            raise RuntimeError(
                "no 0x3318 USB devices found - is the Air plugged into the "
                "Deck's USB-C port (top port) and awake?")
        for p in paths:
            dev = self._try_open(p)
            if dev:
                self.dev = dev
                print("[imu] stream open", flush=True)
                return
        raise RuntimeError(
            "Xreal devices present but the IMU handshake got no data. "
            "Most likely: replug the glasses (udev rule applies on plug), or "
            "check permissions on /dev/hidraw* - see README step 1.")

    # -- lifecycle --------------------------------------------------------
    def stop(self):
        self._stop.set()

    def recenter(self):
        self._recenter.set()

    def run(self):
        self.open()
        last_ts = None
        last_wall = time.monotonic()
        count = 0
        while not self._stop.is_set():
            try:
                pkt = self._rd(PKT, 100)
            except Exception as e:
                print(f"[imu] read error: {e}", flush=True)
                time.sleep(0.5)
                continue
            if not pkt:
                self.state["hz"] = 0.0
                continue
            s = decode(bytes(pkt))
            if s is None:
                continue
            if self.dump:
                print(" ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                               for k, v in s.items()), flush=True)
                continue

            dt = 0.0
            if last_ts is not None and s["ts_ns"] > last_ts:
                dt = (s["ts_ns"] - last_ts) / 1e9
            last_ts = s["ts_ns"]
            if dt <= 0 or dt > 0.05:
                continue  # first sample, or a gap -> don't integrate garbage

            gx, gy, gz = (s[k] * self.GYRO_DPS_TO_RAD for k in ("gx", "gy", "gz"))
            self.filter.update(gx, gy, gz, s["ax"], s["ay"], s["az"], dt)
            roll, pitch, yaw = euler_deg(self.filter.q)

            # auto-calibrate 1g from the first ~100 samples (assumes still)
            if self._one_g is None:
                an = math.sqrt(s["ax"] ** 2 + s["ay"] ** 2 + s["az"] ** 2)
                if an > 0:
                    self._rest_vals.append(an)
                if len(self._rest_vals) >= 100:
                    self._one_g = sum(self._rest_vals) / len(self._rest_vals)
                    print(f"[imu] 1g reference = {self._one_g:.3f}", flush=True)
                g_force = 1.0
            else:
                g_force = math.sqrt(s["ax"] ** 2 + s["ay"] ** 2 + s["az"] ** 2) / self._one_g

            if self._recenter.is_set():
                self._recenter.clear()
                self._offsets = [roll, pitch, yaw]
                print("[imu] recentered", flush=True)

            o_roll, o_pitch, o_yaw = self._offsets
            now = time.monotonic()
            count += 1
            if now - last_wall >= 1.0:
                self.state["hz"] = count / (now - last_wall)
                count, last_wall = 0, now
            self.state.update(ok=True,
                              bank=roll - o_roll,
                              pitch=pitch - o_pitch,
                              yaw=(yaw - o_yaw + 180) % 360 - 180,
                              g=g_force,
                              temp_c=s["temp_c"])

    def close(self):
        if self.dev:
            try:
                self.dev.write(build_msg(MSG_STREAM, b"\x00"))
            except Exception:
                pass
            try:
                self.dev.close()
            except Exception:
                pass


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
                print({k: (round(v, 2) if isinstance(v, float) else v)
                       for k, v in r.state.items()}, flush=True)
        except KeyboardInterrupt:
            r.stop()
            r.close()
