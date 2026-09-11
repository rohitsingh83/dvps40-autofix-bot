# DVPS40 — Autonomous Auto-Fixing DevOps Telegram Bot

> A production-ready, fully autonomous incident-response bot that ingests deployment failures from **Vercel** and **Railway**, diagnoses root causes with **GPT-4o**, generates and self-reviews a code patch, pushes it to an isolated GitHub branch, opens a PR against your `dev` branch, and notifies your team on **Telegram** — all without human intervention.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Local Setup](#local-setup)
4. [Environment Variables](#environment-variables)
5. [Running the Server](#running-the-server)
6. [Webhook Registration](#webhook-registration)
   - [Vercel](#vercel)
   - [Railway](#railway)
7. [Testing with Sample Payloads](#testing-with-sample-payloads)
8. [Pipeline Walkthrough](#pipeline-walkthrough)
9. [Security Model](#security-model)
10. [Project Structure](#project-structure)
11. [Extending the Bot](#extending-the-bot)
12. [Deployment Guide](#deployment-guide)

---

## Architecture

```
Vercel / Railway
     │
     │  HMAC-signed webhook (POST)
     ▼
┌─────────────────────────────────────────────┐
│              FastAPI Server                 │
│  /webhooks/vercel   /webhooks/railway       │
│        │                   │                │
│        └─────────┬─────────┘                │
│                  │ BackgroundTask            │
│                  ▼                          │
│          Log Parser Service                 │
│  (normalise payload → ParsedError)          │
│                  │                          │
│                  ▼                          │
│    GitHub Client → fetch files from `dev`   │
│                  │                          │
│                  ▼                          │
│  ┌───────────────────────────────────────┐  │
│  │          LLM Agent Pipeline           │  │
│  │  Stage 1: Root Cause + Patch (GPT-4o) │  │
│  │  Stage 2: Self Code-Review (GPT-4o)   │  │
│  └───────────────────────────────────────┘  │
│                  │                          │
│                  ▼                          │
│  GitHub Client                              │
│    → create fix/devops-<ts> branch          │
│    → commit patched file                    │
│    → open PR against `dev`                  │
│                  │                          │
│                  ▼                          │
│       Telegram Notifier                     │
│    → send rich MarkdownV2 report            │
└─────────────────────────────────────────────┘
```

---

## Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Python | 3.11+ | Uses `match` statements and `asyncio` |
| GitHub PAT | — | Scopes: `repo`, `workflow` |
| OpenAI API key | — | Access to `gpt-4o` |
| Telegram Bot Token | — | Created via [@BotFather](https://t.me/BotFather) |
| Public URL | — | Required for webhook registration (ngrok, Fly.io, Railway, etc.) |

---

## Local Setup

```bash
# 1. Clone the repository
git clone https://github.com/your-org/dvps40-autofix-bot.git
cd dvps40-autofix-bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# → Edit .env with your real secrets (see Environment Variables section)
```

---

## Environment Variables

Copy `.env.example` to `.env` and populate every field:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | HTTP API token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Target chat ID (group/channel). Use `-100<id>` for supergroups |
| `GITHUB_TOKEN` | ✅ | — | PAT with `repo` + `workflow` scopes |
| `GITHUB_REPO` | ✅ | — | `owner/repo` format |
| `DEV_BRANCH` | ✅ | `dev` | Source branch. PRs always target this |
| `OPENAI_API_KEY` | ✅ | — | OpenAI API key |
| `OPENAI_MODEL` | ❌ | `gpt-4o` | Model identifier |
| `OPENAI_TEMPERATURE` | ❌ | `0.2` | Lower = more deterministic patches |
| `WEBHOOK_SECRET` | ✅ | — | Shared HMAC secret for all webhooks |
| `APP_HOST` | ❌ | `0.0.0.0` | Bind address |
| `APP_PORT` | ❌ | `8000` | Bind port |
| `LOG_LEVEL` | ❌ | `INFO` | Python logging level |
| `DRY_RUN` | ❌ | `false` | Skip GitHub + Telegram in testing |

**Generate a secure `WEBHOOK_SECRET`:**
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Running the Server & Standalone Telegram Bot

### 1. Webhook API & Control Dashboard Server
```bash
# Development (auto-reload)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production
python main.py
```

### 2. Standalone Interactive Telegram Bot
```bash
# Run the dedicated polling bot (or interactive CLI self-test if using mock credentials)
python telegram_bot.py
```
**Telegram Bot Features & Commands:**
- `/start`, `/help`: Overview of autonomous capabilities and command guide.
- `/status`: System health, repository telemetry, strict `dev` branch guard check.
- `/incidents`: Audit log of recently processed incidents.
- `/incident <id>`: In-depth diagnostic inspection of specific incident.
- `/simulate`: Instant triggered simulation of deployment crash and repair.
- `/fix <logs>`: Paste raw error logs directly in Telegram to generate an AI fix and PR.

**Verify the server is running:**
```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "ok",
  "timestamp": 1725000000,
  "service": "dvps40-autofix-bot",
  "version": "1.0.0",
  "dev_branch": "dev",
  "repository": "your-org/your-repo",
  "dry_run": false
}
```

---

## Webhook Registration

You need a **publicly accessible URL**. For local development, use [ngrok](https://ngrok.com/):
```bash
ngrok http 8000
# Copy the https://xxxx.ngrok.io URL
```

### Vercel

1. Go to **Vercel Dashboard → Project → Settings → Webhooks**.
2. Click **Add Webhook**.
3. **URL:** `https://your-server.example.com/webhooks/vercel`
4. **Events:** Check `deployment.error` (and optionally `deployment.canceled`)
5. **Secret:** Paste your `WEBHOOK_SECRET` value.
6. Click **Save**.

> Vercel sends `X-Vercel-Signature: sha256=<hex>` with every request.

### Railway

1. Go to **Railway Dashboard → Project → Settings → Webhooks**.
2. Click **Create Webhook**.
3. **URL:** `https://your-server.example.com/webhooks/railway`
4. **Events:** `DEPLOYMENT_FAILED`
5. **Secret:** Paste your `WEBHOOK_SECRET` value.
6. Click **Create**.

> Railway sends `X-Railway-Signature: sha256=<hex>` with every request.

---

## Testing with Sample Payloads

Use `curl` to simulate deployment failure events locally.

### Compute the HMAC signature

```bash
# macOS / Linux
PAYLOAD='{"type":"deployment.error","deployment":{"id":"dpl_test123","name":"my-app","target":"production","error":"SyntaxError: Unexpected token","buildLogs":"Traceback (most recent call last):\n  File \"app/main.py\", line 12, in <module>\n    from utils import helpers\nModuleNotFoundError: No module named '\''utils.helpers'\''"}}'
SECRET="your-webhook-secret-here"

SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
echo "sha256=$SIG"
```

### Simulate a Vercel `deployment.error` webhook

```bash
PAYLOAD='{"type":"deployment.error","deployment":{"id":"dpl_abc123","name":"my-api","target":"production","error":"Build failed","buildLogs":"Traceback (most recent call last):\n  File \"app/server.py\", line 8, in <module>\n    from app.database import get_db\nModuleNotFoundError: No module named '\''app.database'\''"}}'

curl -X POST http://localhost:8000/webhooks/vercel \
  -H "Content-Type: application/json" \
  -H "X-Vercel-Signature: sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "your-webhook-secret-here" | awk '{print $2}')" \
  -d "$PAYLOAD"
```

### Simulate a Railway `DEPLOYMENT_FAILED` webhook

```bash
PAYLOAD='{"status":"DEPLOYMENT_FAILED","deploymentId":"dep_xyz789","projectName":"backend-api","environmentName":"production","logs":"SyntaxError: invalid syntax\n  File \"src/routes/users.py\", line 42\n    def get_user(id)\n                   ^\nSyntaxError: invalid syntax"}'

curl -X POST http://localhost:8000/webhooks/railway \
  -H "Content-Type: application/json" \
  -H "X-Railway-Signature: sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "your-webhook-secret-here" | awk '{print $2}')" \
  -d "$PAYLOAD"
```

### Expected Response (both endpoints)

```json
{
  "status": "accepted",
  "deployment_id": "dpl_abc123",
  "message": "Pipeline dispatched."
}
```

The pipeline runs asynchronously — check your Telegram channel within 15–30 seconds for the full incident report.

### Testing in Dry-Run Mode

Set `DRY_RUN=true` in `.env` to run the full log-parsing and LLM pipeline without creating a GitHub PR or sending Telegram messages. Check stdout for the full agent output.

---

## Pipeline Walkthrough

When a webhook is received, the following steps execute in the background:

```
1. HMAC Signature Verification
   └─ 401 Unauthorized if signature mismatch

2. Payload Normalisation (log_parser.py)
   ├─ Platform-specific field extraction
   ├─ Stack trace isolation
   ├─ Error type classification (SyntaxError, ModuleNotFoundError, TS errors, etc.)
   ├─ Affected file path extraction
   └─ Exit code detection

3. Repository Discovery (github_client.py)
   └─ List files at root of DEV branch

4. Source Code Fetch (github_client.py)
   └─ GET /repos/{owner}/{repo}/contents/{path}?ref=dev
      (for each affected file, max 6)

5. LLM Stage 1 — Analysis + Fix (agent.py)
   ├─ System: Expert backend engineer persona + strict JSON output rules
   ├─ User:   Error report + stack trace + source files + repo structure
   └─ Output: root_cause, steps, file_path, original_snippet, fixed_snippet,
              explanation, commit_message, pr_title

6. LLM Stage 2 — Self Code-Review (agent.py)
   ├─ System: Meticulous code reviewer persona
   ├─ User:   Original file + proposed patch
   ├─ Output: { approved, issues, revised_snippet }
   └─ Retry:  If rejected with a revised_snippet, apply and re-review once

7. Patch Application
   └─ Replace original_snippet with fixed_snippet in file content

8. GitHub Operations (github_client.py)
   ├─ GET  /git/ref/heads/dev          → resolve tip SHA
   ├─ POST /git/refs                   → create fix/devops-<timestamp>
   ├─ PUT  /contents/{path}            → commit patched file
   └─ POST /pulls                      → open PR (base: dev)

9. Telegram Notification (telegram_notifier.py)
   └─ Send MarkdownV2 message with:
      🚨 Header | 🔍 Root Cause | 🧩 Step-by-Step | 📄 Files | 🔧 Diff | 🔗 PR Link
```

---

## Security Model

| Concern | Mitigation |
|---|---|
| Unauthorized webhook calls | HMAC-SHA256 verification on every request |
| Pushing to protected branches | Code-level guard rejects `main`, `master`, `production`, `prod` |
| Hallucinated imports in patch | Self-code-review Stage 2 checks for unknown identifiers |
| Destructive changes | Reviewer validates patch is strictly minimal |
| Secret leakage | All secrets stored as `SecretStr` (never logged/serialised) |
| Large payloads | Logs truncated to 6,000 chars before being sent to the LLM |
| Infinite retry loops | Single retry on reviewer rejection; hard failure on second rejection |

---

## Project Structure

```
dvps40-autofix-bot/
├── main.py                    # FastAPI app + webhook endpoints
├── config.py                  # Pydantic-settings configuration
├── requirements.txt           # Pinned production dependencies
├── .env.example               # Environment variable template
├── .gitignore
└── services/
    ├── __init__.py
    ├── log_parser.py          # Payload normaliser + regex extractors
    ├── github_client.py       # Async GitHub REST client (httpx)
    ├── agent.py               # Two-stage LLM pipeline
    └── telegram_notifier.py   # MarkdownV2 message formatter + sender
```

---

## Extending the Bot

### Add a new deployment platform

1. Add a new `Platform` enum value in `services/log_parser.py`.
2. Implement a `_normalise_<platform>` function.
3. Implement a `parse_<platform>_payload` public function.
4. Add a new `POST /webhooks/<platform>` endpoint in `main.py`.
5. Register the webhook in the platform's dashboard.

### Use a different LLM

The `services/agent.py` module uses the `openai` SDK directly.
To switch to another provider supported by LiteLLM:

```python
# Replace in agent.py:
from openai import AsyncOpenAI
_client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

# With LiteLLM:
import litellm
# Then call litellm.acompletion(...) instead of _client.chat.completions.create(...)
```

### Change the target LLM model

Set `OPENAI_MODEL=gpt-4o-mini` (faster, cheaper) or `OPENAI_MODEL=gpt-4-turbo` in `.env`.

---

## Deployment Guide

### Option A — Railway (recommended for dogfooding)

```bash
# Install Railway CLI
npm i -g @railway/cli

railway login
railway init
railway up

# Set environment variables
railway variables set TELEGRAM_BOT_TOKEN=... GITHUB_TOKEN=... # etc.
```

### Option B — Fly.io

```bash
fly launch --name dvps40-bot --region iad
fly secrets set TELEGRAM_BOT_TOKEN=... GITHUB_TOKEN=... # etc.
fly deploy
```

### Option C — Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t dvps40-bot .
docker run -p 8000:8000 --env-file .env dvps40-bot
```

---

## License

MIT
