import json
import socket
import threading
import time


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
