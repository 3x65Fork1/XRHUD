import threading
import time

try:
    from imu import ImuReader
except ImportError:
    ImuReader = None


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
