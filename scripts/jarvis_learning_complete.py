#!/usr/bin/env python3
"""Record the verified outcome of one JARVIS capability-learning job."""
from __future__ import annotations

import argparse
import sys

from core.esp32_voice_workflows import LearningQueue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue-dir",
        default="/home/lacasa/waveshare-va-test/learning-queue",
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--status", required=True, choices=("completed", "failed"))
    parser.add_argument("--capability", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        queue = LearningQueue(args.queue_dir)
        queue.write_completion(
            args.job_id,
            status=args.status,
            capability=args.capability,
        )
    except (OSError, ValueError) as exc:
        print(f"completion record rejected: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"recorded={args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
