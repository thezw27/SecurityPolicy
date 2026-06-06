---
case_id: <snake_case_id>            # must match the id in registry.yaml
title: <human readable case title>
version: 1
owner: <team or person responsible for this policy>
# Routing signals also live in registry.yaml; repeat the most important here for readers.
routing_signals:
  filenames: ["<glob>", "..."]
  keywords: ["<word>", "..."]
  file_types: ["<ext or 'folder'>", "..."]
---

# <Case Title> Policy

> This document is the **ground truth** the Policy Agent applies for this case.
> The agent decides ALLOW / BLOCK / ESCALATE for sending prompt+data to a frontier cloud model.

## 1. Scope — what data this case covers
Describe the documents/artifacts this policy governs.

## 2. Proprietary categories (BLOCK when present)
List each rule with a stable ID. The agent cites these IDs in `matched_policy_rules`.

| Rule ID | Category | What to detect | Severity | Redaction hint |
|---------|----------|----------------|----------|----------------|
| XX-1    | <phi/pii/secret/ip/credential/network/export_control> | <concrete signal> | <low/med/high> | <mask/drop/generalize> |

## 3. ALLOW examples (no proprietary data → send to cloud)
- <example prompt+data combination that is safe>

## 4. BLOCK examples (proprietary present → hand to data massager)
- <example prompt+data combination that must not leave>

## 5. ESCALATE conditions (uncertain → human review)
- Cannot confidently determine whether data falls under this case.
- Data appears to mix this case with another (multi-domain).
- Required content could not be extracted (e.g. encrypted/binary blob) so presence of proprietary data cannot be ruled out.

## 6. Notes for the data massager (out of scope, courtesy)
Guidance on how the BLOCKed items should be neutralized so a re-run can pass.
