#!/usr/bin/env python3
"""
pythonengine.py

Headless runner for the SOP engine (same logic the UI runs in-process).
Use this if you want to run the engine without the web UI, e.g. under systemd.

  python pythonengine.py            # run continuously
  python pythonengine.py --once     # single pass (good for cron / testing)
  python pythonengine.py --interval 60
"""

import sys
import logging
import argparse
import threading

import engine_core

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="ServiceNow -> AWS DevOps Agent SOP engine")
    parser.add_argument("--once", action="store_true", help="run a single poll pass and exit")
    parser.add_argument("--interval", type=int, default=30, help="poll interval seconds")
    args = parser.parse_args()

    missing = engine_core.missing_config()
    if missing:
        logging.error("Missing required config (set in .env): %s", ", ".join(missing))
        sys.exit(1)

    if args.once:
        engine_core.poll_once()
        return

    stop = threading.Event()
    try:
        engine_core.run_loop(stop, args.interval)
    except KeyboardInterrupt:
        stop.set()


if __name__
