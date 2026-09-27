"""Pick a reachable Hugging Face Hub endpoint and apply it to huggingface_hub."""

from __future__ import annotations

import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

AUTODL_HF_HOME = Path("/root/autodl-tmp/huggingface")
HF_HUB_URL = "https://huggingface.co"
HF_MIRROR_URL = "https://hf-mirror.com"
CONNECT_TIMEOUT_S = 5


def _hub_reachable(base_url: str) -> bool:
    probe = f"{base_url.rstrip('/')}/"
    try:
        req = urllib.request.Request(probe, method="HEAD")
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (OSError, urllib.error.URLError, socket.timeout, TimeoutError):
        return False


def _using_mirror(endpoint: str) -> bool:
    return endpoint.rstrip("/") != HF_HUB_URL.rstrip("/")


def _apply_xet_policy(endpoint: str) -> None:
    """Xet/CAS auth is tied to huggingface.co; mirrors need plain HTTP downloads."""
    if _using_mirror(endpoint):
        os.environ["HF_HUB_DISABLE_XET"] = "1"
    mod_name = "huggingface_hub.constants"
    if mod_name not in sys.modules:
        return
    hf_constants = sys.modules[mod_name]
    if _using_mirror(endpoint):
        hf_constants.HF_HUB_DISABLE_XET = True


def _patch_hf_constants(endpoint: str) -> None:
    """Refresh huggingface_hub.constants if the package was already imported."""
    endpoint = endpoint.rstrip("/")
    mod_name = "huggingface_hub.constants"
    if mod_name not in sys.modules:
        return
    hf_constants = sys.modules[mod_name]
    hf_constants.ENDPOINT = endpoint
    hf_constants.HUGGINGFACE_CO_URL_TEMPLATE = (
        endpoint + "/{repo_id}/resolve/{revision}/{filename}"
    )
    host = urlsplit(endpoint).hostname
    hosts = set(hf_constants.HF_URL_HOSTS)
    if host:
        hosts.add(host.lower())
    hf_constants.HF_URL_HOSTS = frozenset(hosts)


def configure_hf_cache() -> str:
    """Use AutoDL data disk for model cache when available."""
    if explicit := os.environ.get("HF_HOME"):
        return explicit.rstrip("/")

    autodl_tmp = AUTODL_HF_HOME.parent
    if autodl_tmp.is_dir() and os.access(autodl_tmp, os.W_OK):
        AUTODL_HF_HOME.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(AUTODL_HF_HOME)
        default_link = Path.home() / ".cache" / "huggingface"
        if not default_link.exists():
            default_link.parent.mkdir(parents=True, exist_ok=True)
            default_link.symlink_to(AUTODL_HF_HOME)
        return str(AUTODL_HF_HOME)

    fallback = Path.home() / ".cache" / "huggingface"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def configure_hf_hub_endpoint() -> str:
    """Return the active hub URL and ensure huggingface_hub uses it."""
    configure_hf_cache()
    if explicit := os.environ.get("HF_ENDPOINT"):
        endpoint = explicit.rstrip("/")
    elif _hub_reachable(HF_HUB_URL):
        endpoint = HF_HUB_URL.rstrip("/")
    else:
        endpoint = HF_MIRROR_URL.rstrip("/")
        os.environ["HF_ENDPOINT"] = endpoint

    _apply_xet_policy(endpoint)
    _patch_hf_constants(endpoint)
    return endpoint
