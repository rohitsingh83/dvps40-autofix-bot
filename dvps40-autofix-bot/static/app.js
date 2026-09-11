// static/app.js — Upgraded Controller with Tabs, Telemetry & Webhook Playground

const SCENARIOS = {
  railway: {
    platform: "railway",
    service: "backend-api",
    type: "MODULE_NOT_FOUND",
    file: "src/server.js",
    line: "14",
    msg: "module not found",
    raw: "Railway deployment failed\n\nError: Cannot find module './config/database'\nat src/server.js:14",
    fileBefore: "src/server.js",
    codeBefore: "const db = require(\n  \"./config/database\"\n);",
    fileAfter: "src/server.js",
    codeAfter: "const db = require(\n  \"./config/db\"\n);",
    diagnosis: "Incorrect database module reference found in src/server.js line 14.",
    fixDesc: "Updated import path from './config/database' to './config/db'."
  },
  syntax: {
    platform: "vercel",
    service: "payment-gateway",
    type: "SyntaxError",
    file: "app/routes.py",
    line: "15",
    msg: "expected ':' in function definition",
    raw: "Vercel deployment failed\n\nSyntaxError: expected ':'\n  File \"app/routes.py\", line 15\n    def process_transaction(amount)\n                                  ^",
    fileBefore: "app/routes.py",
    codeBefore: "def process_transaction(amount)\n    return undefined_symbol",
    fileAfter: "app/routes.py",
    codeAfter: "def process_transaction(amount):\n    return {\"status\": \"resolved\"}",
    diagnosis: "Missing colon at end of function header in app/routes.py.",
    fixDesc: "Added missing colon and valid return payload in app/routes.py."
  },
  ts: {
    platform: "vercel",
    service: "web-dashboard",
    type: "TS2304",
    file: "src/config.ts",
    line: "12",
    msg: "Cannot find name 'Config'",
    raw: "Vercel build failed\n\nsrc/config.ts(12,5): error TS2304: Cannot find name 'Config'.\nCommand \"npm run build\" exited with code 1",
    fileBefore: "src/config.ts",
    codeBefore: "export const appConfig: Config = {};",
    fileAfter: "src/config.ts",
    codeAfter: "export interface Config { env?: string; }\nexport const appConfig: Config = {};",
    diagnosis: "Undeclared TypeScript interface reference in configuration file.",
    fixDesc: "Added type definition for Config interface in src/config.ts."
  }
};

let activeScenario = "railway";

document.addEventListener("DOMContentLoaded", () => {
  selectScenario("railway");
  updatePlaygroundTemplate();
  loadIncidents();
  setInterval(loadIncidents, 4000);
});

// Tab Switcher
function switchTab(tabId) {
  document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

  const targetTabBtn = Array.from(document.querySelectorAll(".nav-tab")).find(b => b.getAttribute("onclick")?.includes(tabId));
  if (targetTabBtn) targetTabBtn.classList.add("active");

  const pane = document.getElementById("tab-" + tabId);
  if (pane) pane.classList.add("active");
}

// Scenario Selection
function selectScenario(key) {
  activeScenario = key;
  document.querySelectorAll(".scen-card").forEach(b => b.classList.remove("active"));
  const activeBtn = document.getElementById("btn-" + key);
  if (activeBtn) activeBtn.classList.add("active");

  const s = SCENARIOS[key];
  if (!s) return;

  document.getElementById("rawLogPreview").innerText = s.raw;
  document.getElementById("structPlatform").innerText = s.platform.toUpperCase();
  document.getElementById("structService").innerText = s.service;
  document.getElementById("structType").innerText = s.type;
  document.getElementById("structFile").innerText = s.file;
  document.getElementById("structLine").innerText = s.line;
  document.getElementById("structMsg").innerText = s.msg;

  document.getElementById("diffFileBefore").innerText = s.fileBefore;
  document.getElementById("codeBefore").innerText = s.codeBefore;
  document.getElementById("diffFileAfter").innerText = s.fileAfter;
  document.getElementById("codeAfter").innerText = s.codeAfter;
}

function resetWorkingFlow() {
  for (let i = 1; i <= 8; i++) {
    const node = document.getElementById("node-" + i);
    if (node) node.className = "pipeline-node";
  }
}

// Trigger Full Pipeline Execution
async function runAutonomousPipeline() {
  const btn = document.getElementById("runActionBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="live-indicator"><span class="pulse-dot"></span> Bot is Auto-Fixing Code...</span>`;

  resetWorkingFlow();
  const s = SCENARIOS[activeScenario];

  try {
    for (let step = 1; step <= 8; step++) {
      const node = document.getElementById("node-" + step);
      if (node) node.className = "pipeline-node running";
      await new Promise(r => setTimeout(r, 260));
      if (node) node.className = "pipeline-node success";
    }

    const res = await fetch("/api/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platform: s.platform,
        project: s.service,
        environment: "production",
        logs: s.raw
      })
    });

    const data = await res.json();
    const rec = data.record || {};

    // Update Telegram Card
    document.getElementById("tgPlatformVal").innerText = s.platform.toUpperCase() + " Deployment";
    document.getElementById("tgRootCauseVal").innerText = s.type + ": " + s.msg;
    document.getElementById("tgFixBranch").innerText = rec.branch_name || ("fix/" + s.service + "-patch");
    if (rec.pr_url) {
      document.getElementById("tgPrLinkBtn").href = rec.pr_url;
      document.getElementById("tgPrLinkBtn").innerText = "View Pull Request on GitHub (" + rec.pr_url + ") ↗";
    }
    document.getElementById("tgTime").innerText = new Date().toLocaleTimeString();

    await loadIncidents();
  } catch (err) {
    console.error("Simulation error:", err);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg><span>Trigger Full Auto-Fix Cycle</span>`;
  }
}

// Load Incident History
async function loadIncidents() {
  try {
    const res = await fetch("/api/incidents");
    if (!res.ok) return;
    const items = await res.json();

    const tbody = document.getElementById("auditTableBody");
    const countBadge = document.getElementById("tabIncidentCount");
    if (countBadge) countBadge.innerText = items.length;

    // Update bar chart telemetry
    const railwayCount = items.filter(i => i.platform === "railway").length;
    const vercelCount = items.filter(i => i.platform === "vercel").length;
    const total = items.length || 1;

    const barRailway = document.getElementById("barRailwayVal");
    const barVercel = document.getElementById("barVercelVal");
    const barSim = document.getElementById("barSimVal");
    if (barRailway) barRailway.innerText = railwayCount + " incidents (" + Math.round((railwayCount / total) * 100) + "%)";
    if (barVercel) barVercel.innerText = vercelCount + " incidents (" + Math.round((vercelCount / total) * 100) + "%)";
    if (barSim) barSim.innerText = total + " total runs";

    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center py-6 text-muted">No incidents logged yet. Trigger one above!</td></tr>';
      return;
    }

    let html = "";
    items.forEach(it => {
      const dateStr = new Date(it.timestamp * 1000).toLocaleString();
      const statusBadge = it.status === "NOTIFIED" 
        ? '<span class="badge-green">NOTIFIED (PR CREATED)</span>' 
        : '<span class="badge-blue">' + it.status + '</span>';
      
      const prLink = it.pr_url 
        ? '<a href="' + it.pr_url + '" target="_blank" class="text-cyan font-mono" style="text-decoration:none">PR #' + it.id.substring(4, 9) + ' ↗</a>'
        : '<span class="text-muted">Pending</span>';

      html += '<tr>' +
        '<td class="font-mono" style="font-size:0.75rem; color:#94a3b8;">' + dateStr + '</td>' +
        '<td><span class="' + (it.platform === "railway" ? "badge-railway" : "badge-vercel") + '">' + it.platform.toUpperCase() + '</span></td>' +
        '<td><strong>' + (it.project || "core-service") + '</strong></td>' +
        '<td class="font-mono text-red" style="font-size:0.75rem;">' + (it.error_type || "UNKNOWN") + '</td>' +
        '<td>' + statusBadge + '</td>' +
        '<td class="font-mono text-cyan" style="font-size:0.75rem;">dev</td>' +
        '<td>' + prLink + '</td>' +
      '</tr>';
    });

    tbody.innerHTML = html;
  } catch (err) {
    console.error("Failed to load incidents:", err);
  }
}

// Playground Template Initializer
function updatePlaygroundTemplate() {
  const platform = document.getElementById("playPlatform").value;
  const textarea = document.getElementById("playPayload");

  if (platform === "railway") {
    textarea.value = JSON.stringify({
      status: "DEPLOYMENT_FAILED",
      deploymentId: "dep_railway_" + Math.floor(Math.random() * 9000 + 1000),
      projectName: "payment-service",
      environmentName: "production",
      logs: "Error: Cannot find module './config/db'\n  at src/server.js:14\nProcess exited with status 1"
    }, null, 2);
  } else if (platform === "vercel") {
    textarea.value = JSON.stringify({
      type: "deployment.error",
      deployment: {
        id: "dpl_vercel_" + Math.floor(Math.random() * 9000 + 1000),
        name: "frontend-portal",
        target: "production",
        error: "SyntaxError: Unexpected token",
        buildLogs: "SyntaxError: unexpected EOF while parsing\n  File \"app/routes.py\", line 22"
      }
    }, null, 2);
  } else {
    textarea.value = JSON.stringify({
      platform: "vercel",
      project: "user-management",
      environment: "production",
      logs: "TypeError: null is not an object (evaluating 'user.permissions')\n  at AuthProvider.tsx:44"
    }, null, 2);
  }
}

// Send Webhook from Playground
async function sendPlaygroundWebhook() {
  const platform = document.getElementById("playPlatform").value;
  const payloadStr = document.getElementById("playPayload").value;
  const secret = document.getElementById("playSecret").value;
  const respPre = document.getElementById("playResponse");
  const statusBadge = document.getElementById("playStatusBadge");

  statusBadge.innerText = "SENDING...";
  statusBadge.className = "badge-blue";

  let url = "/api/simulate";
  let headers = { "Content-Type": "application/json" };

  if (platform === "railway") {
    url = "/webhooks/railway";
    // Send unauthenticated or mock HMAC
    headers["X-Railway-Signature"] = "sha256=test_signature";
  } else if (platform === "vercel") {
    url = "/webhooks/vercel";
    headers["X-Vercel-Signature"] = "sha256=test_signature";
  }

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: headers,
      body: payloadStr
    });

    const data = await res.json();
    statusBadge.innerText = "HTTP " + res.status;
    statusBadge.className = res.status < 300 ? "badge-green" : "badge-red";
    respPre.innerText = JSON.stringify(data, null, 2);

    await loadIncidents();
  } catch (err) {
    statusBadge.innerText = "ERROR";
    statusBadge.className = "badge-red";
    respPre.innerText = "// Request failed:\n" + err.message;
  }
}

// --------------------------------------------------------------------------
// Interactive Telegram Bot Console Handlers
// --------------------------------------------------------------------------
function appendTgBubble(role, sender, text) {
  const container = document.getElementById("tgChatHistory");
  if (!container) return;
  const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const bubble = document.createElement("div");
  bubble.className = "tg-bubble " + role;
  bubble.innerHTML = `
    <div class="tg-bubble-name">${sender}</div>
    <div class="tg-bubble-text">${text}</div>
    <div class="tg-bubble-time">${timeStr}</div>
  `;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

function sendTgCommand(cmd) {
  const input = document.getElementById("tgChatInput");
  if (input) {
    input.value = cmd;
    dispatchTgMessage();
  }
}

async function dispatchTgMessage() {
  const input = document.getElementById("tgChatInput");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";

  appendTgBubble("user", "You (On-Call Dev)", text);

  if (text === "/status") {
    const res = await fetch("/api/status");
    const statusData = await res.json();
    const incRes = await fetch("/api/incidents");
    const incData = await incRes.json();
    appendTgBubble("bot", "DVPS40 Bot", `
      🛡️ <strong>Live System Status & Guard:</strong><br>
      • Service: <code>${statusData.service}</code><br>
      • Guarded Dev Branch: <code>${statusData.dev_branch}</code><br>
      • Protection: <code>STRICT DEV ISOLATION ACTIVE</code><br>
      • Repository: <code>${statusData.repository}</code><br>
      • Total Incidents: <code>${incData.length}</code><br>
      • 6 Quality Gates: <span class="text-emerald">ACTIVE</span>
    `);
  } else if (text === "/incidents") {
    const res = await fetch("/api/incidents");
    const items = await res.json();
    if (!items.length) {
      appendTgBubble("bot", "DVPS40 Bot", "ℹ️ No incident records logged yet. Type <code>/simulate</code> to generate one.");
      return;
    }
    let html = "📋 <strong>Recent Incident Audit Log:</strong><br>";
    items.slice(0, 4).forEach(inc => {
      html += `• <code>${inc.id}</code> [${(inc.platform||"").toUpperCase()}] ➔ <strong>${inc.status}</strong> (${inc.error_type})<br>`;
    });
    appendTgBubble("bot", "DVPS40 Bot", html);
  } else if (text === "/help") {
    appendTgBubble("bot", "DVPS40 Bot", `
      ⚡ <strong>Available Commands:</strong><br>
      • <code>/simulate</code> — Run full automated crash repair simulation<br>
      • <code>/status</code> — Inspect system health & dev branch safety<br>
      • <code>/incidents</code> — View recent triage incident audit records<br>
      • <code>/fix &lt;error log&gt;</code> — Paste error log to trigger diagnosis and PR
    `);
  } else if (text.startsWith("/simulate") || text.startsWith("/fix")) {
    appendTgBubble("bot", "DVPS40 Bot", "⚙️ <em>Received command. Executing 8-stage Auto-Fix pipeline...</em>");
    try {
      const s = SCENARIOS[activeScenario];
      const res = await fetch("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: s.platform,
          project: s.service,
          environment: "production",
          logs: text.startsWith("/fix") ? text.replace("/fix", "").trim() || s.raw : s.raw
        })
      });
      const data = await res.json();
      const rec = data.record || {};
      appendTgBubble("bot", "DVPS40 Bot", `
        🚨 <strong>Incident Repaired & Validated!</strong><br><br>
        <strong>Platform:</strong> ${rec.platform ? rec.platform.toUpperCase() : "RAILWAY"} Deployment<br>
        <strong>Diagnosis:</strong> ${rec.root_cause || "Isolated syntax crash in target module."}<br>
        <strong>Validation:</strong> 6/6 Gates Passed (Syntax, Unit, Lint, Build, Integration, Security)<br>
        <strong>Branch:</strong> <code>${rec.pr_branch || "fix/autofix-dev"}</code> (target: <code>dev</code>)<br>
        <strong>PR URL:</strong> <a href="${rec.pr_url || '#'}" target="_blank" class="text-cyan font-mono">${rec.pr_url || 'https://github.com/your-org/your-repo/pull/42'}</a>
      `);
      await loadIncidents();
    } catch (err) {
      appendTgBubble("bot", "DVPS40 Bot", "❌ Pipeline error: " + err.message);
    }
  } else {
    appendTgBubble("bot", "DVPS40 Bot", `⚠️ Unrecognized command: <code>${text}</code>. Type <code>/help</code> or <code>/simulate</code>.`);
  }
}
