"""Local command-line interface. No automatic model or network calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from . import Engine, ReweaveError, __version__
from .bundle import verify_bundle
from .codec import parse


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        try:
            from .mcp_server import main as serve
        except ImportError:
            print("From the source checkout, install the optional adapter with: python -m pip install '.[mcp]'", file=sys.stderr)
            return 2
        return serve(argv[1:]) or 0
    parser = argparse.ArgumentParser(description="Reuse computation while rechecking acceptance.")
    parser.add_argument("--version", action="version", version=f"Atlas Reweave {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the labelled deterministic examples")
    demo.add_argument("--store", type=Path, required=True)
    demo.add_argument("--integer-only", action="store_true")
    inspect = commands.add_parser("inspect", help="Read historical metadata without returning output")
    inspect.add_argument("--store", type=Path, required=True)
    inspect.add_argument("--work", required=True)
    inspect.add_argument("--scope", default="default")
    verify = commands.add_parser("verify-bundle", help="Check bundle integrity and accepted closure, without semantic verification")
    verify.add_argument("path", type=Path)
    verify.add_argument("--max-bytes", type=int, default=16_777_216)
    benchmark = commands.add_parser("benchmark", help="Measure actual calls on deterministic fixtures")
    benchmark.add_argument("--output", type=Path)
    commands.add_parser("serve", help="Run the optional MCP stdio adapter; use serve --help")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from .examples import run_demo

            _print(run_demo(args.store, integer_only=args.integer_only))
        elif args.command == "inspect":
            from .examples import make_stages

            if not args.store.is_file():
                raise ReweaveError("The store does not exist")
            with Engine(args.store, make_stages(), scope=args.scope) as engine:
                _print(engine.inspect(args.work))
        elif args.command == "verify-bundle":
            if not 1 <= args.max_bytes <= 268_435_456:
                raise ReweaveError("--max-bytes must be between 1 and 268435456")
            if args.path.stat().st_size > args.max_bytes:
                raise ReweaveError("Bundle file exceeds the CLI read limit")
            _print(verify_bundle(parse(args.path.read_text(encoding="utf-8"), args.max_bytes)))
        elif args.command == "benchmark":
            from .benchmark import run_benchmark

            report = run_benchmark()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
            _print(report)
        return 0
    except (ReweaveError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
