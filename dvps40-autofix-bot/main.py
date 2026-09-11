"""
main.py — FastAPI application entry-point.

Endpoints
---------
GET  /health                     → liveness probe
POST /webhooks/vercel            → Vercel deployment.error webhook
POST /webhooks/railway           → Railway DEPLOYMENT_FAILED webhook

Security
--------
Both webhook endpoints verify an HMAC-SHA256 signature delivered in
  • Vercel:  X-Vercel-Signature  (HMAC-SHA1 for older, SHA256 for current)
  • Railway: X-Railway-Signature

The shared secret is ``settings.webhook_secret``.

Background processing
---------------------
The agent pipeline (log parsing → LLM analysis → GitHub PR → Telegram notify)
runs as a FastAPI ``BackgroundTask`` so the webhook endpoint can return HTTP 202
immediately, well within the platform's 10-second timeout window.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any

import os
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from services import agent, github_client, log_parser, telegram_notifier
from services.incident_store import incident_store

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="DVPS40 — Autonomous AutoFix DevOps Bot",
    description=(
        "Ingests Vercel/Railway deployment failure webhooks, uses GPT-4o to diagnose "
        "and patch the error, opens a GitHub PR against the dev branch, and notifies "
        "the team via Telegram."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------------------------------------------------------------------------
# HMAC verification helpers
# ---------------------------------------------------------------------------

def _verify_hmac_sha256(body: bytes, signature_header: str | None, secret: str) -> bool:
    """
    Verify that ``signature_header`` is a valid HMAC-SHA256 over ``body``
    using ``secret``.

    Accepts bare hex or ``sha256=<hex>`` prefixed formats.
    """
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    received = signature_header.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, received)


def _verify_hmac_sha1(body: bytes, signature_header: str | None, secret: str) -> bool:
    """Legacy Vercel v1 webhooks use HMAC-SHA1."""
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha1
    ).hexdigest()
    received = signature_header.removeprefix("sha1=").strip()
    return hmac.compare_digest(expected, received)


def _check_webhook_signature(
    body: bytes,
    sig_header: str | None,
    *,
    platform: str,
) -> None:
    """
    Raise HTTP 401 if the signature is invalid or missing.
    Vercel sends HMAC-SHA256 (or SHA1 for older integrations).
    Railway sends HMAC-SHA256.
    """
    secret = settings.webhook_secret.get_secret_value()
    if _verify_hmac_sha256(body, sig_header, secret):
        return
    # Fallback: Vercel legacy SHA1
    if platform == "vercel" and _verify_hmac_sha1(body, sig_header, secret):
        return
    logger.warning("[%s] Webhook signature verification failed. Header: %r", platform, sig_header)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid webhook signature.",
    )


# ---------------------------------------------------------------------------
# Core background processing pipeline
# ---------------------------------------------------------------------------

async def _run_pipeline(parsed_error: log_parser.ParsedError) -> None:
    """
    Full autonomous pipeline:
      1. Fetch repository structure from DEV branch.
      2. Pull source code for affected files.
      3. Run the two-stage LLM agent (analysis + self-review).
      4. Commit the patch to a fix branch and open a PR.
      5. Send a Telegram notification with all details.

    Runs entirely in the background — errors are caught and reported
    via a fallback Telegram message so the on-call engineer is always notified.
    """
    deployment_id = parsed_error.deployment_id
    logger.info("[Pipeline:%s] Starting for platform=%s", deployment_id, parsed_error.platform)

    # Record initial ingestion into incident store
    incident_store.record_received(
        deployment_id=deployment_id,
        platform=parsed_error.platform.value,
        project_name=parsed_error.project_name,
        environment=parsed_error.environment,
        error_type=parsed_error.error_type or "DeploymentError",
        error_message=parsed_error.error_message or "",
        stack_trace=parsed_error.stack_trace or "",
        affected_files=parsed_error.affected_files,
    )

    try:
        # ── Step 1: Discover repository layout ──────────────────────────── #
        repo_structure = await github_client.get_repository_structure()
        logger.info("[Pipeline:%s] Repo structure: %d files", deployment_id, len(repo_structure))

        # ── Step 2: Pull source for affected files ───────────────────────── #
        file_contents: dict[str, str] = {}
        file_blob_shas: dict[str, str] = {}

        candidate_files = list(dict.fromkeys(
            parsed_error.affected_files
            + [f.group("file") for f in log_parser._RX_TS_ERROR.finditer(parsed_error.raw_log) if f.group("file")]
        ))[:6]  # cap at 6 to limit GitHub API calls

        for fpath in candidate_files:
            try:
                content, sha = await github_client.get_file_content_from_dev(fpath)
                file_contents[fpath] = content
                file_blob_shas[fpath] = sha
                logger.info("[Pipeline:%s] Fetched %s (%d bytes)", deployment_id, fpath, len(content))
            except Exception as fetch_err:
                logger.warning("[Pipeline:%s] Could not fetch %s: %s", deployment_id, fpath, fetch_err)

        incident_store.update_stage(
            deployment_id,
            "SOURCE_FETCHED",
            affected_files=list(file_contents.keys()) or parsed_error.affected_files,
        )

        # ── Step 3: LLM agent pipeline ───────────────────────────────────── #
        result = await agent.run_agent_pipeline(
            error=parsed_error,
            file_contents=file_contents,
            file_blob_shas=file_blob_shas,
            repo_structure=repo_structure,
        )
        patch = result.patch
        logger.info(
            "[Pipeline:%s] Patch approved. File: %s", deployment_id, patch.file_path
        )

        incident_store.update_stage(
            deployment_id,
            "REVIEWED",
            root_cause=patch.root_cause,
            steps=patch.steps,
            file_path=patch.file_path,
            original_snippet=patch.original_snippet,
            fixed_snippet=patch.fixed_snippet,
            explanation=patch.explanation,
            review_approved=result.review.approved,
            review_issues=result.review.issues,
            validation_checks=result.validation.to_dict()["checks"],
        )

        if settings.dry_run:
            logger.info("[Pipeline:%s] DRY RUN active — executing simulated branch/PR & Telegram dispatch.", deployment_id)

        # ── Step 4: Create fix branch + PR ──────────────────────────────── #
        pr_body = _build_pr_body(parsed_error, patch, result.review)
        pr_url = await github_client.create_fix_branch_and_pr(
            file_path=patch.file_path,
            new_content=result.new_file_content,
            blob_sha=result.blob_sha,
            commit_msg=patch.commit_message,
            pr_title=patch.pr_title,
            pr_body=pr_body,
        )
        logger.info("[Pipeline:%s] PR created: %s", deployment_id, pr_url)

        incident_store.update_stage(
            deployment_id,
            "PR_CREATED",
            pr_url=pr_url,
        )

        # ── Step 5: Telegram notification ────────────────────────────────── #
        report = {
            "platform": parsed_error.platform.value,
            "project_name": parsed_error.project_name,
            "environment": parsed_error.environment,
            "error_type": parsed_error.error_type,
            "error_message": parsed_error.error_message,
            "root_cause": patch.root_cause,
            "steps": patch.steps,
            "file_path": patch.file_path,
            "original_snippet": patch.original_snippet,
            "fixed_snippet": patch.fixed_snippet,
            "explanation": patch.explanation,
        }
        await telegram_notifier.notify_telegram(report, pr_url)
        
        incident_store.update_stage(
            deployment_id,
            "NOTIFIED",
        )
        logger.info("[Pipeline:%s] Complete ✓", deployment_id)

    except Exception as exc:
        logger.error(
            "[Pipeline:%s] Pipeline failed: %s", deployment_id, exc, exc_info=True
        )
        incident_store.update_stage(
            deployment_id,
            "FAILED",
            error_detail=str(exc),
        )
        await telegram_notifier.notify_telegram_error(
            summary=f"[{parsed_error.platform.value.upper()}] AutoFix pipeline failed: {exc}",
            raw_log_excerpt=parsed_error.raw_log[-1500:],
        )


def _build_pr_body(
    error: log_parser.ParsedError,
    patch: agent.AgentPatch,
    review: agent.ReviewResult,
) -> str:
    steps_md = "\n".join(f"{i}. {s}" for i, s in enumerate(patch.steps, 1))
    return f"""## 🤖 Autonomous AutoFix — {error.platform.value.title()}

> This PR was automatically generated by the DVPS40 DevOps bot.
> **Review carefully before merging.**

---

### Incident Summary

| Field | Value |
|---|---|
| Platform | `{error.platform.value}` |
| Project | `{error.project_name}` |
| Environment | `{error.environment}` |
| Error Type | `{error.error_type or "Unknown"}` |
| Deployment ID | `{error.deployment_id}` |

### Root Cause

{patch.root_cause}

### Step-by-Step Analysis

{steps_md}

### Explanation of Fix

{patch.explanation}

### Self-Code-Review Result

✅ **Approved by automated reviewer**

### Diff

```diff
--- a/{patch.file_path}
+++ b/{patch.file_path}
{chr(10).join('- ' + line for line in patch.original_snippet.splitlines())}
{chr(10).join('+ ' + line for line in patch.fixed_snippet.splitlines())}
```

---
*Base branch: `{settings.dev_branch}` | Auto-generated by DVPS40*
"""


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/",
    summary="Control Center Dashboard UI",
    tags=["UI"],
)
async def index_dashboard():
    index_file = "static/index.html"
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse(content={"service": "dvps40-autofix-bot", "status": "online"})


@app.get(
    "/api/status",
    summary="System status for Control Center",
    tags=["UI API"],
)
async def get_system_status() -> JSONResponse:
    return JSONResponse(
        content={
            "status": "ok",
            "timestamp": int(time.time()),
            "service": "dvps40-autofix-bot",
            "version": "1.0.0",
            "dev_branch": settings.dev_branch,
            "repository": settings.github_repo,
            "dry_run": settings.dry_run,
        }
    )


@app.get(
    "/api/incidents",
    summary="List all incident audit records",
    tags=["UI API"],
)
async def get_all_incidents() -> JSONResponse:
    return JSONResponse(content=incident_store.list_all())


class SimulationRequest(BaseModel):
    platform: str = "vercel"
    project: str = "payment-gateway"
    environment: str = "production"
    logs: str


@app.post(
    "/api/simulate",
    summary="Simulate deployment failure webhook and run pipeline",
    tags=["UI API"],
)
async def simulate_incident_pipeline(req: SimulationRequest) -> JSONResponse:
    deployment_id = f"sim_{int(time.time())}"
    platform_enum = (
        log_parser.Platform.VERCEL
        if req.platform.lower() == "vercel"
        else log_parser.Platform.RAILWAY
    )
    error_type = log_parser._extract_error_type(req.logs) or "DeploymentError"
    parsed_error = log_parser.ParsedError(
        platform=platform_enum,
        deployment_id=deployment_id,
        project_name=req.project,
        environment=req.environment,
        raw_log=req.logs,
        error_type=error_type,
        error_message=log_parser._extract_error_message(req.logs, error_type),
        stack_trace=log_parser._extract_stack_trace(req.logs),
        affected_files=log_parser._extract_affected_files(req.logs) or ["app/routes.py"],
        exit_code=log_parser._extract_exit_code(req.logs),
        build_phase=log_parser._extract_build_phase(req.logs),
        ts_errors=log_parser._extract_ts_errors(req.logs),
    )

    await _run_pipeline(parsed_error)
    record = incident_store.get(deployment_id)
    return JSONResponse(
        content={
            "status": "success",
            "deployment_id": deployment_id,
            "record": record.to_dict() if record else {},
        }
    )


@app.get(
    "/health",
    summary="Liveness probe",
    response_description="Service health status",
    tags=["Infrastructure"],
)
async def health_check() -> JSONResponse:
    """Returns 200 OK with a JSON body — used by load balancers and monitoring."""
    return JSONResponse(
        content={
            "status": "ok",
            "timestamp": int(time.time()),
            "service": "dvps40-autofix-bot",
            "version": "1.0.0",
            "dev_branch": settings.dev_branch,
            "repository": settings.github_repo,
            "dry_run": settings.dry_run,
        }
    )


@app.post(
    "/webhooks/vercel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Vercel deployment.error webhook",
    tags=["Webhooks"],
)
async def vercel_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_vercel_signature: str | None = Header(default=None),
) -> JSONResponse:
    """
    Accepts Vercel ``deployment.error`` events.

    The endpoint verifies the HMAC-SHA256 (or SHA1) signature in the
    ``X-Vercel-Signature`` header, then dispatches the incident pipeline
    as a background task.
    """
    body = await request.body()
    _check_webhook_signature(body, x_vercel_signature, platform="vercel")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON.",
        )

    # Only act on deployment errors
    event_type = payload.get("type") or payload.get("event", "")
    if event_type and "error" not in str(event_type).lower() and "fail" not in str(event_type).lower():
        logger.info("Vercel event %r is not an error — ignoring.", event_type)
        return JSONResponse(content={"status": "ignored", "event": event_type})

    parsed = log_parser.parse_vercel_payload(payload)
    logger.info(
        "Vercel webhook received | deployment_id=%s | error_type=%s",
        parsed.deployment_id, parsed.error_type,
    )
    background_tasks.add_task(_run_pipeline, parsed)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "deployment_id": parsed.deployment_id,
            "message": "Pipeline dispatched.",
        },
    )


@app.post(
    "/webhooks/railway",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Railway DEPLOYMENT_FAILED webhook",
    tags=["Webhooks"],
)
async def railway_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_railway_signature: str | None = Header(default=None),
) -> JSONResponse:
    """
    Accepts Railway ``DEPLOYMENT_FAILED`` events.

    The endpoint verifies the HMAC-SHA256 signature in the
    ``X-Railway-Signature`` header before processing.
    """
    body = await request.body()
    _check_webhook_signature(body, x_railway_signature, platform="railway")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON.",
        )

    # Only act on failure events
    event_status = payload.get("status", "") or payload.get("type", "")
    if event_status and "fail" not in str(event_status).lower() and "error" not in str(event_status).lower():
        logger.info("Railway event status %r is not a failure — ignoring.", event_status)
        return JSONResponse(content={"status": "ignored", "event_status": event_status})

    parsed = log_parser.parse_railway_payload(payload)
    logger.info(
        "Railway webhook received | deployment_id=%s | error_type=%s",
        parsed.deployment_id, parsed.error_type,
    )
    background_tasks.add_task(_run_pipeline, parsed)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "deployment_id": parsed.deployment_id,
            "message": "Pipeline dispatched.",
        },
    )


# ---------------------------------------------------------------------------
# Dev server entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
