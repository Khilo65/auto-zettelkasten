from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - package dependency supplies PyYAML
    yaml = None  # type: ignore[assignment]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    try:
        if path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return default
    if yaml is not None:
        value = yaml.safe_load(text)
    else:
        value = json.loads(text)
    return default if value is None else value


def write_yaml(path: Path, value: Any) -> None:
    if yaml is not None:
        text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, text)


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str, fallback: str = "item") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return value[:96] or fallback


def safe_filename(value: str, fallback: str = "Untitled") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:120] or fallback).strip()


def require_loopback_http_url(value: str, *, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an HTTP(S) loopback URL")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost":
        return value
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError(f"{label} must use localhost or a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError(f"{label} must use localhost or a loopback IP address")
    return value
