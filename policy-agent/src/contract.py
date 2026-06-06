"""Data contracts shared across the Policy Agent.

These dataclasses define the inputs (DocumentBundle) and the output (Verdict) of the agent.
The Verdict mirrors agent/schema/verdict.schema.json — keep them in sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# --- Input side -------------------------------------------------------------

@dataclass
class DocumentBundle:
    """Format-flexible normalized view of whatever was attached to the prompt.

    Intake fills in whatever it can. The agent reasons over this uniform shape regardless of
    whether the source was a PDF, a controller backup folder, raw text, or just a filename.
    """
    filenames: List[str] = field(default_factory=list)
    file_types: List[str] = field(default_factory=list)
    extracted_text: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)        # structured key/values when available
    file_inventory: List[str] = field(default_factory=list)     # for folders: list of contained paths
    metadata: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)            # things that couldn't be read/extracted

    def excerpt(self, limit: int = 1500) -> str:
        text = self.extracted_text or ""
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]..."


# --- Output side ------------------------------------------------------------

VERDICTS = ("ALLOW", "BLOCK", "ESCALATE")
CASES = ("industrial_ot", "healthcare", "unknown")


@dataclass
class Finding:
    category: str          # phi|pii|secret|ip|credential|network|export_control|other
    location: str
    severity: str          # low|med|high
    rationale: str
    redaction_hint: str    # mask|drop|generalize


@dataclass
class Verdict:
    verdict: str                                   # ALLOW|BLOCK|ESCALATE
    matched_case: str                              # industrial_ot|healthcare|unknown
    classification_confidence: float
    explanation: str
    matched_policy_rules: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Verdict":
        raw_findings = d.get("findings") or []
        findings = [
            Finding(
                category=f.get("category", "other"),
                location=f.get("location", ""),
                severity=f.get("severity", "med"),
                rationale=f.get("rationale", ""),
                redaction_hint=f.get("redaction_hint", "mask"),
            )
            for f in raw_findings
        ]
        return Verdict(
            verdict=d.get("verdict", "ESCALATE"),
            matched_case=d.get("matched_case", "unknown"),
            classification_confidence=float(d.get("classification_confidence", 0.0)),
            explanation=d.get("explanation", ""),
            matched_policy_rules=list(d.get("matched_policy_rules") or []),
            findings=findings,
        )

    @staticmethod
    def escalate(reason: str, case: str = "unknown", confidence: float = 0.0) -> "Verdict":
        return Verdict(
            verdict="ESCALATE",
            matched_case=case,
            classification_confidence=confidence,
            explanation=reason,
        )
