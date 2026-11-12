"""Command line: `evaluate` one posting file, or `run` the whole fetch-evaluate-report cycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import collect, evaluate as CE, pipeline as P, report, sources as CS
from .util import iso, parse_ts


def fixture_transport(directory: Path):
    """Serve recorded provider JSON from files named by a hash-free slug of the URL, so demos and tests need no network."""
    index = json.loads((directory / "index.json").read_text())

    def transport(url: str):
        name = index.get(url)
        if not name:
            return 404, b""
        return 200, (directory / name).read_bytes()
    return transport


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="job-scoring")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="evaluate normalized postings from a JSON file (list of posting objects)")
    ev.add_argument("postings", type=Path)
    ev.add_argument("--profile", type=Path)
    run = sub.add_parser("run", help="fetch boards, evaluate, write a report")
    run.add_argument("--boards", type=Path, default=P.BOARDS_PATH)
    run.add_argument("--profile", type=Path)
    run.add_argument("--fixtures", type=Path, help="serve provider payloads from this directory instead of the network")
    run.add_argument("--now", default=None, help="ISO timestamp with timezone (default: current time)")
    run.add_argument("--out", type=Path, default=Path("out/report.md"))
    args = ap.parse_args(argv)
    prof = CE.load_profile(args.profile)

    if args.cmd == "evaluate":
        postings = json.loads(args.postings.read_text())
        out = [{"pid": p.get("pid"), "title": p["title"], **{k: v for k, v in CE.evaluate(p, prof).items() if k in ("tier", "score", "exclusions", "gaps", "uncertainties")}} for p in postings]
        print(json.dumps(out, indent=2))
        return 0

    from datetime import datetime, timezone
    now = iso(args.now) if args.now else iso(datetime.now(timezone.utc))
    fetcher = CS.Fetcher(transport=fixture_transport(args.fixtures), sleep=lambda s: None, gap=0) if args.fixtures else CS.Fetcher()
    state = collect.run({}, prof, fetcher, now, args.boards)
    text, meta = report.render_report(state, prof, now)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(f"{len(state['postings'])} postings kept, shortlist {meta['top']}, wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
