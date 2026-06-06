---
case_id: healthcare
title: Healthcare — clinical documents containing PHI
version: 1
owner: Policy Agent team
routing_signals:
  filenames: ["*discharge*", "*summary*", "*patient*"]
  keywords: ["patient", "discharge", "diagnosis", "MRN", "medical record", "attending", "admitted", "follow-up", "prescription", "clinical"]
  file_types: ["pdf", "docx", "txt"]
---

# Healthcare / PHI Policy

> Ground truth for prompts that attach a clinical document (e.g. a hospital discharge summary).
> Typical ask: *"find any follow-ups."* The **clinical question is benign — the patient
> identifiers in the document are the risk** (HIPAA-regulated PHI must not reach a cloud model).

## 1. Scope — what data this case covers
Patient-level clinical documents: discharge summaries, visit/progress notes, lab/imaging reports,
referral letters, prescriptions. Both the structured header (patient demographics) and the
free-text narrative are in scope.

## 2. Proprietary categories (BLOCK when present)
Detect the **18 HIPAA identifiers**. The presence of *any* identifier tied to clinical content is a BLOCK.

| Rule ID | Category | What to detect | Severity | Redaction hint |
|---------|----------|----------------|----------|----------------|
| HC-1 | phi | Patient name (incl. in title/header/footer) | high | mask |
| HC-2 | phi | Medical record number (MRN), account/encounter #, health-plan/beneficiary #, SSN | high | mask |
| HC-3 | phi | Dates tied to the individual: DOB, admission, discharge, death, procedure dates | high | generalize |
| HC-4 | phi | Ages over 89 / dates implying age over 89 | med | generalize |
| HC-5 | phi | Geographic detail smaller than state (street, city, ZIP, facility name/address) | med | generalize |
| HC-6 | phi | Contact identifiers: phone, fax, email, URLs, IP | med | mask |
| HC-7 | phi | Provider names that identify the patient's care (attending, surgeon) when combined with above | med | mask |
| HC-8 | phi | Other unique IDs: device serials, biometric IDs, certificate/license numbers, vehicle/plate, photos | med | drop |

## 3. ALLOW examples (no proprietary data → send to cloud)
- "For a patient with HFrEF (EF 28%) discharged on torsemide, what follow-ups are typical?" — **de-identified**, no name/MRN/dates/facility.
- A clinical question about guidelines or drug interactions with no patient-specific identifiers attached.

## 4. BLOCK examples (proprietary present → hand to data massager)
- "Attached is a patient discharge summary, find any follow-ups" **with the PDF attached** → header carries HC-1 name ("Okafor, Emmanuel C."), HC-2 MRN ("NL-448821"), HC-3 DOB/admit/discharge dates, HC-5 facility ("Northlake Regional Medical Center"), HC-7 attending ("Dr. Priya Mehta") — all BLOCK.
- Any clinical narrative that names the patient or references their MRN/specific dates.

## 5. ESCALATE conditions (uncertain → human review)
- Document is a scanned image / PDF with no extractable text layer — cannot confirm de-identification, so **do not assume safe**.
- Ambiguous whether a name refers to a patient vs. a public author/guideline.
- Classifier confidence below the registry threshold.

## 5b. Already-redacted data (→ ALLOW)
This document may be a *re-check* of output from the downstream redactor
(`redact-healthcare.py` + `rules/healthcare.yaml`). That engine replaces PHI with these
placeholder tokens — **their presence means the field was already removed, so they are NOT findings**:

| Token | Was | Rule |
|-------|-----|------|
| `[PATIENT]` | patient name (all variants) | HC-PATIENT |
| `[MRN]` | medical record number | HC-MRN |
| `[DOB]` | date of birth | HC-DOB |
| `[PROVIDER]` | care-provider name | HC-PROVIDER |
| `[FACILITY]` | hospital/facility name | HC-FACILITY |
| bare year only (e.g. `2026`) | a full calendar date, generalized | HC-DATE |

If all identifiers in the document are placeholders/absent and only clinical content + retained
items (age < 90, bare years, relative intervals) remain, the verdict is **ALLOW** — the redactor did
its job and the document may go to the cloud.

## 6. Notes for the data massager (out of scope, courtesy)
- `mask` names/MRN/contact to stable tokens (so the model can still refer to "the patient").
- `generalize` dates to relative offsets (e.g. "hospital day 3") or month-only; ages over 89 → "90+".
- The clinical content (diagnoses, meds, course) is **not** PHI on its own and should be preserved so follow-ups can still be extracted after massaging.
