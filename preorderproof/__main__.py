"""Offline command line entry points for three independent business systems."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __file__ as imported_file
from .audit import audit, report
from .store import connect, ingest

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = {"offer-import": "offer", "payment-import": "payment", "delivery-import": "delivery",
            "temperature-import": "temperature"}


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preorder evidence and delivery rights")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in [*COMMANDS, "report", "audit", "demo", "init"]:
        child = sub.add_parser(command)
        child.add_argument("--db", default="preorder.sqlite")
        if command in COMMANDS:
            child.add_argument("--file", "--fixture", required=True)
    args = parser.parse_args(argv)
    try:
        conn = connect(args.db)
        if args.command in COMMANDS:
            source = Path(args.file)
            events = json.loads(source.read_text(encoding="utf-8"))
            print_json({"source": str(source.resolve()), "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                        **ingest(conn, COMMANDS[args.command], events)})
        elif args.command == "report":
            print_json(report(conn))
        elif args.command == "audit":
            verdict = audit(conn)
            print_json(verdict)
            return 0 if verdict["ok"] else 2
        elif args.command == "demo":
            stages = [
                ("offer", "offer-ok.json"), ("payment", "payment-ok.json"),
                ("delivery", "delivery-ok.json"), ("delivery", "delivery-recovery.json"),
                ("offer", "offer-restock.json"), ("offer", "offer-bad.json"),
                ("payment", "payment-bad.json"), ("delivery", "delivery-bad.json"),
                ("offer", "offer-restock-bad.json"),
            ]
            transitions = []
            for subsystem, name in stages:
                fixture = ROOT / "fixtures" / name
                result = ingest(conn, subsystem, json.loads(fixture.read_text(encoding="utf-8")))
                transitions.append({"fixture": name, **result,
                                    "current": {k: v["status"] for k, v in report(conn)["decisions"].items()}})
            print_json({"imported_module": str(Path(imported_file).resolve()),
                        "transitions": transitions, "final": report(conn), "audit": audit(conn)})
        else:
            print_json({"db": str(Path(args.db).resolve()), "initialized": True,
                        "imported_module": str(Path(imported_file).resolve())})
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
