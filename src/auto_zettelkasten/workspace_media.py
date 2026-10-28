"""Confined, bounded raster attachments shared by Markdown and annotation previews."""
from __future__ import annotations

from io import BytesIO
import hashlib
import os
from pathlib import PurePosixPath
import posixpath
import re
import secrets
import stat
import unicodedata
import warnings
from urllib.parse import quote, unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from .documents import DocumentError, MAX_SCANNED_ENTRIES, _LOCK, _SAFE_WRITES, _identity, _linked, _parts, _stamp


MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_MEDIA_BYTES = 256 * 1024 * 1024
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}


def image_metadata(data, *, filename=None):
    """Inspect raster bytes before decoding; never accept executable/animated media."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise DocumentError("Images must contain at most 25 MiB of raster data.")
    if filename is not None and (not isinstance(filename, str) or not 0 < len(filename) <= 512
                                 or any(ord(char) < 32 for char in filename)):
        raise DocumentError("An image filename must be bounded text.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in _FORMATS or getattr(image, "n_frames", 1) != 1:
                    raise DocumentError("Only non-animated PNG, JPEG and WebP images are supported.")
                width, height = image.size
                if not width or not height or width * height > MAX_IMAGE_PIXELS:
                    raise DocumentError("Images must contain at most 40 megapixels.")
                mime, extension = _FORMATS[image.format]
                if filename is not None:
                    suffix = PurePosixPath(filename).suffix.casefold()
                    allowed = {".jpg", ".jpeg"} if extension == ".jpg" else {extension}
                    if suffix not in allowed:
                        raise DocumentError("Image content does not match its filename extension.")
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombWarning,
            Image.DecompressionBombError) as exc:
        if isinstance(exc, DocumentError):
            raise
        raise DocumentError("The image is damaged, unsupported or exceeds its decoding limit.") from exc
    return {"revision": hashlib.sha256(data).hexdigest(), "mime": mime, "extension": extension,
            "width": width, "height": height, "size": len(data)}


def image_references(text):
    """Return local raster reference spans, excluding Markdown code examples.

    This intentionally leaves remote/unsupported references to UI placeholders.
    Destination spans, rather than entire markup, preserve labels and dimensions.
    """
    masked = list(text)
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^(?: {0,3}> ?)* {0,3}(`{3,}|~{3,})(.*)", line)
        if fence or match or line.startswith(("    ", "\t")):
            masked[offset:offset + len(line)] = " " * len(line)
        if match:
            if fence and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
            elif not fence:
                fence = match[1][0], len(match[1])
        offset += len(line)
    source = "".join(masked)
    for match in re.finditer(r"<!--[\s\S]*?(?:-->|$)", source):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    header = re.match(r"^\ufeff?---\r?\n[\s\S]*?^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", source, re.MULTILINE)
    if header:
        masked[header.start():header.end()] = " " * (header.end() - header.start())
    source = "".join(masked)
    for match in re.finditer(r"(`+)[^\n]*?\1", source):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    source = "".join(masked)
    patterns = [
        (True, re.compile(r"!?\[\[(?P<target>[^\]\n|]+)(?:\|[^\]\n]*)?\]\]")),
        (False, re.compile(r"!?\[(?:\\.|[^\]\\\n])*\]\([ \t]*(?:<(?P<angle>[^>\n]+)>|(?P<target>(?:\\.|[^\s()\\])+(?:\([^\n()]*\)[^\s()]*)?))(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'))?[ \t]*\)")),
    ]
    found = []
    def append(match, wiki, group):
        target = match[group]
        decoded = unquote(re.sub(r"\\([ ()])", r"\1", target))
        if PurePosixPath(decoded.split("#", 1)[0]).suffix.casefold() not in IMAGE_EXTENSIONS:
            return
        if urlsplit(decoded).scheme or decoded.startswith(("//", "/")):
            return
        found.append({"target": target, "from": match.start(group), "to": match.end(group), "wiki": wiki})

    for wiki, pattern in patterns:
        for match in pattern.finditer(source):
            preceding = source[:match.start()]
            if (len(preceding) - len(preceding.rstrip("\\"))) % 2:
                continue
            group = "angle" if not wiki and match.groupdict().get("angle") is not None else "target"
            append(match, wiki, group)
    # Reference-style Markdown images share a single destination definition.
    # Freeze/repair that destination once even when several embeds use it.
    def label_key(value):
        return " ".join(value.split()).casefold()
    definitions = {}
    for match in re.finditer(r"(?m)^ {0,3}\[(?P<label>[^\]\n]+)\]:[ \t]*(?:<(?P<angle>[^>\n]+)>|(?P<target>[^\s]+))", source):
        definitions.setdefault(label_key(match["label"]), match)
    labels = set()
    for match in re.finditer(r"!?\[(?P<label>(?:\\.|[^\]\\\n])*)\]\[(?P<id>[^\]\n]*)\]", source):
        labels.add(label_key(match["id"] or match["label"]))
    for match in re.finditer(r"!\[(?P<label>(?:\\.|[^\]\\\n])+)\](?![\[(])", source):
        labels.add(label_key(match["label"]))
    for label in labels:
        match = definitions.get(label)
        if match:
            append(match, False, "angle" if match["angle"] is not None else "target")
    return sorted({(row["from"], row["to"]): row for row in found}.values(), key=lambda row: row["from"])


class WorkspaceMedia:
    def __init__(self, service):
        self.service = service

    def _source(self, value):
        parts = _parts(value, published=True)
        if PurePosixPath(value).suffix.casefold() != ".md":
            raise DocumentError("Images must belong to a Markdown document.")
        return parts

    def _read(self, path):
        parts = _parts(path)
        if PurePosixPath(path).suffix.casefold() not in IMAGE_EXTENSIONS:
            raise DocumentError("Only PNG, JPEG and WebP workspace images can be opened.")
        with self.service._directory(parts[:-1]) as directory:
            before = directory.stat(parts[-1])
            if _linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise DocumentError("Images must be regular, non-linked workspace files.")
            if not 0 < before.st_size <= MAX_IMAGE_BYTES:
                raise DocumentError("Images must contain at most 25 MiB of raster data.")
            fd = os.open(parts[-1] if directory.fd is not None else directory.path / parts[-1],
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory.fd)
            try:
                opened = os.fstat(fd)
                if _stamp(opened) != _stamp(before):
                    raise DocumentError("The image changed while opening it.")
                chunks = bytearray()
                while len(chunks) <= MAX_IMAGE_BYTES:
                    chunk = os.read(fd, min(65536, MAX_IMAGE_BYTES + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    raise DocumentError("The image exceeds the 25 MiB limit.")
                directory.verify()
                if _stamp(os.fstat(fd)) != _stamp(opened) or _stamp(directory.stat(parts[-1])) != _stamp(opened):
                    raise DocumentError("The image changed while reading it.")
                data = bytes(chunks)
            finally:
                os.close(fd)
        metadata = image_metadata(data, filename=path)
        return data, {"path": path, **metadata}

    def read(self, workspace_id, path, *, revision=None):
        with _LOCK, self.service._errors():
            self.service._workspace(workspace_id)
            data, metadata = self._read(path)
            if revision is not None and revision != metadata["revision"]:
                raise DocumentError("The image changed. Refresh its reference.")
            return data, metadata

    def resolve(self, workspace_id, source_path, target, *, wiki=False):
        with _LOCK, self.service._errors():
            self.service._workspace(workspace_id)
            self._source(source_path)
            if not isinstance(target, str) or not target or len(target) > 4096:
                raise DocumentError("A bounded local image path is required.")
            value = unquote(re.sub(r"\\([ ()])", r"\1", target)).split("#", 1)[0]
            if urlsplit(value).scheme or value.startswith(("/", "//")) or "\\" in value:
                raise DocumentError("Images must use local workspace paths.")
            candidates = []
            for candidate in (posixpath.normpath(posixpath.join(posixpath.dirname(source_path), value)), posixpath.normpath(value)):
                if candidate in candidates:
                    continue
                try:
                    _parts(candidate)
                except DocumentError:
                    continue
                candidates.append(candidate)
            if not candidates:
                raise DocumentError("The image path leaves the workspace.")
            for candidate in candidates:
                try:
                    _, metadata = self._read(candidate)
                    return {**metadata, "relative_path": self.relative_path(source_path, candidate)}
                except FileNotFoundError:
                    continue
            if wiki and "/" not in value and value not in {".", ".."}:
                inventory = self.inventory(workspace_id)
                wanted = unicodedata.normalize("NFC", value).casefold()
                matches = [row for row in inventory["assets"]
                           if unicodedata.normalize("NFC", PurePosixPath(row["path"]).name).casefold() == wanted]
                unavailable = [path for path in inventory.get("unavailable", [])
                               if unicodedata.normalize("NFC", PurePosixPath(path).name).casefold() == wanted]
                if inventory["truncated"]:
                    raise DocumentError("A complete asset inventory is required to resolve this filename.")
                if len(matches) + len(unavailable) > 1:
                    raise DocumentError("Several images share this filename. Use an exact relative path.")
                if unavailable:
                    raise DocumentError("The referenced image could not be verified.")
                if len(matches) == 1:
                    return {**matches[0], "relative_path": self.relative_path(source_path, matches[0]["path"])}
            raise DocumentError("The referenced image is missing from this workspace.")

    @staticmethod
    def relative_path(source_path, destination):
        return quote(posixpath.relpath(destination, posixpath.dirname(source_path) or "."), safe="/")

    def _mkdirs(self, parts, *, live=False):
        for number, name in enumerate(parts):
            with self.service._directory(parts[:number]) as directory:
                self.service._writable(directory, live=live)
                try:
                    os.mkdir(name, 0o755, dir_fd=directory.fd)
                    os.fsync(directory.fd)
                except FileExistsError:
                    pass
                directory.verify()
            with self.service._directory(parts[:number + 1]):
                pass

    def _write(self, workspace_id, source_path, data, *, generated=None, filename=None):
        with _LOCK, self.service._errors():
            self.service._workspace(workspace_id)
            self._source(source_path)
            document = self.service.read(workspace_id, source_path)
            if not document.get("editable"):
                raise DocumentError("Images can only be attached to an editable note.")
            if not _SAFE_WRITES or os.link not in os.supports_dir_fd:
                raise DocumentError("Safe image writes are unavailable on this platform.")
            metadata = image_metadata(data, filename=filename)
            folder = ("attachments", "zotero" if generated is not None else "images")
            if generated is not None and (not isinstance(generated, str) or not 0 < len(generated) <= 8192):
                raise DocumentError("A bounded generated-image identity is required.")
            prefix = hashlib.sha256(generated.encode()).hexdigest()[:24] + "-" if generated is not None else ""
            name = prefix + metadata["revision"] + metadata["extension"]
            path = "/".join((*folder, name))
            # The source document has passed its own override/live ownership
            # checks. Permit its dedicated attachment directories even when a
            # mapped workspace's root marker protects generated source files.
            self._mkdirs(folder, live=True)
            try:
                existing, row = self._read(path)
                if existing != data:
                    raise DocumentError("An image asset identity collision requires review.")
                return {**row, "relative_path": self.relative_path(source_path, path), "reused": True}
            except FileNotFoundError:
                pass
            with self.service._directory(folder) as directory:
                temporary = ".az-image-" + secrets.token_hex(16)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
                try:
                    offset = 0
                    while offset < len(data):
                        written = os.write(fd, data[offset:])
                        if written <= 0:
                            raise DocumentError("The image could not be written completely.")
                        offset += written
                    os.fsync(fd)
                    directory.verify()
                    self.service._writable(directory, live=True)
                    # A hard-link publication is exclusive; the transient second
                    # link is removed before any asset can be served.
                    try:
                        os.link(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd, follow_symlinks=False)
                    except FileExistsError:
                        existing, _ = self._read(path)
                        if existing != data:
                            raise DocumentError("An image asset identity collision requires review.")
                    os.unlink(temporary, dir_fd=directory.fd)
                    os.fsync(directory.fd)
                finally:
                    os.close(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory.fd)
                    except FileNotFoundError:
                        pass
            return {"path": path, **metadata, "relative_path": self.relative_path(source_path, path), "reused": False}

    def import_image(self, workspace_id, source_path, data, *, filename=None):
        return self._write(workspace_id, source_path, data, filename=filename)

    def write_generated(self, workspace_id, source_path, data, *, identity):
        return self._write(workspace_id, source_path, data, generated=identity, filename="preview.png")

    def inventory(self, workspace_id):
        with _LOCK, self.service._errors():
            self.service._workspace(workspace_id)
            pending, assets, warnings_found, unavailable, scanned, total = [()], [], [], [], 0, 0
            while pending:
                parts = pending.pop()
                with self.service._directory(parts) as directory:
                    with os.scandir(directory.fd if directory.fd is not None else directory.path) as scan:
                        names = sorted(entry.name for entry in scan)
                    for name in names:
                        scanned += 1
                        if scanned > MAX_SCANNED_ENTRIES:
                            return {"assets": assets, "unavailable": unavailable, "warnings": [*warnings_found, "The asset inventory reached its entry limit."], "truncated": True}
                        path = "/".join((*parts, name))
                        try:
                            child = _parts(path)
                        except DocumentError:
                            continue
                        info = directory.stat(name)
                        if _linked(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(child)
                        elif stat.S_ISREG(info.st_mode) and PurePosixPath(path).suffix.casefold() in IMAGE_EXTENSIONS:
                            if total + info.st_size > MAX_MEDIA_BYTES:
                                return {"assets": assets, "unavailable": unavailable, "warnings": [*warnings_found, "The asset inventory reached its byte limit."], "truncated": True}
                            total += info.st_size
                            try:
                                _, metadata = self._read(path)
                                assets.append(metadata)
                            except (DocumentError, OSError):
                                unavailable.append(path)
                                warnings_found.append(f"Image could not be read: {path}")
                    directory.verify()
            return {"assets": sorted(assets, key=lambda row: row["path"]), "unavailable": unavailable,
                    "warnings": warnings_found, "truncated": False}

    def dependencies(self, workspace_id, contents):
        """Freeze referenced image bytes for a portable export or reviewed move."""
        assets = {}
        for source, text in contents.items():
            for reference in image_references(text):
                row = self.resolve(workspace_id, source, reference["target"], wiki=reference["wiki"])
                if row["path"] not in assets:
                    data, metadata = self.read(workspace_id, row["path"], revision=row["revision"])
                    assets[row["path"]] = {"metadata": metadata, "data": data}
                    if sum(len(item["data"]) for item in assets.values()) > MAX_MEDIA_BYTES:
                        raise DocumentError("Referenced images exceed the 256 MiB operation limit.")
        return assets
