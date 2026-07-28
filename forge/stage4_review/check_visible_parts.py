#!/usr/bin/env python3
"""Capture or verify a local visible-part fidelity report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_shared"))
from visible_part_contracts import evaluate_visible_part_report, stamp_visible_part_report


def load_object(path: Path, label: str) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def load_reviews(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print("PASS" if result.get("passed") else "FAIL")
    for failure in result.get("failedGates", []):
        print(f"- {failure}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture",
        help="Stamp current source/dependency fingerprints onto authored per-part observations",
    )
    capture.add_argument("--spec", type=Path, required=True)
    capture.add_argument("--pass-id", required=True)
    capture.add_argument("--reviews", type=Path, required=True)
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--json", action="store_true")

    verify = subparsers.add_parser(
        "verify",
        help="Re-evaluate a captured report and fail when contracts or source files are stale",
    )
    verify.add_argument("--spec", type=Path, required=True)
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--pass-id")
    verify.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    spec_path = args.spec.expanduser().resolve()
    try:
        spec = load_object(spec_path, "spec")
        if args.command == "capture":
            observations = load_reviews(args.reviews.expanduser().resolve())
            report = stamp_visible_part_report(
                spec, args.pass_id, observations, spec_path.parent
            )
            output = args.out.expanduser().resolve()
            write_report(output, report)
            result = {
                "passed": report["verdict"] == "pass",
                "action": report["action"],
                "failedGates": report["failedGates"],
                "report": str(output),
            }
        else:
            report = load_object(args.report.expanduser().resolve(), "report")
            pass_id = args.pass_id or report.get("passId")
            if not isinstance(pass_id, str) or not pass_id:
                raise ValueError("--pass-id is required when the report has no passId")
            result = evaluate_visible_part_report(spec, report, pass_id, spec_path.parent)
        print_result(result, args.json)
        return 0 if result.get("passed") else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
