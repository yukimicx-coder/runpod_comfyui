"""Model downloader — asyncio-based, no threads, no temp files."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import traceback
from urllib.parse import parse_qs, urlparse

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
IO_CHUNK_SIZE = 64 * 1024 * 1024          # 64 MiB per recv chunk
DEFAULT_BUFFER_LIMIT = 512 * 1024 * 1024   # 512 MiB
DEFAULT_CHUNK_SIZE = 1 * 1024 * 1024 * 1024  # 1 GiB for range type
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
        filesize = entry.get("filesize")
        if filesize is not None and filesize != "":
            try:
                parse_size(filesize)
            except ValueError as e:
                errors.append(f"{tag} filesize: {e}")
    elif entry_type == "split":
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
    Raises SystemExit on validation errors.
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
    results are written to entry["filesize] and entry["sha256"]. Raises ValueError if size cannot be
    determined, or if provider-reported sizes conflict with the YAML value.
    """
    assert entry["type"] == "range"

    yaml_size = entry.get("filesize")
    yaml_sha = entry.get("sha256","").lower()

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

    if api_sizes:
        if any([s != api_sizes[0] for s in api_sizes]):
            raise ValueError(
                f"Provider size mismatch for {entry['file']}: "
                f"API reports different sizes {api_sizes}"
            )
        if yaml_size and yaml_size != api_sizes[0]:
            raise ValueError(
                f"Provider size mismatch for {entry['file']}: "
                f"YAML says {yaml_size}, API reports {api_sizes}"
            )

        entry["filesize"] = size
    else:
        header_sizes = []
        for url in entry["urls"]:
            size = await _head_content_length(client, url)
            if size:
                header_sizes.append(size)

        if not header_sizes:
            if yaml_size is None:
                raise ValueError(
                    f"Cannot determine total file size for {entry['file']} from header"
                )
        else:
            if any(s != header_sizes[0] for s in header_sizes):
                raise ValueError(
                    f"header sizes differ for {entry['file']}: {header_sizes}"
                )
            if yaml_size and yaml_size != header_sizes[0]:
                raise ValueError(
                    f"Provider size mismatch for {entry['file']}: "
                    f"YAML says {yaml_size}, Header reports {header_sizes[0]}"
                )

            entry["filesize"] = header_sizes[0]

    if api_shas:
        if any([h != api_shas[0] for h in api_shas]):
            raise ValueError(
                f"Provider hash mismatch for {entry['file']}: "
                f"API reports different hases {api_shas}"
            )
        if yaml_sha and yaml_sha != api_shas[0]:
            raise ValueError(
                f"Provider size mismatch for {entry['file']}: "
                f"YAML says {yaml_sha}, API reports {api_shas}"
            )

        entry["sha256"] = api_shas[0]

    if "sha256" not in entry:
        logger.warning(f"sha256 for {entry['file']} is not specified")
        entry["sha256"] = ""


async def resolve_split_sizes(
    client: httpx.AsyncClient, entry: dict
):
    """Resolve part sizes for a split entry.

    Returns list of part sizes in bytes.
    """
    part_sizes_raw = entry.get("part-sizes")
    if part_sizes_raw is not None:
        entry["part-sizes"] = [parse_size(ps) for ps in part_sizes_raw]
        return

    # HEAD fallback for each URL
    sizes: list[int] = []
    for url in entry["urls"]:
        cl = await _head_content_length(client, url)
        if cl is None:
            raise ValueError(
                f"Cannot determine part size for URL: {url} "
                f"(HEAD returned no Content-Length)"
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
) -> int:
    """Download a segment from url, writing at offset in output_path.

    Returns the number of bytes written.
    Raises on failure after all retries exhausted.
    """
    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(retries):
            received = 0
            try:
                headers: dict[str, str] = {}
                if extra_headers:
                    headers.update(extra_headers)
                # Disable compression so Content-Length and received bytes match
                headers.setdefault("Accept-Encoding", "identity")
                # Auth via header (never put tokens in the URL)
                headers.update(_auth_headers(url))

                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()

                    # Validate range response
                    if "Range" in headers:
                        if resp.status_code != 206:
                            raise ValueError(
                                f"Expected 206 for Range request, got {resp.status_code}"
                            )
                        cr = resp.headers.get("content-range", "")
                        # Format: bytes start-end/total
                        cr_match = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                        if not cr_match:
                            raise ValueError(f"Missing or invalid Content-Range: {cr!r}")
                        cr_start = int(cr_match.group(1))
                        cr_end = int(cr_match.group(2))
                        cr_total = int(cr_match.group(3))
                        if cr_start != offset:
                            raise ValueError(
                                f"Content-Range start mismatch: expected {offset}, got {cr_start}"
                            )
                        expected_chunk = cr_end - cr_start + 1
                        if expected_size is not None and expected_chunk != expected_size:
                            raise ValueError(
                                f"Content-Range chunk size mismatch: expected {expected_size}, got {expected_chunk}"
                            )
                        if expected_total is not None and cr_total != expected_total:
                            raise ValueError(
                                f"Content-Range total mismatch: expected {expected_total}, got {cr_total}"
                            )

                    # Stream to file with seek
                    received = 0
                    with open(output_path, "r+b") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=IO_CHUNK_SIZE):
                            if not chunk:
                                continue
                            chunk_len = len(chunk)
                            if expected_size is not None and received + chunk_len > expected_size:
                                raise ValueError(
                                    f"Received {received + chunk_len} bytes, "
                                    f"expected {expected_size} max"
                                )
                            async with write_lock:
                                f.seek(offset + received)
                                f.write(chunk)
                            received += chunk_len
                            if progress:
                                progress.update(chunk_len)

                    if expected_size is not None and received != expected_size:
                        raise ValueError(
                            f"Incomplete download: got {received}, expected {expected_size}"
                        )

                    return received  # success

            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as e:
                last_error = e
                logger.warning(
                    "Segment download error (attempt %d/%d) %s: %s",
                    attempt + 1, retries, sanitize_url(url), e,
                )
                if attempt < retries - 1:
                    wait = RETRY_BACKOFF ** (attempt + 1)
                    await asyncio.sleep(wait)

        raise RuntimeError(
            f"Failed to download segment after {retries} attempts: {url}"
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
                    tg.create_task(
                        download_segment(
                            client, url, output_path, write_lock,
                            offset, size, None, global_semaphore,
                            pbar, MAX_RETRIES,
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
        "--buffer-limit", default=str(DEFAULT_BUFFER_LIMIT), type=str,
        help=f"Max buffer in bytes (default: {DEFAULT_BUFFER_LIMIT})"
    )
    g.add_argument(
        "--max-concurrent", default=None, type=int,
        help="Override max concurrent streams (clamped by buffer limit)"
    )
    return p


async def main_async(args: argparse.Namespace) -> int:
    """Main async entry point. Returns exit code."""


    # Load YAML
    try:
        yaml_data = load_and_validate_yaml(MODEL_DL_LIST)
    except Exception as e:
        logger.error("failed to load YAML: %s: %s", e.__class__.__name__, e)
        return 1

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

    # Parse size params
    try:
        buffer_limit = parse_size(args.buffer_limit)
        default_chunk_size = parse_size(args.chunk_size)
    except ValueError as e:
        logger.error("Invalid size argument: %s", e)
        return 2

    # Calculate max streams from buffer limit
    max_streams_from_buffer = buffer_limit // (2 * io_chunk_size)
    if max_streams_from_buffer < 1:
        logger.error(
            "Buffer limit too small: %d bytes (need at least %d)",
            buffer_limit, 2 * io_chunk_size,
        )
        return 2

    if args.max_concurrent is not None:
        if args.max_concurrent < 1:
            logger.error("--max-concurrent must be >= 1, got %d", args.max_concurrent)
            return 2
        max_streams = min(args.max_concurrent, max_streams_from_buffer)
    else:
        max_streams = max_streams_from_buffer

    logger.info(
        "Config: buffer_limit=%d MiB, chunk_size=%d MiB, max_streams=%d",
        buffer_limit // (1024**2),
        default_chunk_size // (1024**2),
        max_streams,
    )

    # Phase1: getting file info only

    get_info_failures = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=max_streams, max_keepalive_connections=4),
    ) as client:
        # Resolve metadata to confirm downloads are possible
        for entry in [entry for v in selected.values() for entry in v]:
            try:
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
            except Exception as e:
                info = f"ERROR: {e}"
                logger.error(traceback.format_exc())
                get_info_failures += 1
            logger.info(
                "  [%s] %s: %s (%s)",
                entry["category"], entry["type"],
                entry["file"], info,
            )

    # Resolve output paths for checking existing files
    to_download: list[dict] = []
    to_skip: list[dict] = []
    total_size = 0
    for cat, entries in selected.items():
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
        return 3 if get_info_failures else 0

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
