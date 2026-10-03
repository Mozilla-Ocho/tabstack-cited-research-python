from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .tabstack_runner import run_research


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cited-research",
        description="Ask one question of Tabstack /research and save the report and cited sources.",
    )
    p.add_argument("--query", required=True, help="The research question.")
    p.add_argument("--mode", choices=("fast", "balanced"), default="fast")
    p.add_argument(
        "--nocache", action="store_true", help="Bypass the content cache; force fresh retrieval."
    )
    p.add_argument("--fetch-timeout", type=int, default=None, metavar="SECONDS")
    p.add_argument(
        "--silence-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="Stop waiting if no event arrives for this long (exit 7). Off by default. "
        "The request is not retried and may still complete and bill server-side.",
    )
    p.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Directory for report.md, sources.json, manifest.",
    )
    p.add_argument("--quiet", action="store_true")
    return p


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def command_line(argv: Sequence[str]) -> str:
    """The invocation as a copy-pasteable command, one flag per line. The CLI takes no secret
    arguments, so this is safe to record; the runner still scrubs it."""
    parts: List[str] = []
    tokens = list(argv)
    i = 0
    while i < len(tokens):
        token = shlex.quote(tokens[i])
        has_value = i + 1 < len(tokens) and not tokens[i + 1].startswith("--")
        if tokens[i].startswith("--") and has_value:
            token += " " + shlex.quote(tokens[i + 1])
            i += 1
        parts.append(token)
        i += 1
    return " \\\n  ".join(["cited-research", *parts])


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if not os.environ.get("TABSTACK_API_KEY"):
        sys.stderr.write(
            "TABSTACK_API_KEY is not set. Export it in your shell; "
            "the CLI never accepts it as a flag.\n"
        )
        return 5
    return run_research(
        query=args.query,
        mode=args.mode,
        nocache=args.nocache,
        fetch_timeout=args.fetch_timeout,
        output_dir=args.output,
        quiet=args.quiet,
        silence_timeout=args.silence_timeout,
        command=command_line(argv),
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
