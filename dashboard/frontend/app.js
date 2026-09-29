// Engineering dashboard client: one Server-Sent Events stream of state
// snapshots; everything is rendered with DOM APIs / textContent (never
// innerHTML), so strings that arrive via MQTT can't inject markup.
"use strict";

const LIFECYCLE = ["NORMAL", "OUTAGE", "BUFFERING", "RECONNECTING", "REPLAYING", "RECOVERED"];
const CHART_SIGNALS = ["vehicle_speed_kph", "engine_rpm", "battery_soc_pct", "battery_current_a"];
const CHART_WINDOW_S = 60;
const SVGNS = "http://www.w3.org/2000/svg";

let selectedVehicle = null;
let source = null;
let lastSnapshot = null;

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v == null ? "" : String(v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c != null) node.append(c);
  return node;
}
function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}
function setText(id, value, cls) {
  const node = $(id);
  node.textContent = value == null || value === "" ? "–" : String(value);
  if (cls !== undefined) node.className = node.className.replace(/\b(ok|warn|bad)-text\b/g, "").trim() + (cls ? " " + cls : "");
}
function setPill(id, text, level) { const p = $(id); p.textContent = text; p.className = "pill" + (level ? " " + level : ""); }
function fmtTime(iso) { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0"); }
function fmtNum(v, digits = 2) { return typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(digits)) : "–"; }
function short(id) { return id ? String(id).slice(0, 8) + "…" : "–"; }
function fmtDuration(s) { if (s == null) return "–"; s = Math.round(s); const m = Math.floor(s / 60); return m ? `${m}m ${s % 60}s` : `${s}s`; }
function fillBody(table, rows) { const body = $(table).tBodies[0]; body.replaceChildren(...rows); }

// ------------------------------------------------------------------ stream
function connect() {
  if (source) source.close();
  const q = selectedVehicle ? "?vehicle=" + encodeURIComponent(selectedVehicle) : "";
  source = new EventSource("/api/stream" + q);
  source.onopen = () => setPill("pill-stream", "dashboard: live", "ok");
  source.onerror = () => setPill("pill-stream", "dashboard backend: reconnecting…", "bad");
  source.onmessage = (msg) => {
    try { render(JSON.parse(msg.data)); } catch (e) { console.error("render failed", e); }
  };
}

// ------------------------------------------------------------------ render
function render(s) {
  lastSnapshot = s;
  renderStatus(s); renderMetrics(s); renderResilience(s); renderControls(s);
  renderVehicleSelect(s); renderCharts(s); renderLatest(s); renderVehicles(s);
  renderEvents(s); renderDiagnostics(s); renderActivity(s);
}

function renderStatus(s) {
  const g = s.gateway, live = g.live, t = s.telemetry, b = s.broker;
  if (live.running) {
    setText("gw-state", "running", "ok-text");
    $("gw-meta").textContent = `hosted live demo · vehicle ${live.vehicle_id} · up ${fmtDuration(live.uptime_seconds)}`;
    setText("gw-session", live.session_id);
    $("gw-session-meta").textContent = "run-level correlation ID (on every gateway log line and payload)";
    setText("gw-mqtt", live.mqtt_connected ? "connected" : "disconnected", live.mqtt_connected ? "ok-text" : "bad-text");
    $("gw-mqtt-meta").textContent = `${live.broker} · outage: ${live.outage}`;
  } else {
    setText("gw-state", g.mode === "scripted" ? "scripted resilience run" : "not hosted", g.mode === "scripted" ? "warn-text" : "");
    $("gw-meta").textContent = g.mode === "scripted" ? "see Activity for its stages" : "start the live demo, or watch an external gateway via MQTT";
    const latest = t.sessions_seen[0];
    setText("gw-session", latest || null);
    // Not necessarily an external gateway: after "Stop live demo" this is
    // usually the stopped hosted run's session.
    $("gw-session-meta").textContent = latest ? "latest session_id seen in telemetry payloads (no hosted run active)" : "no gateway session observed yet";
    setText("gw-mqtt", "unknown");
    $("gw-mqtt-meta").textContent = "only known for gateways hosted by the dashboard";
  }
  const flowCls = { flowing: "ok-text", stale: "warn-text", none: "" }[t.flow];
  setText("flow-state", t.flow === "none" ? "no telemetry yet" : t.flow, flowCls);
  $("flow-meta").textContent = `${t.total_received} received · last event ${fmtTime(t.last_event_timestamp)} · malformed ${t.malformed} · duplicates ${t.duplicates}`;
  setText("vehicle-count", t.vehicle_count);
  $("sessions-meta").textContent = `${t.sessions_seen.length} gateway session(s) seen in payloads`;
  const bs = { connected: "ok-text", connecting: "warn-text", disconnected: "bad-text" }[b.state] || "";
  setText("broker-state", b.state, bs);
  $("broker-meta").textContent = `${b.host}:${b.port} · ${b.topic_filter}` + (b.state !== "connected" && b.last_error ? ` · ${b.last_error}` : "");

  setPill("pill-broker", `broker ${b.host}:${b.port}: ${b.state}`, b.state === "connected" ? "ok" : b.state === "connecting" ? "warn" : "bad");
  setPill("pill-flow", `telemetry: ${t.flow === "none" ? "none yet" : t.flow} · ${t.rate_per_second}/s`, t.flow === "flowing" ? "ok" : t.flow === "stale" ? "warn" : "");
}

function renderMetrics(s) {
  const live = s.gateway.live, last = s.gateway.last_run;
  const m = live.running ? live.metrics : last ? last.metrics : null;
  $("metrics-source").textContent = live.running
    ? `GatewayMetrics snapshot for session ${short(live.session_id)} (cumulative since the run started)`
    : last ? `last hosted run (stopped ${fmtTime(last.stopped_at)}), final GatewayMetrics snapshot` : "no gateway run hosted by the dashboard yet (metrics live in the gateway's own process)";
  for (const k of ["processed", "rejected", "publish_failures", "buffered", "replayed", "dropped"]) setText("m-" + k, m ? m[k] : null);
  const depth = live.running ? live.buffer_depth : m ? m.buffer_pending : null;
  setText("m-depth", depth);
  // Only a running hosted gateway has a buffer to read "now"; otherwise this
  // is the stopped run's final pending count (its temp buffer is gone).
  $("m-depth-label").textContent = live.running || !m ? "Buffer depth (now)" : "Buffer depth (at stop)";
  $("m-depth-note").textContent = live.running || !m ? "rows in SQLite right now" : "pending rows when the last hosted run stopped";
  setText("m-rate", s.telemetry.rate_per_second);
  const sum = s.last_stopped_summary;
  $("last-summary").textContent = sum
    ? `Last "gateway stopped" summary (${fmtTime(sum.at)}): session ${short(sum.session_id)} · processed ${sum.processed} · rejected ${sum.rejected} · publish failures ${sum.publish_failures} · buffered ${sum.buffered} · replayed ${sum.replayed} · dropped ${sum.dropped} · pending ${sum.buffer_pending ?? "unknown"}`
    : "";
}

function renderResilience(s) {
  const r = s.gateway.live.resilience;
  const list = $("lifecycle");
  if (!list.children.length) LIFECYCLE.forEach((name) => list.append(el("li", { text: name, "data-state": name })));
  for (const li of list.children) li.className = li.dataset.state === r.state ? "active " + r.state : "";
  $("lifecycle-detail").textContent = `${r.state}: ${r.detail}`;
  const lvl = { NORMAL: "ok", RECOVERED: "ok", REPLAYING: "warn", RECONNECTING: "warn", BUFFERING: "bad", OUTAGE: "bad" }[r.state] || "";
  setPill("pill-resilience", "resilience: " + r.state, lvl);
  const lr = s.last_replay;
  $("replay-info").textContent = lr
    ? `Last replay batch: ${lr.replayed} event(s), buffer left ${lr.buffer_size}, ${lr.seconds_ago}s ago (session ${short(lr.session_id)})`
    : "No replay activity observed yet.";
  const ext = s.gateway.external_buffer;
  $("external-buffer").textContent = ext ? `External buffer ${ext.path}: depth ${ext.depth ?? "unavailable"} (read-only)` : "";
}

function renderControls(s) {
  const g = s.gateway, running = g.live.running, scripted = g.mode === "scripted";
  const enabled = {
    "live/start": !running && !scripted, "live/stop": running,
    "outage/inject": running && g.live.outage !== "active", "outage/clear": running && g.live.outage === "active",
    "resilience/run": !running && !scripted, "diagnostics/run": !g.diagnostics_running,
  };
  document.querySelectorAll("button[data-action]").forEach((b) => { b.disabled = !enabled[b.dataset.action]; });
  const sc = g.scripted;
  let text = "";
  if (sc.status === "running") text = "Scripted resilience run in progress…";
  else if (sc.status === "passed" || sc.status === "failed") {
    text = `Scripted run ${sc.status.toUpperCase()}: buffered ${sc.buffered_during_outage}, replayed ${sc.replayed}/${sc.replayed_received} received, FIFO ${sc.fifo_replay_verified ? "verified" : "NOT verified"}, final depth ${sc.final_buffer_depth ?? "?"}` + (sc.failure_reason ? ` (${sc.failure_reason})` : "");
  }
  $("scripted-result").textContent = text;
}

function renderVehicleSelect(s) {
  const sel = $("vehicle-select");
  const ids = s.vehicles.map((v) => v.vehicle_id);
  const current = Array.from(sel.options).map((o) => o.value);
  if (ids.join("|") !== current.join("|")) sel.replaceChildren(...ids.map((id) => el("option", { value: id, text: id })));
  if (s.selected_vehicle) sel.value = s.selected_vehicle;
}

function renderCharts(s) {
  const box = $("charts");
  const now = new Date(s.generated_at).getTime() / 1000;
  box.replaceChildren(...CHART_SIGNALS.map((name) => chart(name, s.signals[name], s.series[name] || [], now)));
  $("telemetry-empty").classList.toggle("hidden", s.vehicles.length > 0);
}

function chart(name, meta, points, now) {
  const W = 400, H = 150, L = 46, R = 8, T = 10, B = 22;
  const pts = points.filter(([t]) => t >= now - CHART_WINDOW_S);
  const wrap = el("div", { class: "chart" });
  const latest = pts.length ? pts[pts.length - 1][1] : null;
  wrap.append(el("div", { class: "chart-title" }, [
    el("span", { text: `${name} (${meta ? meta.unit : ""})` }),
    el("span", { class: "chart-now", text: latest == null ? "–" : fmtNum(latest) }),
  ]));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": name });
  let lo = pts.length ? Math.min(...pts.map((p) => p[1])) : 0, hi = pts.length ? Math.max(...pts.map((p) => p[1])) : 1;
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const x = (t) => L + ((t - (now - CHART_WINDOW_S)) / CHART_WINDOW_S) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  for (let i = 0; i <= 2; i++) {
    const v = lo + ((hi - lo) * i) / 2, yy = y(v);
    root.append(svg("line", { class: "grid-line", x1: L, x2: W - R, y1: yy, y2: yy }));
    root.append(svg("text", { x: L - 4, y: yy + 3, "text-anchor": "end" }, fmtNum(v, 1)));
  }
  root.append(svg("line", { class: "axis", x1: L, x2: L, y1: T, y2: H - B }));
  root.append(svg("line", { class: "axis", x1: L, x2: W - R, y1: H - B, y2: H - B }));
  for (const s of [-60, -30, 0]) root.append(svg("text", { x: x(now + s), y: H - 6, "text-anchor": s === -60 ? "start" : s === 0 ? "end" : "middle" }, s === 0 ? "now" : `${s}s`));
  if (pts.length > 1) root.append(svg("polyline", { class: "line", points: pts.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ") }));
  else root.append(svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, "no data in the last 60 s"));
  wrap.append(root);
  return wrap;
}

function renderLatest(s) {
  const v = s.vehicles.find((x) => x.vehicle_id === s.selected_vehicle);
  const rows = [];
  if (v) for (const [ecu, info] of Object.entries(v.ecus))
    for (const [sig, val] of Object.entries(info.signals).sort())
      rows.push(el("tr", {}, [el("td", { text: ecu }), el("td", { text: sig }), el("td", { class: "num", text: fmtNum(val.value) }),
        el("td", { text: val.unit }), el("td", { text: fmtTime(val.timestamp) }), el("td", { class: "mono", text: short(val.event_id) })]));
  fillBody("latest-table", rows);
}

function renderVehicles(s) {
  const rows = [];
  for (const v of s.vehicles) for (const [ecu, info] of Object.entries(v.ecus)) {
    const vals = Object.entries(info.signals).map(([k, x]) => `${k}=${fmtNum(x.value, 1)}`).join("  ");
    rows.push(el("tr", {}, [el("td", { class: "mono", text: v.vehicle_id }), el("td", { text: ecu }), el("td", { class: "num", text: info.event_count }),
      el("td", { text: fmtTime(info.last_seen) }), el("td", { class: "wrap", text: vals })]));
  }
  fillBody("vehicles-table", rows);
}

function renderEvents(s) {
  fillBody("events-table", s.events.map((e) => el("tr", {}, [
    el("td", { text: fmtTime(e.timestamp) }), el("td", {}, el("span", { class: "tag " + e.status, text: e.status })),
    el("td", { class: "num", text: fmtNum(e.lag_seconds) }), el("td", { class: "mono", text: e.vehicle_id }),
    el("td", { text: `${e.source_ecu} / ${e.signal_name}` }), el("td", { class: "num", text: `${fmtNum(e.value)} ${e.unit}` }),
    el("td", { class: "mono", text: e.event_id }), el("td", { class: "mono", text: short(e.session_id) }), el("td", { class: "mono", text: e.topic }),
  ])));
}

function renderDiagnostics(s) {
  const d = s.diagnostics;
  $("diag-status").textContent = d.status === "not run" ? "Not run yet. Use “Run UDS diagnostic session”."
    : d.status === "running" ? "UDS session running over the virtual CAN bus…"
    : d.status === "failed" ? `UDS session failed: ${d.error}`
    : `Completed ${fmtTime(d.finished_at)} for ${d.vehicle_id}: ${d.events.length} diagnostic events, ${d.anomalies.length} deterministic finding(s). LLM: ${d.llm}.`;
  fillBody("anomaly-table", d.anomalies.map((a) => el("tr", {}, [
    el("td", {}, el("span", { class: "tag " + a.severity, text: a.severity })), el("td", { class: "mono", text: a.rule_id }),
    el("td", { class: "wrap", text: `${a.title}: ${a.description}` }), el("td", { class: "num", text: a.event_ids.length }),
    el("td", { class: "wrap muted", text: a.llm_explanation || "not requested (advisory, optional)" }),
  ])));
  fillBody("diag-table", d.events.map((e) => el("tr", {}, [
    el("td", { text: fmtTime(e.timestamp) }), el("td", { text: e.service_name }), el("td", { class: "mono", text: e.request_summary }),
    el("td", { class: "wrap", text: e.response_summary }),
    el("td", {}, el("span", { class: "tag " + (e.is_positive_response ? "live" : "critical"), text: e.is_positive_response ? "positive" : (e.negative_response_code || "negative") })),
  ])));
}

function renderActivity(s) {
  fillBody("activity-table", s.activity.map((a) => el("tr", {}, [
    el("td", { text: fmtTime(a.at) }), el("td", {}, el("span", { class: "tag " + a.level, text: a.level })),
    el("td", { class: "mono", text: a.source }), el("td", { class: "wrap", text: a.message }), el("td", { class: "num", text: a.count }),
    el("td", { class: "wrap mono", text: Object.entries(a.fields).map(([k, v]) => `${k}=${v}`).join("  ") }),
  ])));
}

// ------------------------------------------------------------------ actions
async function perform(action, button) {
  button.disabled = true;
  const toast = $("toast");
  toast.textContent = "…";
  try {
    const res = await fetch("/api/actions/" + action, { method: "POST", headers: { "X-Dashboard-Action": "1" } });
    const body = await res.json();
    toast.textContent = body.ok ? body.message : "Not done: " + body.error;
    toast.className = "toast " + (body.ok ? "ok-text" : "warn-text");
  } catch (e) {
    toast.textContent = "Dashboard backend unreachable"; toast.className = "toast bad-text";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("button[data-action]").forEach((b) => b.addEventListener("click", () => perform(b.dataset.action, b)));
  $("vehicle-select").addEventListener("change", (e) => { selectedVehicle = e.target.value; connect(); });
  connect();
});
