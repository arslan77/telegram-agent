"""Photos, audio, and other files sent in either direction."""

from __future__ import annotations

from pathlib import Path
import base64
import os
import secrets

from app.activity import _success_result, _tool_parts
from app.constants import FILE_PAGE, MAX_UPLOAD, MEDIA_EXT, SKIP_DIRS


def safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base).strip("._")
    return (cleaned or "file")[:80]


def media_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext == ".gif":
        return "animation"
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    if ext in {".ogg", ".opus"}:
        return "voice"
    if ext in {".mp3", ".m4a", ".wav", ".flac", ".aac"}:
        return "audio"
    return "document"


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXT


def blocked_file(path: Path) -> bool:
    name = path.name
    if name.startswith(".env") or name in {"id_rsa", "credentials.json"}:
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def inbound_prompt(path: Path, caption: str) -> str:
    kind = media_kind(path)
    if kind == "photo":
        note = f"The user sent an image. Read this file and look at it before answering:\n{path}"
    elif kind == "animation":
        note = f"The user sent an animation. Read this file:\n{path}"
    elif kind in {"audio", "voice"}:
        note = f"The user sent an audio file:\n{path}"
    elif kind == "video":
        note = f"The user sent a video file:\n{path}"
    else:
        note = f"The user sent a file:\n{path}"
    text = caption.strip()
    if text:
        return f"{note}\n\n{text}"
    if kind == "photo":
        return note + "\n\nDescribe what you see."
    return note + "\n\nLook at it and respond."


def attachment_from_message(message: dict) -> dict | None:
    caption = message.get("caption") if isinstance(message.get("caption"), str) else ""
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        sizes = [item for item in photos if isinstance(item, dict) and isinstance(item.get("file_id"), str)]
        if sizes:
            best = max(sizes, key=lambda item: item.get("file_size") or 0)
            return {
                "file_id": best["file_id"],
                "name": "photo.jpg",
                "size": best.get("file_size") or 0,
                "caption": caption,
            }
    for key, default in (
        ("document", "file"),
        ("audio", "audio.mp3"),
        ("voice", "voice.ogg"),
        ("video", "video.mp4"),
        ("video_note", "video.mp4"),
        ("animation", "animation.mp4"),
    ):
        body = message.get(key)
        if isinstance(body, dict) and isinstance(body.get("file_id"), str):
            name = body.get("file_name") if isinstance(body.get("file_name"), str) and body.get("file_name") else default
            return {
                "file_id": body["file_id"],
                "name": name,
                "size": body.get("file_size") or 0,
                "caption": caption,
            }
    return None


def inside_workspace(workspace: Path, raw: str) -> Path | None:
    text = raw.strip()
    if not text or "\x00" in text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        root = workspace.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None
    if blocked_file(relative):
        return None
    return resolved


def output_paths(tool_call: object, workspace: Path) -> list[Path]:
    """Media files a finished tool wrote. Reads and searches are ignored."""
    label, args = _tool_parts(tool_call)
    if label.lower() in {"read", "grep", "rg", "glob", "ls"}:
        return []
    found: list[Path] = []
    seen: set[Path] = set()

    def add(raw: object) -> None:
        if not isinstance(raw, str):
            return
        path = inside_workspace(workspace, raw)
        if path is None or not path.is_file() or not is_media(path) or path in seen:
            return
        seen.add(path)
        found.append(path)

    for key in ("path", "file_path", "filePath", "filename", "target_file"):
        add(args.get(key))
    success = _success_result(tool_call)
    for key in ("file_path", "filePath", "path", "image_path"):
        add(success.get(key))
    image = success.get("image_data") or success.get("imageData")
    if isinstance(image, str) and image.strip():
        try:
            raw_bytes = base64.b64decode(image, validate=False)
        except (ValueError, TypeError):
            raw_bytes = b""
        if raw_bytes and len(raw_bytes) <= MAX_UPLOAD:
            folder = workspace / ".cursor-telegram" / "outbound"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{secrets.token_hex(4)}.png"
            dest.write_bytes(raw_bytes)
            found.append(dest.resolve())
    return found


def media_index(root: Path) -> dict[str, float]:
    index: dict[str, float] = {}
    if not root.is_dir():
        return index
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if not is_media(path) or blocked_file(path):
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(base)
                index[str(resolved)] = resolved.stat().st_mtime
            except (OSError, ValueError):
                continue
    return index


def changed_media(before: dict[str, float], root: Path) -> list[Path]:
    current = media_index(root)
    found: list[Path] = []
    for key, mtime in current.items():
        previous = before.get(key)
        if previous is None or mtime > previous + 0.001:
            found.append(Path(key))
    return found


def list_sendable(root: Path, query: str = "") -> list[Path]:
    needle = query.strip().lower()
    found: list[tuple[float, Path]] = []
    if not root.is_dir():
        return []
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if blocked_file(path):
                continue
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(base)
                size = resolved.stat().st_size
                mtime = resolved.stat().st_mtime
            except (OSError, ValueError):
                continue
            if size > MAX_UPLOAD or size <= 0:
                continue
            if needle and needle not in str(relative).lower():
                continue
            found.append((mtime, resolved))
            if len(found) >= 2000:
                break
        if len(found) >= 2000:
            break
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def file_page(paths: list[Path], page: int, root: Path) -> tuple[list[Path], str]:
    start = max(0, page) * FILE_PAGE
    chunk = paths[start : start + FILE_PAGE]
    if not paths:
        return [], "No matching files."
    if not chunk:
        return [], "No more files."
    lines = [f"Files {start + 1}–{start + len(chunk)} of {len(paths)}"]
    for path in chunk:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(path.name)
        lines.append(str(rel))
    return chunk, "\n".join(lines)
