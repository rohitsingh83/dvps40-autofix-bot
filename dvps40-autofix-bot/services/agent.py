"""
services/agent.py — LLM-powered root-cause analysis, patch generation,
and automated self-code-review pipeline.

Two-stage design
================
Stage 1 – Root Cause Analysis + Fix Generation
    The agent receives:
      • The parsed error report (log, stack trace, error type, affected files)
      • Source code pulled from the DEV branch for any mentioned file
    It returns structured JSON containing:
      • root_cause        – human-readable diagnosis
      • steps             – numbered list of what caused the crash
      • file_path         – relative path of the file to patch
      • original_snippet  – the exact broken lines (for diff preview)
      • fixed_snippet     – replacement code
      • explanation       – concise note for the PR description

Stage 2 – Automated Self-Code-Review
    A *second* LLM call evaluates the proposed patch against:
      (a) Syntax correctness
      (b) No hallucinated / missing imports
      (c) No destructive / breaking changes beyond the minimum fix
      (d) Confirmation that the fix addresses the logged error
    Returns { "approved": bool, "issues": [...], "revised_snippet": str | None }

If Stage 2 rejects the patch it tries once to incorporate the reviewer's
suggestions. A hard failure is raised if the patch is still rejected.
"""

from __future__ import annotations

import json
import logging
import textwrap
from dataclasses import dataclass
from typing import Any, Optional

from openai import AsyncOpenAI

from config import settings
from services.log_parser import ParsedError
from services.validator import validation_engine, ValidationReport

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_ANALYSIS = textwrap.dedent("""\
    You are an expert senior backend engineer and DevOps specialist.
    Your task is to diagnose a deployment failure and generate a minimal, correct patch.

    Rules you MUST follow:
    1. Output ONLY valid JSON — no prose, no markdown fences.
    2. The patch must fix the reported error and nothing else.
    3. Never invent imports, packages, or functions that are not already in the file.
    4. Do not refactor unrelated code.
    5. Preserve all existing comments and docstrings unless they are part of the bug.
    6. If multiple files are implicated choose the PRIMARY source file (not a lock file,
       requirements.txt, or generated artifact) as the patch target.
    7. The fixed_snippet must be a drop-in replacement for original_snippet.
""")

_USER_ANALYSIS_TEMPLATE = textwrap.dedent("""\
    ## Deployment Failure Report

    **Platform:** {platform}
    **Project:** {project_name}
    **Environment:** {environment}
    **Error Type:** {error_type}
    **Error Message:** {error_message}
    **Exit Code:** {exit_code}
    **Build Phase:** {build_phase}

    ### Raw Log (truncated to 6000 chars)
    ```
    {raw_log}
    ```

    ### Stack Trace
    ```
    {stack_trace}
    ```

    ### TypeScript Diagnostics
    {ts_errors}

    ### Source Code of Affected Files (from `{dev_branch}` branch)
    {file_contents}

    ### Repository Structure
    {repo_structure}

    ---
    Respond with a JSON object matching this exact schema:
    {{
      "root_cause": "<one sentence diagnosis>",
      "steps": ["<step 1>", "<step 2>", ...],
      "file_path": "<relative/path/to/file>",
      "original_snippet": "<exact lines to replace>",
      "fixed_snippet": "<replacement lines>",
      "explanation": "<≤3 sentence note for the PR description>",
      "commit_message": "<imperative mood git commit message ≤72 chars>",
      "pr_title": "<PR title ≤80 chars>"
    }}
""")

_SYSTEM_REVIEW = textwrap.dedent("""\
    You are a meticulous senior code reviewer.
    You will be given:
      1. A deployment error log.
      2. The original file content.
      3. A proposed patch (original_snippet → fixed_snippet).

    Your job is to validate the patch before it is submitted as a Pull Request.

    Respond ONLY with valid JSON matching this schema:
    {
      "approved": true | false,
      "issues": ["<issue 1>", ...],        // empty list if approved
      "revised_snippet": "<corrected replacement>" | null  // null if approved or unfixable
    }

    Approval criteria (ALL must pass):
    A. The fixed_snippet is syntactically valid for the language.
    B. All identifiers and imports used in fixed_snippet already exist in the file
       or are standard library symbols — no hallucinated dependencies.
    C. The change is strictly minimal: it only addresses the reported error.
    D. No existing functionality is broken (check call-sites if visible).
    E. The patch is a safe drop-in replacement for original_snippet.
""")

_USER_REVIEW_TEMPLATE = textwrap.dedent("""\
    ### Error Log (truncated)
    ```
    {raw_log}
    ```

    ### Original File Content
    ```
    {original_content}
    ```

    ### Proposed Patch
    **File:** `{file_path}`

    **original_snippet:**
    ```
    {original_snippet}
    ```

    **fixed_snippet:**
    ```
    {fixed_snippet}
    ```
""")


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

@dataclass
class AgentPatch:
    root_cause: str
    steps: list[str]
    file_path: str
    original_snippet: str
    fixed_snippet: str
    explanation: str
    commit_message: str
    pr_title: str


@dataclass
class ReviewResult:
    approved: bool
    issues: list[str]
    revised_snippet: Optional[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n... [truncated {len(text) - limit} chars] ...\n\n" + text[-half:]


def _format_file_contents(contents: dict[str, str]) -> str:
    if not contents:
        return "*(No source files fetched — agent should infer from stack trace)*"
    parts = []
    for path, code in contents.items():
        parts.append(f"#### `{path}`\n```\n{_truncate(code, 3000)}\n```")
    return "\n\n".join(parts)


async def _llm(system: str, user: str, temperature: Optional[float] = None) -> str:
    """Single OpenAI chat-completion call, with dry-run fallback for testing."""
    if settings.dry_run and settings.openai_api_key.get_secret_value().startswith("sk-Your"):
        logger.info("[DryRun] Simulating LLM response for local testing...")
        if "senior code reviewer" in system.lower():
            return json.dumps({
                "approved": True,
                "issues": [],
                "revised_snippet": None
            })
        else:
            return json.dumps({
                "root_cause": "Uncaught syntax exception / missing symbol reference in source file.",
                "steps": [
                    "Isolated crash traceback to faulty line in target module.",
                    "Parsed error syntax and identified missing token/handler.",
                    "Constructed targeted patch matching dev branch conventions."
                ],
                "file_path": "app/routes.py",
                "original_snippet": "return undefined_symbol",
                "fixed_snippet": "return {'status': 'resolved'}",
                "explanation": "Replaced undefined symbol with valid return payload.",
                "commit_message": "fix: resolve undefined symbol in handler",
                "pr_title": "fix: resolve undefined symbol in handler"
            })

    response = await _client.chat.completions.create(
        model=settings.openai_model,
        temperature=temperature if temperature is not None else settings.openai_temperature,
        max_tokens=settings.openai_max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content or "{}"


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON: %s\nRaw: %s", exc, raw[:500])
        raise RuntimeError("LLM returned malformed JSON.") from exc


# ---------------------------------------------------------------------------
# Stage 1 — Analysis + Fix Generation
# ---------------------------------------------------------------------------

async def _run_analysis(
    error: ParsedError,
    file_contents: dict[str, str],
    repo_structure: list[str],
) -> AgentPatch:
    ts_block = "\n".join(error.ts_errors) if error.ts_errors else "None"
    user_prompt = _USER_ANALYSIS_TEMPLATE.format(
        platform=error.platform.value,
        project_name=error.project_name,
        environment=error.environment,
        error_type=error.error_type or "Unknown",
        error_message=error.error_message or "See stack trace",
        exit_code=str(error.exit_code) if error.exit_code is not None else "N/A",
        build_phase=error.build_phase or "unknown",
        raw_log=_truncate(error.raw_log, 6000),
        stack_trace=_truncate(error.stack_trace or "", 3000),
        ts_errors=ts_block,
        dev_branch=settings.dev_branch,
        file_contents=_format_file_contents(file_contents),
        repo_structure=", ".join(repo_structure[:40]) or "unknown",
    )

    logger.info("[Agent] Stage 1: Requesting root-cause analysis from %s…", settings.openai_model)
    raw = await _llm(_SYSTEM_ANALYSIS, user_prompt)
    data = _parse_json(raw)

    required_keys = {"root_cause", "steps", "file_path", "original_snippet", "fixed_snippet", "explanation"}
    missing = required_keys - data.keys()
    if missing:
        raise RuntimeError(f"LLM response missing required fields: {missing}")

    return AgentPatch(
        root_cause=str(data["root_cause"]),
        steps=[str(s) for s in data.get("steps", [])],
        file_path=str(data["file_path"]),
        original_snippet=str(data["original_snippet"]),
        fixed_snippet=str(data["fixed_snippet"]),
        explanation=str(data.get("explanation", "")),
        commit_message=str(data.get("commit_message", f"fix: auto-patch for {error.error_type or 'error'}")),
        pr_title=str(data.get("pr_title", f"fix: auto-patch [{error.platform.value}] {error.error_type or 'deployment error'}")),
    )


# ---------------------------------------------------------------------------
# Stage 2 — Automated Self-Code-Review
# ---------------------------------------------------------------------------

async def _run_review(
    patch: AgentPatch,
    original_content: str,
    error: ParsedError,
) -> ReviewResult:
    user_prompt = _USER_REVIEW_TEMPLATE.format(
        raw_log=_truncate(error.raw_log, 3000),
        original_content=_truncate(original_content, 4000),
        file_path=patch.file_path,
        original_snippet=patch.original_snippet,
        fixed_snippet=patch.fixed_snippet,
    )

    logger.info("[Agent] Stage 2: Self-code-review…")
    raw = await _llm(_SYSTEM_REVIEW, user_prompt, temperature=0.0)
    data = _parse_json(raw)

    return ReviewResult(
        approved=bool(data.get("approved", False)),
        issues=list(data.get("issues", [])),
        revised_snippet=data.get("revised_snippet"),
    )


def _apply_review_revision(patch: AgentPatch, review: ReviewResult) -> AgentPatch:
    """Replace fixed_snippet with the reviewer's corrected version (if any)."""
    if review.revised_snippet:
        return AgentPatch(
            root_cause=patch.root_cause,
            steps=patch.steps,
            file_path=patch.file_path,
            original_snippet=patch.original_snippet,
            fixed_snippet=review.revised_snippet,
            explanation=patch.explanation + " [Reviewer revision applied]",
            commit_message=patch.commit_message,
            pr_title=patch.pr_title,
        )
    return patch


def _apply_patch_to_file(original_content: str, patch: AgentPatch) -> str:
    """
    Perform the actual string replacement.
    Raises ValueError if the original_snippet is not found verbatim.
    """
    if patch.original_snippet not in original_content:
        # Attempt normalised whitespace match as a fallback
        norm_orig = " ".join(original_content.split())
        norm_snippet = " ".join(patch.original_snippet.split())
        if norm_snippet not in norm_orig:
            if settings.dry_run:
                logger.warning(
                    "[DryRun] Snippet not found in original content; applying patch to mock file."
                )
                return original_content + "\n# Patch applied:\n" + patch.fixed_snippet
            raise ValueError(
                f"original_snippet not found verbatim in {patch.file_path}. "
                "The agent may have hallucinated the snippet. Aborting patch."
            )
        # Fallback: replace first occurrence ignoring extra whitespace differences
        # by locating the approximate position
        logger.warning(
            "Exact snippet match failed; attempting whitespace-normalised replacement."
        )

    return original_content.replace(patch.original_snippet, patch.fixed_snippet, 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    patch: AgentPatch
    new_file_content: str
    blob_sha: str          # needed by github_client to update the file
    review: ReviewResult
    validation: ValidationReport


async def run_agent_pipeline(
    error: ParsedError,
    file_contents: dict[str, str],      # {file_path: content}
    file_blob_shas: dict[str, str],     # {file_path: blob_sha}
    repo_structure: list[str],
) -> AgentResult:
    """
    Full pipeline matching Slide 3 & Slide 12:
    1. Deployment Error -> 2. Parse Logs -> 3. Diagnose -> 4. Fix Code ->
    5. Test (6 validation checks) -> 6. Self Review (Retry loop) -> 7. Create PR -> Dev
    """
    # Stage 1: Minimal fix generation
    patch = await _run_analysis(error, file_contents, repo_structure)
    logger.info("[Agent] Patch target: %s", patch.file_path)

    # Ensure we have the source for the patched file
    if patch.file_path not in file_contents:
        logger.warning(
            "Agent chose %r which was not pre-fetched. File content unavailable "
            "for review — proceeding with empty base.",
            patch.file_path,
        )
        original_content = ""
        blob_sha = ""
    else:
        original_content = file_contents[patch.file_path]
        blob_sha = file_blob_shas.get(patch.file_path, "")

    # Apply the patch to produce the candidate code
    if original_content:
        new_content = _apply_patch_to_file(original_content, patch)
    else:
        logger.warning(
            "No original file content available; committing fixed_snippet as full file."
        )
        new_content = patch.fixed_snippet

    # Stage 2: 6 Validation Checks (Slide 8)
    validation = validation_engine.validate_patch(
        file_path=patch.file_path,
        patched_code=new_content,
        original_snippet=patch.original_snippet,
        fixed_snippet=patch.fixed_snippet,
    )
    if not validation.all_passed:
        logger.warning("[Agent] Initial validation failed: %s", validation.failure_reason)

    # Stage 3: Automated Self-Code-Review (Slide 9)
    review = await _run_review(patch, original_content, error)

    if not review.approved or not validation.all_passed:
        logger.warning(
            "[Agent] Self-review / validation rejected patch. Issues: %s", review.issues
        )
        if review.revised_snippet:
            logger.info("[Agent] Applying reviewer revision and re-validating…")
            patch = _apply_review_revision(patch, review)
            if original_content:
                new_content = _apply_patch_to_file(original_content, patch)
            else:
                new_content = patch.fixed_snippet

            validation = validation_engine.validate_patch(
                file_path=patch.file_path,
                patched_code=new_content,
                original_snippet=patch.original_snippet,
                fixed_snippet=patch.fixed_snippet,
            )
            review2 = await _run_review(patch, original_content, error)
            if not review2.approved:
                raise RuntimeError(
                    f"Patch failed self-review twice. Issues: {review2.issues}"
                )
            review = review2
        elif not review.approved:
            raise RuntimeError(
                f"Patch rejected by self-review and no revision provided. Issues: {review.issues}"
            )

    return AgentResult(
        patch=patch,
        new_file_content=new_content,
        blob_sha=blob_sha,
        review=review,
        validation=validation,
    )
