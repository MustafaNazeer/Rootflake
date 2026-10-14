"""Corpus builder commands: `fetch-idoft`, `screen`, and `draw`.

`draw` refuses to run until rule 11 and 12 checks exist and `R` is measured (ADR 0006).
"""

import argparse
import json
import sys
from collections import Counter

from rootflake.corpus import idoft
from rootflake.corpus.allocate import plan
from rootflake.corpus.funnel import screen
from rootflake.sandbox.workspace import default_root


def _fetch(_args: argparse.Namespace) -> int:
    print(idoft.fetch(default_root()))
    return 0


def _screen(_args: argparse.Namespace) -> int:
    path = idoft.cache_path(default_root(), idoft.PINNED_COMMIT)
    if not path.exists():
        print(f"{path} is missing; run `python -m rootflake.corpus fetch-idoft`", file=sys.stderr)
        return 1
    rows = idoft.parse(path.read_bytes(), idoft.PINNED_SHA256)
    result = screen(rows)
    eligible = result.eligible
    prs: dict[str, set[str]] = {}
    for item in eligible:
        prs.setdefault(item.stratum, set()).add(item.pr)
    counts = {name: len(urls) for name, urls in sorted(prs.items())}
    report = {
        "source": {"commit": idoft.PINNED_COMMIT, "sha256": idoft.PINNED_SHA256},
        "funnel": {"status_not_accepted": result.not_accepted, **Counter(result.reasons.values())},
        "eligible_rows": dict(sorted(Counter(item.stratum for item in eligible).items())),
        "eligible_prs": counts,
        "plan": {name: list(split) for name, split in plan(counts).items()},
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _draw(_args: argparse.Namespace) -> int:
    print(
        "the draw is not available yet: rules 11 and 12 need sandbox builds of target "
        "projects, and R (the passing configuration run count in ADR 0006) is not measured",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rootflake.corpus")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch-idoft", help="download and verify the pinned pr-data.csv")
    commands.add_parser("screen", help="apply rules 1 to 5 and print the allocation plan")
    commands.add_parser("draw", help="draw the corpus (not available yet)")
    args = parser.parse_args(argv)
    return {"fetch-idoft": _fetch, "screen": _screen, "draw": _draw}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
