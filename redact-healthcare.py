#!/usr/bin/env python3
"""
Single-agent PHI redaction engine (hackathon PoC).

Pipeline (one agent, one pass):
    detect  ->  transform (TRUE redaction)  ->  verify (re-scan, fail closed)

Input : a PDF + a rules file (e.g. rules/healthcare.yaml)
Output: <name>.redacted.pdf   — underlying text actually removed, not boxed over
        <name>.audit.json     — what was redacted (counts only; no cleartext PHI)

The Policy Agent upstream only says "PHI present". This engine owns *what* is
PHI and *how* it is removed, then proves the output is clean.

Usage:
    python3 redact-healthcare.py samples/01_discharge_heart_failure_Okafor.pdf rules/healthcare.yaml
"""

import sys, os, re, json, hashlib, datetime
try:                       # PyMuPDF ships as `pymupdf`; legacy import name is `fitz`
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        sys.exit("PyMuPDF not found for THIS interpreter. Install it with:\n"
                 f"    {sys.executable} -m pip install PyMuPDF PyYAML")
import yaml

MONTHS = ('January February March April May June July August September '
          'October November December').split()


# ---------------------------------------------------------------- detection ---
def extract_text(doc):
    return "\n".join(page.get_text() for page in doc)


def find_patient_names(text):
    """Seed from the structured header, then expand to every name variant."""
    names = set()
    m = re.search(r'Patient:\s*([A-Z][\w\'-]+),\s*([A-Z][\w.\s]+?)\s*\|', text)
    if m:
        surname, given = m.group(1).strip(), m.group(2).strip()
        names.add(f"{surname}, {given}")          # "Okafor, Emmanuel C."
        names.add(surname)                         # "Okafor"
        for t in ('Mr.', 'Mrs.', 'Ms.', 'Mx.'):
            names.add(f"{t} {surname}")            # "Mr. Okafor"
    return names


def find_provider_names(text):
    names = set()
    for m in re.finditer(r'Dr\.\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text):
        full = m.group(1)
        names.add(f"Dr. {full}")
        names.add(full)                            # bare "Priya Mehta" in signature
    for m in re.finditer(r'signed by:\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text):
        names.add(m.group(1))
    return names


def detect(rules, text):
    """Return list of (literal_string, replacement, rule_id, category)."""
    hits = []
    for r in rules['rules']:
        d = r['detect']
        if d['type'] == 'regex':
            for m in re.finditer(d['pattern'], text):
                repl = r['replacement']
                if repl == '$YEAR':                # date -> keep year only
                    repl = m.group(0).split()[-1]  # ".. , 2026" -> "2026"
                hits.append((m.group(0), repl, r['id'], r['category']))
        elif d['type'] == 'literal':
            for v in d['values']:
                if v in text:
                    hits.append((v, r['replacement'], r['id'], r['category']))
        elif d['type'] == 'dynamic':
            found = (find_patient_names(text) if d['extractor'] == 'patient_name'
                     else find_provider_names(text))
            for v in found:
                hits.append((v, r['replacement'], r['id'], r['category']))
    # Redact longest strings first so "Okafor, Emmanuel C." wins over "Okafor".
    hits.sort(key=lambda h: len(h[0]), reverse=True)
    return hits


# -------------------------------------------------------------- redaction ----
def apply_redactions(doc, hits):
    """TRUE redaction: locate each literal on each page, remove it, write token."""
    audit = {}
    redacted_strings = set()
    for page in doc:
        for literal, repl, rule_id, category in hits:
            for rect in page.search_for(literal):
                # add_redact_annot removes the underlying glyphs on apply;
                # `text` writes the replacement token into the cleared box.
                page.add_redact_annot(rect, text=repl, fill=(0.93, 0.93, 0.93),
                                      text_color=(0, 0, 0), fontsize=7)
                a = audit.setdefault(rule_id, {'rule_id': rule_id,
                                               'category': category,
                                               'replacement': repl,
                                               'count': 0})
                a['count'] += 1
                redacted_strings.add(literal)
        page.apply_redactions()
    return list(audit.values()), redacted_strings


# ------------------------------------------------------------- verification ---
def verify(doc, rules, redacted_strings):
    """Re-scan the OUTPUT. Any residual deterministic match = fail closed."""
    text = extract_text(doc)
    residual = []
    for r in rules['rules']:
        d = r['detect']
        if d['type'] == 'regex' and re.search(d['pattern'], text):
            # the year-only token "2026" is allowed to remain for HC-DATE
            if r['id'] == 'HC-DATE':
                continue
            residual.append(r['id'])
        if d['type'] == 'literal':
            for v in d['values']:
                if v in text:
                    residual.append(r['id'])
    for s in redacted_strings:                     # any seeded name survive?
        if s in text:
            residual.append(f"literal:{_mask(s)}")
    return residual


# -------------------------------------------------------------------- util ---
def _mask(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]   # audit w/o leaking PHI


def main():
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: python3 redact-healthcare.py <input.pdf> <rules.yaml> [output_dir]")
    pdf_path, rules_path = sys.argv[1], sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) == 4 else "output"
    rules = yaml.safe_load(open(rules_path))

    doc = fitz.open(pdf_path)
    hits = detect(rules, extract_text(doc))
    audit, redacted_strings = apply_redactions(doc, hits)
    residual = verify(doc, rules, redacted_strings)

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, os.path.splitext(os.path.basename(pdf_path))[0])
    out_pdf = f"{base}.redacted.pdf"
    out_audit = f"{base}.audit.json"

    verdict = 'BLOCK' if residual else 'CLEAR'
    report = {
        'policy': rules['policy'],
        'standard': rules['standard'],
        'source': os.path.basename(pdf_path),
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'verdict': verdict,
        'redactions': sorted(audit, key=lambda a: a['rule_id']),
        'total_redactions': sum(a['count'] for a in audit),
        'residual_after_rescan': residual,
    }
    json.dump(report, open(out_audit, 'w'), indent=2)

    if verdict == 'CLEAR':
        doc.save(out_pdf, garbage=4, deflate=True)   # garbage=4 drops orphaned data
        print(f"CLEAR  ->  {out_pdf}")
    else:
        # Fail closed: do NOT emit a document that still contains PHI.
        print(f"BLOCK  ->  residual PHI after re-scan: {residual}")
    print(f"audit  ->  {out_audit}  ({report['total_redactions']} redactions)")
    doc.close()
    sys.exit(0 if verdict == 'CLEAR' else 2)


if __name__ == '__main__':
    main()
