// Guardian dashboard: plain fetch + Server-Sent Events, no build step.

const state = { home: null, devices: [], incidents: [], open: new Set(), details: {} };
const TERMINAL = ["contained", "escalated", "resolved", "false_alarm"];
const SCENARIOS = {
  leak_empty_home: "Leak, empty home",
  leak_valve_fault: "Leak, valve fault",
  leak_single_sensor: "Leak, single sensor",
  night_entry: "Night entry",
  caregiver_no_show: "Caregiver no-show",
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tz = () => (state.home && state.home.timezone) || "Asia/Kolkata";
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString("en-GB", { timeZone: tz() }) : "—");
const date = (iso) => new Date(iso).toLocaleDateString("en-GB", { timeZone: tz(), day: "numeric", month: "short" });

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const msg = await res.text();
    toast(`Request failed: ${msg}`, "error");
    throw new Error(msg);
  }
  return res.json();
}

const STATUS_STYLE = {
  open: "bg-sky-100 text-sky-800",
  awaiting_approval: "bg-amber-100 text-amber-800",
  acting: "bg-indigo-100 text-indigo-800",
  verifying: "bg-indigo-100 text-indigo-800",
  contained: "bg-emerald-100 text-emerald-800",
  resolved: "bg-emerald-100 text-emerald-800",
  false_alarm: "bg-slate-200 text-slate-700",
  escalated: "bg-red-100 text-red-800",
  completed: "bg-emerald-100 text-emerald-800",
  late: "bg-amber-100 text-amber-800",
  short: "bg-amber-100 text-amber-800",
  no_show: "bg-red-100 text-red-800",
  unverified: "bg-slate-200 text-slate-700",
};
const pill = (s) => `<span class="text-xs font-medium rounded px-1.5 py-0.5 ${STATUS_STYLE[s] || "bg-slate-100"}">${esc(s.replace("_", " "))}</span>`;
const simBadge = `<span class="text-[10px] font-medium bg-amber-100 text-amber-800 rounded px-1 py-0.5">Simulated</span>`;

// ------------------------------------------------------------------ status bar

function renderStatus() {
  const h = state.home;
  if (!h) return;
  $("#home-name").textContent = `${h.name} · ${tz()}`;
  document.querySelectorAll("#mode-switch button").forEach((b) => {
    b.className = "px-2.5 sm:px-3 py-1.5 " + (b.dataset.mode === h.mode ? "bg-white text-slate-900 font-semibold" : "text-slate-300 hover:bg-slate-800");
  });
  const active = state.incidents.filter((i) => !TERMINAL.includes(i.status));
  const head = active.length
    ? `<div class="flex items-center gap-2"><span class="dot bg-red-500"></span><span class="font-semibold">${esc(active[0].title)}</span>${pill(active[0].status)}</div>
       <p class="text-sm text-slate-600 mt-1">${active.length > 1 ? `${active.length} active incidents · ` : ""}Step: ${esc(active[0].current_step)}</p>`
    : `<div class="flex items-center gap-2"><span class="dot bg-emerald-500"></span><span class="font-semibold">All clear</span></div>`;
  $("#status").innerHTML = `${head}
    <p class="text-sm mt-2"><span class="text-slate-500">Occupancy:</span> <b>${esc(h.occupancy)}</b>
    <span class="text-slate-500">(${Math.round(h.occupancy_confidence * 100)}%)</span> — ${h.reasons.map(esc).join("; ")}</p>`;
}

// ------------------------------------------------------------------ devices

function deviceState(d) {
  if (d.kind === "flood_sensor") return d.state.wet ? `<b class="text-red-700">Wet</b>` : "Dry";
  if (d.kind === "contact_sensor") return d.state.open ? `<b class="text-amber-700">Open</b>` : "Closed";
  if (d.kind === "valve") return `Valve <b>${esc(d.state.position || "?")}</b>`;
  return d.online ? "Online" : "Offline";
}

function renderDevices() {
  $("#devices").innerHTML = state.devices
    .map((d) => {
      const health = !d.online ? "bg-red-500" : d.usable ? "bg-emerald-500" : "bg-amber-500";
      const valveControls =
        d.simulated && d.kind === "valve"
          ? `<div class="mt-2 flex gap-2 text-xs">
               <button class="border rounded px-2 py-0.5 hover:bg-slate-50" onclick="simulate('${d.id}','fault_on_close')">Jam on next close</button>
               <button class="border rounded px-2 py-0.5 hover:bg-slate-50" onclick="simulate('${d.id}','reset')">Reset valve</button>
             </div>`
          : "";
      return `<div class="bg-white rounded-xl shadow-sm p-3">
        <div class="flex items-center justify-between gap-2">
          <div class="flex items-center gap-2"><span class="dot ${health}" title="${esc(d.problems.join("; ") || "healthy")}"></span>
          <span class="font-medium">${esc(d.name)}</span></div>
          ${d.simulated ? simBadge : `<span class="text-[10px] text-slate-500">Ring</span>`}
        </div>
        <div class="text-xs text-slate-500 mt-1">${esc(d.room || "—")} · ${esc(d.kind.replace("_", " "))}${d.battery_pct != null ? ` · ${d.battery_pct}%` : ""}</div>
        <div class="text-sm mt-1">${deviceState(d)}</div>
        ${d.problems.length ? `<div class="text-xs text-amber-700 mt-1">${d.problems.map(esc).join("; ")}</div>` : ""}
        ${valveControls}
      </div>`;
    })
    .join("");
}

async function simulate(id, action) {
  await api(`/devices/${id}/simulate`, { method: "POST", body: { action } });
  refresh("devices");
}

// ------------------------------------------------------------------ incidents

const KIND_STYLE = {
  signal: ["Signal", "bg-sky-500"],
  home_state: ["Home", "bg-violet-500"],
  device_health: ["Health", "bg-slate-400"],
  snapshot: ["Snapshot", "bg-cyan-600"],
  policy_decision: ["Policy", "bg-amber-500"],
  human_decision: ["Owner", "bg-pink-500"],
  verification: ["Verify", "bg-emerald-600"],
  action: ["Action", "bg-indigo-600"],
};

function timelineItem(item) {
  const [label, color] = KIND_STYLE[item.kind] || [item.kind, "bg-slate-400"];
  let body = esc(item.summary);
  if (item.kind === "policy_decision" && item.payload.reasons) {
    body += `<ul class="list-disc ml-5 text-xs text-slate-600 mt-1">${item.payload.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`;
  }
  if (item.kind === "snapshot" && item.payload.data_uri) {
    body += `<div class="mt-1"><img src="${item.payload.data_uri}" alt="camera snapshot" class="w-40 h-24 object-cover rounded border">
             <div class="text-[10px] text-slate-500">ring-sandbox emulator image. No facial recognition is performed.</div></div>`;
  }
  if (item.kind === "verification" && item.payload.passed === false) body = `<span class="text-red-700">${body}</span>`;
  return `<li class="flex gap-2 py-1 px-1 rounded transition-colors" id="row-${item.rowId || item.id}">
    <span class="text-xs text-slate-500 w-16 shrink-0 pt-0.5">${time(item.created_at)}</span>
    <span class="text-[10px] text-white rounded px-1.5 h-4 leading-4 mt-0.5 shrink-0 ${color}">${label}</span>
    <div class="text-sm min-w-0">${body}</div></li>`;
}

function actionAsItem(a) {
  const target = state.devices.find((d) => d.id === a.target_device_id);
  const approval = a.approval_status !== "n/a" ? `, approval ${a.approval_status}` : "";
  return {
    kind: "action",
    rowId: a.id,
    created_at: a.created_at,
    summary: `${a.action_type}${target ? " → " + target.name : ""}: policy ${a.policy_outcome}${approval}, result ${a.result || "not run"}, verified ${a.verified}`,
    payload: {},
  };
}

function approvalCard(inc) {
  const pending = (inc.actions || []).filter((a) => a.approval_status === "pending");
  return pending
    .map(
      (a) => `<div class="border border-amber-300 bg-amber-50 rounded-lg p-3 mt-3">
        <div class="font-medium text-sm">Approval needed: ${esc(a.action_type)}</div>
        <ul class="list-disc ml-5 text-xs text-slate-700 mt-1">${a.policy_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <div class="flex items-center gap-2 mt-2">
          <button class="bg-emerald-600 text-white text-sm rounded px-3 py-1" onclick="decideAction('${a.id}','approve')">Approve</button>
          <button class="bg-white border text-sm rounded px-3 py-1" onclick="decideAction('${a.id}','reject')">Reject</button>
          <span class="text-xs text-slate-600 ml-auto">expires in <b data-expires="${a.expires_at}">…</b></span>
        </div></div>`
    )
    .join("");
}

function decisionCard(inc) {
  const decided = (inc.evidence || []).some((e) => e.kind === "human_decision");
  if (inc.playbook_id !== "suspicious_entry" || inc.status !== "awaiting_approval" || decided) return "";
  const btn = (choice, label, cls) => `<button class="${cls} text-sm rounded px-3 py-1" onclick="decide('${inc.id}','${choice}')">${label}</button>`;
  return `<div class="border border-amber-300 bg-amber-50 rounded-lg p-3 mt-3">
    <div class="font-medium text-sm">What should Guardian do?</div>
    <div class="flex flex-wrap gap-2 mt-2">
      ${btn("known_person", "Known person", "bg-white border")}
      ${btn("handle_it", "I'll handle it", "bg-white border")}
      ${btn("escalate", "Escalate", "bg-red-600 text-white")}
    </div></div>`;
}

function renderIncidents() {
  if (!state.incidents.length) {
    $("#incidents").innerHTML = `<p class="text-sm text-slate-500">No incidents yet. Run a demo scenario below.</p>`;
    return;
  }
  $("#incidents").innerHTML = state.incidents
    .map((i) => {
      const d = state.details[i.id];
      const isOpen = state.open.has(i.id);
      let inner = "";
      if (isOpen && d) {
        const items = [...d.evidence, ...d.actions.map(actionAsItem)].sort((a, b) => a.created_at.localeCompare(b.created_at));
        inner = `<div class="border-t mt-3 pt-2">
          ${decisionCard(d)}${approvalCard(d)}
          <ol class="mt-2 divide-y divide-slate-100">${items.map(timelineItem).join("")}</ol>
          ${reportBlock(d)}
        </div>`;
      }
      return `<div class="bg-white rounded-xl shadow-sm p-3">
        <button class="w-full text-left" onclick="toggleIncident('${i.id}')">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">${esc(i.title)}</span>${pill(i.status)}
          </div>
          <div class="text-xs text-slate-500 mt-1">${date(i.opened_at)} ${time(i.opened_at)} · ${esc(i.severity)} · confidence ${Math.round(i.confidence * 100)}% · ${esc(i.current_step)}</div>
        </button>${inner}</div>`;
    })
    .join("");
  tickCountdowns();
}

const badge = (text, cls) => `<span class="text-[10px] font-medium rounded px-1.5 py-0.5 ${cls}">${text}</span>`;

function reportBlock(d) {
  const r = d.llm_report;
  if (r) {
    const chip = (c) => {
      const ref = r.refs[c];
      return `<button class="text-[10px] font-mono bg-sky-100 text-sky-800 rounded px-1 ml-0.5 hover:bg-sky-200" title="Show evidence ${c}"
        onclick="highlightRow('${ref ? ref.id : ""}')">${esc(c)}</button>`;
    };
    return `<div class="mt-3"><div class="flex flex-wrap items-center gap-2">
        <h4 class="font-medium text-sm">Report</h4>${badge("Validated against evidence", "bg-emerald-100 text-emerald-800")}
        <span class="text-[10px] text-slate-500">AI-written (${esc(r.model_id || "Bedrock")}); every sentence cites evidence</span></div>
      <div class="text-sm bg-slate-50 border rounded p-2 mt-1 space-y-1">
        <div class="font-medium">${esc(r.title)}</div><div class="text-slate-700">${esc(r.summary)}</div>
        <ul class="list-disc ml-5">${r.sentences.map((s) => `<li>${esc(s.text)} ${s.cites.map(chip).join("")}</li>`).join("")}</ul>
        ${r.open_questions.length ? `<div class="text-xs text-slate-600">Open questions: ${r.open_questions.map(esc).join("; ")}</div>` : ""}
      </div>
      <details class="mt-1"><summary class="text-xs text-slate-500">Template report</summary>
        <pre class="text-xs bg-slate-50 border rounded p-2 mt-1 whitespace-pre-wrap">${esc(d.report_text)}</pre></details></div>`;
  }
  if (!d.report_text) return "";
  return `<div class="mt-3"><div class="flex items-center gap-2"><h4 class="font-medium text-sm">Report</h4>${badge("Template report", "bg-slate-200 text-slate-700")}</div>
    <pre class="text-xs bg-slate-50 border rounded p-2 mt-1 whitespace-pre-wrap">${esc(d.report_text)}</pre></div>`;
}

function highlightRow(rowId) {
  const el = document.getElementById("row-" + rowId);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("bg-yellow-100");
  setTimeout(() => el.classList.remove("bg-yellow-100"), 2000);
}

async function toggleIncident(id) {
  if (state.open.has(id)) state.open.delete(id);
  else {
    state.open.add(id);
    state.details[id] = await api(`/incidents/${id}`);
  }
  renderIncidents();
}

async function decideAction(id, verb) {
  await api(`/actions/${id}/${verb}`, { method: "POST" });
}

async function decide(id, choice) {
  await api(`/incidents/${id}/decision`, { method: "POST", body: { choice } });
}

function tickCountdowns() {
  document.querySelectorAll("[data-expires]").forEach((el) => {
    const secs = Math.max(0, Math.round((new Date(el.dataset.expires) - new Date(state.home.now) - (Date.now() - state.homeFetchedAt)) / 1000));
    el.textContent = `${secs}s`;
  });
}
setInterval(tickCountdowns, 1000);

// ------------------------------------------------------------------ care visits

async function renderVisits() {
  const visits = await api("/care/visits");
  $("#visits").innerHTML = visits.length
    ? visits
        .map(
          (v) => `<tr class="border-b last:border-0">
          <td class="p-2">${esc(v.date)}</td><td class="p-2">${esc(v.caregiver)}</td><td class="p-2">${esc(v.window)}</td>
          <td class="p-2">${time(v.arrived_at)}</td><td class="p-2">${time(v.departed_at)}</td>
          <td class="p-2">${v.duration_min ?? "—"}</td><td class="p-2">${pill(v.status)}</td></tr>`
        )
        .join("")
    : `<tr><td class="p-2 text-slate-500" colspan="7">No visits checked yet.</td></tr>`;
}

// ------------------------------------------------------------------ demo panel

function renderDemo() {
  $("#demo").innerHTML =
    Object.entries(SCENARIOS)
      .map(([name, label]) => `<button class="bg-slate-900 text-white text-sm rounded px-3 py-1.5" onclick="runScenario('${name}')">${label}</button>`)
      .join("") + `<button class="border text-sm rounded px-3 py-1.5" onclick="resetDemo()">Reset</button>`;
}

async function runScenario(name) {
  toast(`Running ${SCENARIOS[name]}…`);
  state.open.clear();
  await api(`/demo/run/${name}`, { method: "POST" });
}

async function resetDemo() {
  state.open.clear();
  await api("/demo/reset", { method: "POST" });
  toast("Home reset");
}

// ------------------------------------------------------------------ toasts

function toast(text, kind = "info") {
  const el = document.createElement("div");
  el.className = `rounded-lg shadow-lg p-3 text-sm ${kind === "error" ? "bg-red-600 text-white" : kind === "alert" ? "bg-amber-500 text-slate-900" : "bg-slate-800 text-white"}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

// ------------------------------------------------------------------ data + live updates

async function refresh(what) {
  if (what === "home" || what === "all") {
    state.home = await api("/home");
    state.homeFetchedAt = Date.now();
  }
  if (what === "devices" || what === "all") {
    state.devices = await api("/devices");
    renderDevices();
  }
  if (what === "incidents" || what === "all") {
    state.incidents = await api("/incidents");
    for (const id of state.open) state.details[id] = await api(`/incidents/${id}`);
    // auto-expand the newest active incident so the live timeline is visible
    const active = state.incidents.find((i) => !TERMINAL.includes(i.status));
    if (active && !state.open.has(active.id)) {
      state.open.add(active.id);
      state.details[active.id] = await api(`/incidents/${active.id}`);
    }
    renderIncidents();
  }
  if (what === "care" || what === "all") await renderVisits();
  renderStatus();
}

const pending = new Set();
let timer = null;
function schedule(what) {
  pending.add(what);
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const jobs = pending.has("all") ? ["all"] : [...pending];
    pending.clear();
    for (const j of jobs) await refresh(j);
  }, 150);
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.topic === "notification") {
      toast(`To ${msg.to}: ${msg.message}`, "alert");
      return;
    }
    if (msg.topic === "reset") return schedule("all");
    if (msg.topic === "incident") {
      schedule("incidents");
      schedule("home");
    }
    if (msg.topic === "device" || msg.topic === "signal") {
      schedule("devices");
      schedule("home");
    }
    if (msg.topic === "home") schedule("home");
    if (msg.topic === "care") schedule("care");
  };
  es.onerror = () => {
    es.close();
    setTimeout(connect, 2000);
  };
}

document.querySelectorAll("#mode-switch button").forEach((b) =>
  b.addEventListener("click", async () => {
    state.home = await api("/home", { method: "PATCH", body: { mode: b.dataset.mode } });
    state.homeFetchedAt = Date.now();
    renderStatus();
  })
);

renderDemo();
refresh("all");
connect();
