/* ─── Virtual Charger Farm Manager — Client JS ─────────────────────────────── */

// ─── State ─────────────────────────────────────────────────────────────────
let sseSource = null;
let logPaused = false;
let logCollapsed = false;
let currentModalCpId = null;
const sparkData = { connected: [], mps: [] };
const MAX_SPARK = 30;

// ─── Init ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  connectSSE();
  startPolling();
});

// ─── OCPP Version Toggle ────────────────────────────────────────────────────
function setOcppVersion(version, btn) {
  document.getElementById("ocpp-version").value = version;
  document.querySelectorAll("#ocpp-toggle .toggle-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");

  const urlField = document.getElementById("ocpp-url");
  if (version === "2.0.1") {
    if (urlField.value.includes("9100")) urlField.value = urlField.value.replace("9100", "9201");
  } else {
    if (urlField.value.includes("9201")) urlField.value = urlField.value.replace("9201", "9100");
  }
}

// ─── Farm Control ───────────────────────────────────────────────────────────
async function startFarm() {
  const payload = {
    count: parseInt(document.getElementById("count-slider").value),
    ocpp_version: document.getElementById("ocpp-version").value,
    profile: document.getElementById("profile-select").value,
    ocpp_url: document.getElementById("ocpp-url").value,
  };
  const res = await fetch("/api/farm/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  showStatus(`▶ Farm started — ${data.started} chargers spawned`);
  refreshStatus();
}

async function stopFarm() {
  const res = await fetch("/api/farm/stop", { method: "POST" });
  const data = await res.json();
  showStatus(`■ Farm stopped — ${data.stopped} chargers removed`);
  refreshStatus();
  document.getElementById("charger-grid").innerHTML =
    '<div class="grid-empty" id="grid-empty">No chargers running. Start the farm to see charger cards here.</div>';
  document.getElementById("grid-count").textContent = "0 chargers";
}

async function pauseFarm() {
  const res = await fetch("/api/farm/pause", { method: "POST" });
  const data = await res.json();
  showStatus(data.paused ? "⏸ Farm paused" : "▶ Farm resumed");
  refreshStatus();
}

async function resetCounters() {
  if (!confirm("Reset alle tellers en event log?")) return;
  const res = await fetch("/api/metrics/reset", { method: "POST" });
  const data = await res.json();
  showStatus(data.ok ? "🔄 Counters reset" : "❌ Reset failed");
  refreshStatus();
}

function showStatus(msg) {
  const el = document.getElementById("farm-status-text");
  if (el) el.textContent = msg;
}

// ─── Conditions ─────────────────────────────────────────────────────────────
async function applyConditions() {
  const faultTypes = [...document.querySelectorAll("#fault-types input:checked")].map(i => i.value);
  const payload = {
    latency_ms: parseInt(document.getElementById("latency-slider").value),
    packet_loss_pct: parseInt(document.getElementById("loss-slider").value),
    disconnect_interval_s: parseInt(document.getElementById("disc-slider").value),
    fault_types: faultTypes,
    fault_probability: parseInt(document.getElementById("fprob-slider").value),
    fault_target: document.getElementById("fault-target").value,
    fault_target_ids: document.getElementById("fault-target-ids").value,
    auto_sessions: document.getElementById("auto-sessions").checked,
    soc_min: parseInt(document.getElementById("soc-min-slider").value),
    soc_max: parseInt(document.getElementById("soc-max-slider").value),
    session_duration_min: parseInt(document.getElementById("dur-slider").value),
    concurrent_sessions: parseInt(document.getElementById("conc-slider").value),
    accept_smart_charging: document.getElementById("accept-sc").checked,
    smart_charging_delay_s: parseInt(document.getElementById("sc-delay-slider").value),
    smart_charging_override_pct: parseInt(document.getElementById("sc-override-slider").value),
    pnc_enabled: document.getElementById("pnc-enabled").checked,
    pnc_cert_validity: document.getElementById("pnc-cert").value,
  };
  await fetch("/api/farm/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  showStatus("✓ Conditions applied");
}

function toggleFaultIds(val) {
  document.getElementById("fault-target-ids").style.display = val === "specific" ? "block" : "none";
}

async function reconnectStorm() {
  await fetch("/api/farm/reconnect-storm", { method: "POST" });
  showStatus("⚡ Reconnect storm triggered");
}

// ─── Metrics Poll ───────────────────────────────────────────────────────────
function startPolling() {
  refreshStatus();
  refreshChargers();
  setInterval(refreshStatus, 2000);
  setInterval(refreshChargers, 3000);
}

async function refreshStatus() {
  try {
    const [statusRes, metricsRes] = await Promise.all([
      fetch("/api/farm/status"),
      fetch("/api/metrics")
    ]);
    const status = await statusRes.json();
    const metrics = await metricsRes.json();
    updateMetrics(status, metrics);
  } catch (e) { /* silently ignore if server not up */ }
}

function updateMetrics(status, metrics) {
  // Status badge
  const badge = document.getElementById("farm-status-badge");
  if (badge) {
    if (status.running) {
      badge.className = "status-badge running";
      badge.textContent = `● ${status.connected}/${status.total_chargers} connected, ${status.charging} charging`;
    } else {
      badge.className = "status-badge stopped";
      badge.textContent = "○ Farm stopped";
    }
  }

  // Metric tiles
  setText("m-connected", status.connected ?? 0);
  setText("m-charging", status.charging ?? 0);
  setText("m-mps", (metrics.messages_per_sec ?? 0).toFixed(1));
  setText("m-errors", metrics.total_errors ?? 0);
  setText("m-latency", (metrics.avg_latency_ms ?? 0).toFixed(0));
  setText("m-sessions", metrics.total_sessions_started ?? 0);

  // Sparklines
  sparkData.connected.push(status.connected ?? 0);
  sparkData.mps.push(metrics.messages_per_sec ?? 0);
  if (sparkData.connected.length > MAX_SPARK) sparkData.connected.shift();
  if (sparkData.mps.length > MAX_SPARK) sparkData.mps.shift();
  drawSparkline("spark-connected", sparkData.connected, "#22c55e");
  drawSparkline("spark-mps", sparkData.mps, "#00B0E4");

  // Error info
  const errEl = document.getElementById("m-last-error");
  if ((metrics.total_errors ?? 0) > 0 && errEl) {
    errEl.style.display = "block";
  }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ─── Charger Grid ───────────────────────────────────────────────────────────
async function refreshChargers() {
  try {
    const res = await fetch("/api/chargers");
    const chargers = await res.json();
    renderChargerGrid(chargers);
  } catch (e) { /* ignore */ }
}

function renderChargerGrid(chargers) {
  const grid = document.getElementById("charger-grid");
  const countEl = document.getElementById("grid-count");
  if (!grid) return;

  if (!chargers.length) {
    grid.innerHTML = '<div class="grid-empty">No chargers running. Start the farm to see charger cards here.</div>';
    if (countEl) countEl.textContent = "0 chargers";
    return;
  }

  if (countEl) countEl.textContent = `${chargers.length} chargers`;
  const html = chargers.map(c => chargerCard(c)).join("");
  grid.innerHTML = html;
}

function chargerCard(c) {
  const id = c.cp_id || c.id || "unknown";
  const status = c.status || "Offline";
  const cls = statusClass(status);
  const conns = c.connectors || c.evses || {};
  let details = "";
  for (const [k, v] of Object.entries(conns)) {
    const kw = v.power_kw !== undefined ? `${(v.power_kw || 0).toFixed(1)}kW` : "";
    const soc = v.soc_pct !== undefined ? ` ${v.soc_pct}%` : "";
    details += `<div class="cc-detail">C${k}: ${v.status || "—"} ${kw}${soc}</div>`;
  }
  return `<div class="charger-card ${cls}" onclick="openModal('${id}', '${status}')">
    <div class="cc-id">${id}</div>
    <div class="cc-status ${cls}">${status}</div>
    ${details}
  </div>`;
}

function statusClass(status) {
  if (!status) return "offline";
  const s = status.toLowerCase();
  if (s.includes("charging")) return "charging";
  if (s.includes("fault")) return "faulted";
  if (s.includes("available") || s === "connected") return "available";
  if (s === "offline" || s === "unavailable") return "offline";
  return "offline";
}

// ─── Modal ───────────────────────────────────────────────────────────────────
function openModal(cpId, status) {
  currentModalCpId = cpId;
  document.getElementById("modal-title").textContent = `Charger: ${cpId}`;
  document.getElementById("modal-status").textContent = `Status: ${status}`;
  document.getElementById("charger-modal").style.display = "flex";
}

function closeModal() {
  document.getElementById("charger-modal").style.display = "none";
  currentModalCpId = null;
}

async function modalAction(action) {
  if (!currentModalCpId) return;
  await fetch(`/api/charger/${encodeURIComponent(currentModalCpId)}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
  });
  closeModal();
  setTimeout(refreshChargers, 500);
}

async function modalInjectFault() {
  if (!currentModalCpId) return;
  const errCode = document.getElementById("modal-fault-type").value;
  await fetch(`/api/charger/${encodeURIComponent(currentModalCpId)}/fault`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ error_code: errCode, connector: 1 })
  });
  closeModal();
}

// ─── Event Log via SSE ───────────────────────────────────────────────────────
function connectSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource("/api/events/stream");
  sseSource.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "event") appendLog(msg.data);
      if (msg.type === "metrics") updateMetricsFromSSE(msg.data);
    } catch {}
  };
  sseSource.onerror = () => {
    setTimeout(connectSSE, 5000);
  };
}

function appendLog(event) {
  if (logPaused) return;
  const container = document.getElementById("log-container");
  if (!container) return;

  const filterCpId = (document.getElementById("log-filter-id")?.value || "").toLowerCase();
  const filterType = (document.getElementById("log-filter-type")?.value || "").toLowerCase();
  const src = (event.source || "").toLowerCase();
  const msg = (event.message || "").toLowerCase();

  if (filterCpId && !src.includes(filterCpId)) return;
  if (filterType && !msg.includes(filterType)) return;

  // Remove hint line
  const hint = container.querySelector(".log-hint");
  if (hint) hint.remove();

  const ts = new Date(event.timestamp * 1000).toLocaleTimeString();
  const level = event.level || "info";
  const line = document.createElement("div");
  line.className = `log-line level-${level}`;
  line.textContent = `[${ts}] [${event.source}] ${event.message}`;
  container.appendChild(line);

  // Keep last 200 lines
  while (container.children.length > 200) container.removeChild(container.firstChild);

  // Scroll to bottom
  container.scrollTop = container.scrollHeight;
}

function updateMetricsFromSSE(metrics) {
  setText("m-mps", (metrics.messages_per_sec ?? 0).toFixed(1));
  setText("m-errors", metrics.total_errors ?? 0);
  setText("m-latency", (metrics.avg_latency_ms ?? 0).toFixed(0));
  setText("m-sessions", metrics.total_sessions_started ?? 0);
  sparkData.mps.push(metrics.messages_per_sec ?? 0);
  if (sparkData.mps.length > MAX_SPARK) sparkData.mps.shift();
  drawSparkline("spark-mps", sparkData.mps, "#00B0E4");
}

function toggleLogPause() {
  logPaused = !logPaused;
  const btn = document.getElementById("btn-log-pause");
  if (btn) btn.textContent = logPaused ? "▶ Resume" : "⏸ Pause";
}

function clearLog() {
  const c = document.getElementById("log-container");
  if (c) c.innerHTML = '<div class="log-line muted log-hint">Log cleared</div>';
}

function toggleLog() {
  logCollapsed = !logCollapsed;
  const body = document.getElementById("log-body");
  const icon = document.getElementById("log-collapse-icon");
  if (body) body.classList.toggle("collapsed", logCollapsed);
  if (icon) icon.textContent = logCollapsed ? "▶" : "▼";
}

// ─── Scenarios ───────────────────────────────────────────────────────────────
async function runScenario(name) {
  const res = await fetch(`/api/scenario/${name}/start`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ intensity: "medium" })
  });
  if (res.ok) {
    const data = await res.json();
    showStatus(`▶ Scenario "${data.started}" started`);
    const badge = document.getElementById("scenario-badge");
    if (badge) { badge.textContent = `⟳ ${data.started}`; badge.style.display = "inline"; }
    const statusEl = document.getElementById("scenario-status");
    if (statusEl) statusEl.textContent = `Running: ${data.started}`;
  } else {
    const err = await res.json();
    showStatus(`✗ Scenario failed: ${err.detail || "unknown error"}`);
  }
}

async function saveCustomScenario() {
  const name = document.getElementById("custom-name").value.trim();
  if (!name) { alert("Enter a scenario name"); return; }
  showStatus(`✓ Custom scenario "${name}" saved (local)`);
}

async function runCustomScenario() {
  showStatus("Running custom scenario…");
  // TODO: build custom scenario via API
}

// ─── Sparkline Canvas ────────────────────────────────────────────────────────
function drawSparkline(canvasId, data, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;

  const max = Math.max(...data, 1);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  data.forEach((val, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - (val / max) * h;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}
