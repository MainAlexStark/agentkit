"""Файлы и картинки в сообщениях.

Файл кладётся в рабочую папку сессии (uploads/), в истории хранится только
ссылка — часть сообщения {"type": "file", "path": …}. Перед запросом к модели
ссылка разворачивается: картинка — в image_url (если модель видит
изображения), текст и извлечённый текст PDF/DOCX — в текстовую часть,
длинное — пометкой «читай read_file». Base64 в базе не хранится: история
оставалась бы лёгкой, а файл — доступным агенту инструментами.

Текст из PDF/DOCX извлекается один раз при загрузке в соседний файл
<имя>.txt — чтобы не разбирать документ заново на каждом шаге цикла.
"""

from __future__ import annotations

import base64
import fnmatch
import io
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

UPLOADS = "uploads"
INLINE_TEXT_LIMIT = 12_000
MODEL_IMAGE_SIDE = 1600  # больше модели всё равно не используют — лишние токены и трафик
MAX_IMAGE_BYTES = 4_500_000

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts",
            ".go", ".sql", ".log", ".ini", ".toml", ".cfg", ".sh", ".css"}

# Модели, которым можно отдавать картинки. Шаблоны по «провайдер:модель»; переопределяются в настройках.
DEFAULT_VISION = ["*claude*", "*gpt-4o*", "*gpt-4.1*", "*gpt-5*", "*o3*", "*o4*", "*gemini*", "*-vl*", "*vl-*",
                  "*vision*", "*pixtral*", "*llama-4*", "*qwen*vl*", "*gemma-3*", "fake:*"]


def is_vision(model: str, patterns: list[str] | None = None) -> bool:
    m = model.lower()
    return any(fnmatch.fnmatchcase(m, p.lower()) for p in (patterns or DEFAULT_VISION))


def kind_of(name: str, mime: str = "") -> str:
    ext = Path(name).suffix.lower()
    mime = mime or (mimetypes.guess_type(name)[0] or "")
    if mime.startswith("image/") and ext not in (".svg",):
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".xlsx":
        return "xlsx"
    if ext in TEXT_EXT or mime.startswith("text/"):
        return "text"
    return "binary"


def _safe_name(name: str) -> str:
    name = Path(name).name or "file"
    name = re.sub(r"[^\w.\-() ]+", "_", name, flags=re.UNICODE).strip(" .") or "file"
    return name[:120]


def save_upload(workspace: Path, filename: str, data: bytes, mime: str = "") -> dict[str, Any]:
    """Сохраняет файл в uploads/ и готовит производные (текст, уменьшенная картинка)."""
    up = workspace / UPLOADS
    up.mkdir(parents=True, exist_ok=True)
    name = _safe_name(filename)
    target = up / name
    if target.exists():
        target = up / f"{target.stem}-{int(time.time() * 1000) % 100000}{target.suffix}"
    target.write_bytes(data)
    kind = kind_of(target.name, mime)
    info: dict[str, Any] = {"type": "file", "path": f"{UPLOADS}/{target.name}", "name": filename, "kind": kind,
                            "size": len(data), "mime": mime or mimetypes.guess_type(target.name)[0] or ""}
    try:
        if kind == "pdf":
            info["text_chars"] = _write_sidecar(target, _pdf_text(data))
        elif kind == "docx":
            info["text_chars"] = _write_sidecar(target, _docx_text(data))
        elif kind == "xlsx":
            info["text_chars"] = _write_sidecar(target, _xlsx_text(data))
        elif kind == "image":
            _prepare_image(target, data)
    except Exception as e:  # noqa: BLE001 — битый файл не должен ронять загрузку
        info["note"] = f"не удалось разобрать: {e}"
    return info


def _write_sidecar(target: Path, text: str) -> int:
    (target.parent / (target.name + ".txt")).write_text(text, encoding="utf-8")
    return len(text)


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # noqa: PLC0415 — необязательная зависимость
    except ImportError:
        return ""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        pages.append(f"--- страница {i} ---\n{t}")
    text = "\n\n".join(pages)
    if not re.search(r"\w{3,}", text):
        return ""  # скан без текстового слоя
    return text


def _docx_text(data: bytes) -> str:
    try:
        import docx  # noqa: PLC0415
    except ImportError:
        return ""
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _xlsx_text(data: bytes) -> str:
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError:
        return ""
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"--- лист {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                out.append("\t".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def _prepare_image(target: Path, data: bytes) -> None:
    """Большую картинку уменьшаем для модели (копия .model.jpg), оригинал остаётся как есть."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return
    im = Image.open(io.BytesIO(data))
    if max(im.size) <= MODEL_IMAGE_SIDE and len(data) <= MAX_IMAGE_BYTES:
        return
    im.thumbnail((MODEL_IMAGE_SIDE, MODEL_IMAGE_SIDE))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(target.parent / (target.name + ".model.jpg"), "JPEG", quality=85)


def expand_parts(content: Any, workspace: Path | None, *, vision: bool) -> Any:
    """Ссылки на файлы → части, понятные модели. Строка и части без файлов — как есть."""
    if not isinstance(content, list) or not any(isinstance(p, dict) and p.get("type") == "file" for p in content):
        return content
    out: list[dict[str, Any]] = []
    for p in content:
        if not (isinstance(p, dict) and p.get("type") == "file"):
            out.append(p)
            continue
        out.extend(_expand_file(p, workspace, vision))
    return out


def _expand_file(p: dict[str, Any], workspace: Path | None, vision: bool) -> list[dict[str, Any]]:
    name, path = p.get("name") or p.get("path", ""), p.get("path", "")
    if workspace is None:
        return [{"type": "text", "text": f"[вложение {name} недоступно: у агента нет рабочей папки]"}]
    f = (workspace / path).resolve()
    if workspace.resolve() not in f.parents or not f.exists():
        return [{"type": "text", "text": f"[вложение {name} не найдено]"}]
    kind = p.get("kind") or kind_of(f.name)
    where = f"файл сохранён в рабочей папке: {path}"
    if kind == "image":
        if not vision:
            return [{"type": "text", "text": f"[картинка {name}: эта модель не видит изображения; {where}]"}]
        small = f.parent / (f.name + ".model.jpg")
        src, mime = (small, "image/jpeg") if small.exists() else (f, p.get("mime") or "image/png")
        b64 = base64.b64encode(src.read_bytes()).decode()
        return [{"type": "text", "text": f"[картинка {name}; {where}]"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
    if kind == "text":
        text = f.read_text(encoding="utf-8", errors="replace")
    elif kind in ("pdf", "docx", "xlsx"):
        side = f.parent / (f.name + ".txt")
        text = side.read_text(encoding="utf-8") if side.exists() else ""
        if not text.strip():
            hint = " (похоже на скан без текстового слоя — попросите прислать страницы картинками)" if kind == "pdf" else ""
            return [{"type": "text", "text": f"[документ {name}: текст извлечь не удалось{hint}; {where}]"}]
        where += f"; извлечённый текст — {path}.txt"
    else:
        return [{"type": "text", "text": f"[файл {name}, {p.get('size', 0)} байт, {p.get('mime', '')}; {where}]"}]
    if len(text) <= INLINE_TEXT_LIMIT:
        return [{"type": "text", "text": f"[вложение {name}; {where}]\n```\n{text}\n```"}]
    head = text[:4000]
    return [{"type": "text", "text": f"[вложение {name}: {len(text)} символов, ниже начало; целиком читай "
                                     f"read_file(\"{path + '.txt' if kind != 'text' else path}\"); {where}]\n"
                                     f"```\n{head}\n```"}]
