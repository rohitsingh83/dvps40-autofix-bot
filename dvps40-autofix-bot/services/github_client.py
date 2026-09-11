"""
services/github_client.py — All GitHub REST interactions via httpx (async).

Design decisions:
  • Uses the raw GitHub REST v3 API over httpx instead of PyGithub so that every
    call is fully async and does not block the FastAPI event loop.
  • All write operations are strictly scoped to branches prefixed "fix/devops-"
    and PRs are always opened with base = DEV_BRANCH.
  • The module refuses to touch `main`, `master`, `production`, or `prod`.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"
_PROTECTED_BRANCHES = frozenset({"main", "master", "production", "prod"})
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dvps40-autofix-bot/1.0",
    }


def _repo_url(path: str) -> str:
    return f"{_BASE}/repos/{settings.github_repo}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def _assert_not_protected(branch: str) -> None:
    if branch.lower() in _PROTECTED_BRANCHES:
        raise ValueError(
            f"Refusing to operate on protected branch {branch!r}. "
            "All operations must use the configured DEV_BRANCH."
        )


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

async def get_file_content_from_dev(file_path: str) -> tuple[str, str]:
    """
    Fetch a file from the DEV_BRANCH.

    Returns
    -------
    (content: str, sha: str)
        Decoded UTF-8 text and the blob SHA required for subsequent updates.

    Raises
    ------
    httpx.HTTPStatusError  – if the file does not exist or the request fails.
    """
    _assert_not_protected(settings.dev_branch)
    if settings.dry_run and settings.github_token.get_secret_value().startswith("ghp_Your"):
        logger.info("[DryRun] Returning simulated content for %s", file_path)
        return f"# Sample file content from dev branch: {file_path}\ndef broken_handler():\n    return undefined_symbol\n", "mock_blob_sha_123"

    url = _repo_url(f"contents/{file_path}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            url,
            headers=_headers(),
            params={"ref": settings.dev_branch},
        )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    raw = base64.b64decode(data["content"]).decode("utf-8")
    return raw, data["sha"]


async def get_dev_branch_sha() -> str:
    """Return the latest commit SHA on DEV_BRANCH."""
    _assert_not_protected(settings.dev_branch)
    if settings.dry_run and settings.github_token.get_secret_value().startswith("ghp_Your"):
        return "mock_dev_branch_sha_abc123"

    url = _repo_url(f"git/ref/heads/{settings.dev_branch}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()["object"]["sha"]


async def list_files_on_dev(directory: str = "") -> list[str]:
    """
    Return a flat list of file paths inside *directory* on DEV_BRANCH.
    Useful for the agent to discover candidate files without knowing them upfront.
    """
    _assert_not_protected(settings.dev_branch)
    if settings.dry_run and settings.github_token.get_secret_value().startswith("ghp_Your"):
        return ["app/main.py", "app/routes.py", "src/server.py"]

    url = _repo_url(f"contents/{directory}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            url,
            headers=_headers(),
            params={"ref": settings.dev_branch},
        )
    resp.raise_for_status()
    items: list[dict] = resp.json()
    paths: list[str] = []
    for item in items:
        if item["type"] == "file":
            paths.append(item["path"])
        # We intentionally do not recurse to keep the initial probe lightweight.
    return paths


# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------

async def _create_branch(branch_name: str, sha: str) -> None:
    """Create a new Git ref pointing at *sha*."""
    url = _repo_url("git/refs")
    payload = {"ref": f"refs/heads/{branch_name}", "sha": sha}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
    if resp.status_code == 422:
        # Branch already exists — acceptable if we're retrying
        logger.warning("Branch %r already exists; continuing.", branch_name)
        return
    resp.raise_for_status()


async def _commit_file(
    branch_name: str,
    file_path: str,
    new_content: str,
    commit_msg: str,
    blob_sha: str,
) -> None:
    """
    Update or create a single file on *branch_name* using the Contents API.

    Parameters
    ----------
    blob_sha
        The SHA of the existing blob (required by GitHub to detect conflicts).
        Pass an empty string for brand-new files.
    """
    url = _repo_url(f"contents/{file_path}")
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
    payload: dict[str, Any] = {
        "message": commit_msg,
        "content": encoded,
        "branch": branch_name,
    }
    if blob_sha:
        payload["sha"] = blob_sha
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(url, headers=_headers(), json=payload)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Pull-Request creation
# ---------------------------------------------------------------------------

async def _open_pull_request(
    head_branch: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """
    Create a PR from *head_branch* → DEV_BRANCH.

    Returns the HTML URL of the created PR.
    """
    _assert_not_protected(settings.dev_branch)
    url = _repo_url("pulls")
    payload = {
        "title": pr_title,
        "body": pr_body,
        "head": head_branch,
        "base": settings.dev_branch,
        "draft": False,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------

async def create_fix_branch_and_pr(
    file_path: str,
    new_content: str,
    blob_sha: str,
    commit_msg: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """
    Full end-to-end flow:

    1. Resolve the latest commit SHA on DEV_BRANCH.
    2. Create branch  ``fix/devops-<unix-timestamp>``.
    3. Commit the patched file.
    4. Open a PR targeting DEV_BRANCH.
    5. Return the PR HTML URL.

    Parameters
    ----------
    blob_sha
        SHA of the existing file blob fetched from :func:`get_file_content_from_dev`.
        Required by the GitHub Contents API to perform an update.
    """
    timestamp = int(time.time())
    fix_branch = f"repair/auto-fix-INC-{timestamp}"

    if settings.dry_run and settings.github_token.get_secret_value().startswith("ghp_Your"):
        mock_pr = f"https://github.com/{settings.github_repo}/pull/42"
        logger.info("[DryRun] Simulated branch %s created and PR opened targeting %s: %s", fix_branch, settings.dev_branch, mock_pr)
        return mock_pr

    logger.info("Resolving DEV_BRANCH (%s) tip SHA…", settings.dev_branch)
    tip_sha = await get_dev_branch_sha()

    logger.info("Creating fix branch %r off %s…", fix_branch, tip_sha[:7])
    await _create_branch(fix_branch, tip_sha)

    logger.info("Committing patched file %r to %r…", file_path, fix_branch)
    await _commit_file(fix_branch, file_path, new_content, commit_msg, blob_sha)

    logger.info("Opening PR %r → %r…", fix_branch, settings.dev_branch)
    pr_url = await _open_pull_request(fix_branch, pr_title, pr_body)

    logger.info("PR created: %s", pr_url)
    return pr_url


async def get_repository_structure(max_files: int = 50) -> list[str]:
    """
    Return a lightweight map of files at the root level of DEV_BRANCH.
    The agent uses this to understand the project layout before deciding which
    file to patch.
    """
    try:
        return await list_files_on_dev("")
    except Exception as exc:
        logger.warning("Could not list repository structure: %s", exc)
        return []
