from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.sync_api import Response

SCRIPT_PATTERNS = [
    re.compile(r'finished\("([^\"]+)",'),
    re.compile(r'display\("([^\"]+)",'),
]
FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", re.IGNORECASE)
ALLOWED_DOWNLOAD_HOSTS = {"konto.flatex.at", "konto.flatex.de"}


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    safe = safe.strip("._")
    if not safe:
        return "document.pdf"

    stem, ext = Path(safe).stem, Path(safe).suffix
    stem = stem.rstrip("._")
    if not stem:
        stem = "document"
    if not ext:
        ext = ".pdf"
    return f"{stem}{ext}"


def filename_from_headers_or_url(response: Response, url: str, fallback_stem: str) -> str:
    content_disp = response.headers.get("content-disposition", "")
    match = FILENAME_RE.search(content_disp)
    if match:
        candidate = unquote(match.group(1)).strip()
        if candidate:
            if not candidate.lower().endswith(".pdf"):
                candidate += ".pdf"
            return sanitize_filename(candidate)

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "file", "name", "documentName", "id"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if not candidate.lower().endswith(".pdf"):
                candidate += ".pdf"
            return sanitize_filename(candidate)

    tail = Path(parsed.path).name
    if tail:
        if not tail.lower().endswith(".pdf"):
            tail += ".pdf"
        return sanitize_filename(unquote(tail))

    return sanitize_filename(f"{fallback_stem}.pdf")


def filename_from_url(url: str, fallback_stem: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "file", "name", "documentName", "id"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0]).strip()
            if candidate:
                if not candidate.lower().endswith(".pdf"):
                    candidate += ".pdf"
                return sanitize_filename(candidate)

    tail = Path(parsed.path).name
    if tail:
        if not tail.lower().endswith(".pdf"):
            tail += ".pdf"
        return sanitize_filename(unquote(tail))

    return sanitize_filename(f"{fallback_stem}.pdf")


def build_stable_stem(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("id", "documentId", "docId", "mailingId", "uuid"):
        if key in qs and qs[key]:
            value = sanitize_filename(qs[key][0])
            if value.lower().endswith(".pdf"):
                value = Path(value).stem
            return f"flatex_{value}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"flatex_{digest}"


def normalize_command_url(url: str, base_url: str) -> str:
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    return urljoin(base_url, url)


def extract_pdf_link_from_script(script: str, base_url: str) -> str:
    for pattern in SCRIPT_PATTERNS:
        match = pattern.search(script)
        if match:
            return normalize_command_url(match.group(1), base_url)
    raise RuntimeError("command-invalid: no PDF link pattern matched")


def is_allowed_download_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.lower() in ALLOWED_DOWNLOAD_HOSTS
