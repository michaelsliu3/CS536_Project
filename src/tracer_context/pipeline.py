"""Setup checks, pinned downloads, run lock and logging for scripts/dataset/build_dataset.py."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# AACR-Bench's public positive samples, pinned (196 PRs).
AACR_COMMIT = "ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df"
AACR_FILE = "positive_samples.json"
AACR_URL = f"https://raw.githubusercontent.com/alibaba/aacr-bench/{AACR_COMMIT}/dataset/{AACR_FILE}"
AACR_SHA256 = "02c8561f1c3994a26f427d479b2a3f6e6c8e6858161fbc903b45248fa2cb2097"

# codebase-memory-mcp release builds, pinned with the SHA-256 values from the release's checksums.txt.
TOOL_VERSION = "0.12.1"
TOOL_URL = "https://github.com/DeusData/codebase-memory-mcp/releases/download/v{version}/{asset}"
_DARWIN_ARM = ("codebase-memory-mcp-darwin-arm64.tar.gz",
               "9bfcbe38414b47969a3e1d1c1999f5357409387c86fa3d24221b5458267660d2")
_LINUX_ARM = ("codebase-memory-mcp-linux-arm64-portable.tar.gz",
              "ed0f1f548cd79b121c401c2e9f7841c189a001af26d3768368088c24f6a53533")
TOOL_ASSETS = {
    ("Darwin", "arm64"): _DARWIN_ARM,
    ("Darwin", "x86_64"): ("codebase-memory-mcp-darwin-amd64.tar.gz",
                           "ca6e64397a8ae80cec898a8205a6aa16750ac798acc05a0c3ed983837944a581"),
    ("Linux", "x86_64"): ("codebase-memory-mcp-linux-amd64-portable.tar.gz",
                          "8c3d0341064301e78ecc84cf7348d4e2c6562a0f57448243ea560aaa34de36c1"),
    ("Linux", "aarch64"): _LINUX_ARM,
    ("Linux", "arm64"): _LINUX_ARM,
}
TOKENIZER_FILES = ["tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "chat_template.jinja",
                   "config.json"]

# What the builder writes in its root folder.
OUTPUT_FILE = "context_levels.jsonl.gz"
LOCK_FILE = ".build.lock"
WORK_DIR = ".work"   # repos/ (clones), tmp/ (tool caches), tools/ (a downloaded tool)
LOG_DIR = "logs"     # build_<time>.log
_GITIGNORE_TEXT = "# Dataset build output (scripts/dataset/build_dataset.py): keep out of git.\n*\n"
# The inspection runner's default work folder (scripts/tracer/run_tracer_batch.py --work), under the project root.
RUNNER_WORK = Path("data") / "aacr_tracer"


class SetupError(RuntimeError):
    """A prerequisite is missing; the message says how to fix it."""


# ---------- logging ----------

def setup_logging(log_dir: Path, verbose: bool = False) -> tuple[logging.Logger, Path]:
    """Log to the terminal (INFO, or DEBUG with verbose) and to logs/build_<time>.log (DEBUG)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"build_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    logger = logging.getLogger("build_dataset")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    file = logging.FileHandler(path, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(console)
    logger.addHandler(file)
    return logger, path


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


class Progress:
    """Per-entry progress lines with counts and an estimate of the time left."""

    def __init__(self, logger: logging.Logger, total: int, to_build: int):
        self.log, self.total, self.to_build = logger, total, to_build
        self.position, self.durations = 0, []
        self.counts = {"ok": 0, "failed": 0, "skipped": 0}

    def eta(self) -> str:
        remaining = self.to_build - len(self.durations)
        if not self.durations or remaining <= 0:
            return "?"
        return format_duration(sum(self.durations) / len(self.durations) * remaining)

    def __call__(self, record: dict, outcome: str, seconds: float) -> None:
        self.position += 1
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        head = f"[{self.position}/{self.total}] {record['id']} ({record.get('language')})"
        if outcome == "skipped":
            self.log.debug(f"{head} already built, skipped")
            return
        self.durations.append(seconds)
        tail = (f"{self.counts['ok']} built, {self.counts['failed']} failed, "
                f"{self.counts['skipped']} skipped; about {self.eta()} left")
        if outcome == "ok":
            tokens = [level["input_tokens"] for level in record["levels"].values()]
            self.log.info(f"{head} ok in {format_duration(seconds)}: {len(tokens)} levels, "
                          f"{min(tokens):,}-{max(tokens):,} input tokens | {tail}")
            for warning in record.get("warnings", []):
                self.log.warning(f"{record['id']}: {warning}")
        else:
            self.log.error(f"{head} FAILED at {record.get('failed_stage')} after {format_duration(seconds)}: "
                           f"{record.get('error', '')[:300]} | {tail}")
            if record.get("traceback"):
                self.log.debug(f"{record['id']} traceback:\n{record['traceback']}")


# ---------- the run lock ----------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class BuildLock:
    """One build per root: <root>/.build.lock holds the running build's pid. A lock left by a
    crashed build (its pid no longer running on this host) is taken over."""

    def __init__(self, root: Path, logger: logging.Logger | None = None):
        self.path, self.log = Path(root) / ".build.lock", logger
        self.held = False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    info = {}
                pid, host = info.get("pid"), info.get("host")
                if isinstance(pid, int) and host == socket.gethostname() and _pid_alive(pid):
                    raise SetupError(f"another build (pid {pid}, started {info.get('started')}) is running on this "
                                     f"folder; stop it first or wait for it to finish ({self.path})")
                if self.log:
                    self.log.warning(f"removing a stale lock from a build that is no longer running: {info}")
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                           "started": datetime.now().isoformat(timespec="seconds")}, handle)
            self.held = True
            return self
        raise SetupError(f"could not take the build lock {self.path}")

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False
        return False


# ---------- checks and downloads ----------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, expected_sha256: str, logger: logging.Logger, timeout: float = 600) -> Path:
    """Download to a temporary name, verify the SHA-256, then move into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    logger.info(f"downloading {url}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, open(tmp, "wb") as handle:
            shutil.copyfileobj(response, handle)
    except OSError as error:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"download failed ({error}): {url}") from None
    actual = sha256_file(tmp)
    if actual != expected_sha256:
        tmp.unlink(missing_ok=True)
        raise SetupError(f"checksum mismatch for {url}: expected {expected_sha256}, got {actual}")
    os.replace(tmp, dest)
    return dest


def ensure_dataset_file(root: Path, logger: logging.Logger, allow_download: bool = True) -> Path:
    """<root>/positive_samples.json; downloaded at the pinned AACR commit if missing."""
    path = Path(root) / AACR_FILE
    if path.is_file():
        actual = sha256_file(path)
        if actual != AACR_SHA256:
            logger.warning(f"{path} differs from the pinned AACR release ({actual[:12]} vs {AACR_SHA256[:12]}); "
                           "using it as is")
        return path
    if not allow_download:
        raise SetupError(f"{path} is missing (download disabled); get it from {AACR_URL}")
    return download(AACR_URL, path, AACR_SHA256, logger)


def tool_version(binary: Path) -> str:
    try:
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    except OSError as error:
        raise SetupError(f"cannot run {binary}: {error}") from None
    if result.returncode != 0:
        raise SetupError(f"{binary} --version failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def ensure_tool(configured: Path, tools_dir: Path, logger: logging.Logger, allow_download: bool = True) -> Path:
    """The codebase-memory-mcp binary: the configured one if present, else the pinned release
    build for this platform, downloaded and checksum-verified into tools_dir."""
    if Path(configured).is_file():
        binary = Path(configured)
    else:
        key = (platform.system(), platform.machine())
        if key not in TOOL_ASSETS:
            raise SetupError(f"no pinned codebase-memory-mcp build for {key}; install v{TOOL_VERSION} and pass --tool")
        binary = Path(tools_dir) / f"codebase-memory-mcp-{TOOL_VERSION}" / "codebase-memory-mcp"
        if not binary.is_file():
            if not allow_download:
                raise SetupError(f"codebase-memory-mcp not found at {configured} (download disabled); pass --tool")
            asset, sha = TOOL_ASSETS[key]
            archive = download(TOOL_URL.format(version=TOOL_VERSION, asset=asset), Path(tools_dir) / asset, sha, logger)
            with tarfile.open(archive) as tar:
                members = [m for m in tar.getmembers() if m.isfile() and not m.name.startswith(("/", ".."))
                           and ".." not in Path(m.name).parts]
                tar.extractall(binary.parent, members=members, filter="data")
            archive.unlink(missing_ok=True)
            (binary.parent / "install.sh").unlink(missing_ok=True)  # never used: it edits AI client configs
            binary.chmod(0o755)
    version = tool_version(binary)
    if TOOL_VERSION not in version:
        logger.warning(f"{binary} reports {version!r}, not v{TOOL_VERSION}; results may differ from other builds")
    return binary


def ensure_tokenizer(name: str, logger: logging.Logger, allow_download: bool = True) -> None:
    """The reviewer's tokenizer, cached locally (tokenizer files only, never weights)."""
    if name == "approx":
        return
    from transformers import AutoTokenizer

    try:
        AutoTokenizer.from_pretrained(name, local_files_only=True)
        return
    except Exception as error:  # noqa: BLE001 - any load failure means "not cached"
        if not allow_download:
            raise SetupError(f"tokenizer {name} is not cached (download disabled): {type(error).__name__}") from None
    from huggingface_hub import snapshot_download

    logger.info(f"downloading tokenizer files for {name} (no weights)")
    try:
        snapshot_download(name, allow_patterns=TOKENIZER_FILES)
        AutoTokenizer.from_pretrained(name, local_files_only=True)
    except Exception as error:  # noqa: BLE001
        raise SetupError(f"could not download tokenizer {name}: {type(error).__name__}: {error}") from None


def require_command(name: str, purpose: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SetupError(f"`{name}` is not installed or not on PATH; it is needed to {purpose}")
    return path


def require_gh_login() -> None:
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise SetupError("GitHub CLI is not logged in (needed for PR titles and descriptions); run `gh auth login`")


def require_tool_idle() -> None:
    """The tool allows one active cache at a time, so no other session may be running."""
    result = subprocess.run(["pgrep", "-fl", "codebase-memory-mcp"], capture_output=True, text=True)
    running = [line for line in result.stdout.splitlines() if line.strip()]
    if running:
        raise SetupError("codebase-memory-mcp is already running (its UI or another build?); stop it first:\n  "
                         + "\n  ".join(running[:5]))


def require_runner_idle() -> None:
    """The inspection runner's clones must not be deleted while it uses them."""
    result = subprocess.run(["pgrep", "-fl", "run_tracer_batch.py"], capture_output=True, text=True)
    running = [line for line in result.stdout.splitlines() if line.strip()]
    if running:
        raise SetupError("scripts/tracer/run_tracer_batch.py is running; stop it before deleting its clones:\n  "
                         + "\n  ".join(running[:5]))


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def require_free_disk(path: Path, min_free_gb: float) -> float:
    free = free_gb(path)
    if free < min_free_gb:
        raise SetupError(f"only {free:.1f} GB free at {path}; need at least {min_free_gb:g} GB (--min-free-gb)")
    return free


def ensure_ignored(root: Path, project_root: Path, logger: logging.Logger) -> None:
    """Keep the dataset out of git: if root is inside the project and not already ignored,
    write <root>/.gitignore containing `*`."""
    root, project_root = Path(root).resolve(), Path(project_root).resolve()
    if not root.is_relative_to(project_root):
        return
    probe = root / OUTPUT_FILE
    ignored = subprocess.run(["git", "-C", str(project_root), "check-ignore", "-q", str(probe)]).returncode == 0
    if not ignored:
        (root / ".gitignore").write_text(_GITIGNORE_TEXT, encoding="utf-8")
        logger.info(f"{root} was not ignored by git; wrote {root / '.gitignore'}")


# ---------- cleaning a build (scripts/dataset/clean_dataset.py) ----------

def clean_targets(root: Path, logs: bool = False, everything: bool = False) -> list[Path]:
    """The paths scripts/dataset/clean_dataset.py deletes so the next build starts over.

    Default: the built records (OUTPUT_FILE and leftover rewrite files) and the work folder's
    clones and tool caches; the AACR file, the logs and a downloaded tool stay, so a restart
    needs no downloads. `logs` adds the build logs. `everything` adds all else the builder
    wrote: the AACR file, the downloaded tool, the logs and its .gitignore. Only the builder's
    own file names are listed. A folder with no build files raises SetupError, so a mistyped
    root deletes nothing."""
    root = Path(root)
    work, log_dir = root / WORK_DIR, root / LOG_DIR
    build_logs = sorted(log_dir.glob("build_*.log")) if log_dir.is_dir() else []
    outputs = sorted(root.glob(f"{OUTPUT_FILE}*"))
    markers = outputs + build_logs + [p for p in (work, root / LOCK_FILE) if p.exists() or p.is_symlink()]
    if everything:
        markers += [p for p in (root / AACR_FILE,) if p.exists()]
    if not root.is_dir() or not markers:
        raise SetupError(f"{root} has no dataset build files ({OUTPUT_FILE}, {WORK_DIR}/, {LOG_DIR}/build_*.log); "
                         "nothing deleted")
    targets = list(outputs)
    if everything:
        targets += [work] if work.exists() or work.is_symlink() else []
    else:
        targets += [p for p in (work / "repos", work / "tmp") if p.exists() or p.is_symlink()]
    if logs or everything:
        targets += build_logs
    if everything:
        targets += [p for p in (root / AACR_FILE,) if p.exists() or p.is_symlink()]
        ignore = root / ".gitignore"
        if ignore.is_file() and ignore.read_text(encoding="utf-8", errors="replace") == _GITIGNORE_TEXT:
            targets.append(ignore)  # only the one the builder wrote
    return targets


def runner_clones(work: Path) -> list[Path]:
    """Repository clones the inspection runner downloaded into <work>/repos: folders holding
    .git, never followed through a symlink. Its saved results in <work>/entries are not listed."""
    repos = Path(work) / "repos"
    if not repos.is_dir():
        return []
    return sorted(p for p in repos.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".git").exists())


def path_size(path: Path) -> int:
    """Bytes under path, without following symlinks."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size if path.exists() or path.is_symlink() else 0
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def remove_paths(root: Path, paths: list[Path]) -> None:
    """Delete the paths (folders recursively, symlinks themselves), then the builder's folders
    under root that this left empty, and root itself if it ends up empty."""
    root = Path(root)
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    for folder in (root / WORK_DIR, root / LOG_DIR, root):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass


def wait_seconds(seconds: float) -> None:  # separate so tests can replace it
    time.sleep(seconds)
