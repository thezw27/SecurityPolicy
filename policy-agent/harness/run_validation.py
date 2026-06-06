"""Validation harness for the Policy Agent.

Runs every fixture through the full agent and compares the verdict to test_cases.yaml.

Examples:
    python harness/run_validation.py --provider mock          # offline smoke (no keys)
    python harness/run_validation.py --provider gemini        # real, needs GEMINI_API_KEY
    python harness/run_validation.py --provider openrouter --model google/gemini-2.0-flash-001
    python harness/run_validation.py --provider gemini --real # also runs local real-data cases
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

# Make src/ importable
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from agent import PolicyAgent          # noqa: E402
from providers import get_provider     # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
# Synthetic verdicts are safe to commit; real-data verdicts go to a gitignored folder.
EXAMPLES_DIR = os.path.join(ROOT, "examples", "verdicts")
REAL_OUT_DIR = os.path.join(ROOT, "local_data", "verdicts")


def _load_fixture(name: str):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def _save_verdict(out_dir: str, name: str, verdict, provider) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.splitext(name)[0] + ".verdict.json")
    payload = {"_input": name, "_provider": provider.name, "_model": provider.model, **verdict.to_dict()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def _row(label, expected, got_v, got_case, ok):
    mark = "PASS" if ok else "FAIL"
    return f"  [{mark}] {label:<28} expect={expected:<9} got={got_v:<9} case={got_case}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=os.getenv("POLICY_PROVIDER", "mock"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--real", action="store_true", help="also run local real-data cases (gitignored paths)")
    ap.add_argument("--save", action="store_true",
                    help="write each verdict to examples/verdicts/ (synthetic) and local_data/verdicts/ (real)")
    args = ap.parse_args()

    agent = PolicyAgent(provider=get_provider(args.provider, args.model))
    spec = yaml.safe_load(open(os.path.join(HERE, "test_cases.yaml"), "r", encoding="utf-8"))

    print(f"\nPolicy Agent validation — provider={agent.provider.name} model={agent.provider.model}\n")
    passed = total = 0

    print("Synthetic fixtures:")
    for case in spec.get("cases", []):
        fx = _load_fixture(case["fixture"])
        v = agent.run(fx.get("prompt", ""), fx.get("source"))
        ok = v.verdict == case["expect"]
        if "expect_case" in case and v.verdict != "ESCALATE":
            ok = ok and v.matched_case == case["expect_case"]
        passed += ok
        total += 1
        print(_row(case["fixture"], case["expect"], v.verdict, v.matched_case, ok))
        if args.save:
            _save_verdict(EXAMPLES_DIR, case["fixture"], v, agent.provider)

    if args.real:
        print("\nReal-data cases (local only):")
        for case in spec.get("real_cases", []):
            src = case.get("source")
            if src and not os.path.exists(src):
                print(f"  [SKIP] {os.path.basename(src)} — not found on this machine")
                continue
            v = agent.run(case.get("prompt", ""), src)
            ok = v.verdict == case["expect"]
            if "expect_case" in case and v.verdict != "ESCALATE":
                ok = ok and v.matched_case == case["expect_case"]
            passed += ok
            total += 1
            label = os.path.basename(src) if src else "(inline)"
            print(_row(label, case["expect"], v.verdict, v.matched_case, ok))
            for fnd in v.findings[:4]:
                print(f"          - {fnd.category}/{fnd.severity} @ {fnd.location}: {fnd.rationale[:70]}")
            if args.save:
                _save_verdict(REAL_OUT_DIR, label, v, agent.provider)

    print(f"\nResult: {passed}/{total} passed\n")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
