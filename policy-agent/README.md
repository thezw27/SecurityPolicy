# Policy Agent

A local, model-agnostic **data-loss-prevention gate** for the AgentCOP flow. It sits between an
on-prem/local model and a frontier **cloud** model and decides whether a prompt + its attached
documents may be sent to the cloud, based on a per-use-case **policy** that defines what counts as
proprietary.

## Where this fits in the full flow

```
User prompt + Box docs (via MCP)
  -> local model draft answer
  -> hallucination check
        NO  -> return local answer (STOP)
        YES -> ┌──────────── THIS PROJECT ────────────┐
               │ POLICY AGENT                          │
               │  intake -> classify -> apply policy   │
               │  verdict:                             │
               │    ALLOW    -> send to cloud (STOP)   │
               │    BLOCK    -> data massager ─────────┼─> (out of scope) -> loops back
               │    ESCALATE -> human review           │
               └────────────────────────────────────────┘
```

**Scope:** from the hallucination check returning YES, through the policy decision, up to handing a
`BLOCK` verdict (with findings) to the downstream data massager. The massager, redaction, and the
re-evaluation loop are **out of scope** — but the agent emits the contract they consume and is
safely re-runnable.

## How it works (two stages)

1. **Classify** (`agent/classifier_prompt.md`) — route the prompt+data to a use-case using the
   signals in `policies/registry.yaml`. Low confidence / unknown / mixed-domain → `ESCALATE`.
2. **Evaluate** (`agent/evaluator_prompt.md`) — apply that case's `policy.md` and return a
   structured **3-state verdict** (`agent/schema/verdict.schema.json`):
   - `ALLOW` — no proprietary data → forward to cloud.
   - `BLOCK` — proprietary found → `findings[]` (category, location, severity, redaction_hint) for the massager.
   - `ESCALATE` — can't confirm safety (opaque/binary, missing text, uncertain) → human review. **Never guesses ALLOW.**

## Cases (extensible)

| case | covers | example ask |
|------|--------|-------------|
| `industrial_ot` | robot/PLC controller backups, configs, logs (e.g. ABB RobotStudio `...restore`) | "check the errors on the log and steps to resolve" |
| `healthcare` | clinical documents with PHI (discharge summaries) | "find any follow-ups" |

Add a case: copy `policies/_template/policy.md`, fill it in, and register it in
`policies/registry.yaml`. No code changes. (The dropped simulation/CFD case can be re-added this way.)

## Models

Model-agnostic provider layer (`src/providers.py`): `gemini`, `openrouter`, `anthropic` (optional),
and `mock` (offline). Put your keys in a `.env` file (auto-loaded; gitignored):

```
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
# then edit .env:
POLICY_PROVIDER=gemini            # or openrouter | anthropic | mock
POLICY_MODEL=gemini-2.0-flash     # optional
GEMINI_API_KEY=...                # or OPENROUTER_API_KEY=...
```

(Shell env vars also work if you prefer not to use `.env`.)

## Run

```bash
pip install -r requirements.txt

# offline smoke (no keys) — proves the wiring end to end
python harness/run_validation.py --provider mock

# real validation
set GEMINI_API_KEY=...      &&  python harness/run_validation.py --provider gemini
set OPENROUTER_API_KEY=...  &&  python harness/run_validation.py --provider openrouter --model google/gemini-2.0-flash-001

# one-off
python src/agent.py --prompt "Attached is a discharge summary, find follow-ups" --source path/to/file.pdf --provider gemini

# real local data (gitignored paths in harness/test_cases.yaml -> real_cases)
python harness/run_validation.py --provider gemini --real

# write each verdict to a file you can inspect
python harness/run_validation.py --provider openrouter --real --save
```

## Generated verdict files (`--save`)

With `--save`, each run writes the full verdict JSON per input:

- `examples/verdicts/*.verdict.json` — from the **synthetic** fixtures; safe and **committed** so anyone can see sample outputs.
- `local_data/verdicts/*.verdict.json` — from the **real** `--real` data; **gitignored**, never committed.

Each file includes the `_provider`/`_model` used plus the verdict, matched case, fired policy rules, and findings.

## Default model

The default OpenRouter model is `meta-llama/llama-3.1-8b-instruct` — a small, **local-class** (8B)
model representative of what would run on-prem as the AgentCOP "local LLM". It is reliable on the
safety-critical BLOCK cases; for higher accuracy on nuanced ALLOW/ESCALATE calls, point
`POLICY_MODEL` at a larger model (e.g. `google/gemini-2.5-flash`). A model-independent guard in
`src/agent.py` deterministically ESCALATEs any attachment whose contents can't be extracted, so the
unreadable-data case never depends on model strength.

## Leak guard

Real controller backups and patient PDFs are **never committed** (see `.gitignore`). The repo ships
only synthetic fixtures in `harness/fixtures/`. Real files are read locally for `--real` validation.

## End-to-end integration (`../orchestrator.py`)

The repo-root `orchestrator.py` wires all four AgentCOP components into one flow:

```
box_openrouter_pipeline (prompt + Box -> local LLM draft)
  -> hallucination_detector (trustworthy? no -> STOP)
  -> policy-agent           (ALLOW -> cloud | ESCALATE -> human | BLOCK -> massager)
  -> redact-healthcare.py   (Emma's PHI redactor, fail-closed)
  -> re-feed redacted doc back into policy-agent  (loop until ALLOW / ESCALATE)
```

Run it on a local doc (Box + hallucination stages are optional, since they need live creds):

```bash
python ../orchestrator.py --provider openrouter --model google/gemini-2.5-flash \
  --prompt "Attached is a patient discharge summary, find any follow-ups." \
  --doc data/healthcare/01_discharge_heart_failure_Okafor.pdf
# loop 1 BLOCK (PHI) -> redactor (CLEAR) -> loop 2 ALLOW -> SEND_TO_CLOUD (redacted doc)
```

**Two integration contracts that make the loop converge:**
1. **Redaction-token awareness.** The massager replaces PHI with placeholder tokens
   (`[PATIENT]`, `[MRN]`, `[DOB]`, `[PROVIDER]`, `[FACILITY]`, year-only dates). The evaluator prompt
   and healthcare policy treat those tokens as *already neutralized* (not findings) — otherwise the
   policy agent would re-flag them forever and the loop would never end.
2. **Massager coverage.** A massager exists only for `healthcare` PDFs today. `industrial_ot` BLOCK
   currently routes to **human review** (no massager yet) — handled gracefully by the orchestrator.

**Model note:** the *post-redaction re-check* needs a capable evaluator. The small local-class
default (`llama-3.1-8b`) is reliable for the initial BLOCK but over-flags cleaned docs (confabulates
PHI on empty fields), so the loop only converges with a stronger model (e.g. `google/gemini-2.5-flash`).
Pass it via `--model`.

## Layout

```
policies/      registry.yaml + per-case policy.md (the ground truth)
agent/         classifier & evaluator prompts + verdict JSON schema
src/           contract.py, providers.py, intake.py, agent.py
harness/       fixtures/, test_cases.yaml, run_validation.py
```
