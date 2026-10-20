"""Запуск набора проверок из командной строки.

    python -m arli_engine.conformance --target http://127.0.0.1:8801 --holder http://127.0.0.1:8802

Без --holder обе стороны это одна и та же реализация: так проверяют свой движок.
Код выхода 0 означает соответствие, 1 означает расхождения с их списком.
"""

from __future__ import annotations

import argparse
import json
import sys

from arli_engine.conformance.checks import Target, run_conformance


def main() -> int:
    parser = argparse.ArgumentParser(description="ARLI discovery conformance kit")
    parser.add_argument("--target", required=True, help="base URL of the implementation")
    parser.add_argument("--holder", default=None, help="base URL of the other side")
    parser.add_argument("--name", default="target", help="label for the report")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    target = Target(base_url=args.target, name=args.name)
    holder = Target(base_url=args.holder, name="holder") if args.holder else None
    try:
        report = run_conformance(target, holder)
    finally:
        target.close()
        if holder is not None:
            holder.close()

    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=1))
    else:
        print(f"набор проверок: {report.target}")
        for check in report.checks:
            mark = "ок" if check["passed"] else "НЕ СООТВЕТСТВУЕТ"
            print(f"  [{mark}] {check['code']}: {check['detail']}")
        print(f"\nвсего проверок {len(report.checks)}, расхождений {len(report.failures)}")
        print("итог: соответствует" if report.compliant else "итог: есть расхождения")
    return 0 if report.compliant else 1


if __name__ == "__main__":
    sys.exit(main())
