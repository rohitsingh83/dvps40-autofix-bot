"""
services/log_parser.py — Normalise raw webhook payloads from Vercel and Railway,
then extract actionable signals: stack traces, error codes, and affected files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class Platform(str, Enum):
    VERCEL = "vercel"
    RAILWAY = "railway"
    UNKNOWN = "unknown"


@dataclass
class ParsedError:
    """Structured representation of a deployment failure."""

    platform: Platform
    deployment_id: str
    project_name: str
    environment: str
    raw_log: str
    # Extracted fields
    error_type: Optional[str] = None       # e.g. "SyntaxError", "ModuleNotFoundError"
    error_message: Optional[str] = None    # first line of the error message
    stack_trace: Optional[str] = None      # full stack trace block
    affected_files: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    build_phase: Optional[str] = None      # "build" | "runtime" | "install"
    ts_errors: list[str] = field(default_factory=list)  # TypeScript diagnostic lines


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Python/Node exception types on a line by themselves or at start of message
_RX_ERROR_TYPE = re.compile(
    r"\b("
    r"SyntaxError|IndentationError|TabError|"
    r"ModuleNotFoundError|ImportError|"
    r"AttributeError|NameError|TypeError|ValueError|RuntimeError|"
    r"KeyError|IndexError|FileNotFoundError|PermissionError|"
    r"ZeroDivisionError|OverflowError|RecursionError|"
    r"AssertionError|NotImplementedError|StopIteration|"
    r"Error|Exception"               # generic fallbacks — listed last
    r")\b"
)

# Python traceback header
_RX_PY_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):", re.M)

# Node / JS error blocks (often start with "Error:" or "UnhandledPromiseRejection")
_RX_NODE_ERROR_START = re.compile(
    r"^(?:Error:|UnhandledPromiseRejection|Uncaught|node:internal)", re.M
)

# TypeScript diagnostic lines: src/foo.ts(12,5): error TS2304: ...
_RX_TS_ERROR = re.compile(
    r"(?P<file>[^\s]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>TS\d+):\s*(?P<msg>.+)"
)

# File paths referenced in stack traces (Python, Node, generic)
_RX_FILE_PATH = re.compile(
    r'(?:File "(?P<pyfile>[^"]+)"'           # Python:  File "foo/bar.py", line N
    r'|at .+ \((?P<jsfile>[^:)]+):\d+:\d+\)'  # Node:    at fn (foo/bar.js:1:2)
    r'|(?P<tsfile>[^\s"\']+\.tsx?):\d+)'       # TS reference without parens
)

# Exit code patterns from build runners
_RX_EXIT_CODE = re.compile(r"(?:exit(?:ed)? (?:with )?(?:code )?|exited with code )(\d+)", re.I)

# Build phase identifiers
_RX_BUILD_PHASE = re.compile(
    r"\b(install(?:ation)?|build(?:ing)?|deploy(?:ment)?|start(?:up)?|runtime)\b", re.I
)


# ---------------------------------------------------------------------------
# Platform-specific payload normalisation
# ---------------------------------------------------------------------------


def _normalise_vercel(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    """
    Extract (deployment_id, project_name, environment, raw_log) from a Vercel
    `deployment.error` webhook payload.

    Vercel sends a nested JSON object; the exact schema differs slightly between
    webhook versions but the fields below are stable.
    """
    deployment = payload.get("deployment", payload)  # handle both wrapper shapes
    deployment_id = (
        deployment.get("id")
        or deployment.get("deploymentId")
        or payload.get("id", "unknown")
    )
    project_name = (
        deployment.get("name")
        or deployment.get("projectName")
        or payload.get("name", "unknown")
    )
    environment = deployment.get("target") or payload.get("target", "preview")

    # Vercel may embed build logs directly or reference them via URL.
    # We concatenate all text-bearing fields we can find.
    log_parts: list[str] = []
    for key in ("error", "buildError", "buildLogs", "logs", "stderr", "output"):
        val = deployment.get(key) or payload.get(key)
        if isinstance(val, str):
            log_parts.append(val)
        elif isinstance(val, list):
            log_parts.extend(str(item) for item in val if item)
        elif isinstance(val, dict):
            for sub_val in val.values():
                if isinstance(sub_val, str):
                    log_parts.append(sub_val)

    raw_log = "\n".join(log_parts) or str(payload)
    return str(deployment_id), str(project_name), str(environment), raw_log


def _normalise_railway(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    """
    Extract (deployment_id, project_name, environment, raw_log) from a Railway
    `DEPLOYMENT_FAILED` webhook payload.
    """
    deployment_id = payload.get("deploymentId") or payload.get("id", "unknown")
    project_name = payload.get("projectName") or payload.get("project", {}).get("name", "unknown")
    environment = payload.get("environmentName") or payload.get("environment", {}).get("name", "production")

    log_parts: list[str] = []
    for key in ("logs", "buildLogs", "error", "stderr", "output", "reason"):
        val = payload.get(key)
        if isinstance(val, str):
            log_parts.append(val)
        elif isinstance(val, list):
            log_parts.extend(str(item) for item in val if item)

    raw_log = "\n".join(log_parts) or str(payload)
    return str(deployment_id), str(project_name), str(environment), raw_log


# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------


def _extract_stack_trace(log: str) -> Optional[str]:
    """Return the most prominent stack trace block found in *log*, or None."""
    # Try Python traceback first
    py_match = _RX_PY_TRACEBACK_START.search(log)
    if py_match:
        start = py_match.start()
        # A traceback block ends at the first blank line after the error type line
        block = log[start:]
        end_match = re.search(r"\n\n", block)
        return block[: end_match.start()] if end_match else block[:4000]

    # Try Node/JS error block
    node_match = _RX_NODE_ERROR_START.search(log)
    if node_match:
        start = node_match.start()
        block = log[start:]
        end_match = re.search(r"\n\n", block)
        return block[: end_match.start()] if end_match else block[:4000]

    # Fallback: return last 2000 chars of the log as a pseudo-trace
    if len(log) > 100:
        return log[-2000:]
    return None


def _extract_affected_files(log: str) -> list[str]:
    """Return de-duplicated list of source file paths mentioned in the log."""
    files: list[str] = []
    for m in _RX_FILE_PATH.finditer(log):
        path = m.group("pyfile") or m.group("jsfile") or m.group("tsfile")
        if path and path not in files:
            # Reject obvious non-source paths
            if not any(skip in path for skip in ("node_modules", "<stdin>", "<anonymous>", "internal/")):
                files.append(path)
    return files[:10]  # cap at 10 to avoid noise


def _extract_error_type(log: str) -> Optional[str]:
    m = _RX_ERROR_TYPE.search(log)
    return m.group(1) if m else None


def _extract_error_message(log: str, error_type: Optional[str]) -> Optional[str]:
    if not error_type:
        return None
    pattern = re.compile(rf"{re.escape(error_type)}:\s*(.+)")
    m = pattern.search(log)
    if m:
        return m.group(1).strip()[:300]
    return None


def _extract_exit_code(log: str) -> Optional[int]:
    m = _RX_EXIT_CODE.search(log)
    if m:
        try:
            code = int(m.group(1))
            return code if code != 0 else None  # exit 0 means success — ignore
        except ValueError:
            pass
    return None


def _extract_build_phase(log: str) -> Optional[str]:
    m = _RX_BUILD_PHASE.search(log)
    if m:
        return m.group(1).lower()
    return None


def _extract_ts_errors(log: str) -> list[str]:
    return [m.group(0) for m in _RX_TS_ERROR.finditer(log)][:20]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_vercel_payload(payload: dict[str, Any]) -> ParsedError:
    """
    Convert a raw Vercel deployment.error webhook payload into a :class:`ParsedError`.
    """
    dep_id, project, env, raw_log = _normalise_vercel(payload)
    return _build_parsed_error(Platform.VERCEL, dep_id, project, env, raw_log)


def parse_railway_payload(payload: dict[str, Any]) -> ParsedError:
    """
    Convert a raw Railway DEPLOYMENT_FAILED webhook payload into a :class:`ParsedError`.
    """
    dep_id, project, env, raw_log = _normalise_railway(payload)
    return _build_parsed_error(Platform.RAILWAY, dep_id, project, env, raw_log)


def _build_parsed_error(
    platform: Platform,
    deployment_id: str,
    project_name: str,
    environment: str,
    raw_log: str,
) -> ParsedError:
    error_type = _extract_error_type(raw_log)
    return ParsedError(
        platform=platform,
        deployment_id=deployment_id,
        project_name=project_name,
        environment=environment,
        raw_log=raw_log,
        error_type=error_type,
        error_message=_extract_error_message(raw_log, error_type),
        stack_trace=_extract_stack_trace(raw_log),
        affected_files=_extract_affected_files(raw_log),
        exit_code=_extract_exit_code(raw_log),
        build_phase=_extract_build_phase(raw_log),
        ts_errors=_extract_ts_errors(raw_log),
    )
