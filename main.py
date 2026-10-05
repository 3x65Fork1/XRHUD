#!/usr/bin/env python3

import argparse

from hud import Hud


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
