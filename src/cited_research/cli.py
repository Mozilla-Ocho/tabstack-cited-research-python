from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

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
        "--output",
        required=True,
        type=Path,
        help="Directory for report.md, sources.json, manifest.",
    )
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
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
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
