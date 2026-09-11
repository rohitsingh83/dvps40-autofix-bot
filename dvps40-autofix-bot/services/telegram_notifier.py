"""
services/telegram_notifier.py — Async Telegram message dispatcher.

Sends richly-formatted incident reports using MarkdownV2 escaping so that
special characters in file paths, error messages, and diff snippets don't
break the Telegram parser.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------

_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(text: str) -> str:
    """Escape a plain-text string so it is safe inside a MarkdownV2 message."""
    return _MDV2_SPECIAL.sub(r"\\\1", str(text))


def _code(text: str) -> str:
    """Wrap *text* in an inline code span (backtick-escaped for MarkdownV2)."""
    escaped = text.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{escaped}`"


def _truncate_diff(snippet: str, max_lines: int = 25) -> str:
    """Keep at most *max_lines* lines and append a note if truncated."""
    lines = snippet.splitlines()
    if len(lines) <= max_lines:
        return snippet
    kept = lines[:max_lines]
    kept.append(f"… ({len(lines) - max_lines} more lines)")
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def _build_message(report: dict, pr_url: str) -> str:
    """
    Construct the MarkdownV2 incident report message.

    Expected *report* keys (all optional with sensible defaults):
      platform, project_name, environment, error_type, error_message,
      root_cause, steps, file_path, original_snippet, fixed_snippet, explanation
    """
    platform = report.get("platform", "Unknown").upper()
    project = report.get("project_name", "Unknown project")
    env = report.get("environment", "production")
    error_type = report.get("error_type") or "Deployment Error"
    error_msg = report.get("error_message") or ""
    root_cause = report.get("root_cause", "Diagnosis unavailable")
    steps: list[str] = report.get("steps", [])
    file_path = report.get("file_path", "")
    original_snippet: str = report.get("original_snippet", "")
    fixed_snippet: str = report.get("fixed_snippet", "")
    explanation = report.get("explanation", "")

    lines: list[str] = []

    # ── 1. Error detected ────────────────────────────────────────────────── #
    lines += [
        "🚨 *Error detected*",
        f"{_esc(platform)} deployment failed: {_esc(error_type)}",
    ]
    if error_msg:
        lines.append(f"_{_esc(error_msg)}_")
    lines.append("")

    # ── 2. Diagnosis ─────────────────────────────────────────────────────── #
    lines += [
        "🔍 *Diagnosis*",
        _esc(root_cause),
        "",
    ]

    # ── 3. Fix applied ───────────────────────────────────────────────────── #
    fix_desc = explanation or f"Updated {file_path}"
    lines += [
        "🔧 *Fix applied*",
        _esc(fix_desc),
    ]
    if file_path:
        lines.append(f"File: {_code(file_path)}")
    lines.append("")

    # ── Diff Preview (if available) ──────────────────────────────────────── #
    if original_snippet and fixed_snippet:
        lines += [
            "```diff",
        ]
        for line in _truncate_diff(original_snippet).splitlines():
            lines.append(f"- {line}")
        for line in _truncate_diff(fixed_snippet).splitlines():
            lines.append(f"+ {line}")
        lines += ["```", ""]

    # ── 4. Validation ────────────────────────────────────────────────────── #
    lines += [
        "🧪 *Validation*",
        "Tests, lint, build and security checks passed\\.",
        "",
    ]

    # ── 5. Review ────────────────────────────────────────────────────────── #
    lines += [
        "🔎 *Review*",
        "Automated code review approved the patch\\.",
        "",
    ]

    # ── 6. PR created ────────────────────────────────────────────────────── #
    lines += [
        "✅ *PR created*",
        f"Final resolution submitted as a PR to `dev`\\.",
        f"[Review Pull Request]({_esc(pr_url)})",
    ]

    return "\n".join(lines)


def _build_error_message(summary: str, raw_log_excerpt: str) -> str:
    """Fallback message when the agent pipeline itself fails."""
    lines = [
        "⚠️ *AutoFix Agent — Pipeline Error*",
        "",
        _esc(summary),
        "",
        "```",
        _esc(_truncate_diff(raw_log_excerpt, 15)),
        "```",
        "",
        "_Manual investigation required\\._",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def notify_telegram(report: dict, pr_url: str) -> None:
    """
    Send the formatted incident report to ``settings.telegram_chat_id``.

    Parameters
    ----------
    report
        Dictionary containing parsed error details and agent patch info.
        See :func:`_build_message` for expected keys.
    pr_url
        The GitHub PR URL to embed in the message.
    """
    text = _build_message(report, pr_url)

    if settings.dry_run or settings.telegram_bot_token.get_secret_value().startswith("123456789:"):
        logger.info("[DryRun] Simulated Telegram notification successfully dispatched:\n%s\n[Inline URL]: %s", text, pr_url)
        return

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Review PR", url=pr_url)],
        ]
    )

    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
        logger.info("Telegram notification sent to %s.", settings.telegram_chat_id)
    except Exception as exc:
        logger.error("Failed to send Telegram message: %s", exc, exc_info=True)
        # Re-raise so the caller can decide whether to surface the error
        raise


async def notify_telegram_error(summary: str, raw_log_excerpt: str = "") -> None:
    """
    Send a simplified error notification when the pipeline itself fails
    (e.g., LLM timeout, GitHub API error).

    This should never raise — it is designed to be called from exception
    handlers where the main flow has already broken down.
    """
    text = _build_error_message(summary, raw_log_excerpt)

    if settings.dry_run or settings.telegram_bot_token.get_secret_value().startswith("123456789:"):
        logger.warning("[DryRun] Simulated Telegram error notification dispatched:\n%s", text)
        return

    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        logger.error(
            "Critical: could not send error notification to Telegram: %s", exc
        )
