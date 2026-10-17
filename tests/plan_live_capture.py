"""Run the shipped planner against a captured Apple DOM (no browser or network).

Browser inspection/actions use the supported browser tool. This harness only
feeds its unmodified DOM capture to the production decision code. It cannot
place an order, and the desktop application's target remains iPhone Duo.
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from duo.browser import Adapter
from duo.model import Config, DUO_TARGET, ProductTarget

AIR = ProductTarget("iPhone Air", "云白色", "/shop/buy-iphone/iphone-air")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--air", action="store_true")
    parser.add_argument("--added", action="store_true")
    parser.add_argument("--store", default="")
    args = parser.parse_args()
    snap = json.loads(args.capture.read_text(encoding="utf-8"))
    adapter = Adapter(Config(), AIR if args.air else DUO_TARGET)
    adapter.added = adapter.product_verified = args.added
    adapter.chosen_store = args.store
    decision = asdict(adapter.plan(snap))
    result = {"capture": args.capture.name, "target": asdict(adapter.target),
              "checks": adapter.selected_checks(snap), "decision": decision}
    path = ROOT / "artifacts" / "live-next.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
