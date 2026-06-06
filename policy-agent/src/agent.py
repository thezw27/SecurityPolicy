"""Policy Agent orchestrator.

Pipeline (this is MY SCOPE in the AgentCOP flow, entered after the hallucination check says YES):

    intake(prompt, source) -> DocumentBundle
      -> Stage 1: classify case (router)            -> ESCALATE if low confidence / unknown / mixed
      -> Stage 2: apply that case's policy           -> ALLOW | BLOCK | ESCALATE  (+ findings)

The verdict is the contract handed to the downstream data massager (out of scope). The agent is
idempotent/re-runnable so massaged data can loop back through it.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import yaml

from contract import DocumentBundle, Verdict
from intake import build_bundle
from providers import Provider, get_provider

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_dotenv() -> None:
    """Load policy-agent/.env into the environment if python-dotenv is installed.
    No-op otherwise — env vars set directly in the shell still work."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(ROOT, ".env"))


_load_dotenv()
POLICIES_DIR = os.path.join(ROOT, "policies")
AGENT_DIR = os.path.join(ROOT, "agent")

CLASSIFIER_SCHEMA = {
    "title": "CaseClassification",
    "type": "object",
    "properties": {
        "case_id": {"type": "string"},
        "confidence": {"type": "number"},
        "signals": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["case_id", "confidence"],
}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_registry() -> Dict[str, Any]:
    return yaml.safe_load(_read(os.path.join(POLICIES_DIR, "registry.yaml")))


def _load_verdict_schema() -> Dict[str, Any]:
    return json.loads(_read(os.path.join(AGENT_DIR, "schema", "verdict.schema.json")))


def _cases_block(registry: Dict[str, Any]) -> str:
    lines = []
    for c in registry.get("cases", []):
        sig = c.get("signals", {})
        lines.append(
            f"- id: {c['id']} — {c.get('title','')}\n"
            f"    keywords: {', '.join(sig.get('keywords', []))}\n"
            f"    filenames: {', '.join(sig.get('filenames', []))}"
        )
    return "\n".join(lines)


class PolicyAgent:
    def __init__(self, provider: Optional[Provider] = None):
        self.provider = provider or get_provider()
        self.registry = _load_registry()
        self.verdict_schema = _load_verdict_schema()
        self.threshold = float(self.registry.get("classification_confidence_threshold", 0.6))
        self.classifier_prompt = _read(os.path.join(AGENT_DIR, "classifier_prompt.md"))
        self.evaluator_prompt = _read(os.path.join(AGENT_DIR, "evaluator_prompt.md"))
        self._case_index = {c["id"]: c for c in self.registry.get("cases", [])}

    # --- Stage 1 -----------------------------------------------------------
    def classify(self, prompt: str, bundle: DocumentBundle) -> Dict[str, Any]:
        user = self.classifier_prompt.format(
            cases_block=_cases_block(self.registry),
            prompt=prompt or "(none)",
            filenames=", ".join(bundle.filenames) or "(none)",
            file_types=", ".join(bundle.file_types) or "(none)",
            file_inventory=", ".join(bundle.file_inventory[:50]) or "(none)",
            excerpt=bundle.excerpt() or "(none)",
            missing="; ".join(bundle.missing) or "(none)",
        )
        return self.provider.complete(
            system="You are a precise routing classifier. Output only JSON.",
            user=user, schema=CLASSIFIER_SCHEMA,
        )

    # --- Stage 2 -----------------------------------------------------------
    def evaluate(self, prompt: str, bundle: DocumentBundle, case_id: str, confidence: float) -> Verdict:
        case = self._case_index[case_id]
        policy_md = _read(os.path.join(POLICIES_DIR, case["policy_file"]))
        user = self.evaluator_prompt.format(
            policy_markdown=policy_md,
            prompt=prompt or "(none)",
            filenames=", ".join(bundle.filenames) or "(none)",
            file_types=", ".join(bundle.file_types) or "(none)",
            file_inventory=", ".join(bundle.file_inventory[:80]) or "(none)",
            full_text=bundle.extracted_text or "(none)",
            missing="; ".join(bundle.missing) or "(none)",
            case_id=case_id,
            classification_confidence=confidence,
        )
        raw = self.provider.complete(
            system="You are a conservative data-loss-prevention policy evaluator. Output only JSON.",
            user=user, schema=self.verdict_schema,
        )
        # The case + confidence are decided authoritatively by the router (Stage 1), not the
        # evaluator model — overwrite whatever the model echoed.
        raw["matched_case"] = case_id
        raw["classification_confidence"] = confidence
        return Verdict.from_dict(raw)

    # --- Full pipeline -----------------------------------------------------
    def run(self, prompt: str, source: Any = None) -> Verdict:
        bundle = build_bundle(source, prompt=prompt)

        # Deterministic safety net (model-independent): if an attachment is present but nothing
        # could be extracted from it (opaque/binary/unreadable), we cannot rule out proprietary
        # data — escalate to human review rather than letting the model guess. Matches every
        # policy's ESCALATE conditions. Self-contained prompts with no attachment are unaffected.
        has_attachment = bool(bundle.filenames) and bundle.filenames != ["<inline>"]
        if has_attachment and not bundle.extracted_text.strip() and bundle.missing:
            return Verdict.escalate(
                "Attachment present but no content could be extracted "
                "(opaque/binary/unreadable); cannot rule out proprietary data. Routing to human review.",
                case="unknown",
            )

        cls = self.classify(prompt, bundle)
        case_id = cls.get("case_id", "unknown")
        confidence = float(cls.get("confidence", 0.0))

        if case_id not in self._case_index:
            return Verdict.escalate(
                f"Unrecognized or unknown case ('{case_id}'): {cls.get('reasoning','')}. Routing to human review.",
                case="unknown", confidence=confidence,
            )
        if confidence < self.threshold:
            return Verdict.escalate(
                f"Classification confidence {confidence:.2f} below threshold {self.threshold:.2f}; "
                f"will not guess. Routing to human review.",
                case=case_id, confidence=confidence,
            )

        return self.evaluate(prompt, bundle, case_id, confidence)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run the Policy Agent on a prompt + optional attachment.")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--source", help="path to a file/folder, or inline text", default=None)
    ap.add_argument("--provider", default=None, help="mock|gemini|openrouter|anthropic")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    agent = PolicyAgent(provider=get_provider(args.provider, args.model))
    verdict = agent.run(args.prompt, args.source)
    print(json.dumps(verdict.to_dict(), indent=2))
