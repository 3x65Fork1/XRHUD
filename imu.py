#!/usr/bin/env python3
"""
Xreal Air IMU reader + Madgwick fusion.

Protocol reference (reverse-engineered, confirmed against the community driver
and the voidcomputing writeups):
  - USB vendor 0x3318, IMU stream lives on a 0x40-byte HID interface
    (interface 3 on the original Air; Air 2/Ultra may differ, so we probe).
  - Enable stream:  0xaa-headered command packet, cmd 0x19, data 0x01.
  - IMU event:      64-byte packet, header 0x01 0x02, fields per SPEC below.

If the glasses' enable packet layout ever changes, run with --dump and compare
against: https://github.com/wheaney/XRLinuxDriver (modules/xrealAirDeviceKit,
modules/xrealInterfaceLibrary) and voidcomputing's "worse-better-prettier" post.

NOTE: if wheaney's xr-driver systemd service is installed it owns the device.
  sudo systemctl stop xr-driver   # before running this
"""

import math
import struct
import sys
import threading
import time

try:
    import hid
except ImportError:
    sys.exit("missing dependency: sudo pacman -S python-hidapi")

VID = 0x3318
PKT = 0x40

# ---------------------------------------------------------------- packet codec

def adler32(data: bytes, seed: int = 1) -> int:
    """The glasses use an Adler-style checksum (mod 65521), seeded at 1."""
    MOD = 65521
    a = seed & 0xFFFF
    b = (seed >> 16) & 0xFFFF
    for x in data:
        a = (a + x) % MOD
        b = (b + a) % MOD
    return (b << 16) | a


def build_disable() -> bytes:
    pkt = bytearray(PKT)
    pkt[0] = 0xAA
    struct.pack_into("<H", pkt, 5, 1)
    pkt[7] = 0x19
    pkt[8] = 0x00                      # 0 = disable
    struct.pack_into("<I", pkt, 1, adler32(bytes(pkt[5:PKT])))
    return bytes(pkt)


def build_enable(checksum_span: str) -> bytes:
    """Build the 'enable IMU stream' command packet.

    checksum_span: 'padded'  -> checksum over bytes 5..64 (zero-padded packet)
                   'minimal' -> checksum over bytes 5..9  (length+cmd+data only)
    Both are tried at connect time; whichever produces IMU packets wins.
    """
    pkt = bytearray(PKT)
    pkt[0] = 0xAA                      # command header
    struct.pack_into("<H", pkt, 5, 1)  # additional-data length = 1 byte
    pkt[7] = 0x19                      # command: enable IMU stream
    pkt[8] = 0x01                      # 1 = enable, 0 = disable
    span = bytes(pkt[5:PKT]) if checksum_span == "padded" else bytes(pkt[5:9])
    struct.pack_into("<I", pkt, 1, adler32(span))
    return bytes(pkt)


def _s24(b: bytes) -> int:
    """3-byte little-endian signed integer (two's complement)."""
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    return v - (1 << 24) if v & 0x800000 else v


def decode(pkt: bytes):
    """Parse one 64-byte IMU event packet -> dict, or None if not an IMU packet."""
    if len(pkt) < PKT or pkt[0] != 0x01 or pkt[1] != 0x02:
        return None
    temp = struct.unpack_from("<H", pkt, 0x02)[0]
    ts = struct.unpack_from("<Q", pkt, 0x04)[0]          # nanoseconds (device clock)
    gmul, gdiv = struct.unpack_from("<HI", pkt, 0x0C)
    gx, gy, gz = (_s24(pkt[o:o + 3]) for o in (0x12, 0x15, 0x18))
    amul, adiv = struct.unpack_from("<HI", pkt, 0x1B)
    ax, ay, az = (_s24(pkt[o:o + 3]) for o in (0x21, 0x24, 0x27))
    mmul, mdiv = struct.unpack_from("<HI", pkt, 0x2A)
    mx, my, mz = struct.unpack_from("<hhh", pkt, 0x30)

    def scaled(raw, mul, div):
        return (raw * mul / div) if div else 0.0

    return {
        "ts_ns": ts, "temp_raw": temp,
        "gx": scaled(gx, gmul, gdiv), "gy": scaled(gy, gmul, gdiv), "gz": scaled(gz, gmul, gdiv),
        "ax": scaled(ax, amul, adiv), "ay": scaled(ay, amul, adiv), "az": scaled(az, amul, adiv),
        "mx": scaled(mx, mmul, mdiv), "my": scaled(my, mmul, mdiv), "mz": scaled(mz, mmul, mdiv),
    }


# ------------------------------------------------------------------- fusion

class Madgwick:
    """Madgwick MARG filter. gyro in rad/s; accel & mag any consistent units."""

    def __init__(self, beta=0.08):
        self.q = [1.0, 0.0, 0.0, 0.0]
        self.beta = beta

    def update(self, gx, gy, gz, ax, ay, az, mx, my, mz, dt):
        q0, q1, q2, q3 = self.q
        an = math.sqrt(ax * ax + ay * ay + az * az)
        if an < 1e-6:
            self._integrate(gx, gy, gz, dt)
            return
        ax, ay, az = ax / an, ay / an, az / an

        mn = math.sqrt(mx * mx + my * my + mz * mz)
        if mn > 1e-3:  # magnetometer present -> full MARG correction
            mx, my, mz = mx / mn, my / mn, mz / mn
            _2q0mx, _2q0my, _2q0mz = 2 * q0 * mx, 2 * q0 * my, 2 * q0 * mz
            _2q1mx = 2 * q1 * mx
            _2q0 = 2 * q0
            _2q1, _2q2, _2q3 = 2 * q1, 2 * q2, 2 * q3
            _2q0q2, _2q2q3 = 2 * q0 * q2, 2 * q2 * q3
            q0q0, q0q1, q0q2, q0q3 = q0 * q0, q0 * q1, q0 * q2, q0 * q3
            q1q1, q1q2, q1q3 = q1 * q1, q1 * q2, q1 * q3
            q2q2, q2q3, q3q3 = q2 * q2, q2 * q3, q3 * q3
            hx = mx * q0q0 - _2q0my * q3 + _2q0mz * q2 + mx * q1q1 + _2q1 * my * q2 + _2q1 * mz * q3 - mx * q2q2 - mx * q3q3
            hy = _2q0mx * q3 + my * q0q0 - _2q0mz * q1 + _2q1mx * q2 - my * q1q1 + my * q2q2 + _2q2 * mz * q3 - my * q3q3
            _2bx = math.sqrt(hx * hx + hy * hy)
            _2bz = -_2q0mx * q2 + _2q0my * q1 + mz * q0q0 + _2q1mx * q3 - mz * q1q1 + _2q2 * my * q3 - mz * q2q2 + mz * q3q3
            _4bx, _4bz = 2 * _2bx, 2 * _2bz

            s0 = (-_2q2 * (2 * q1q3 - _2q0q2 - ax) + _2q1 * (2 * q0q1 + _2q2q3 - ay)
                  - _2bz * q2 * (_2bx * (0.5 - q2q2 - q3q3) + _2bz * (q1q3 - q0q2) - mx)
                  + (-_2bx * q3 + _2bz * q1) * (_2bx * (q1q2 - q0q3) + _2bz * (q0q1 + q2q3) - my)
                  + _2bx * q2 * (_2bx * (q0q2 + q1q3) + _2bz * (0.5 - q1q1 - q2q2) - mz))
            s1 = (_2q3 * (2 * q1q3 - _2q0q2 - ax) + _2q0 * (2 * q0q1 + _2q2q3 - ay)
                  - 4 * q1 * (1 - 2 * q1q1 - 2 * q2q2 - az)
                  + _2bz * q3 * (_2bx * (0.5 - q2q2 - q3q3) + _2bz * (q1q3 - q0q2) - mx)
                  + (_2bx * q2 + _2bz * q0) * (_2bx * (q1q2 - q0q3) + _2bz * (q0q1 + q2q3) - my)
                  + (_2bx * q3 - _4bz * q1) * (_2bx * (q0q2 + q1q3) + _2bz * (0.5 - q1q1 - q2q2) - mz))
            s2 = (-_2q0 * (2 * q1q3 - _2q0q2 - ax) + _2q3 * (2 * q0q1 + _2q2q3 - ay)
                  - 4 * q2 * (1 - 2 * q1q1 - 2 * q2q2 - az)
                  + (-_4bx * q2 - _2bz * q0) * (_2bx * (0.5 - q2q2 - q3q3) + _2bz * (q1q3 - q0q2) - mx)
                  + (_2bx * q1 + _2bz * q3) * (_2bx * (q1q2 - q0q3) + _2bz * (q0q1 + q2q3) - my)
                  + (_2bx * q0 - _4bz * q2) * (_2bx * (q0q2 + q1q3) + _2bz * (0.5 - q1q1 - q2q2) - mz))
            s3 = (_2q1 * (2 * q1q3 - _2q0q2 - ax) + _2q2 * (2 * q0q1 + _2q2q3 - ay)
                  + (-_4bx * q3 + _2bz * q1) * (_2bx * (0.5 - q2q2 - q3q3) + _2bz * (q1q3 - q0q2) - mx)
                  + (-_2bx * q0 + _2bz * q2) * (_2bx * (q1q2 - q0q3) + _2bz * (q0q1 + q2q3) - my)
                  + _2bx * q1 * (_2bx * (q0q2 + q1q3) + _2bz * (0.5 - q1q1 - q2q2) - mz))
        else:  # IMU-only (no magnetometer)
            _2q0q2, _2q2q3 = 2 * q0 * q2, 2 * q2 * q3
            q0q0, q0q1, q0q2, q0q3 = q0 * q0, q0 * q1, q0 * q2, q0 * q3
            q1q1, q1q2, q1q3 = q1 * q1, q1 * q2, q1 * q3
            q2q2, q2q3, q3q3 = q2 * q2, q2 * q3, q3 * q3
            s0 = _2q0q2 * ax + _2q0q2 * az - _2q2q3 * ay if False else (
                -_2q2 * (2 * (q1q3 - q0q2) - ax) + _2q1 * (2 * (q0q1 + q2q3) - ay))
            s1 = (_2q3 * (2 * (q1q3 - q0q2) - ax) + _2q0 * (2 * (q0q1 + q2q3) - ay)
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


# ------------------------------------------------------------------- reader

class ImuReader(threading.Thread):
    """Opens the glasses' IMU HID interface, fuses samples, publishes euler state."""

    # scaling: the packet's mult/div fields put gyro in deg/s (verify with
    # --dump: rotate ~90 deg in one second, peaks should read ~90). Set
    # GYRO_DPS_TO_RAD to 1.0 if a future firmware reports rad/s instead.
    GYRO_DPS_TO_RAD = math.pi / 180.0

    def __init__(self, dump=False):
        super().__init__(daemon=True)
        self.dump = dump
        self.filter = Madgwick()
        self.dev = None
        self.state = {"ok": False, "hz": 0.0, "pitch": 0.0, "bank": 0.0,
                      "yaw": 0.0, "g": 1.0, "temp_c": 0.0}
        self._stop = threading.Event()
        self._recenter = threading.Event()
        self._offsets = [0.0, 0.0, 0.0]
        self._one_g = None     # accel norm measured at rest -> defines 1g
        self._rest_vals = []

    # -- device discovery ------------------------------------------------
    def _candidates(self):
        devs = hid.enumerate(VID, 0)
        # IMU interface is 3 on the original Air; other models differ, so
        # probe interface-3 first, then everything else as fallback.
        devs.sort(key=lambda d: 0 if d.get("interface_number") == 3 else 1)
        return [d["path"] for d in devs]

    def _try_open(self, path):
        for span in ("padded", "minimal"):
            try:
                dev = hid.device()
                dev.open_path(path)
                dev.set_nonblocking(0)
                dev.write(build_enable(span))
                pkt = dev.read(PKT, timeout_ms=300)
                if pkt and decode(bytes(pkt)):
                    return dev, span
                dev.close()
            except Exception:
                try:
                    dev.close()
                except Exception:
                    pass
        return None, None

    def open(self):
        paths = self._candidates()
        if not paths:
            raise RuntimeError(
                "no 0x3318 USB devices found - is the Air plugged in via the Deck's USB-C?")
        for p in paths:
            dev, span = self._try_open(p)
            if dev:
                self.dev = dev
                print(f"[imu] stream open (checksum span: {span})", flush=True)
                return
        raise RuntimeError(
            "found Xreal USB devices but no IMU stream - check that "
            "wheaney's xr-driver service is stopped (it owns the device)")

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
                pkt = self.dev.read(PKT, timeout_ms=100)
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
            self.filter.update(gx, gy, gz, s["ax"], s["ay"], s["az"],
                               s["mx"], s["my"], s["mz"], dt)
            roll, pitch, yaw = euler_deg(self.filter.q)

            # auto-calibrate 1g from the first ~1s of samples (assumes still)
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
                              temp_c=s["temp_raw"] / 333.87 + 21.0)  # ICM-20602 datasheet

    def close(self):
        if self.dev:
            try:
                self.dev.write(build_disable())
            except Exception:
                pass
            self.dev.close()


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
