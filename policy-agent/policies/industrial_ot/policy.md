---
case_id: industrial_ot
title: Industrial / OT — robot & PLC controller backups, configs, logs
version: 1
owner: Policy Agent team
routing_signals:
  filenames: ["*.mod", "*.sys", "*.cfg", "netconfig.db", "system.xml", "RW6system.xml", "uas_application_grants.xml", "*restore*"]
  keywords: ["controller", "backup", "RAPID", "PLC", "robot", "ABB", "IRB", "error log", "elog", "teach pendant"]
  file_types: ["folder", "xml", "cfg", "txt", "mod", "sys", "db"]
---

# Industrial / OT Controller Policy

> Ground truth for prompts that attach an industrial controller backup, configuration, or log
> (e.g. an ABB RobotStudio `...restore` folder). Typical ask: *"check the errors on the log and
> steps to resolve the issues."* The **question is usually benign — the attached backup is the risk.**

## 1. Scope — what data this case covers
Robot/PLC controller backups and their contents:
- Full RobotStudio / RobotWare backup folders (`...restore`) and their subtrees: `HOME/`, `INTERNAL/`, `SYSPAR/`, `PRODUCTS/`.
- RAPID program modules (`*.mod`, `*.sys`), config files (`*.cfg`, `SIO.cfg`), system descriptors (`system.xml`, `RW6system.xml`, `fpsystem.xml`).
- Network/registry databases (`netconfig.db`, `Registry.db`), licensing/identity (`key.id`, `INTERNAL/license`, `system.guid`), authorization (`uas_application_grants.xml`).
- Event/error logs (`INTERNAL/log`, `INTERNAL/REPORTS`, `error_reporter.mod`).

## 2. Proprietary categories (BLOCK when present)

| Rule ID | Category | What to detect | Severity | Redaction hint |
|---------|----------|----------------|----------|----------------|
| OT-1 | network | IP addresses, subnets, gateways, MAC, hostnames, PROFINET/EtherNet-IP device addresses, network topology — esp. from `netconfig.db`, `ippnio.xml`, `*.cfg` EIO/SIO sections | high | mask |
| OT-2 | credential | Usernames, passwords, password hashes, API keys, UAS grants, certificates — esp. `uas_application_grants.xml`, user/grant entries in config | high | drop |
| OT-3 | secret | License keys, controller serial/`key.id`, `system.guid`, `template.guid`, activation/option codes (`option.cmd`, `opt_l0.cmd`) | high | mask |
| OT-4 | ip | Proprietary RAPID programs and motion logic (`HOME/*.mod`, `*.sys`), end-effector trajectories / taught positions (`eef_pos_roll_*.txt`), tool/workobject calibration, process recipes & tuning parameters in `SYSPAR` | high | generalize |
| OT-5 | pii | Operator/engineer names, emails, employee IDs embedded in logs, comments, or program headers | med | mask |
| OT-6 | secret | Site/plant/line/cell identifiers, customer or program names that reveal the deployment | med | generalize |

## 3. ALLOW examples (no proprietary data → send to cloud)
- "What does ABB error code **50204** mean and how do I clear it?" — generic, no backup attached.
- A single **public** error string pasted inline with no IPs, credentials, names, or proprietary program code.
- A question about RAPID *syntax* using a generic illustrative snippet that contains no real positions, addresses, or identifiers.

## 4. BLOCK examples (proprietary present → hand to data massager)
- "Attached is an ABB controller backup from Box, check the errors on the log and steps to resolve the issues" **with the `...restore` folder attached** → the backup contains OT-1 network config (`netconfig.db`), OT-2 UAS grants, OT-3 keys/`system.guid`, and OT-4 RAPID programs + `eef_pos_roll_*.txt` trajectories.
- An error log excerpt that includes the controller's IP, hostname, or an operator's name (OT-1/OT-5).
- A pasted `*.mod` motion program with real taught coordinates (OT-4).

## 5. ESCALATE conditions (uncertain → human review)
- The attachment is largely opaque binary (`image.bin`, `ctrl.bin`, `*.db`) and text extraction can't confirm whether proprietary data is inside — **do not assume safe**.
- Prompt mixes OT content with another domain (e.g. a controller log that also names patients).
- Classifier confidence below the registry threshold.

## 6. Notes for the data massager (out of scope, courtesy)
- `mask` network/IP/host to placeholders; `drop` credentials and UAS grants entirely.
- `generalize` RAPID coordinates/trajectories (e.g. strip numeric positions, keep structure) so the model can still reason about logic/errors.
- After massaging, the agent re-runs; a clean pass returns ALLOW.
