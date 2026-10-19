#!/usr/bin/env python3
"""Dispatch queued JARVIS learning requirements to a private Hermes webhook."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.esp32_voice_workflows import LearningDispatcher, LearningQueue
from scripts.esp32_api_acceptance import KeyFileError, load_api_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue-dir",
        default="/home/lacasa/waveshare-va-test/learning-queue",
    )
    parser.add_argument("--webhook-url-file", required=True)
    parser.add_argument("--webhook-secret-file", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        webhook_url = load_api_key(Path(args.webhook_url_file))
        webhook_secret = load_api_key(Path(args.webhook_secret_file))
        queue = LearningQueue(args.queue_dir)
        dispatcher = LearningDispatcher(
            queue,
            webhook_url=webhook_url,
            secret=webhook_secret,
            timeout=args.timeout,
        )
        accepted = dispatcher.dispatch_once()
    except (KeyFileError, OSError, ValueError) as exc:
        print(f"learning dispatcher unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"accepted={accepted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
