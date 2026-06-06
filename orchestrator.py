#!/usr/bin/env python3
"""
ApexClaw end-to-end orchestrator — ties the four components into one flow.

    [box_openrouter_pipeline]  prompt + Box docs -> local LLM draft answer
            |
    [hallucination_detector]   draft trustworthy?
            |  NO  -> return local answer (STOP)
            |  YES -> escalate
            v
    [policy-agent]  classify -> evaluate  ->  ALLOW | BLOCK | ESCALATE
            |  ALLOW    -> send to frontier cloud model (STOP)
            |  ESCALATE -> human review (STOP)
            |  BLOCK
            v
    [redact-healthcare.py]  Emma's data massager (true PHI redaction, fail-closed)
            |
            +--> re-feed the redacted document back into policy-agent  (loop)
                 until ALLOW, or ESCALATE if it can't be cleared.

Components own their own concerns:
  - policy-agent decides WHETHER data may leave (and what/where is sensitive).
  - the massager owns WHAT counts as PHI and HOW it is removed, then proves it.

Box retrieval + hallucination check need live credentials, so they are OPTIONAL
here: pass a local --doc to start the flow at the policy gate (the part this repo
can run offline), and optionally --draft to exercise the hallucination gate.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO, "policy-agent", "src"))

from agent import PolicyAgent                      # noqa: E402  (policy-agent/src)
from providers import get_provider                 # noqa: E402
from contract import Verdict                       # noqa: E402

# Map a policy-agent case -> the massager that can clean it.
# Today only healthcare/PDF has a massager (Emma's). Others have none yet.
REDACT_SCRIPT = os.path.join(REPO, "redact-healthcare.py")
HEALTHCARE_RULES = os.path.join(REPO, "rules", "healthcare.yaml")


@dataclass
class PipelineResult:
    outcome: str                 # SEND_TO_CLOUD | HUMAN_REVIEW | LOCAL_ANSWER
    final_doc: Optional[str]     # path to the (possibly redacted) doc cleared for cloud
    loops: int
    detail: str


# ----------------------------------------------------------- hallucination ---
def hallucination_gate(prompt: str, draft_answer: str) -> bool:
    """Return True if we should ESCALATE (draft is untrustworthy), False to STOP."""
    from hallucination_detector import detect_hallucination
    res = detect_hallucination(prompt, draft_answer)
    print(f"[hallucination] domain={res.domain} hallucination={res.hallucination} "
          f"reason={res.reason}")
    return res.hallucination


# ---------------------------------------------------------------- massager ---
def run_healthcare_redactor(pdf_path: str, out_dir: str) -> Optional[str]:
    """Call Emma's redactor. Returns the redacted PDF path on CLEAR, else None
    (engine fails closed and emits no document if residual PHI remains)."""
    proc = subprocess.run(
        [sys.executable, REDACT_SCRIPT, pdf_path, HEALTHCARE_RULES, out_dir],
        capture_output=True, text=True,
    )
    print(f"[massager] {proc.stdout.strip()}")
    if proc.returncode != 0:
        print(f"[massager] redactor did not produce a clean doc (exit {proc.returncode})")
        return None
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    redacted = os.path.join(out_dir, f"{base}.redacted.pdf")
    return redacted if os.path.exists(redacted) else None


def select_massager(case: str, doc_path: Optional[str]):
    """Return a callable(doc_path, out_dir) -> redacted_path|None, or None if no
    massager exists for this case/format yet."""
    if case == "healthcare" and isinstance(doc_path, str) and doc_path.lower().endswith(".pdf"):
        return run_healthcare_redactor
    return None


# ------------------------------------------------------------- orchestration --
def run(prompt: str,
        doc_path: str,
        draft_answer: Optional[str] = None,
        provider_name: Optional[str] = None,
        model: Optional[str] = None,
        out_dir: str = "output",
        max_loops: int = 3) -> PipelineResult:

    # Stage 2: hallucination gate (only if a draft answer is supplied)
    if draft_answer is not None:
        if not hallucination_gate(prompt, draft_answer):
            return PipelineResult("LOCAL_ANSWER", None, 0,
                                  "Local answer trusted; no escalation needed.")

    # Stages 3 + 4: policy gate, then redact-and-recheck loop
    agent = PolicyAgent(provider=get_provider(provider_name, model))
    print(f"[policy] provider={agent.provider.name} model={agent.provider.model}")
    current = doc_path

    for loop in range(1, max_loops + 1):
        v: Verdict = agent.run(prompt, current)
        print(f"[policy] loop {loop}: verdict={v.verdict} case={v.matched_case} "
              f"conf={v.classification_confidence:.2f} findings={len(v.findings)}")

        if v.verdict == "ALLOW":
            return PipelineResult("SEND_TO_CLOUD", current, loop - 1,
                                  "No proprietary data remaining; cleared for frontier model.")
        if v.verdict == "ESCALATE":
            return PipelineResult("HUMAN_REVIEW", None, loop - 1, v.explanation)

        # BLOCK -> hand to the massager for this case
        massager = select_massager(v.matched_case, current)
        if massager is None:
            return PipelineResult(
                "HUMAN_REVIEW", None, loop - 1,
                f"BLOCK on case '{v.matched_case}' but no massager available for this "
                f"format yet; routing to human review.")

        redacted = massager(current, out_dir)
        if redacted is None:
            return PipelineResult(
                "HUMAN_REVIEW", None, loop,
                "Massager could not produce a clean document (fail-closed); human review.")
        if os.path.abspath(redacted) == os.path.abspath(current):
            return PipelineResult("HUMAN_REVIEW", None, loop,
                                  "Massager made no progress; human review.")
        current = redacted  # re-check the redacted document on the next loop

    return PipelineResult("HUMAN_REVIEW", None, max_loops,
                          f"Still BLOCK after {max_loops} redaction passes; human review.")


def main() -> int:
    ap = argparse.ArgumentParser(description="ApexClaw end-to-end policy + redaction pipeline.")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--doc", required=True, help="local document (PDF) or controller-backup folder")
    ap.add_argument("--draft", default=None, help="optional local-LLM draft answer (runs hallucination gate)")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=os.path.join(REPO, "output"))
    ap.add_argument("--max-loops", type=int, default=3)
    args = ap.parse_args()

    result = run(args.prompt, args.doc, draft_answer=args.draft,
                 provider_name=args.provider, model=args.model,
                 out_dir=args.out, max_loops=args.max_loops)

    print("\n==================== PIPELINE RESULT ====================")
    print(f"outcome : {result.outcome}")
    print(f"loops   : {result.loops}")
    print(f"doc     : {result.final_doc or '(none)'}")
    print(f"detail  : {result.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
