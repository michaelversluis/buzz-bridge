"""CLI entry point: `python -m buzzbridge run --config bridge.toml`."""
import argparse
import sys

from .bridge import Bridge
from .config import Config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="buzzbridge",
                                 description="Connect any agent to a Buzz channel.")
    ap.add_argument("action", choices=["run", "once"], help="run (daemon) or once (single pass)")
    ap.add_argument("--config", "-c", required=True, help="path to bridge.toml")
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    Bridge(cfg).run(once=args.action == "once")
    return 0


if __name__ == "__main__":
    sys.exit(main())
