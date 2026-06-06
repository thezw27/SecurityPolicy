"""Format-flexible intake: turn whatever was attached into a uniform DocumentBundle.

Accepts any of:
  - a dict that's already a partial bundle (pre-extracted text/fields/metadata)
  - a path to a PDF            -> extract text (pypdf, optional)
  - a path to a folder         -> walk it (e.g. ABB RobotStudio "...restore" backup)
  - a path to a text-ish file  -> read it
  - a path to an opaque/binary -> record as present-but-unreadable (so we can ESCALATE, not assume safe)
  - raw text passed inline

The goal is never to silently lose sensitive content: anything we cannot read is recorded in
`missing` so the evaluator downgrades to ESCALATE rather than ALLOW.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from contract import DocumentBundle

TEXT_EXT = {".txt", ".md", ".xml", ".cfg", ".sys", ".mod", ".json", ".csv", ".log", ".ini", ".rsf", ".cmd", ".id"}
OPAQUE_EXT = {".bin", ".db", ".dll", ".guid", ".png", ".jpg", ".jpeg", ".gz", ".zip"}
MAX_TEXT = 60_000          # cap total extracted text fed to the model
MAX_FILE = 40_000          # cap per-file read


def build_bundle(source: Any, prompt: str = "") -> DocumentBundle:
    if source is None:
        return DocumentBundle()
    if isinstance(source, DocumentBundle):
        return source
    if isinstance(source, dict):
        return _from_dict(source)
    if isinstance(source, str):
        if os.path.isdir(source):
            return _from_folder(source)
        if os.path.isfile(source):
            return _from_file(source)
        # treat as inline raw text
        return DocumentBundle(filenames=["<inline>"], file_types=["text"], extracted_text=source[:MAX_TEXT])
    raise TypeError(f"Unsupported intake source: {type(source)}")


def _from_dict(d: Dict[str, Any]) -> DocumentBundle:
    return DocumentBundle(
        filenames=d.get("filenames", []),
        file_types=d.get("file_types", []),
        extracted_text=(d.get("extracted_text", "") or "")[:MAX_TEXT],
        fields=d.get("fields", {}) or {},
        file_inventory=d.get("file_inventory", []) or [],
        metadata=d.get("metadata", {}) or {},
        missing=d.get("missing", []) or [],
    )


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(MAX_FILE)
    except (OSError, UnicodeError):
        return None


def _extract_pdf(path: str) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_TEXT]
    except Exception:
        return None


def _from_file(path: str) -> DocumentBundle:
    name = os.path.basename(path)
    ext = _ext(path)
    b = DocumentBundle(filenames=[name], file_types=[ext.lstrip(".") or "unknown"])
    if ext == ".pdf":
        text = _extract_pdf(path)
        if text and text.strip():
            b.extracted_text = text
        else:
            b.missing.append(f"{name}: PDF text not extractable (scanned image or pypdf missing)")
    elif ext in TEXT_EXT or ext == "":
        text = _read_text(path)
        if text is not None:
            b.extracted_text = text
        else:
            b.missing.append(f"{name}: unreadable")
    else:
        b.missing.append(f"{name}: opaque/binary ({ext or 'no ext'}) — contents not inspected")
    return b


def _from_folder(root: str) -> DocumentBundle:
    """Walk a controller-backup-style folder. Extract text from text files, flag binaries/opaque
    files as present-but-uninspected, and keep an inventory so the policy can reason by filename too."""
    b = DocumentBundle(filenames=[os.path.basename(root.rstrip("/\\"))], file_types=["folder"])
    parts = []
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            b.file_inventory.append(rel)
            ext = _ext(fn)
            if ext in OPAQUE_EXT:
                b.missing.append(f"{rel}: opaque/binary — contents not inspected")
                continue
            if ext in TEXT_EXT or ext == "":
                if total >= MAX_TEXT:
                    continue
                text = _read_text(full)
                if text is None:
                    b.missing.append(f"{rel}: unreadable")
                    continue
                snippet = text[: max(0, MAX_TEXT - total)]
                parts.append(f"\n===== {rel} =====\n{snippet}")
                total += len(snippet)
            else:
                b.missing.append(f"{rel}: not inspected ({ext})")
    b.extracted_text = "".join(parts)
    b.metadata["file_count"] = len(b.file_inventory)
    return b
