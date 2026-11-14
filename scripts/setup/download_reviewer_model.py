"""Download the outcome table's reviewer model through a Hugging Face mirror, verify it, and
print the vLLM command that serves it.

The `cloud` profile of notebooks/outcome_table.ipynb sends each review to a vLLM server, and
vLLM loads the model from the Hugging Face cache. This script:

1. reads the model id and pinned revision from the notebook's `cloud` profile, so the two
   cannot drift apart (Qwen/Qwen3.8-27B at 37561816..., about 55.6 GB in 18 shards);
2. downloads exactly that snapshot through https://hf-mirror.com (or --endpoint) into the
   Hugging Face cache, the same place `vllm serve` looks. On AutoDL that is
   /root/autodl-tmp/huggingface, the data disk, as in notebooks/hf_hub_endpoint.py.
   With a mirror, the run never contacts huggingface.co, which is unreachable from mainland
   China: every request and redirect is checked, a request to huggingface.co (or a subdomain)
   stops the run with an error, and the log lists every host contacted. hf-mirror.com serves
   the file list and small files itself and redirects the large weight files to Hugging
   Face's file CDN (hosts under hf.co, such as cas-bridge.xethub.hf.co), which is reachable
   from China; those hosts are allowed and logged;
3. checks every file's size and hash, the safetensors shards against the index, and that the
   config and tokenizer load offline from the cache;
4. prints the `vllm serve` command. Once that server is up, run the notebook.

Usage, from the project root with the project's environment:

    .venv/bin/python scripts/setup/download_reviewer_model.py            # download and verify
    .venv/bin/python scripts/setup/download_reviewer_model.py --dry-run  # plan only, no download

Re-running resumes file by file: finished files are kept and only the rest are downloaded. A
file cut off by Ctrl+C or a crash restarts from its beginning (huggingface_hub 1.x and later
do not reuse partial files; within one run they do resume a dropped connection). Each run's
full log (DEBUG level) goes to logs/download_reviewer_model_<time>.log.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.util
import json
import logging
import os
import platform
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = PROJECT_ROOT / "notebooks" / "outcome_table.ipynb"
HF_HELPER = PROJECT_ROOT / "notebooks" / "hf_hub_endpoint.py"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_ENDPOINT = "https://hf-mirror.com"
OFFICIAL_ENDPOINT = "https://huggingface.co"
AUTODL_DATA_DISK = Path("/root/autodl-tmp")
# Tokens the level cap leaves for the prompt (rubric, request, chat template); see
# configs/tracer_levels_base.yaml, packing.max_input_tokens.
PROMPT_ALLOWANCE = 1_710
GB = 1e9
COMMIT_HASH = re.compile(r"^[0-9a-f]{40}$")
# huggingface_hub 1.x+ downloads to a per-call temp file, <etag>.<8 hex>.incomplete, which is
# never reused; older versions resume a plain <etag>.incomplete instead.
UNIQUE_TEMP = re.compile(r"^[0-9a-f]+\.[0-9a-f]{8}\.incomplete$")
STALE_SECONDS = 60  # a live download writes continuously; an untouched temp file is an orphan

# The official hub: huggingface.co with every subdomain, and its short name hf.co. It is
# unreachable from mainland China, so through a mirror this script refuses to contact it.
HUB_DOMAIN = "huggingface.co"
# Hugging Face's file CDN: the subdomains of hf.co (cas-bridge.xethub.hf.co, cdn-lfs.hf.co,
# us.aws.cdn.hf.co, ...). hf-mirror.com redirects large files there, and they are reachable
# from mainland China, so they are allowed and logged.
FILE_CDN_DOMAIN = "hf.co"
PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

log = logging.getLogger("download_reviewer_model")
_blobs_dir: Path | None = None  # for cleaning up temp files on Ctrl+C


def is_official_host(host: str | None) -> bool:
    """The hub itself: huggingface.co, its subdomains, and the bare hf.co."""
    host = (host or "").lower().rstrip(".")
    return host == HUB_DOMAIN or host.endswith("." + HUB_DOMAIN) or host == FILE_CDN_DOMAIN


def is_file_cdn_host(host: str | None) -> bool:
    """Hugging Face's file CDN: a subdomain of hf.co."""
    return (host or "").lower().rstrip(".").endswith("." + FILE_CDN_DOMAIN)


def strip_query(url: str) -> str:
    return url.split("?", 1)[0]  # signed CDN links carry long tokens; keep them out of the log


class OfficialHubBlocked(RuntimeError):
    """A request was about to reach the official Hugging Face hub during a mirror download."""


class NetworkGuard:
    """Logs every host the run contacts and, when downloading through a mirror, refuses the
    official hub (huggingface.co); Hugging Face's file CDN, where a mirror sends large files, is
    allowed. Two layers: huggingface_hub's HTTP client, which sees every request and every
    redirect hop (also through a proxy), and DNS lookups, which catch any other Python code."""

    def __init__(self, block_official: bool):
        self.block_official = block_official
        self.requests: dict[str, int] = {}
        self.blocked: str | None = None
        self.lock = threading.Lock()

    def check(self, host: str | None, url: str | None = None, via: str = "http") -> None:
        host = (host or "").lower().rstrip(".")
        if self.block_official and is_official_host(host):
            with self.lock:
                self.blocked = self.blocked or (f"{host} ({strip_query(url)})" if url else host)
            log.error(f"BLOCKED {via} request to {host}{' for ' + strip_query(url) if url else ''}; "
                      f"this run uses only the mirror and the file CDN it redirects to")
            raise OfficialHubBlocked(f"refused to contact {host}, the official Hugging Face hub")
        if via == "dns":
            log.debug(f"dns lookup {host}")
            return
        with self.lock:
            first = host not in self.requests
            self.requests[host] = self.requests.get(host, 0) + 1
        if first:
            kind = " (Hugging Face's file CDN, where the mirror sends large files)" if is_file_cdn_host(host) else ""
            log.info(f"network: first request to {host}{kind} ({via})")
        log.debug(f"{via} request {strip_query(url) if url else host}")

    def install(self) -> bool:
        """Hook DNS and huggingface_hub's HTTP client. True if the HTTP hook could be installed."""
        real_getaddrinfo, guard = socket.getaddrinfo, self

        def getaddrinfo(host, *args, **kwargs):
            guard.check(host.decode() if isinstance(host, (bytes, bytearray)) else str(host), via="dns")
            return real_getaddrinfo(host, *args, **kwargs)

        socket.getaddrinfo = getaddrinfo
        import huggingface_hub

        setter = getattr(huggingface_hub, "set_client_factory", None)  # huggingface_hub 1.0 and later
        try:
            from huggingface_hub.utils import _http
            factory = getattr(_http, "default_client_factory", None)
        except ImportError:
            factory = None
        if setter is None or factory is None:
            return False

        def client_factory():
            client = factory()
            hooks = dict(client.event_hooks)  # httpx runs request hooks for each redirect hop too
            hooks["request"] = [*hooks.get("request", []), lambda request: guard.check(request.url.host, str(request.url))]
            client.event_hooks = hooks
            return client

        setter(client_factory)
        return True

    def summary(self) -> str:
        with self.lock:
            hosts = ", ".join(f"{host} ({count})" for host, count in sorted(self.requests.items())) or "none"
            official = any(is_official_host(host) for host in self.requests)
            cdn = sorted(host for host in self.requests if is_file_cdn_host(host))
        return (f"hosts contacted (requests): {hosts}; official Hugging Face hub ({HUB_DOMAIN}) "
                f"{'CONTACTED' if official else 'never contacted'}"
                + (f"; large files came from Hugging Face's file CDN ({', '.join(cdn)})" if cdn else ""))


_guard = NetworkGuard(block_official=False)


@dataclass
class RemoteFile:
    name: str
    size: int
    sha256: str | None  # LFS files: the sha256 of the content
    blob_id: str | None  # every file: the git blob sha1 (for LFS files, of the pointer instead)
    force: bool = False  # a cached copy exists but is wrong, so download it again from scratch


# --------------------------------------------------------------------------- formatting

def fmt_bytes(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= 0.1 * GB else f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{int(n):,} B"


def fmt_duration(seconds: float) -> str:
    if seconds == float("inf"):
        return "?"
    if seconds < 10:
        return f"{seconds:.1f}s"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s" if hours else f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def setup_logging(verbose: bool) -> Path:
    """Log to the terminal (INFO, or DEBUG with --verbose) and to a file (always DEBUG)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"download_reviewer_model_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    log.propagate = False
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    file = logging.FileHandler(path, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"))
    log.addHandler(console)
    log.addHandler(file)
    # huggingface_hub's own warnings (dropped connections it resumes, retries) go to the file too;
    # it already prints them to the terminal itself.
    logging.getLogger("huggingface_hub").addHandler(file)
    return path


# --------------------------------------------------------------------------- notebook config

def read_notebook_profile(profile: str, notebook: Path = NOTEBOOK) -> dict:
    """The notebook's PROFILES[profile], read from its source without running the notebook."""
    for cell in json.loads(notebook.read_text(encoding="utf-8"))["cells"]:
        source = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        if cell["cell_type"] != "code" or "PROFILES = {" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROFILES"
                                                    for t in node.targets):
                profiles = ast.literal_eval(node.value)
                if profile not in profiles:
                    raise ValueError(f"profile {profile!r} is not in {notebook.name} (has {sorted(profiles)})")
                return profiles[profile]
    raise ValueError(f"no `PROFILES = {{...}}` assignment found in {notebook}")


# --------------------------------------------------------------------------- environment

def load_hf_helper():
    spec = importlib.util.spec_from_file_location("hf_hub_endpoint", HF_HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_environment(args) -> dict:
    """Set the Hugging Face variables. Must run before huggingface_hub is imported."""
    endpoint = args.endpoint.rstrip("/")
    mirror = endpoint != OFFICIAL_ENDPOINT
    os.environ["HF_ENDPOINT"] = endpoint
    if mirror:
        # Xet transfers authenticate against huggingface.co itself; through a mirror, use plain HTTP.
        os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"  # this script logs progress itself
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")  # mirrors can be slow to start a stream
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        if os.environ.pop(name, None) not in (None, "", "0"):
            log.warning(f"{name} was set; ignoring it for this download")
    if args.hf_home:
        os.environ["HF_HOME"] = str(Path(args.hf_home).expanduser().resolve())

    default_cache = Path.home() / ".cache" / "huggingface"
    had_default = default_cache.exists() or default_cache.is_symlink()
    hf_home = load_hf_helper().configure_hf_cache()  # the project's rule: AutoDL data disk, else ~/.cache
    if not had_default and default_cache.is_symlink():
        log.info(f"linked {default_cache} -> {os.readlink(default_cache)} so tools without HF_HOME find the cache")
    return {"endpoint": endpoint, "mirror": mirror, "hf_home": hf_home}


def run_quiet(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def log_runtime() -> None:
    from importlib.metadata import PackageNotFoundError, version

    def package(name):
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    log.info(f"Python {platform.python_version()} ({sys.executable}) on {platform.platform()}, host {platform.node()}")
    log.info(f"huggingface_hub {package('huggingface_hub')}, transformers {package('transformers') or 'not installed'}, "
             f"hf_xet {package('hf_xet') or 'not installed'}")
    vllm = package("vllm")
    log.info(f"vLLM: {vllm or 'not installed in this environment'}; `vllm` on PATH: {shutil.which('vllm') or 'no'}")
    gpus = run_quiet(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader"])
    if gpus:
        for line in gpus.splitlines():
            log.info(f"GPU: {line}")
    else:
        log.warning("no NVIDIA GPU found (nvidia-smi failed); serving the model with vLLM needs the cloud GPU")
    if not vllm and not shutil.which("vllm"):
        log.warning("vLLM is not installed here; install it on the GPU machine before serving")


def disk_report(path: Path) -> tuple[Path, int]:
    probe = path
    while not probe.exists():
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    log.info(f"disk holding {path}: {fmt_bytes(usage.free)} free of {fmt_bytes(usage.total)} "
             f"({fmt_bytes(usage.used)} used)")
    return probe, usage.free


# --------------------------------------------------------------------------- network

def with_retries(what: str, call, retries: int, stop: threading.Event | None = None):
    for attempt in range(1, retries + 1):
        try:
            return call()
        except (KeyboardInterrupt, OfficialHubBlocked):
            raise
        except Exception as error:  # noqa: BLE001 - network errors come in many types
            if _guard.blocked:  # huggingface_hub may wrap the refusal in its own error; retrying cannot help
                raise OfficialHubBlocked(f"refused to contact {_guard.blocked}") from error
            if attempt == retries:
                log.error(f"{what}: attempt {attempt}/{retries} failed: {type(error).__name__}: {error}")
                raise
            wait = min(120, 5 * 2 ** (attempt - 1))
            log.warning(f"{what}: attempt {attempt}/{retries} failed: {type(error).__name__}: {error}; "
                        f"retrying in {wait}s")
            if stop is not None and stop.wait(wait):
                raise RuntimeError("stopped") from error
            if stop is None:
                time.sleep(wait)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # report each hop instead of following it blindly


def trace_redirects(url: str, max_hops: int = 6) -> list[tuple[int | None, str]]:
    """HEAD the URL and follow its redirects one hop at a time, each hop checked by the guard."""
    opener = urllib.request.build_opener(_NoRedirect)  # keeps the default proxy handling
    hops: list[tuple[int | None, str]] = []
    for _ in range(max_hops):
        _guard.check(urlsplit(url).hostname, url, via="probe")
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "cs536-model-download"})
        try:
            with opener.open(request, timeout=30) as response:
                hops.append((response.status, url))
                return hops
        except urllib.error.HTTPError as error:
            hops.append((error.code, url))
            location = error.headers.get("Location")
            if error.code not in (301, 302, 303, 307, 308) or not location:
                return hops
            url = urljoin(url, location)
    return hops


def probe_endpoint(endpoint: str, repo: str, revision: str, files: list[RemoteFile]) -> None:
    """Show where the mirror sends a plain file and a weight (LFS) file, hop by hop. A hop to the
    official hub raises OfficialHubBlocked before anything is downloaded; a hop to Hugging Face's
    file CDN is expected for the weight file and must answer from this machine."""
    lfs = min((f for f in files if f.sha256), key=lambda f: f.size, default=None)
    for name in ["config.json"] + ([lfs.name] if lfs else []):
        url = f"{endpoint}/{repo}/resolve/{revision}/{name}"
        started = time.monotonic()
        try:
            hops = trace_redirects(url)
        except (OSError, urllib.error.URLError) as error:
            log.warning(f"probe of {name} failed ({type(error).__name__}: {error}); the download will retry")
            continue
        path = " -> ".join(f"{status} {urlsplit(hop).hostname}" for status, hop in hops)
        log.info(f"probe {name}: {path} ({time.monotonic() - started:.2f}s)")
        if hops and hops[-1][0] != 200:
            log.warning(f"probe of {name} ended with HTTP {hops[-1][0]}")


def remote_files(api, repo: str, revision: str, token, retries: int) -> list[RemoteFile]:
    started = time.monotonic()
    info = with_retries("file list", lambda: api.model_info(repo, revision=revision, files_metadata=True,
                                                            token=token), retries)
    log.info(f"file list for {repo}@{revision[:12]} in {time.monotonic() - started:.2f}s: sha {info.sha}, "
             f"pipeline {info.pipeline_tag}, gated {info.gated}, private {info.private}, "
             f"last modified {info.last_modified}")
    if info.sha != revision:
        raise RuntimeError(f"the endpoint resolved revision {revision} to {info.sha}; refusing a different snapshot")
    files = []
    for sibling in info.siblings:
        lfs, sha256 = sibling.lfs, None
        if lfs is not None:
            sha256 = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
        if sibling.size is None:
            raise RuntimeError(f"the endpoint gave no size for {sibling.rfilename}")
        files.append(RemoteFile(sibling.rfilename, int(sibling.size), sha256, sibling.blob_id))
    try:
        main = api.model_info(repo, token=token).sha
        if main == revision:
            log.info("the pinned revision is also the repository's current main")
        else:
            log.info(f"note: main is now {main}; the notebook pins {revision}, which is what gets downloaded")
    except Exception as error:  # noqa: BLE001 - informational only
        log.debug(f"could not read main: {error}")
    return files


# --------------------------------------------------------------------------- cache and progress

def file_ok(path: Path, size: int) -> bool:
    return path.is_file() and path.stat().st_size == size


def partial_bytes(blobs: Path, resumable_only: bool = False) -> int:
    total = 0
    for part in blobs.glob("*.incomplete") if blobs.is_dir() else []:
        if resumable_only and UNIQUE_TEMP.match(part.name):
            continue
        try:
            total += part.stat().st_size
        except FileNotFoundError:  # renamed when it finished
            pass
    return total


def remove_temp_files(blobs: Path, older_than: float | None) -> int:
    """Delete huggingface_hub's per-call temp files (never reused) and return the bytes freed.
    With `older_than`, keep files written to within that many seconds (maybe another run's)."""
    freed = 0
    for part in blobs.glob("*.incomplete") if blobs.is_dir() else []:
        if not UNIQUE_TEMP.match(part.name):
            continue
        try:
            stat = part.stat()
            if older_than is not None and time.time() - stat.st_mtime < older_than:
                log.warning(f"{part.name} is still being written; another download may be running")
                continue
            part.unlink()
        except FileNotFoundError:
            continue
        freed += stat.st_size
        log.info(f"deleted unusable partial file {part.name} ({fmt_bytes(stat.st_size)})")
    return freed


class Progress:
    def __init__(self, files: list[RemoteFile], cached_bytes: int, blobs: Path, cached_files: int = 0):
        self.total = sum(f.size for f in files)
        self.count, self.done_files = len(files), cached_files
        self.cached, self.finished, self.blobs = cached_bytes, 0, blobs
        self.active: set[str] = set()
        self.lock = threading.Lock()

    def start(self, name):
        with self.lock:
            self.active.add(name)

    def finish(self, f: RemoteFile):
        with self.lock:
            self.active.discard(f.name)
            self.finished += f.size
            self.done_files += 1

    def fail(self, name):
        with self.lock:
            self.active.discard(name)

    def current(self) -> int:
        with self.lock:
            done = self.cached + self.finished
        return min(self.total, done + partial_bytes(self.blobs))


def monitor(progress: Progress, stop: threading.Event, every: float) -> None:
    started = last_time = time.monotonic()
    first = last = progress.current()
    while not stop.wait(every):
        now, current = time.monotonic(), progress.current()
        rate = (current - last) / max(now - last_time, 1e-9)
        average = (current - first) / max(now - started, 1e-9)
        eta = (progress.total - current) / average if average > 0 else float("inf")
        with progress.lock:
            active, done = sorted(progress.active), progress.done_files
        log.info(f"progress {fmt_bytes(current)} / {fmt_bytes(progress.total)} "
                 f"({100 * current / max(progress.total, 1):.1f}%), {rate / 1e6:.1f} MB/s now, "
                 f"{average / 1e6:.1f} MB/s average, ETA {fmt_duration(eta)}; files {done}/{progress.count} done; "
                 f"downloading: {', '.join(active) or '-'}")
        last_time, last = now, current


def download_all(todo: list[RemoteFile], ctx: dict, progress: Progress, args) -> list[str]:
    from huggingface_hub import hf_hub_download

    failed: list[str] = []
    stop = ctx["stop"]

    def download(f: RemoteFile) -> float:
        progress.start(f.name)
        started = time.monotonic()
        log.debug(f"start {f.name} ({fmt_bytes(f.size)}){' (replacing a bad cached copy)' if f.force else ''}")

        def attempt():
            path = hf_hub_download(ctx["repo"], f.name, revision=ctx["revision"], cache_dir=ctx["cache"],
                                   token=ctx["token"], force_download=f.force)
            size = Path(path).stat().st_size
            if size != f.size:
                f.force = True  # the next attempt must not trust this copy
                raise RuntimeError(f"downloaded {size:,} bytes, expected {f.size:,}")
            return path

        with_retries(f.name, attempt, args.retries, stop)
        return time.monotonic() - started

    # Small files first, so the config and tokenizer arrive (and mirror problems show) early.
    ordered = sorted(todo, key=lambda f: f.size)
    pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="dl")
    try:
        futures = {pool.submit(download, f): f for f in ordered}
        for future in as_completed(futures):
            f = futures[future]
            try:
                seconds = future.result()
            except OfficialHubBlocked:
                raise  # stops every download: the mirror is sending this run to the official hub
            except Exception as error:  # noqa: BLE001 - recorded and reported at the end
                progress.fail(f.name)
                failed.append(f.name)
                log.error(f"FAILED {f.name}: {type(error).__name__}: {error}")
                continue
            progress.finish(f)
            log.info(f"[{progress.done_files}/{progress.count}] {f.name}: {fmt_bytes(f.size)} in "
                     f"{fmt_duration(seconds)} ({f.size / max(seconds, 1e-9) / 1e6:.1f} MB/s)")
    except (KeyboardInterrupt, OfficialHubBlocked):
        stop.set()  # a `with` block would wait here for every running file to finish
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return failed


# --------------------------------------------------------------------------- verification

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(8 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    """Git's object id for a file: sha1 of "blob <size>\\0" followed by the content."""
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with open(path, "rb") as handle:
        while chunk := handle.read(8 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(f: RemoteFile, snapshot: Path, check_hash: bool) -> str | None:
    """None if the cached file is right, else what is wrong."""
    path = snapshot / f.name
    if not path.is_file():
        return "missing"
    size = path.stat().st_size
    if size != f.size:
        return f"size {size:,} bytes, expected {f.size:,}"
    if check_hash and f.sha256:
        actual = sha256_file(path)
        if actual != f.sha256:
            return f"sha256 {actual}, expected {f.sha256}"
    elif check_hash and f.blob_id:
        actual = git_blob_sha1(path)
        if actual != f.blob_id:
            return f"git blob sha1 {actual}, expected {f.blob_id}"
    return None


def verify_all(files: list[RemoteFile], snapshot: Path, check_hash: bool, workers: int) -> dict[str, str]:
    problems: dict[str, str] = {}
    started, checked = time.monotonic(), 0
    total = sum(f.size for f in files)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verify") as pool:
        futures = {pool.submit(verify_file, f, snapshot, check_hash): f for f in sorted(files, key=lambda f: -f.size)}
        for future in as_completed(futures):
            f = futures[future]
            problem = future.result()
            checked += f.size
            kind = "sha256" if f.sha256 else "git sha1"
            if problem:
                problems[f.name] = problem
                log.error(f"verify {f.name}: {problem}")
            else:
                level = logging.INFO if f.size >= 100e6 else logging.DEBUG
                log.log(level, f"verified {f.name} ({fmt_bytes(f.size)}, {kind if check_hash else 'size only'}); "
                               f"{100 * checked / max(total, 1):.0f}% checked")
    log.info(f"verification of {len(files)} files ({fmt_bytes(total)}) took {fmt_duration(time.monotonic() - started)}")
    return problems


def remove_cached(f: RemoteFile, snapshot: Path) -> None:
    path = snapshot / f.name
    blob = path.resolve() if path.is_symlink() else None
    for target in (path, blob):
        if target is not None and (target.exists() or target.is_symlink()):
            target.unlink()
            log.info(f"deleted bad cached copy {target}")


def check_safetensors(snapshot: Path) -> list[str]:
    """Every tensor in the index is in its shard's header, and no shard is truncated."""
    index_path = snapshot / "model.safetensors.index.json"
    if not index_path.is_file():
        return ["model.safetensors.index.json is missing"]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    weight_map: dict[str, str] = index["weight_map"]
    problems = []
    for shard in sorted(set(weight_map.values())):
        path = snapshot / shard
        if not path.is_file():
            problems.append(f"{shard} is missing")
            continue
        with open(path, "rb") as handle:
            header_len = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(header_len))
        tensors = {name: spec for name, spec in header.items() if name != "__metadata__"}
        missing = [name for name, file in weight_map.items() if file == shard and name not in tensors]
        if missing:
            problems.append(f"{shard} lacks {len(missing)} tensors from the index, e.g. {missing[0]}")
        end = max((spec["data_offsets"][1] for spec in tensors.values()), default=0)
        if 8 + header_len + end != path.stat().st_size:
            problems.append(f"{shard} is {path.stat().st_size:,} bytes but its header needs {8 + header_len + end:,}")
        log.debug(f"{shard}: {len(tensors)} tensors, header {header_len:,} bytes")
    if not problems:
        total = index.get("metadata", {}).get("total_size")
        log.info(f"safetensors: all {len(weight_map):,} tensors present in {len(set(weight_map.values()))} shards"
                 + (f", {fmt_bytes(total)} of weights" if total else ""))
    return problems


def model_window(snapshot: Path) -> int | None:
    """max_position_embeddings from config.json (in text_config for multimodal checkpoints)."""
    path = snapshot / "config.json"
    if not path.is_file():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    return (config.get("text_config") or {}).get("max_position_embeddings") or config.get("max_position_embeddings")


def load_check(repo: str, revision: str, cache: str, reviewer: dict) -> int | None:
    """Load the config and tokenizer offline from the cache, as vLLM will, and measure the
    prompt's overhead in tokens. The weights themselves are loaded by vLLM, not here."""
    from huggingface_hub import constants

    constants.HF_HUB_OFFLINE = True  # read the cache only: no request may leave the machine here
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from transformers import AutoConfig, AutoTokenizer
    except ImportError:
        log.warning("transformers is not installed; skipped the config and tokenizer load check")
        return None
    kwargs = {"revision": revision, "cache_dir": cache, "local_files_only": True}
    config = AutoConfig.from_pretrained(repo, **kwargs)
    log.info(f"config loads offline: {type(config).__name__}, model_type {config.model_type}, "
             f"architectures {getattr(config, 'architectures', None)}")
    tokenizer = AutoTokenizer.from_pretrained(repo, **kwargs)
    log.info(f"tokenizer loads offline: {type(tokenizer).__name__}, {len(tokenizer):,} tokens")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.review_outcomes.prompts import load_rubric, review_messages

    thinking = reviewer.get("enable_thinking")
    extra = {} if thinking is None else {"enable_thinking": thinking}
    ids = tokenizer.apply_chat_template(review_messages("", load_rubric()), tokenize=True,
                                        add_generation_prompt=True, **extra)
    ids = ids["input_ids"] if hasattr(ids, "keys") else ids
    overhead = len(ids)
    log.info(f"review prompt without context (rubric + request + chat template): {overhead:,} tokens "
             f"(the level cap allows {PROMPT_ALLOWANCE:,})")
    if overhead > PROMPT_ALLOWANCE:
        log.warning("the prompt is longer than the level cap assumes; the largest levels may not fit the window")
    return overhead


# --------------------------------------------------------------------------- main

def serve_command(repo: str, revision: str, window: int | None, reviewer: dict, hf_home: str,
                  endpoint: str = DEFAULT_ENDPOINT) -> str:
    port = urlsplit(reviewer["base_url"]).port or 8000
    env = [f"HF_HOME={hf_home}"]
    if os.environ.get("HF_HUB_CACHE"):
        env.append(f"HF_HUB_CACHE={os.environ['HF_HUB_CACHE']}")
    env.append("HF_HUB_OFFLINE=1")  # load only what is in the cache, with no network calls
    env.append(f"HF_ENDPOINT={endpoint}")  # if anything still goes online, it goes to the mirror
    command = ["vllm", "serve", repo, "--revision", revision, "--tokenizer-revision", revision,
               "--max-model-len", str(window or 262144), "--generation-config", "vllm", "--port", str(port)]
    return " ".join(env) + " " + shlex.join(command)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help=f"Hugging Face endpoint or mirror (default {DEFAULT_ENDPOINT}; {OFFICIAL_ENDPOINT} for the official hub)")
    parser.add_argument("--hf-home", help="Hugging Face home (default: $HF_HOME, else /root/autodl-tmp/huggingface "
                                          "on AutoDL, else ~/.cache/huggingface)")
    parser.add_argument("--profile", default="cloud", help="notebook profile whose reviewer to download (default cloud)")
    parser.add_argument("--workers", type=int, default=8, help="parallel file downloads (default 8)")
    parser.add_argument("--retries", type=int, default=8, help="attempts per file before giving up (default 8)")
    parser.add_argument("--include", nargs="+", metavar="GLOB",
                        help="download only matching files (testing); the result is not enough for vLLM")
    parser.add_argument("--dry-run", action="store_true", help="show the plan and checks, download nothing")
    parser.add_argument("--skip-hash", action="store_true", help="check sizes only, not sha256 (faster, weaker)")
    parser.add_argument("--progress-every", type=float, default=20, help="seconds between progress lines (default 20)")
    parser.add_argument("--margin-gb", type=float, default=2, help="free space to keep beyond the download (default 2)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show DEBUG lines in the terminal too")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(args.verbose)
    started = time.monotonic()
    commit = run_quiet(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"])
    log.info(f"log file {log_path}")
    log.info(f"command: {shlex.join([sys.executable, *sys.argv])}")
    log.info(f"project {PROJECT_ROOT}, code commit {commit or '?'}")

    # 1. What the notebook expects.
    profile = read_notebook_profile(args.profile)
    reviewer = profile["reviewer"]
    repo, revision = reviewer["model"], reviewer.get("model_revision")
    log.info(f"notebook {NOTEBOOK.relative_to(PROJECT_ROOT)}, profile {args.profile!r}: reviewer {repo} at revision "
             f"{revision}, backend {reviewer['backend']}, server {reviewer.get('base_url')}")
    if reviewer["backend"] != "openai":
        log.error(f"profile {args.profile!r} runs its reviewer in-process with transformers, which downloads "
                  f"{repo} by itself; this script is for the vLLM-served `cloud` reviewer")
        return 1
    if not revision or not COMMIT_HASH.match(revision):
        log.error(f"the notebook's model_revision {revision!r} is not a full commit hash; pin one first")
        return 1
    log.info(f"notebook limits: max_input_tokens {reviewer['max_input_tokens']:,}, max_tokens {reviewer['max_tokens']:,}, "
             f"enable_thinking {reviewer.get('enable_thinking')}")

    # 2. Where the files go, and through which endpoint.
    env = configure_environment(args)
    log_runtime()
    import huggingface_hub
    from huggingface_hub import HfApi, constants, snapshot_download

    global _guard
    _guard = NetworkGuard(block_official=env["mirror"])
    http_hook = _guard.install()
    cache = constants.HF_HUB_CACHE
    token = False if env["mirror"] else None  # never send a Hugging Face token to a third-party mirror
    log.info(f"endpoint {constants.ENDPOINT} ({'mirror' if env['mirror'] else 'official hub'}), "
             f"Xet {'off' if constants.HF_HUB_DISABLE_XET else 'on'}, HF_HOME {env['hf_home']}, hub cache {cache}")
    if constants.ENDPOINT.rstrip("/") != env["endpoint"]:
        log.error(f"huggingface_hub uses {constants.ENDPOINT}, not {env['endpoint']}; refusing to continue")
        return 1
    if env["mirror"]:
        if constants.HF_HUB_DISABLE_XET is not True:
            log.error("Xet is still enabled; its transfers bypass the mirror. Refusing to continue")
            return 1
        log.info(f"network policy: only {urlsplit(env['endpoint']).hostname} and the hosts it sends files from "
                 f"(Hugging Face's file CDN, *.{FILE_CDN_DOMAIN}, is allowed); any request to {HUB_DOMAIN} "
                 f"(or a subdomain) or the bare {FILE_CDN_DOMAIN} stops the run "
                 f"({'checked on every HTTP request and redirect, and on DNS' if http_hook else 'checked on DNS only: this huggingface_hub has no client hook'})")
    for name in PROXY_VARIABLES:
        if value := os.environ.get(name):
            log.info(f"proxy: {name} is set (host {urlsplit(value).hostname}); requests go through it")
            if env["mirror"] and not http_hook:
                log.warning("with a proxy and no HTTP hook, the DNS check cannot see the hosts requested")
    if os.environ.get("HF_TOKEN") or huggingface_hub.get_token():
        log.info("a Hugging Face token is configured" + ("; it is not sent to the mirror" if env["mirror"] else ""))
    repo_cache = Path(cache) / f"models--{repo.replace('/', '--')}"
    snapshot, blobs = repo_cache / "snapshots" / revision, repo_cache / "blobs"
    log.info(f"snapshot folder: {snapshot}")
    if AUTODL_DATA_DISK.is_dir() and not str(Path(cache).resolve()).startswith(str(AUTODL_DATA_DISK)):
        log.warning(f"the cache is not on AutoDL's data disk {AUTODL_DATA_DISK}; the system disk is usually too small")

    # 3. What the snapshot holds, where the mirror serves it from, and what is already here.
    files = remote_files(HfApi(endpoint=env["endpoint"]), repo, revision, token, args.retries)
    probe_endpoint(env["endpoint"], repo, revision, files)
    shards = [f for f in files if f.name.endswith(".safetensors")]
    log.info(f"snapshot: {len(files)} files, {fmt_bytes(sum(f.size for f in files))}, "
             f"{len(shards)} safetensors shards ({fmt_bytes(sum(f.size for f in shards))})")
    for f in sorted(files, key=lambda f: f.name):
        log.debug(f"  {f.size:>15,}  {f.name}  {'sha256 ' + f.sha256 if f.sha256 else 'git ' + str(f.blob_id)}")
    selected = files
    if args.include:
        selected = [f for f in files if any(fnmatch.fnmatch(f.name, pattern) for pattern in args.include)]
        log.warning(f"--include {' '.join(args.include)}: {len(selected)} of {len(files)} files; "
                    f"a partial snapshot is not enough for vLLM")
        if not selected:
            log.error("no file matches --include")
            return 1

    todo, cached_bytes = [], 0
    for f in selected:
        path = snapshot / f.name
        if file_ok(path, f.size):
            cached_bytes += f.size
            log.debug(f"cached: {f.name}")
        else:
            f.force = path.exists() or path.is_symlink()
            if f.force:
                log.warning(f"cached {f.name} has the wrong size ({path.stat().st_size if path.exists() else 'broken link'}); "
                            f"it will be downloaded again")
            todo.append(f)
    remaining = sum(f.size for f in todo)
    if not args.dry_run:
        freed = remove_temp_files(blobs, older_than=STALE_SECONDS)
        if freed:
            log.info(f"freed {fmt_bytes(freed)} of partial files left by an interrupted run (they cannot be resumed)")
    resumable = partial_bytes(blobs, resumable_only=True)
    log.info(f"already cached: {len(selected) - len(todo)} files ({fmt_bytes(cached_bytes)}); to download: "
             f"{len(todo)} files ({fmt_bytes(remaining)})"
             + (f", of which {fmt_bytes(resumable)} can resume from an earlier run" if resumable else ""))

    # 4. Room on the disk.
    _, free = disk_report(Path(cache))
    needed = max(remaining - resumable, 0) + args.margin_gb * GB
    if todo and free < needed:
        message = (f"not enough space: {fmt_bytes(free)} free, {fmt_bytes(needed)} needed "
                   f"(download plus {args.margin_gb:g} GB margin); free space or pass --hf-home on a bigger disk")
        if not args.dry_run:
            log.error(message)
            return 1
        log.warning(message)

    window = model_window(snapshot)
    if args.dry_run:
        log.info("dry run: nothing downloaded")
        log.info(f"network: {_guard.summary()}")
        log.info("after the download, serve with:\n    "
                 + serve_command(repo, revision, window, reviewer, env["hf_home"], env["endpoint"]))
        return 0

    # 5. Download.
    if todo:
        global _blobs_dir
        _blobs_dir = blobs
        stop = threading.Event()
        progress = Progress(selected, cached_bytes, blobs, cached_files=len(selected) - len(todo))
        watcher = threading.Thread(target=monitor, args=(progress, stop, args.progress_every), name="progress",
                                   daemon=True)
        watcher.start()
        ctx = {"repo": repo, "revision": revision, "cache": cache, "token": token, "stop": stop}
        download_started = time.monotonic()
        log.info(f"downloading {len(todo)} files with {args.workers} workers, {args.retries} attempts each")
        try:
            failed = download_all(todo, ctx, progress, args)
        finally:
            stop.set()
        seconds = time.monotonic() - download_started
        log.info(f"download phase took {fmt_duration(seconds)} "
                 f"({(remaining - resumable) / max(seconds, 1e-9) / 1e6:.1f} MB/s average)")
        if failed:
            log.error(f"{len(failed)} files failed: {', '.join(failed)}. Re-run the script to resume them.")
            return 1
    else:
        log.info("nothing to download")

    # 6. Verify: hashes, then the shards against the index, then how vLLM resolves the folder.
    log.info(f"verifying {len(selected)} files ({'size only' if args.skip_hash else 'size and hash'})")
    workers = max(1, min(8, os.cpu_count() or 1))
    problems = verify_all(selected, snapshot, not args.skip_hash, workers)
    if problems:
        log.warning(f"{len(problems)} files failed verification; downloading them again once")
        redo = [f for f in selected if f.name in problems]
        for f in redo:
            remove_cached(f, snapshot)
            f.force = True
        progress = Progress(redo, 0, blobs)
        ctx = {"repo": repo, "revision": revision, "cache": cache, "token": token, "stop": threading.Event()}
        failed = download_all(redo, ctx, progress, args)
        problems = verify_all(redo, snapshot, not args.skip_hash, workers) if not failed else {n: "failed" for n in failed}
        if problems:
            log.error(f"still wrong after a fresh download: {', '.join(problems)}")
            return 1

    complete = len(selected) == len(files)
    if complete:
        structure = check_safetensors(snapshot)
        for problem in structure:
            log.error(f"safetensors: {problem}")
        if structure:
            return 1
    resolved = Path(snapshot_download(repo, revision=revision, cache_dir=cache, local_files_only=True))
    if resolved.resolve() != snapshot.resolve():
        log.error(f"huggingface_hub resolves {repo}@{revision[:12]} to {resolved}, not {snapshot}")
        return 1
    log.info(f"offline lookup finds the snapshot, as vLLM will: {resolved}")
    window = model_window(snapshot)
    if (snapshot / "tokenizer.json").is_file() and (snapshot / "config.json").is_file():
        overhead = load_check(repo, revision, cache, reviewer)
        if overhead is not None and window:
            budget = reviewer["max_input_tokens"] + overhead + reviewer["max_tokens"]
            fits = budget <= window
            (log.info if fits else log.warning)(
                f"window check: {reviewer['max_input_tokens']:,} level cap + {overhead:,} prompt + "
                f"{reviewer['max_tokens']:,} review = {budget:,} tokens, window {window:,}: "
                f"{'fits' if fits else 'DOES NOT FIT'}")

    # 7. Next steps.
    log.info(f"network: {_guard.summary()}")
    log.info(f"total time {fmt_duration(time.monotonic() - started)}; full log in {log_path}")
    if not complete:
        log.warning("partial snapshot (--include) verified; run without --include for the full model")
        return 0
    log.info(f"READY: {repo}@{revision} is complete and verified in {snapshot}")
    log.info("next, on this machine, start the reviewer server and leave it running "
             "(the notebook expects it at " + reviewer["base_url"] + "):\n    "
             + serve_command(repo, revision, window, reviewer, env["hf_home"], env["endpoint"]))
    log.info(f"when it is up, `curl -s {reviewer['base_url'].rstrip('/')}/models` lists {repo}; then run "
             f"notebooks/outcome_table.ipynb (OUTCOME_PROFILE=cloud, the default). The judge model is still "
             f"undecided, so the notebook stops at its judge cell until one is set; the reviews run before that.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        if _blobs_dir is not None:
            remove_temp_files(_blobs_dir, older_than=None)  # os._exit skips the downloads' own cleanup
        log.warning("interrupted; finished files are kept, and re-running the script downloads the rest")
        logging.shutdown()
        os._exit(130)  # running download threads cannot be cancelled mid-file
    except OfficialHubBlocked as error:
        if _blobs_dir is not None:
            remove_temp_files(_blobs_dir, older_than=None)
        endpoint = os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT)
        log.error(f"stopped: {error}. The mirror sent this request on to {HUB_DOMAIN}, which this run never "
                  f"contacts. Check that {endpoint} serves files from this machine (`curl -I {endpoint}`), or pass "
                  f"another mirror with --endpoint. Finished files are kept.")
        log.info(f"network: {_guard.summary()}")
        logging.shutdown()
        os._exit(1)  # running download threads cannot be cancelled mid-file
    except Exception:  # noqa: BLE001 - put the traceback in the log file too
        log.exception("failed")
        code = 1
    if code:
        log.info(f"network: {_guard.summary()}")
    sys.exit(code)
