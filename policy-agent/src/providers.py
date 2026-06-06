"""Model-agnostic provider layer.

One interface, `complete(system, user, schema) -> dict`, with swappable backends so the same agent
runs on a small cloud model (Gemini / OpenRouter) for ApexClaw, on Anthropic if a key is ever
available, or fully offline (`mock`) for CI smoke tests.

Selected via env vars:
    POLICY_PROVIDER = mock | gemini | openrouter | anthropic   (default: mock)
    POLICY_MODEL    = provider-specific model id (optional; sensible defaults below)
Keys:
    GEMINI_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional


class ProviderError(RuntimeError):
    pass


def get_provider(name: Optional[str] = None, model: Optional[str] = None) -> "Provider":
    name = (name or os.getenv("POLICY_PROVIDER") or "mock").lower()
    model = model or os.getenv("POLICY_MODEL")
    if name == "mock":
        return MockProvider(model)
    if name == "gemini":
        return GeminiProvider(model)
    if name == "openrouter":
        return OpenRouterProvider(model)
    if name == "anthropic":
        return AnthropicProvider(model)
    raise ProviderError(f"Unknown provider '{name}'")


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort: parse a JSON object out of a model response (handles ```json fences, prose)."""
    text = text.strip()
    # strip code fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to first {...} balanced-ish block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ProviderError(f"Could not parse JSON from model output: {text[:300]}")


class Provider:
    name = "base"

    def __init__(self, model: Optional[str] = None):
        self.model = model or self.default_model

    default_model = ""

    def complete(self, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Return a dict conforming to `schema`. One re-ask on parse failure."""
        raise NotImplementedError


# --- Mock (offline) ---------------------------------------------------------

class MockProvider(Provider):
    """Deterministic, key-free backend for CI and demos.

    It does NOT call any model. It applies crude keyword heuristics so the harness exercises the full
    pipeline (intake -> classify -> evaluate -> schema) end-to-end offline. Real accuracy comes from
    the gemini/openrouter backends; mock only proves the wiring.
    """
    name = "mock"
    default_model = "mock-rules-v1"

    @staticmethod
    def _input_region(user: str) -> str:
        """Mock heuristics must only see the user's prompt+data, not the policy/case reference text.
        Both prompts delimit the real input with these markers."""
        start = user.find("=== BEGIN INPUT ===")
        end = user.find("=== END INPUT ===")
        if start != -1 and end != -1 and end > start:
            return user[start:end]
        return user

    def complete(self, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        blob = self._input_region(user).lower()
        title = (schema.get("title") or "").lower()

        # Classifier schema
        if "case_id" in (schema.get("properties") or {}):
            # OT terms kept robot-specific to avoid collisions with common English/clinical words
            # (e.g. bare "rapid"/"robot" appear in medical text). Real LLM classifier isn't this brittle.
            ot = any(k in blob for k in ["controller", "abb", "robotware", "rapid program", "rapid module",
                                         "restore", "netconfig", ".mod", "eef_pos_roll", "teach pendant", "elog"])
            hc = any(k in blob for k in ["patient", "discharge", "mrn", "diagnosis", "attending", "discharge summary"])
            if ot and not hc:
                return {"case_id": "industrial_ot", "confidence": 0.9, "signals": ["mock: ot keywords"], "reasoning": "mock"}
            if hc and not ot:
                return {"case_id": "healthcare", "confidence": 0.9, "signals": ["mock: phi keywords"], "reasoning": "mock"}
            if ot and hc:
                return {"case_id": "unknown", "confidence": 0.3, "signals": ["mock: mixed"], "reasoning": "mock mixed-domain"}
            return {"case_id": "unknown", "confidence": 0.2, "signals": ["mock: no match"], "reasoning": "mock"}

        # Evaluator (verdict) schema
        findings = []
        rules = []
        if any(k in blob for k in ["mrn", "discharge date", "date of birth", "dob", "patient name", "okafor", "attending"]):
            findings.append({"category": "phi", "location": "header", "severity": "high",
                             "rationale": "mock: HIPAA identifier present", "redaction_hint": "mask"})
            rules = ["HC-1", "HC-2", "HC-3"]
        if any(k in blob for k in ["netconfig", "uas_application_grants", "key.id", "ip address", ".mod", "eef_pos_roll", "system.guid"]):
            findings.append({"category": "network", "location": "backup", "severity": "high",
                             "rationale": "mock: OT secret/network/IP present", "redaction_hint": "mask"})
            rules = ["OT-1", "OT-3", "OT-4"]

        # Findings win first. (Ambiguous/opaque inputs are caught upstream by the classifier, which
        # routes them to ESCALATE before evaluation; "could not extract text" is a content sentinel
        # the ambiguous fixture uses, distinct from the literal "missing/unreadable:" field label.)
        if findings:
            return {"verdict": "BLOCK", "matched_case": "unknown", "classification_confidence": 0.9,
                    "matched_policy_rules": rules, "findings": findings, "explanation": "mock: proprietary data found"}
        if "could not extract text" in blob:
            return {"verdict": "ESCALATE", "matched_case": "unknown", "classification_confidence": 0.3,
                    "matched_policy_rules": [], "findings": [], "explanation": "mock: content not inspectable"}
        return {"verdict": "ALLOW", "matched_case": "unknown", "classification_confidence": 0.9,
                "matched_policy_rules": [], "findings": [], "explanation": "mock: no proprietary data detected"}


# --- Gemini -----------------------------------------------------------------

class GeminiProvider(Provider):
    name = "gemini"
    default_model = "gemini-2.0-flash"

    def complete(self, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ProviderError("pip install google-generativeai") from e
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY not set")
        genai.configure(api_key=key)
        model = genai.GenerativeModel(self.model, system_instruction=system)
        # Gemini supports JSON mode; we ask for JSON and parse defensively.
        resp = model.generate_content(
            user + "\n\nReturn ONLY a single JSON object.",
            generation_config={"response_mime_type": "application/json", "temperature": 0},
        )
        return _extract_json(resp.text)


# --- OpenRouter -------------------------------------------------------------

class OpenRouterProvider(Provider):
    name = "openrouter"
    # Default to a small, local-class model (8B) — representative of what would actually run on-prem
    # as the "local LLM" in ApexClaw. Swap via POLICY_MODEL / --model for a larger one if desired.
    default_model = "meta-llama/llama-3.1-8b-instruct"

    def complete(self, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import requests
        except ImportError as e:
            raise ProviderError("pip install requests") from e
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ProviderError("OPENROUTER_API_KEY not set")
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\nReturn ONLY a single JSON object."},
            ],
        }
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=120,
        )
        if r.status_code >= 400:
            raise ProviderError(f"OpenRouter {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        return _extract_json(content)


# --- Anthropic (optional) ---------------------------------------------------

class AnthropicProvider(Provider):
    name = "anthropic"
    default_model = "claude-sonnet-4-6"

    def complete(self, system: str, user: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError("pip install anthropic") from e
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user + "\n\nReturn ONLY a single JSON object."}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _extract_json(text)
