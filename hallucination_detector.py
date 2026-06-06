from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable


PROJECT_ONE_LINER = (
    "A policy agent determines whether prompt/reference data contains private information "
    "and redacts it before any escalation to a cloud frontier agent."
)

ROBOTICS_PROMPT_HINTS = {
    "robot",
    "robotics",
    "controller",
    "error",
    "errors",
    "log",
    "fault",
    "alarm",
    "abb",
    "flexpendant",
}

HEALTHCARE_PROMPT_HINTS = {
    "healthcare",
    "follow up",
    "follow-up",
    "followups",
    "follows up",
    "follows ups",
    "follow ups",
    "appointment",
    "patient",
    "clinic",
    "medical",
}

ERROR_CODE_PATTERN = re.compile(r"\b\d{5,6}\b")
ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
SLASH_DATE_PATTERN = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
LONG_DATE_PATTERN = re.compile(
    r"\b("
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)(\d{4})\b",
    re.IGNORECASE,
)


@dataclass
class DetectionResult:
    project: str
    domain: str
    hallucination: bool
    reason: str | None
    details: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def load_valid_error_codes(file_path: str | Path) -> set[str]:
    path = Path(file_path)
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def classify_prompt(prompt: str) -> str:
    lowered = prompt.lower()
    if any(token in lowered for token in ROBOTICS_PROMPT_HINTS):
        return "robotics"
    if any(token in lowered for token in HEALTHCARE_PROMPT_HINTS):
        return "healthcare_followup"
    return "unsupported"


def extract_error_codes(text: str) -> list[str]:
    seen = []
    for code in ERROR_CODE_PATTERN.findall(text):
        if code not in seen:
            seen.append(code)
    return seen


def validate_robotics_response(prompt: str, llm_output: str, valid_codes: Iterable[str]) -> DetectionResult:
    extracted = extract_error_codes(llm_output)
    valid_set = set(valid_codes)
    invalid = [code for code in extracted if code not in valid_set]
    return DetectionResult(
        project=PROJECT_ONE_LINER,
        domain="robotics",
        hallucination=bool(invalid),
        reason="invalid_error_code" if invalid else None,
        details={
            "prompt": prompt,
            "extracted_error_codes": extracted,
            "invalid_error_codes": invalid,
            "valid_error_code_count": len(valid_set),
        },
    )


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_followup_date(text: str) -> tuple[str | None, date | None]:
    match = ISO_DATE_PATTERN.search(text)
    if match:
        year, month, day = map(int, match.groups())
        parsed = _safe_date(year, month, day)
        return match.group(0), parsed

    match = LONG_DATE_PATTERN.search(text)
    if match:
        month_name, day, year = match.groups()
        try:
            parsed_dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
            return match.group(0), parsed_dt.date()
        except ValueError:
            return match.group(0), None

    match = SLASH_DATE_PATTERN.search(text)
    if match:
        month, day, year = map(int, match.groups())
        parsed = _safe_date(year, month, day)
        return match.group(0), parsed

    return None, None


def validate_healthcare_response(prompt: str, llm_output: str, today: date | None = None) -> DetectionResult:
    today = today or date.today()
    raw_date, parsed_date = extract_followup_date(llm_output)

    if raw_date is None:
        return DetectionResult(
            project=PROJECT_ONE_LINER,
            domain="healthcare_followup",
            hallucination=True,
            reason="missing_followup_date",
            details={"prompt": prompt, "extracted_date": None, "today": today.isoformat()},
        )

    if parsed_date is None:
        return DetectionResult(
            project=PROJECT_ONE_LINER,
            domain="healthcare_followup",
            hallucination=True,
            reason="invalid_date",
            details={"prompt": prompt, "extracted_date": raw_date, "today": today.isoformat()},
        )

    if parsed_date <= today:
        return DetectionResult(
            project=PROJECT_ONE_LINER,
            domain="healthcare_followup",
            hallucination=True,
            reason="date_not_in_future",
            details={
                "prompt": prompt,
                "extracted_date": raw_date,
                "normalized_date": parsed_date.isoformat(),
                "today": today.isoformat(),
            },
        )

    return DetectionResult(
        project=PROJECT_ONE_LINER,
        domain="healthcare_followup",
        hallucination=False,
        reason=None,
        details={
            "prompt": prompt,
            "extracted_date": raw_date,
            "normalized_date": parsed_date.isoformat(),
            "today": today.isoformat(),
        },
    )


def detect_hallucination(
    prompt: str,
    llm_output: str,
    error_code_file: str | Path = "/workspace/error_codes_from_manual.txt",
    today: date | None = None,
) -> DetectionResult:
    domain = classify_prompt(prompt)
    if domain == "robotics":
        valid_codes = load_valid_error_codes(error_code_file)
        return validate_robotics_response(prompt, llm_output, valid_codes)
    if domain == "healthcare_followup":
        return validate_healthcare_response(prompt, llm_output, today=today)
    return DetectionResult(
        project=PROJECT_ONE_LINER,
        domain="unsupported",
        hallucination=False,
        reason="no_rule_for_domain",
        details={"prompt": prompt},
    )


if __name__ == "__main__":
    demo_cases = [
        (
            "check errors on log and steps to resolve",
            "The main issues are error 10030 and error 99999. Inspect the mains connection and transformer wiring.",
        ),
        (
            "Find any follows ups",
            "Recommended follow-up date: 2026-07-15.",
        ),
    ]
    for prompt, response in demo_cases:
        print(detect_hallucination(prompt, response).to_json())
