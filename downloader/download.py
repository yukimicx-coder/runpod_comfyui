"""Model downloader — asyncio-based, no threads, no temp files."""

import argparse
import asyncio
import hashlib
import html.parser
import json
import logging
import os
import re
import sys
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
import yaml
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
DL_ROOT = os.environ.get("MODEL_DL_ROOT", "/workspace")
MODEL_DL_LOG = os.environ.get("MODEL_DL_LOG", os.path.join(DL_ROOT, "dl.log"))
MODEL_DL_LIST = os.environ.get(
    "MODEL_DL_LIST", os.path.join(DL_ROOT, "dl_model_list.yaml")
)
CIVITAI_API_URL = os.environ.get(
    "CIVITAI_API_URL", "https://civitai.com/api/v1"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODELS_ROOT = os.path.join(DL_ROOT, "models")
IO_CHUNK_SIZE = 64 * 1024 * 1024          # 64 MiB (hash read, internal use only)
DEFAULT_CHUNK_SIZE = 1 * 1024 * 1024 * 1024  # 1 GiB for range type
DEFAULT_MAX_CONCURRENT = 4                # simultaneous download connections
MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, exponential
DOWNLOAD_META_SUFFIX = ".download.json"

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("downloader")
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# Size parser
# ---------------------------------------------------------------------------
_SIZE_SUFFIXES = {
    "GIB": 1024**3, "MIB": 1024**2, "KIB": 1024,
    "GB": 1000**3, "MB": 1000**2, "KB": 1000,
}


def parse_size(value: object) -> int:
    """Parse a human-readable size into bytes.

    Accepts int, or str with optional comma/underscore thousands separators
    and an optional suffix (KiB/MiB/GiB/KB/MB/GB). Commas must form valid
    3-digit groups. Decimals are rejected. Raises ValueError on invalid input.
    """
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"size must be positive, got {value}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"cannot parse size from {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError("empty size string")

    upper = s.upper()
    mult = 1
    num_part = s
    for suf, m in _SIZE_SUFFIXES.items():
        if upper.endswith(suf):
            num_part = s[: -len(suf)].strip()
            mult = m
            break

    if "," in num_part:
        groups = num_part.split(",")
        if (
            len(groups) < 2
            or not (1 <= len(groups[0]) <= 3)
            or any(len(g) != 3 for g in groups[1:])
            or any(not g.isdigit() for g in groups)
        ):
            raise ValueError(f"invalid comma grouping: {value!r}")

    cleaned = num_part.replace(",", "").replace("_", "")
    if not cleaned or not cleaned.isdigit():
        raise ValueError(f"invalid size: {value!r}")
    n = int(cleaned)
    if n <= 0:
        raise ValueError(f"size must be positive, got {n}")
    return n * mult


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def parse_hf_url(url: str) -> tuple[str, str, str | None] | None:
    """Parse a HuggingFace resolve URL.

    Returns (repo_id, filename, subfolder) or None if not HF.
    """
    m = re.match(
        r"https?://huggingface\.co/([^/]+/[^/]+)/resolve/[^/]+/(.+)", url
    )
    if not m:
        return None
    repo_id = m.group(1)
    path = m.group(2)
    parts = path.rsplit("/", 1)
    if len(parts) == 2:
        return repo_id, parts[1], parts[0]
    return repo_id, path, None


def parse_civitai_url(url: str) -> tuple[str, str | None] | None:
    """Parse a CivitAI download URL.

    Returns (version_id, file_id) or None.
    """
    m = re.match(
        r"https?://(?:www\.)?civitai\.com/api/download/models/(\d+)", url
    )
    if not m:
        return None
    version_id = m.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    file_id = qs.get("fileId", [None])[0]
    return version_id, file_id


# ---------------------------------------------------------------------------
# Google Drive URL helpers
# ---------------------------------------------------------------------------
_GOOGLE_DRIVE_HOSTS = {
    "drive.google.com",
    "drive.usercontent.google.com",
    "drive.googleusercontent.com",
    "docs.google.com",
}
_GOOGLE_DRIVE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/39.0.2171.95 Safari/537.36"
)
_GDRIVE_HTML_LIMIT = 4 * 1024 * 1024  # read at most 4 MiB of confirmation HTML
_GDRIVE_UC_URL = "https://drive.google.com/uc?id={file_id}"
_GDRIVE_BASE = "https://docs.google.com"


def is_google_drive_url(url: str) -> bool:
    """Return True if url points to a Google Drive host."""
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in _GOOGLE_DRIVE_HOSTS)


def parse_google_drive_url(url: str) -> str | None:
    """Extract a Google Drive file ID from a URL, or None."""
    parsed = urlparse(url)
    if not is_google_drive_url(url):
        return None
    qs = parse_qs(parsed.query)
    if "id" in qs:
        return qs["id"][0]
    # /file/d/<FILE_ID>/view  (drive.google.com)
    m = re.search(r"/file/d/([^/]+)/", parsed.path)
    if m:
        return m.group(1)
    # /uc?export=download&id=... or /open?id=...
    m = re.search(r"[\?&]id=([^&\s]+)", url)
    if m:
        return m.group(1)
    return None


class _GDriveFormParser(html.parser.HTMLParser):
    """Extract the confirmation form, hidden inputs, and download link."""

    def __init__(self):
        super().__init__()
        self.form_action: str | None = None
        self.hidden_inputs: dict[str, str] = {}
        self.download_href: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "download-form":
            self.form_action = attrs.get("action")
        elif tag == "input":
            if attrs.get("type") == "hidden" and attrs.get("name"):
                self.hidden_inputs[attrs["name"]] = attrs.get("value", "")
        elif tag == "a":
            href = attrs.get("href") or ""
            if "/uc?export=download" in href:
                self.download_href = href


def _parse_gdrive_page(
    html: str,
) -> tuple[str | None, str | None, str | None, dict[str, str]]:
    """Parse a Google Drive confirmation page.

    Returns (download_url, error_msg, form_action, hidden_inputs).
    Only one of download_url/error_msg/form_action is set.
    """
    # "downloadUrl":"..." embedded in a script tag
    m = re.search(r'"downloadUrl"\s*:\s*"([^"]+)"', html)
    if m:
        u = (
            m.group(1)
            .replace("\\u003d", "=")
            .replace("\\u0026", "&")
            .replace("\\/", "/")
        )
        return u, None, None, {}

    parser = _GDriveFormParser()
    parser.feed(html)
    if parser.download_href:
        return _GDRIVE_BASE + parser.download_href, None, None, {}
    m = re.search(r'<p class="uc-error-subcaption">(.*?)</p>', html, re.S)
    if m:
        return None, m.group(1).strip(), None, {}
    if parser.form_action:
        return None, None, parser.form_action, parser.hidden_inputs
    return None, None, None, {}


async def _read_limited(resp: httpx.Response, limit: int) -> str:
    """Read at most `limit` bytes from a streaming response as text."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def resolve_google_drive_url(
    client: httpx.AsyncClient, url: str, max_redirects: int = 5
) -> str:
    """Resolve a Google Drive URL to a direct download URL.

    Skips the "virus scan cannot confirm" page by parsing the confirmation
    form / hidden inputs / downloadUrl, and returns the effective URL that
    serves the file body. Cookies are shared through the AsyncClient.
    """
    file_id = parse_google_drive_url(url)
    if not file_id:
        raise ValueError(
            f"Cannot extract Google Drive file ID from {sanitize_url(url)}"
        )

    current = _GDRIVE_UC_URL.format(file_id=file_id)
    headers = {"User-Agent": _GOOGLE_DRIVE_UA, "Accept-Encoding": "identity"}
    logger.debug("gdrive resolve start id=%s url=%s", file_id, sanitize_url(url))

    for step in range(max_redirects):
        async with client.stream("GET", current, headers=headers) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type") or ""
            logger.debug(
                "gdrive step %d status=%d url=%s ctype=%s cd=%s clen=%s",
                step, resp.status_code, sanitize_url(current), ctype,
                resp.headers.get("content-disposition"),
                resp.headers.get("content-length"),
            )
            # The actual file: has Content-Disposition, or is not HTML.
            if "content-disposition" in resp.headers or "text/html" not in ctype:
                logger.debug(
                    "gdrive step %d -> file response, effective url=%s",
                    step, sanitize_url(str(resp.url)),
                )
                return str(resp.url)
            html = await _read_limited(resp, _GDRIVE_HTML_LIMIT)

        download_url, error, form_action, hidden = _parse_gdrive_page(html)
        logger.debug(
            "gdrive step %d html len=%d download_url=%s error=%r form_action=%r hidden=%s",
            step, len(html), sanitize_url(download_url or ""), error,
            form_action, sorted(hidden.keys()),
        )
        if error:
            raise RuntimeError(f"Google Drive: {error}")
        if download_url:
            return download_url
        if form_action:
            if not form_action.startswith("http"):
                form_action = _GDRIVE_BASE + form_action
            parsed = urlparse(form_action)
            qs = parse_qs(parsed.query)
            for k, v in hidden.items():
                qs[k] = [v]
            current = urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))
            continue
        raise RuntimeError("Google Drive confirmation page could not be resolved")

    raise RuntimeError("Google Drive URL resolution exceeded the redirect limit")


# ---------------------------------------------------------------------------
# YAML validation
# ---------------------------------------------------------------------------
def validate_entry(entry: dict, category: str, index: int) -> list[str]:
    """Return list of error strings for a single entry. Empty = valid."""
    errors: list[str] = []
    tag = f"[{category}#{index}]"

    entry_type = entry.get("type")
    if entry_type not in ("range", "split"):
        errors.append(f"{tag} type must be 'range' or 'split', got {entry_type!r}")

    file_name = entry.get("file")
    if not file_name or not isinstance(file_name, str):
        errors.append(f"{tag} 'file' is required")
    elif "/" in file_name or "\\" in file_name or ".." in file_name:
        errors.append(f"{tag} 'file' must be a simple filename, got {file_name!r}")

    dir_name = entry.get("dir")
    if not dir_name or not isinstance(dir_name, str):
        errors.append(f"{tag} 'dir' is required")
    elif os.path.isabs(dir_name) or ".." in dir_name:
        errors.append(f"{tag} 'dir' must be relative and contain no '..': {dir_name!r}")

    urls = entry.get("urls")
    if not urls or not isinstance(urls, list) or len(urls) == 0:
        errors.append(f"{tag} 'urls' must be a non-empty list")
    else:
        for i, u in enumerate(urls):
            if not isinstance(u, str) or not u.startswith(("http://", "https://")):
                errors.append(f"{tag} urls[{i}] is not a valid HTTP(S) URL")

    sha256 = entry.get("sha256")
    if sha256 is not None and sha256 != "":
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(sha256)):
            errors.append(f"{tag} sha256 must be 64 hex chars or empty, got {sha256!r}")

    if entry_type == "range":
        if any(is_google_drive_url(u) for u in urls if isinstance(u, str)):
            errors.append(
                f"{tag} Google Drive URLs are not supported for type 'range'"
            )
        filesize = entry.get("filesize")
        if filesize is not None and filesize != "":
            try:
                parse_size(filesize)
            except ValueError as e:
                errors.append(f"{tag} filesize: {e}")
    elif entry_type == "split":
        if any(is_google_drive_url(u) for u in urls if isinstance(u, str)):
            if not entry.get("part-sizes"):
                errors.append(
                    f"{tag} 'part-sizes' is required for Google Drive split URLs"
                )
        part_sizes = entry.get("part-sizes")
        url_count = len(urls) if urls else 0
        if part_sizes is not None:
            if not isinstance(part_sizes, list):
                errors.append(f"{tag} 'part-sizes' must be a list")
            elif len(part_sizes) != url_count:
                errors.append(
                    f"{tag} 'part-sizes' length ({len(part_sizes)}) "
                    f"must match 'urls' length ({url_count})"
                )
            else:
                for i, ps in enumerate(part_sizes):
                    try:
                        parse_size(ps)
                    except ValueError as e:
                        errors.append(f"{tag} part-sizes[{i}]: {e}")

    return errors


def load_and_validate_yaml(yaml_path: str | None) -> dict[str, list[dict]]:
    """Load YAML, validate, filter by category.

    Returns {category: [entry, ...]} with 'category' and 'index' keys added.
    Raises ValueError on validation errors.
    """
    if not os.path.isfile(yaml_path):
        raise FileNotFoundError(yaml_path)

    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise TypeError("YAML top-level must be a mapping: %s" % yaml_path)

    all_errors: list[str] = []
    result: dict[str, list[dict]] = {}

    # validate all data
    for cat, entries in data.items():
        if not isinstance(entries, list):
            all_errors.append(f"[{cat}] must be a list")
            continue

        validated: list[dict] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                all_errors.append(f"[{cat}#{i}] must be a mapping")
                continue
            errs = validate_entry(entry, cat, i)
            all_errors.extend(errs)
            if not errs:
                entry["category"] = cat
                entry["index"] = i
                validated.append(entry)
        if validated:
            result[cat] = validated

    if all_errors:
        raise ValueError(f"invalid YAML format: {all_errors}")

    return result

# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------
def resolve_output_path(entry: dict, cat:str) -> str:
    """Compute and validate the output file path."""
    out = os.path.normpath(os.path.join(MODELS_ROOT, entry["dir"], cat, entry["file"]))
    real_out = os.path.realpath(out)
    real_base = os.path.realpath(DL_ROOT)
    if not real_out.startswith(real_base + os.sep) and real_out != real_base:
        raise RuntimeError("Output path escapes DL_ROOT: %s" % out)
    return out


_SENSITIVE_QUERY_KEYS = {
    "token", "key", "api_key", "apikey", "auth", "signature",
    "sig", "x-api-key", "X-Amz-Signature", "X-Amz-Credential",
    "X-Amz-Security-Token",
    "confirm", "uuid", "at",
}


def _auth_headers(url: str) -> dict[str, str]:
    """Build Authorization headers for a given URL, based on env vars."""
    headers: dict[str, str] = {}
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {hf_token}"
    civitai_token = os.environ.get("CIVITAI_API_TOKEN")
    if civitai_token and "civitai.com" in url:
        headers["Authorization"] = f"Bearer {civitai_token}"
    return headers


def sanitize_url(url: str) -> str:
    """Redact sensitive query parameters from a URL for logging."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    qs = parse_qs(parsed.query)
    redacted = "&".join(
        f"{k}={v[0] if k not in _SENSITIVE_QUERY_KEYS else '***'}"
        for k, v in qs.items()
    )
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{redacted}"


# ---------------------------------------------------------------------------
# Metadata: HF
# ---------------------------------------------------------------------------
async def _hf_get_file_info(
    client: httpx.AsyncClient, url: str
) -> tuple[int | None, str | None]:
    """Try to get file size and SHA256 from HF for a resolve URL.

    Returns (size_bytes, sha256_hex) or (None, None).
    """
    parsed = parse_hf_url(url)
    if not parsed:
        return None, None
    repo_id, filename, subfolder = parsed

    headers = _auth_headers(url)

    # Try the model API tree endpoint to get LFS metadata
    file_path = f"{subfolder}/{filename}" if subfolder else filename
    api_url = f"https://huggingface.co/api/models/{repo_id}/tree/main"
    if subfolder:
        api_url += f"/{subfolder}"

    try:
        resp = await client.get(api_url, headers=headers)
        resp.raise_for_status()
        tree = resp.json()
        for item in tree:
            if item.get("path") == filename or item.get("path") == file_path:
                size = item.get("size")
                lfs = item.get("lfs", {})
                sha = lfs.get("sha256") if lfs else None
                return (int(size) if size else None), (sha.lower() if sha else None)
    except Exception:
        pass  # fall through

    return None, None


# ---------------------------------------------------------------------------
# Metadata: CivitAI
# ---------------------------------------------------------------------------
async def _civitai_get_file_info(
    client: httpx.AsyncClient, url: str
) -> tuple[int | None, str | None, str | None]:
    """Get file info from CivitAI API.

    Returns (size_bytes, sha256_hex, download_url) or (None, None, None).
    """
    parsed = parse_civitai_url(url)
    if not parsed:
        return None, None, None
    version_id, file_id = parsed

    api_url = f"{CIVITAI_API_URL}/model-versions/{version_id}"
    headers = _auth_headers(url)

    try:
        resp = await client.get(api_url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None, None, None

    files = data.get("files", [])
    if not files:
        return None, None, None

    target = None
    if file_id:
        for f in files:
            if str(f.get("id")) == str(file_id):
                target = f
                break
    if not target:
        # fallback: first file (same logic as original)
        fp_priority = ["fp8", "int8", "bf16", "fp16"]
        if len(files) > 1:
            fp_map = {}
            for i, f in enumerate(files):
                meta = f.get("metadata", {})
                fp = meta.get("fp")
                if fp:
                    fp_map[fp] = i
            idx = 0
            for fp in fp_priority:
                if fp in fp_map:
                    idx = fp_map[fp]
                    break
            target = files[idx]
        else:
            target = files[0]

    # API returns sizeKB; convert to bytes
    size_kb = target.get("sizeKB", 0)
    size_bytes = int(size_kb * 1024) if size_kb else None
    hashes = target.get("hashes", {})
    sha = hashes.get("SHA256")
    sha = sha.lower() if sha else None
    download_url = target.get("downloadUrl")

    return size_bytes, sha, download_url


# ---------------------------------------------------------------------------
# Metadata resolution
# ---------------------------------------------------------------------------
async def _head_content_length(
    client: httpx.AsyncClient, url: str
) -> int | None:
    """Return Content-Length via HEAD, or None on failure."""
    try:
        headers = _auth_headers(url)
        headers.setdefault("Accept-Encoding", "identity")
        resp = await client.head(url, headers=headers)
        resp.raise_for_status()
        cl = resp.headers.get("content-length")
        return int(cl) if cl else None
    except Exception:
        return None


async def validate_file_size_and_hash(
    client: httpx.AsyncClient, entry: dict
):
    """Resolve total filesize and sha256 for a range entry.

    Size priority: provider API > HEAD > YAML filesize.
    SHA priority: provider API sha > YAML sha256.
    Results are written to entry["filesize"] and entry["sha256"].
    YAML values are treated as reference values: when the server reports a
    different size/hash, a warning is logged and the server value is used.
    Raises ValueError if the size cannot be determined, or if server values
    conflict across multiple URLs.
    """
    assert entry["type"] == "range"

    yaml_size_raw = entry.get("filesize")
    yaml_size = (
        parse_size(yaml_size_raw) if yaml_size_raw not in (None, "") else None
    )
    yaml_sha = (entry.get("sha256") or "").lower()

    api_sizes: list[int] = []
    api_shas: list[str] = []
    for url in entry["urls"]:
        if "huggingface.co" in url:
            size, sha = await _hf_get_file_info(client, url)
        elif "civitai.com" in url:
            size, sha, _ = await _civitai_get_file_info(client, url)
        else:
            continue
        if size:
            api_sizes.append(size)
        if sha:
            api_shas.append(sha)

    server_size: int | None = None
    if api_sizes:
        if any(s != api_sizes[0] for s in api_sizes):
            raise ValueError(
                f"API reports different sizes for {entry['file']}: {api_sizes}"
            )
        server_size = api_sizes[0]
    else:
        header_sizes: list[int] = []
        for url in entry["urls"]:
            cl = await _head_content_length(client, url)
            if cl:
                header_sizes.append(cl)
        if header_sizes:
            if any(s != header_sizes[0] for s in header_sizes):
                raise ValueError(
                    f"HEAD reports different sizes for {entry['file']}: {header_sizes}"
                )
            server_size = header_sizes[0]

    if server_size is not None:
        if yaml_size is not None and yaml_size != server_size:
            logger.warning(
                "filesize mismatch for %s: YAML=%d server=%d (using server value)",
                entry["file"], yaml_size, server_size,
            )
        entry["filesize"] = server_size
    else:
        if yaml_size is None:
            raise ValueError(
                f"Cannot determine total file size for {entry['file']}"
            )
        entry["filesize"] = yaml_size

    if api_shas:
        if any(h != api_shas[0] for h in api_shas):
            raise ValueError(
                f"API reports different SHA256 for {entry['file']}: {api_shas}"
            )
        server_sha = api_shas[0]
        if yaml_sha and yaml_sha != server_sha:
            logger.warning(
                "sha256 mismatch for %s: YAML=%s server=%s (using server value)",
                entry["file"], yaml_sha, server_sha,
            )
        entry["sha256"] = server_sha
    else:
        entry["sha256"] = yaml_sha

    if not entry["sha256"]:
        logger.warning("sha256 for %s is not specified", entry["file"])


async def resolve_split_sizes(
    client: httpx.AsyncClient, entry: dict
):
    """Resolve part sizes for a split entry.

    Priority per URL: HEAD Content-Length > YAML part-sizes (fallback).
    Writes entry["part-sizes"] as a list of int.
    When HEAD value differs from YAML, a warning is logged and HEAD is used.
    Raises ValueError if a part's size cannot be determined.
    """
    yaml_sizes: list[int] | None = None
    if entry.get("part-sizes") is not None:
        yaml_sizes = [parse_size(ps) for ps in entry["part-sizes"]]

    sizes: list[int] = []
    for i, url in enumerate(entry["urls"]):
        if is_google_drive_url(url):
            # HEAD is unreliable for Google Drive; 'part-sizes' is required.
            if yaml_sizes is None or i >= len(yaml_sizes):
                raise ValueError(
                    f"Cannot determine part size for Google Drive URL: {url} "
                    f"('part-sizes' is required)"
                )
            sizes.append(yaml_sizes[i])
            continue
        cl = await _head_content_length(client, url)
        if cl is None:
            if yaml_sizes is not None and i < len(yaml_sizes):
                sizes.append(yaml_sizes[i])
                continue
            raise ValueError(
                f"Cannot determine part size for URL: {url} "
                f"(HEAD returned no Content-Length)"
            )
        if yaml_sizes is not None and i < len(yaml_sizes) and yaml_sizes[i] != cl:
            logger.warning(
                "part-sizes mismatch for %s part %d: YAML=%d HEAD=%d "
                "(using HEAD value)",
                entry["file"], i, yaml_sizes[i], cl,
            )
        sizes.append(cl)

    entry["part-sizes"] = sizes


# ---------------------------------------------------------------------------
# Lock file management
# ---------------------------------------------------------------------------
class DownloadState:
    """Manages the .download.json lock file for a single output file."""

    def __init__(self, output_path: str):
        self.output_path = output_path
        self.lock_path = output_path + DOWNLOAD_META_SUFFIX
        self.pid = os.getpid()

    def acquire(self) -> bool:
        """Try to create lock file. Returns True if acquired."""
        meta = {"pid": self.pid, "ts": time.time()}
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                json.dump(meta, f)
            return True
        except FileExistsError:
            # Check if the existing lock belongs to a live process
            try:
                with open(self.lock_path, "r") as f:
                    existing = json.load(f)
                old_pid = existing.get("pid")
                if old_pid:
                    try:
                        os.kill(old_pid, 0)
                        return False  # process alive, cannot acquire
                    except PermissionError:
                        return False  # owned by another user — treat as alive
                    except OSError:
                        pass  # process dead, stale lock
            except (json.JSONDecodeError, OSError):
                pass
            # Stale lock — remove and retry
            try:
                os.unlink(self.lock_path)
            except OSError:
                pass
            return self.acquire()

    def release(self):
        """Remove lock file."""
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# SHA256 verification
# ---------------------------------------------------------------------------
def verify_sha256(path: str, expected: str | None) -> str:
    """Verify SHA256 of a file. Returns 'verified', 'unverified', or 'mismatch'."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(IO_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    actual = h.hexdigest().lower()
    if expected:
        if actual == expected.lower():
            return "verified"
        return "mismatch"
    return "unverified"


# ---------------------------------------------------------------------------
# Check existing file
# ---------------------------------------------------------------------------
def check_existing(output_path: str, entry:dict) -> bool:
    """Check if existing file is complete and matches hash. Returns True to skip."""
    if not os.path.isfile(output_path):
        return False
    if "filesize" in entry:
        if os.stat(output_path).st_size != entry["filesize"]:
            logger.info("file size mismatch, will re-download: %s", output_path)
            return False

    if "sha256" in entry:
        logger.info("start to check hash... %s", output_path)
        result = verify_sha256(output_path, entry["sha256"])
        if result == "verified":
            logger.info("Skip (hash match): %s", output_path)
            return True
        logger.info("Hash mismatch, will re-download: %s", output_path)
        return False
    logger.info("Can't verify existing file, will re-download: %s", output_path)
    return False


# ---------------------------------------------------------------------------
# Common segment download
# ---------------------------------------------------------------------------
class SegmentSizeError(ValueError):
    """Deterministic size/format error from the response.

    Retrying cannot succeed because the same URL yields the same body.
    These errors must NOT be retried.
    """


async def download_segment(
    client: httpx.AsyncClient,
    url: str,
    output_path: str,
    write_lock: asyncio.Lock,
    offset: int,
    expected_size: int | None,
    extra_headers: dict[str, str] | None,
    semaphore: asyncio.Semaphore,
    progress: tqdm | None,
    retries: int = MAX_RETRIES,
    expected_total: int | None = None,
    label: str = "",
) -> int:
    """Download a segment from url, writing at offset in output_path.

    `label` identifies the segment (e.g. "cat/file#part3@offset") so logs for
    parallel segments from the same/相似 host can be told apart.

    Returns the number of bytes written.
    Raises on failure after all retries exhausted.
    """
    async with semaphore:
        last_error: Exception | None = None
        # Bytes of this part already durably written to disk. Persists across
        # attempts so a TransportError mid-transfer resumes from this offset
        # instead of re-fetching the whole part.
        written = 0
        # Cache the GDrive-resolved URL so retries don't re-run the
        # confirmation-page resolver (avoids an extra GET and HTML re-appearance).
        resolved_url: str | None = None

        for attempt in range(retries):
            try:
                headers: dict[str, str] = {}
                if extra_headers:
                    headers.update(extra_headers)
                # Disable compression so Content-Length and received bytes match
                headers.setdefault("Accept-Encoding", "identity")
                # Auth via header (never put tokens in the URL)
                headers.update(_auth_headers(url))

                # Google Drive: resolve the confirmation page first (cached)
                request_url = url
                gdrive = is_google_drive_url(url)
                if gdrive:
                    if resolved_url is None:
                        resolved_url = await resolve_google_drive_url(client, url)
                        logger.info(
                            "segment[%s] gdrive resolved %s -> %s",
                            label, sanitize_url(url), sanitize_url(resolved_url),
                        )
                    request_url = resolved_url
                    headers.setdefault("User-Agent", _GOOGLE_DRIVE_UA)

                resume = written
                # remaining bytes still needed for this part
                remaining = expected_size - resume if expected_size is not None else None

                # Build the Range header for the remainder of this part.
                if resume > 0:
                    if "Range" in headers:
                        # range type: extra_headers already requested the whole
                        # chunk [offset, offset+expected_size-1]; narrow it to
                        # the not-yet-written suffix.
                        crange = headers["Range"]
                        m = re.match(r"bytes=(\d+)-(\d+)", crange)
                        if m:
                            chunk_end = int(m.group(2))
                            headers["Range"] = (
                                f"bytes={offset + resume}-{chunk_end}"
                            )
                    else:
                        # split type: request the remainder of the remote part.
                        headers["Range"] = f"bytes={resume}-"

                resume_active = resume > 0

                async with client.stream("GET", request_url, headers=headers) as resp:
                    resp.raise_for_status()

                    logger.debug(
                        "segment[%s] attempt=%d/%d url=%s status=%d ctype=%s "
                        "clen=%s crange=%s",
                        label, attempt + 1, retries, sanitize_url(request_url),
                        resp.status_code, resp.headers.get("content-type"),
                        resp.headers.get("content-length"),
                        resp.headers.get("content-range"),
                    )

                    # Validate range response (deterministic: no retry)
                    if "Range" in headers:
                        if resume_active and resp.status_code == 200:
                            # Server ignored our Range: the 200 body is the
                            # whole part. Fall back to a full re-fetch from the
                            # start of the part by overwriting what we had.
                            logger.warning(
                                "segment[%s] server ignored Range for resume; "
                                "re-fetching part from start (%s)",
                                label, sanitize_url(url),
                            )
                            written = 0
                            resume_active = False
                            remaining = expected_size
                        elif resp.status_code != 206:
                            raise SegmentSizeError(
                                f"Expected 206 for Range request, got {resp.status_code}"
                            )
                        else:
                            cr = resp.headers.get("content-range", "")
                            cr_match = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                            if not cr_match:
                                raise SegmentSizeError(
                                    f"Missing or invalid Content-Range: {cr!r}"
                                )
                            cr_start = int(cr_match.group(1))
                            cr_end = int(cr_match.group(2))
                            cr_total = int(cr_match.group(3))
                            if cr_start != offset + written:
                                raise SegmentSizeError(
                                    f"Content-Range start mismatch: expected {offset + written}, got {cr_start}"
                                )
                            expected_chunk = cr_end - cr_start + 1
                            if remaining is not None and expected_chunk != remaining:
                                raise SegmentSizeError(
                                    f"Content-Range chunk size mismatch: expected {remaining}, got {expected_chunk}"
                                )
                            if expected_total is not None and cr_total != expected_total:
                                raise SegmentSizeError(
                                    f"Content-Range total mismatch: expected {expected_total}, got {cr_total}"
                                )

                    # Content-Length hint for early detection (diagnostic + guard)
                    content_length: int | None = None
                    try:
                        content_length = int(resp.headers.get("content-length"))
                    except (TypeError, ValueError):
                        content_length = None
                    if (
                        content_length is not None
                        and "Range" not in headers
                        and expected_size is not None
                        and content_length != expected_size
                    ):
                        logger.warning(
                            "segment[%s] Content-Length %d != expected %d (%s)",
                            label, content_length, expected_size, sanitize_url(url),
                        )

                    # Stream to file with seek
                    with open(output_path, "r+b") as f:
                        # NOTE: aiter_bytes() with no chunk_size returns each
                        # network chunk as it arrives, so we can stop promptly
                        # when the expected size is reached. Passing a large
                        # chunk_size would make httpx buffer up to that many
                        # bytes before yielding, delaying the stop.
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            chunk_len = len(chunk)
                            if expected_size is not None and written + chunk_len > expected_size:
                                raise SegmentSizeError(
                                    f"oversize: received {written + chunk_len} "
                                    f"bytes, expected {expected_size} max"
                                )
                            async with write_lock:
                                f.seek(offset + written)
                                f.write(chunk)
                            written += chunk_len
                            if progress:
                                progress.update(chunk_len)
                            if expected_size is not None and written == expected_size:
                                if (
                                    content_length is not None
                                    and content_length > expected_size
                                ):
                                    raise SegmentSizeError(
                                        f"Content-Length {content_length} exceeds "
                                        f"expected {expected_size}"
                                    )
                                # All required bytes are on disk; do not wait for EOF.
                                logger.debug(
                                    "segment[%s] reached expected size %d (%s); closing stream",
                                    label, expected_size, sanitize_url(url),
                                )
                                break

                    if expected_size is not None and written != expected_size:
                        raise SegmentSizeError(
                            f"incomplete: got {written} bytes, expected {expected_size}"
                        )

                    return written  # success

            except SegmentSizeError as e:
                logger.error(
                    "segment[%s] size error (attempt %d/%d, offset=%d, expected=%s, "
                    "written=%d) %s: %s",
                    label, attempt + 1, retries, offset, expected_size,
                    written, sanitize_url(url), e,
                )
                raise  # deterministic error: do not retry
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as e:
                last_error = e
                logger.warning(
                    "segment[%s] download error (attempt %d/%d, offset=%d, "
                    "expected=%s, written=%d) %s: %s",
                    label, attempt + 1, retries, offset, expected_size,
                    written, sanitize_url(url), e,
                )
                if attempt < retries - 1:
                    wait = RETRY_BACKOFF ** (attempt + 1)
                    logger.info(
                        "segment[%s] retry in %ds (attempt %d/%d, resuming from +%d): %s",
                        label, wait, attempt + 1, retries, written, sanitize_url(url),
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(
            f"Failed to download segment [{label}] after {retries} attempts: {url}"
        ) from last_error


# ---------------------------------------------------------------------------
# Split download
# ---------------------------------------------------------------------------
async def download_split_entry(
    client: httpx.AsyncClient,
    entry: dict,
    output_path: str,
    global_semaphore: asyncio.Semaphore,
    args: argparse.Namespace,
) -> bool:
    """Download a split entry. Returns True on success."""
    urls = entry["urls"]
    total_size = sum(entry["part-sizes"])

    # Compute offsets
    offsets: list[int] = []
    acc = 0
    for ps in entry["part-sizes"]:
        offsets.append(acc)
        acc += ps

    logger.debug(
        "split parts: %s",
        [
            {"i": i, "url": sanitize_url(u), "offset": o, "size": s}
            for i, (u, o, s) in enumerate(zip(urls, offsets, entry["part-sizes"]))
        ],
    )

    # Prepare output file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    state = DownloadState(output_path)
    if not state.acquire():
        logger.error("Cannot acquire lock: %s", state.lock_path)
        return False

    write_lock = asyncio.Lock()

    try:
        with open(output_path, "w+b") as f:
            f.truncate(total_size)

        logger.info(
            "split: %s — %d parts, %d bytes total",
            entry["file"], len(urls), total_size,
        )

        # Progress bar
        pbar = tqdm(
            total=total_size, unit="iB", unit_scale=True,
            desc=f"split: {entry['file']}", leave=True,
            disable=args.no_progress,
        )

        download_ok = True
        try:
            async with asyncio.TaskGroup() as tg:
                for i, (url, offset, size) in enumerate(zip(urls, offsets, entry["part-sizes"])):
                    label = f"{entry.get('category','?')}/{entry['file']}#part{i}@off{offset}"
                    tg.create_task(
                        download_segment(
                            client, url, output_path, write_lock,
                            offset, size, None, global_semaphore,
                            pbar, MAX_RETRIES, label=label,
                        ),
                        name=f"split-{entry['file']}-part{i}",
                    )
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error("split part failed: %s: %s", entry["file"], exc)
            download_ok = False
        finally:
            pbar.close()

        if not download_ok:
            return False

        # Verify SHA256
        sha = entry.get("sha256")
        result = verify_sha256(output_path, sha)
        if result == "mismatch":
            logger.error("SHA256 mismatch: %s", output_path)
            return False
        if result == "unverified":
            logger.warning("SHA256 unverified: %s", output_path)

        logger.info("split: OK (%s) — %s", result, output_path)
        return True

    except Exception:
        logger.error("split failed: %s\n%s", entry["file"], traceback.format_exc())
        return False
    finally:
        state.release()


# ---------------------------------------------------------------------------
# Range download
# ---------------------------------------------------------------------------
async def download_range_entry(
    client: httpx.AsyncClient,
    entry: dict,
    output_path: str,
    global_semaphore: asyncio.Semaphore,
    args: argparse.Namespace,
) -> bool:
    """Download a range entry. Returns True on success."""
    urls = entry["urls"]

    # Determine sha256: YAML > API
    expected_sha = entry["sha256"]

    # Chunk size
    yaml_chunk = entry.get("chunk_size")
    if yaml_chunk is not None:
        chunk_size = parse_size(yaml_chunk)
    else:
        chunk_size = parse_size(args.chunk_size)

    # Build chunks: (start, end inclusive)
    chunks: list[tuple[int, int]] = []
    pos = 0
    while pos < entry["filesize"]:
        end = min(pos + chunk_size - 1, entry["filesize"] - 1)
        chunks.append((pos, end))
        pos = end + 1

    # Assign chunks to URLs round-robin
    assignments: list[tuple[tuple[int, int], str]] = []
    for i, chunk in enumerate(chunks):
        url = urls[i % len(urls)]
        assignments.append((chunk, url))

    # Prepare output file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    state = DownloadState(output_path)
    if not state.acquire():
        logger.error("Cannot acquire lock: %s", state.lock_path)
        return False

    write_lock = asyncio.Lock()

    try:
        with open(output_path, "w+b") as f:
            f.truncate(entry["filesize"])

        logger.info(
            "range: %s — %d bytes, %d chunks, %d URLs",
            entry["file"], entry["filesize"], len(chunks), len(urls),
        )

        pbar = tqdm(
            total=entry["filesize"], unit="iB", unit_scale=True,
            desc=f"range: {entry['file']}", leave=True,
            disable=args.no_progress,
        )

        download_ok = True
        try:
            async with asyncio.TaskGroup() as tg:
                for i, ((start, end), url) in enumerate(assignments):
                    size = end - start + 1
                    extra_headers = {
                        "Range": f"bytes={start}-{end}",
                        "Accept-Encoding": "identity",
                    }
                    tg.create_task(
                        download_segment(
                            client, url, output_path, write_lock,
                            start, size, extra_headers, global_semaphore,
                            pbar, MAX_RETRIES, expected_total=entry["filesize"],
                            label=f"{entry.get('category','?')}/{entry['file']}#chunk{i}@off{start}",
                        ),
                        name=f"range-{entry['file']}-chunk{i}",
                    )
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error("range chunk failed: %s: %s", entry["file"], exc)
            download_ok = False
        finally:
            pbar.close()

        if not download_ok:
            return False

        # Verify SHA256
        result = verify_sha256(output_path, expected_sha)
        if result == "mismatch":
            logger.error("SHA256 mismatch: %s", output_path)
            return False
        if result == "unverified":
            logger.warning("SHA256 unverified: %s", output_path)

        logger.info("range: OK (%s) — %s", result, output_path)
        return True

    except Exception:
        logger.error("range failed: %s\n%s", entry["file"], traceback.format_exc())
        return False
    finally:
        state.release()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Model downloader (asyncio)")
    p.add_argument(
        "--category", type=lambda x: [s.strip() for s in x.split(",") if s.strip()],
        default=[], help="Comma-separated category names to download"
    )
    p.add_argument("--list", action="store_true", help="List categories and exit")
    p.add_argument("--dry-run", action="store_true", help="Show plan without downloading")
    p.add_argument("--no-progress", action="store_true", help="Disable progress bars")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")

    g = p.add_argument_group("size / concurrency")
    g.add_argument(
        "--chunk-size", default=str(DEFAULT_CHUNK_SIZE), type=str,
        help=f"Default chunk size for range type (default: {DEFAULT_CHUNK_SIZE})"
    )
    g.add_argument(
        "--max-concurrent", default=DEFAULT_MAX_CONCURRENT, type=int,
        help=f"Max simultaneous download connections (default: {DEFAULT_MAX_CONCURRENT})"
    )
    return p


async def main_async(args: argparse.Namespace) -> int:
    """Main async entry point. Returns exit code."""


    # Load YAML
    try:
        yaml_data = load_and_validate_yaml(MODEL_DL_LIST)
    except Exception as e:
        logger.error("failed to load YAML: %s: %s", e.__class__.__name__, e)
        return 2

    # List mode
    if args.list:
        for cat in yaml_data.keys():
            files = yaml_data[cat]
            count = len(files)
            disabled_count = len([f for f in files if f.get("disabled", False)])
            logger.info("  %s: %d files (%d disabled)", cat, count, disabled_count)
        return 0

    # Filter out deselected categories and disabled files
    for k,v in yaml_data.items():
        active = [ f for f in v if not f.get("disabled", False) ]
        yaml_data[k] = active

    selected = {k: v for k,v in yaml_data.items() if k in args.category and len(v) > 0 }

    if len(selected) == 0:
        logger.info("nothing to be downloaded")
        return 0

    io_chunk_size = IO_CHUNK_SIZE

    # Parse chunk size
    try:
        default_chunk_size = parse_size(args.chunk_size)
    except ValueError as e:
        logger.error("Invalid size argument: %s", e)
        return 2

    if args.max_concurrent < 1:
        logger.error("--max-concurrent must be >= 1, got %d", args.max_concurrent)
        return 2
    max_streams = args.max_concurrent

    logger.info(
        "Config: chunk_size=%d MiB, max_concurrent=%d",
        default_chunk_size // (1024**2),
        max_streams,
    )

    # Phase1: getting file info only
    metadata_failures = 0
    metadata_ok: list[dict] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=max_streams, max_keepalive_connections=4),
    ) as client:
        # Resolve metadata to confirm downloads are possible (in parallel)
        async def _resolve_metadata(entry: dict) -> dict:
            if entry["type"] == "range":
                await validate_file_size_and_hash(client, entry)
                info = (
                    f"size={entry["filesize"]:,} "
                    f"sha={'available' if entry["sha256"] else 'unavailable'}"
                )
            else:
                await resolve_split_sizes(client, entry)
                entry["filesize"] = sum(entry['part-sizes'])
                info = f"size={entry['filesize']:,} parts={len(entry['part-sizes'])}"
            return entry, info

        all_entries = [entry for v in selected.values() for entry in v]
        results = await asyncio.gather(
            *[_resolve_metadata(e) for e in all_entries],
            return_exceptions=True,
        )

        for entry, result in zip(all_entries, results):
            if isinstance(result, Exception):
                metadata_failures += 1
                logger.error(
                    "  [%s] %s: %s — ERROR: %s",
                    entry["category"], entry["type"],
                    entry["file"], result,
                )
            else:
                _, info = result
                logger.info(
                    "  [%s] %s: %s (%s)",
                    entry["category"], entry["type"],
                    entry["file"], info,
                )
                metadata_ok.append(entry)

    if metadata_failures > 0:
        logger.error(
            "Metadata resolution failed for %d file(s). Aborting download.",
            metadata_failures,
        )
        return 1

    # Group metadata-ok entries by category (only these proceed to phase2)
    ok_by_cat: dict[str, list[dict]] = {}
    for entry in metadata_ok:
        ok_by_cat.setdefault(entry["category"], []).append(entry)

    # Resolve output paths for checking existing files
    to_download: list[dict] = []
    to_skip: list[dict] = []
    total_size = 0
    for cat, entries in ok_by_cat.items():
        for entry in entries:
            try:
                output_path = resolve_output_path(entry, cat)
                entry["_output_path"] = output_path
                if check_existing(output_path, entry):
                    to_skip.append(entry)
                else:
                    to_download.append(entry)
                    meta_path = output_path + DOWNLOAD_META_SUFFIX
                    if os.path.isfile(meta_path):
                        # Incomplete download from a previous run
                        logger.info("Try to resume previous run: %s", output_path)
                    total_size += entry.get("filesize", 0)
            except Exception as e:
                logger.error(traceback.format_exc())
                to_skip.append(entry)

    logger.info(
        "Summary: %d to download, %d to skip: total size = %.1f GiB",
        len(to_download), len(to_skip), total_size / (1024.0**3)
    )

    if not to_download:
        logger.info("Nothing to be downloaded.")
        return 0

    if args.dry_run:
        for dl in to_download:
            logger.info("downloaded: %s (%.1f GiB)", dl["_output_path"], dl["filesize"]/(1024.0**3))
        return 0

    # Phase2: download files

    semaphore = asyncio.Semaphore(max_streams)
    exit_code = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=max_streams, max_keepalive_connections=4),
    ) as client:
        # Download all entries concurrently
        async def _download_one(entry: dict) -> bool:
            output_path = entry["_output_path"]
            if entry["type"] == "split":
                return await download_split_entry(
                    client, entry, output_path, semaphore, args
                )
            elif entry["type"] == "range":
                return await download_range_entry(
                    client, entry, output_path, semaphore, args
                )
            return False

        results = await asyncio.gather(
            *[_download_one(e) for e in to_download],
            return_exceptions=True,
        )

        for entry, result in zip(to_download, results):
            if isinstance(result, Exception):
                logger.error("FAILED: %s — %s", entry["file"], traceback.format_exception(result))
                exit_code = 1
            elif not result:
                exit_code = 1

    # Final summary
    success = sum(1 for r in results if r is True)
    failed = len(results) - success
    logger.info(
        "Done: %d succeeded, %d failed, %d skipped",
        success, failed, len(to_skip),
    )
    return exit_code


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Setup file log
    log_dir = os.path.dirname(MODEL_DL_LOG)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(MODEL_DL_LOG)
    fh.setFormatter(
        logging.Formatter("[%(levelname)s] (%(asctime)s) %(message)s")
    )
    logger.addHandler(fh)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
