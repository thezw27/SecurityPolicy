import argparse
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from docx import Document
from pypdf import PdfReader


SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".csv", ".xlsx", ".json"}
DEFAULT_MAX_FILES_FOR_SELECTION = 300
DEFAULT_MAX_FILES_TO_DOWNLOAD = 12
DEFAULT_MAX_CHARS_PER_FILE = 20000
DEFAULT_MAX_TOTAL_CHARS = 120000


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class BoxEntry:
    id: str
    name: str
    type: str
    path: str
    extension: str
    size: int | None
    modified_at: str | None


class OpenRouterClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str) -> str:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "Box OpenRouter Pipeline",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"]

    def complete_json(self, prompt: str) -> dict[str, Any]:
        text = self.complete(prompt)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenRouter returned non-JSON output: {text}") from exc


class BoxClient:
    def __init__(self, access_token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.get(url, timeout=120, **kwargs)
        response.raise_for_status()
        return response

    def list_files_recursive(self, root_folder_id: str) -> list[BoxEntry]:
        results: list[BoxEntry] = []
        stack: list[tuple[str, str]] = [(root_folder_id, "")]
        while stack:
            folder_id, folder_path = stack.pop()
            offset = 0
            while True:
                response = self._get(
                    f"https://api.box.com/2.0/folders/{folder_id}/items",
                    params={
                        "limit": 1000,
                        "offset": offset,
                        "fields": "id,name,type,path_collection,size,modified_at",
                    },
                )
                payload = response.json()
                entries = payload.get("entries", [])
                for item in entries:
                    item_type = item.get("type")
                    item_id = item.get("id")
                    item_name = item.get("name")
                    if not item_type or not item_id or not item_name:
                        continue
                    current_path = f"{folder_path}/{item_name}" if folder_path else f"/{item_name}"
                    if item_type == "folder":
                        stack.append((item_id, current_path))
                        continue
                    ext = Path(item_name).suffix.lower()
                    results.append(
                        BoxEntry(
                            id=item_id,
                            name=item_name,
                            type=item_type,
                            path=current_path,
                            extension=ext,
                            size=item.get("size"),
                            modified_at=item.get("modified_at"),
                        )
                    )
                offset += len(entries)
                if offset >= payload.get("total_count", 0) or not entries:
                    break
        return results

    def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        response = self._get(
            f"https://api.box.com/2.0/files/{file_id}",
            params={"fields": "id,name,size,download_url,path_collection,modified_at"},
        )
        return response.json()

    def get_download_url(self, file_id: str) -> str:
        metadata = self.get_file_metadata(file_id)
        url = metadata.get("download_url")
        if not url:
            raise RuntimeError(f"No download_url available for file {file_id}")
        return url

    def download_file_bytes(self, file_id: str) -> bytes:
        download_url = self.get_download_url(file_id)
        response = requests.get(download_url, timeout=120)
        response.raise_for_status()
        return response.content


class BoxOpenRouterPipeline:
    def __init__(
        self,
        box_client: BoxClient,
        llm_client: OpenRouterClient,
        max_files_for_selection: int = DEFAULT_MAX_FILES_FOR_SELECTION,
        max_files_to_download: int = DEFAULT_MAX_FILES_TO_DOWNLOAD,
        max_chars_per_file: int = DEFAULT_MAX_CHARS_PER_FILE,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        self.box_client = box_client
        self.llm_client = llm_client
        self.max_files_for_selection = max_files_for_selection
        self.max_files_to_download = max_files_to_download
        self.max_chars_per_file = max_chars_per_file
        self.max_total_chars = max_total_chars

    def choose_files(self, prompt: str, candidates: list[BoxEntry]) -> dict[str, Any]:
        catalog = [
            {
                "id": entry.id,
                "path": entry.path,
                "name": entry.name,
                "extension": entry.extension,
                "size": entry.size,
                "modified_at": entry.modified_at,
            }
            for entry in candidates[: self.max_files_for_selection]
        ]
        planner_prompt = (
            "You are a retrieval planner. Given a user request and a Box file catalog, choose the files most relevant to answering the request. "
            "Return strict JSON with keys: selected_file_ids (array of strings), rationale (string), maybe_relevant_file_ids (array of strings). "
            f"Select no more than {self.max_files_to_download} files.\n\n"
            f"USER REQUEST:\n{prompt}\n\n"
            f"FILE CATALOG:\n{json.dumps(catalog, indent=2)}"
        )
        return self.llm_client.complete_json(planner_prompt)

    def download_text(self, entry: BoxEntry) -> str:
        data = self.box_client.download_file_bytes(entry.id)
        if entry.extension in {".txt", ".md", ".json"}:
            return data.decode("utf-8", errors="replace")
        if entry.extension == ".pdf":
            reader = PdfReader(io.BytesIO(data))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        if entry.extension == ".docx":
            document = Document(io.BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        if entry.extension == ".csv":
            frame = pd.read_csv(io.BytesIO(data))
            return frame.to_csv(index=False)
        if entry.extension == ".xlsx":
            workbook = pd.read_excel(io.BytesIO(data), sheet_name=None)
            parts = []
            for sheet_name, frame in workbook.items():
                parts.append(f"## Sheet: {sheet_name}\n{frame.to_csv(index=False)}")
            return "\n\n".join(parts)
        raise ValueError(f"Unsupported extension after filtering: {entry.extension}")

    def run(self, root_folder_id: str, prompt: str) -> dict[str, Any]:
        candidates = [
            entry
            for entry in self.box_client.list_files_recursive(root_folder_id)
            if entry.extension in SUPPORTED_EXTENSIONS
        ]
        if not candidates:
            raise RuntimeError("No supported files found in the Box folder tree.")

        selection = self.choose_files(prompt=prompt, candidates=candidates)
        selected_ids = selection.get("selected_file_ids", [])[: self.max_files_to_download]
        selected_lookup = {entry.id: entry for entry in candidates if entry.id in selected_ids}

        documents = []
        total_chars = 0
        for file_id in selected_ids:
            entry = selected_lookup.get(file_id)
            if not entry:
                continue
            text = self.download_text(entry)[: self.max_chars_per_file]
            remaining = self.max_total_chars - total_chars
            if remaining <= 0:
                break
            text = text[:remaining]
            total_chars += len(text)
            documents.append({"id": entry.id, "path": entry.path, "content": text})

        final_prompt = (
            "Answer the user request using only the provided Box file contents when possible. "
            "If the files are insufficient, say what is missing.\n\n"
            f"USER REQUEST:\n{prompt}\n\n"
            f"SELECTED FILES AND CONTENTS:\n{json.dumps(documents, indent=2)}"
        )
        answer = self.llm_client.complete(final_prompt)
        return {
            "prompt": prompt,
            "candidate_count": len(candidates),
            "selection": selection,
            "files_used": [{"id": doc["id"], "path": doc["path"]} for doc in documents],
            "answer": answer,
        }

    def answer_file_direct(self, file_id: str, prompt: str) -> dict[str, Any]:
        metadata = self.box_client.get_file_metadata(file_id)
        entry = BoxEntry(
            id=metadata["id"],
            name=metadata["name"],
            type="file",
            path="/".join(node["name"] for node in metadata.get("path_collection", {}).get("entries", [])) + f"/{metadata['name']}",
            extension=Path(metadata["name"]).suffix.lower(),
            size=metadata.get("size"),
            modified_at=metadata.get("modified_at"),
        )
        text = self.download_text(entry)
        final_prompt = (
            "Use the provided file content to answer the user request.\n\n"
            f"USER REQUEST:\n{prompt}\n\n"
            f"FILE PATH: {entry.path}\n"
            f"FILE CONTENT:\n{text[: self.max_total_chars]}"
        )
        return {
            "prompt": prompt,
            "files_used": [{"id": entry.id, "path": entry.path}],
            "answer": self.llm_client.complete(final_prompt),
        }


def build_clients() -> tuple[BoxClient, OpenRouterClient, str]:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_path)
    root_folder_id = os.environ.get("BOX_ROOT_FOLDER_ID")
    box_access_token = os.environ.get("BOX_ACCESS_TOKEN")
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")
    openrouter_model = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    missing = [
        name
        for name, value in [
            ("BOX_ROOT_FOLDER_ID", root_folder_id),
            ("BOX_ACCESS_TOKEN", box_access_token),
            ("OPENROUTER_API_KEY", openrouter_api_key),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return BoxClient(access_token=box_access_token), OpenRouterClient(api_key=openrouter_api_key, model=openrouter_model), root_folder_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-file Box retriever + OpenRouter answer pipeline.")
    parser.add_argument("prompt", help="Prompt to send into the pipeline.")
    parser.add_argument("--root-folder-id", default=None, help="Override BOX_ROOT_FOLDER_ID from .env.")
    parser.add_argument("--file-id", default=None, help="Directly answer using one Box file ID instead of retrieval.")
    parser.add_argument("--json", action="store_true", help="Print full JSON result.")
    args = parser.parse_args()

    box_client, llm_client, env_root_folder_id = build_clients()
    pipeline = BoxOpenRouterPipeline(box_client=box_client, llm_client=llm_client)

    if args.file_id:
        result = pipeline.answer_file_direct(file_id=args.file_id, prompt=args.prompt)
    else:
        result = pipeline.run(root_folder_id=args.root_folder_id or env_root_folder_id, prompt=args.prompt)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["answer"] or "")
        print("\nFiles used:")
        for item in result.get("files_used", []):
            print(f"- {item['path']} ({item['id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

