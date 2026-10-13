"use strict";

const HASH = new URLSearchParams(location.hash.slice(1));
const TOKEN = HASH.get("token") || sessionStorage.getItem("cs2hl-token");
if (TOKEN) sessionStorage.setItem("cs2hl-token", TOKEN);

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let S = null;            // last /api/state
let jobId = null;        // job shown in the panel
let logSeen = 0;
let steamPages = 1;
let loginTimer = null;

// ---------------------------------------------------------------- api

async function api(path, body) {
  const opts = { headers: { "X-Token": TOKEN || "" } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw { title: "The app stopped", why: "The background process is not running anymore.", fix: "Close this window and start CS2 Highlight Maker again." };
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw data.error || { title: `HTTP ${res.status}`, why: "", fix: "" };
  return data;
}

function toast(title, text = "", error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<b>${esc(title)}</b>${text ? `<span class="muted small">${esc(text)}</span>` : ""}`;
  $("#toast").append(el);
  setTimeout(() => el.remove(), error ? 9000 : 3000);
}

function errorHtml(e) {
  return `<b>${esc(e.title)}</b>${e.why ? `<div class="muted">${esc(e.why)}</div>` : ""}${e.fix ? `<div class="fix">👉 ${esc(e.fix)}</div>` : ""}`;
}
const fail = (e) => toast(e.title || "Error", [e.why, e.fix].filter(Boolean).join(" — "), true);

// ---------------------------------------------------------------- navigation

function showPage(name) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
  if (name === "videos") loadVideos();
  if (name === "steam") renderSteam();
}
$$("nav button").forEach((b) => (b.onclick = () => showPage(b.dataset.page)));

$$(".tabs > button[data-tab]").forEach((b) => (b.onclick = () => {
  $$(".tabs > button[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "local") loadLocal();
}));

// ---------------------------------------------------------------- state

async function refreshState() {
  try {
    S = await api("/api/state");
  } catch (e) {
    $("#chips").innerHTML = `<div class="chip bad">${esc(e.title)}</div>`;
    return;
  }
  const chips = [
    S.session ? ["ok", `Steam: ${S.session.account_name || "logged in"}`] : ["warn", "Steam: not logged in"],
    S.cs2_dir ? ["ok", "CS2 found"] : ["bad", "CS2 not found"],
    S.plugin_ok ? ["ok", "Recorder ready"] : ["bad", "Recorder missing"],
    S.steam_running ? ["ok", "Steam running"] : ["warn", "Steam closed (auto-starts)"],
  ];
  $("#chips").innerHTML = chips.map(([c, t]) => `<div class="chip ${c}">${esc(t)}</div>`).join("");
  $("#replays-dir").textContent = S.cs2_dir ? S.cs2_dir + "/game/csgo/replays" : "(CS2 not found)";
  $("#out-dir").textContent = S.clips_dir.replace(/\/clips$/, "");
  $("#work-dir").textContent = S.work_dir;
  $("#btn-latest").classList.toggle("disabled", !S.session);
  $("#latest-hint").innerHTML = S.session
    ? "Downloads your newest Premier / Competitive / Wingman replay from Steam, renders it, then deletes the replay."
    : `Log in on the <a href="#" onclick="showPage('steam');return false">Steam account</a> page first, or use a demo from this PC below.`;
  if (!jobId && S.current_job) watchJob(S.current_job);
  else if (!jobId && S.jobs.length) watchJob(S.jobs[0].id);
}

// ---------------------------------------------------------------- settings

function fillSettings() {
  const c = S.config;
  $$("#mode-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.mode === c.mode));
  $$("#mode-cards .choice").forEach((el) => {
    el.classList.toggle("selected", el.dataset.mode === c.mode);
    $("input", el).checked = el.dataset.mode === c.mode;
  });
  $$("#xhair-cards .choice").forEach((el) => {
    el.classList.toggle("selected", el.dataset.xhair === c.crosshair);
    $("input", el).checked = el.dataset.xhair === c.crosshair;
    drawCrosshair($("canvas", el), crosshairCvars(el.dataset.xhair), 3);
  });
  fillCustomEditor();

  const sel = $("#cs2-select");
  const opts = [["", `Automatic (${S.installs[0] ? S.installs[0].path : "none found"})`]];
  for (const i of S.installs) opts.push([i.path, `${i.path}  ·  updated ${new Date(i.updated * 1000).toLocaleDateString()}`]);
  if (c.cs2_dir && !S.installs.some((i) => i.path === c.cs2_dir)) opts.push([c.cs2_dir, c.cs2_dir]);
  sel.innerHTML = opts.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  sel.value = c.cs2_dir;
  $("#cs2-hint").innerHTML = S.cs2_error ? `<span style="color:var(--bad)">${esc(S.cs2_error.title)}: ${esc(S.cs2_error.fix)}</span>`
    : S.installs.length > 1 ? "Several CS2 installs found. Automatic picks the one Steam updated most recently." : "";

  $("#output-dir").value = c.output_dir;
  $("#keep-demos").checked = c.keep_downloaded_demos;
  $("#min-kills").value = String(c.min_kills);
  $("#clutch-min").value = String(c.clutch_min_enemies);
  $("#pre-seconds").value = c.pre_seconds;
  $("#post-seconds").value = c.post_seconds;
  $("#max-gap").value = c.max_gap_seconds;
  $("#resolution").value = `${c.width}x${c.height}`;
  $("#codec").value = c.video_codec;
  $("#quality").value = c.quality;
  qualityLabel();
  $("#steamids").value = c.steamids.join(", ");
  $("#accounts").innerHTML = S.accounts.length
    ? S.accounts.map((a) => `<span class="tag">${esc(a.name)} · ${esc(a.steamid)}</span>`).join(" ")
    : `<span class="muted">No Steam accounts found on this PC.</span>`;
}

async function save(changes, quiet = false) {
  try {
    S = { ...S, ...(await api("/api/config", changes)) };
    fillSettings();
    if (!quiet) toast("Saved");
  } catch (e) {
    fail(e);
    fillSettings();
  }
}

$$("#mode-toggle button").forEach((b) => (b.onclick = () => save({ mode: b.dataset.mode })));
$$("#mode-cards input").forEach((i) => (i.onchange = () => save({ mode: i.value })));
$$("#xhair-cards input").forEach((i) => (i.onchange = () => save({ crosshair: i.value })));
$("#cs2-select").onchange = (e) => save({ cs2_dir: e.target.value });
$("#output-dir").onchange = (e) => save({ output_dir: e.target.value.trim() });
$("#keep-demos").onchange = (e) => save({ keep_downloaded_demos: e.target.checked });
$("#min-kills").onchange = (e) => save({ min_kills: +e.target.value });
$("#clutch-min").onchange = (e) => save({ clutch_min_enemies: +e.target.value });
$("#pre-seconds").onchange = (e) => save({ pre_seconds: +e.target.value });
$("#post-seconds").onchange = (e) => save({ post_seconds: +e.target.value });
$("#max-gap").onchange = (e) => save({ max_gap_seconds: +e.target.value });
$("#resolution").onchange = (e) => { const [w, h] = e.target.value.split("x").map(Number); save({ width: w, height: h }); };
$("#codec").onchange = (e) => save({ video_codec: e.target.value });
$("#quality").oninput = qualityLabel;
$("#quality").onchange = (e) => save({ quality: +e.target.value });
$("#steamids").onchange = (e) => save({ steamids: e.target.value.split(/[\s,]+/).filter((s) => /^7656\d{13}$/.test(s)) });

function qualityLabel() {
  const q = +$("#quality").value;
  $("#quality-label").textContent = q <= 18 ? `${q} · near lossless, big files` : q <= 23 ? `${q} · high (recommended)` : q <= 26 ? `${q} · medium, smaller files` : `${q} · low, smallest files`;
}

// ---------------------------------------------------------------- crosshair

function crosshairCvars(name) {
  const fixed = { cl_crosshairoutline_r: 0, cl_crosshairoutline_g: 0, cl_crosshairoutline_b: 0, cl_crosshairoutline_a: 255 };
  return { ...(name === "custom" ? S.config.custom_crosshair : S.crosshairs[name]), ...fixed };
}

// Approximation of CS2's pixel crosshair, drawn at `scale` screen pixels per game pixel.
function drawCrosshair(canvas, cv, scale) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  const sky = ctx.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, "#9fc3dd"); sky.addColorStop(0.55, "#d9c9a3"); sky.addColorStop(0.56, "#b89a6a"); sky.addColorStop(1, "#8a7150");
  ctx.fillStyle = sky; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "#c8b48a"; ctx.fillRect(W * 0.62, H * 0.18, W * 0.3, H * 0.4);

  const t = Math.max(1, +cv.cl_crosshair_thickness || 1), L = +cv.cl_crosshair_length || 0, g = +cv.cl_crosshair_gap || 0;
  const color = `rgba(${cv.cl_crosshaircolor_r},${cv.cl_crosshaircolor_g},${cv.cl_crosshaircolor_b},${(cv.cl_crosshaircolor_a ?? 255) / 255})`;
  const cx = Math.round(W / 2 / scale), cy = Math.round(H / 2 / scale);
  const half = t / 2, off = half + g;
  const rects = [];
  if (L > 0) {
    rects.push([cx + off, cy - half, L, t], [cx - off - L, cy - half, L, t], [cx - half, cy + off, t, L]);
    if (!+cv.cl_crosshair_t) rects.push([cx - half, cy - off - L, t, L]);
  }
  if (+cv.cl_crosshairdot) rects.push([cx - half, cy - half, t, t]);
  if (+cv.cl_crosshair_drawoutline) {
    ctx.fillStyle = "#000";
    for (const [x, y, w, h] of rects) ctx.fillRect((x - 1) * scale, (y - 1) * scale, (w + 2) * scale, (h + 2) * scale);
  }
  ctx.fillStyle = color;
  for (const [x, y, w, h] of rects) ctx.fillRect(x * scale, y * scale, w * scale, h * scale);
}

function fillCustomEditor() {
  const cv = S.config.custom_crosshair;
  $("#xhair-editor").classList.toggle("disabled", S.config.crosshair !== "custom");
  $$("#xhair-editor [data-cv]").forEach((el) => {
    const v = cv[el.dataset.cv] ?? 0;
    if (el.type === "checkbox") el.checked = !!+v;
    else { el.value = v; el.nextElementSibling && (el.nextElementSibling.textContent = v); }
  });
  const hex = (n) => (+n).toString(16).padStart(2, "0");
  $("#xhair-color").value = `#${hex(cv.cl_crosshaircolor_r)}${hex(cv.cl_crosshaircolor_g)}${hex(cv.cl_crosshaircolor_b)}`;
  drawCrosshair($("#xhair-big"), crosshairCvars("custom"), 6);
}

function readCustomEditor() {
  const cv = { ...S.config.custom_crosshair };
  $$("#xhair-editor [data-cv]").forEach((el) => {
    cv[el.dataset.cv] = el.type === "checkbox" ? (el.checked ? 1 : 0) : +el.value;
    if (el.type === "range") el.nextElementSibling.textContent = el.value;
  });
  const hex = $("#xhair-color").value;
  cv.cl_crosshaircolor_r = parseInt(hex.slice(1, 3), 16);
  cv.cl_crosshaircolor_g = parseInt(hex.slice(3, 5), 16);
  cv.cl_crosshaircolor_b = parseInt(hex.slice(5, 7), 16);
  cv.cl_crosshairstyle = 4;
  return cv;
}
$$("#xhair-editor input").forEach((el) => {
  el.oninput = () => {
    const cv = readCustomEditor();
    drawCrosshair($("#xhair-big"), { ...cv, cl_crosshairoutline_r: 0, cl_crosshairoutline_g: 0, cl_crosshairoutline_b: 0 }, 6);
    drawCrosshair($('#xhair-cards [data-xhair="custom"] canvas'), cv, 3);
  };
  el.onchange = () => save({ custom_crosshair: readCustomEditor() }, true);
});

// ---------------------------------------------------------------- folder picker

let pickerResolve = null;
async function pickFolder(title, start) {
  $("#picker-title").textContent = title;
  await pickerLoad(start || "");
  $("#picker").showModal();
  return new Promise((r) => (pickerResolve = r));
}
async function pickerLoad(path) {
  try {
    const d = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    $("#picker-path").value = d.path;
    $("#picker-note").textContent = d.is_cs2 ? "✔ This is a CS2 install" : "";
    $("#picker-roots").innerHTML = d.roots.map((r) => `<button class="btn small" data-p="${esc(r)}">${esc(r.split("/").pop() || r)}</button>`).join("");
    $("#picker-list").innerHTML = `<div data-p="${esc(d.parent)}">⬆ ..</div>` + d.dirs.map((n) => `<div data-p="${esc(d.path + "/" + n)}">📁 ${esc(n)}</div>`).join("");
    $$("#picker [data-p]").forEach((el) => (el.onclick = () => pickerLoad(el.dataset.p)));
  } catch (e) { fail(e); }
}
$("#picker-go").onclick = () => pickerLoad($("#picker-path").value);
$("#picker-close").onclick = () => { $("#picker").close(); pickerResolve?.(null); };
$("#picker-ok").onclick = () => { $("#picker").close(); pickerResolve?.($("#picker-path").value); };

$("#cs2-browse").onclick = async () => {
  const p = await pickFolder("Choose the 'Counter-Strike Global Offensive' folder", S.cs2_dir);
  if (p) save({ cs2_dir: p });
};
$("#out-browse").onclick = async () => {
  const p = await pickFolder("Choose where videos are saved", S.config.output_dir || S.work_dir);
  if (p) save({ output_dir: p });
};
$("#btn-open-out").onclick = () => api("/api/open", { path: S.clips_dir.replace(/\/clips$/, "") }).catch(fail);

// ---------------------------------------------------------------- matches

const fmtTime = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";

async function loadSteam(refresh = false) {
  const box = $("#steam-list");
  if (!S?.session) {
    box.innerHTML = `<div class="empty">Log in on the <a href="#" onclick="showPage('steam');return false">Steam account</a> page to see your matches.</div>`;
    $("#btn-more").classList.add("hidden");
    return;
  }
  box.innerHTML = `<div class="empty">Loading your matches from Steam…</div>`;
  try {
    const list = await api(`/api/matches?pages=${steamPages}${refresh ? "&refresh=1" : ""}`);
    $("#btn-more").classList.remove("hidden");
    if (!list.length) { box.innerHTML = `<div class="empty">Steam lists no matches yet.</div>`; return; }
    box.innerHTML = "";
    for (const m of list) {
      const el = document.createElement("div");
      el.className = "item";
      el.innerHTML = `<div class="meta">
          <span class="map">${esc(m.map)}</span>
          <span class="tag">${esc(m.mode)}</span>
          <span class="tag ${m.result}">${esc(m.score || "–")} ${m.result ? esc(m.result) : ""}</span>
          <span class="muted small">${esc(m.kills)} / ${esc(m.assists)} / ${esc(m.deaths)} K/A/D</span>
          <span class="muted small">${esc(fmtTime(m.time))}</span>
          ${m.rendered ? `<span class="tag done">✔ made</span>` : ""}
        </div>
        <div class="actions">${m.url ? `<button class="btn small primary">Make highlights</button>` : `<span class="muted small">replay expired</span>`}</div>`;
      const btn = $("button", el);
      if (btn) btn.onclick = () => startJob({ kind: "match", match: m });
      box.append(el);
    }
  } catch (e) {
    box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`;
  }
}
$("#btn-more").onclick = () => { steamPages += 1; loadSteam(true); };
$("#btn-refresh").onclick = () => { $("#tab-local").classList.contains("active") ? loadLocal() : loadSteam(true); };

async function loadLocal() {
  const box = $("#local-list");
  try {
    const list = await api("/api/local");
    if (!list.length) { box.innerHTML = `<div class="empty">No .dem files in the replays folder.</div>`; return; }
    box.innerHTML = "";
    for (const d of list) {
      const el = document.createElement("div");
      el.className = "item";
      el.innerHTML = `<div class="meta"><span class="map">${esc(d.name)}</span>
          <span class="muted small">${esc(d.size_mb)} MB · ${esc(fmtTime(d.modified))}</span>
          ${d.rendered ? `<span class="tag done">✔ made</span>` : ""}${d.skipped ? `<span class="tag">not your match</span>` : ""}</div>
        <div class="actions"><button class="btn small primary">Make highlights</button></div>`;
      $("button", el).onclick = () => startJob({ kind: "local", path: d.path });
      box.append(el);
    }
  } catch (e) { box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`; }
}

async function loadVideos() {
  const box = $("#videos-list");
  try {
    const list = await api("/api/outputs");
    if (!list.length) { box.innerHTML = `<div class="empty">No videos yet. Make some highlights first!</div>`; return; }
    box.innerHTML = "";
    for (const v of list) box.append(videoItem(v.path, `${v.kind === "montage" ? "🎞 " : ""}${v.name}`, `${v.group} · ${v.size_mb} MB`));
  } catch (e) { box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`; }
}

function videoItem(path, title, sub) {
  const el = document.createElement("div");
  el.className = "item";
  el.innerHTML = `<div class="meta"><span class="map">${esc(title)}</span><span class="muted small">${esc(sub)}</span></div>
    <div class="actions"><button class="btn small primary">▶ Play</button><button class="btn small">Show in folder</button></div>`;
  const [play, show] = $$("button", el);
  play.onclick = () => api("/api/open", { path }).catch(fail);
  show.onclick = () => api("/api/open", { path, reveal: true }).catch(fail);
  return el;
}

// ---------------------------------------------------------------- jobs

async function startJob(body) {
  try {
    const { id } = await api("/api/jobs", body);
    toast(S.current_job ? "Queued" : "Started", "Progress shows at the top of this page.");
    if (!S.current_job || !jobId) watchJob(id);
    showPage("home");
    $("#job").scrollIntoView({ behavior: "smooth" });
    refreshState();
  } catch (e) { fail(e); }
}
$("#btn-latest").onclick = () => startJob({ kind: "latest" });

function watchJob(id) {
  jobId = id;
  logSeen = 0;
  $("#job-log").textContent = "";
  $("#job").classList.remove("hidden");
}

async function pollJob() {
  if (!jobId) return;
  let j;
  try { j = await api(`/api/jobs/${jobId}?since=${logSeen}`); } catch { return; }
  $("#job-title").textContent = j.title;
  const st = $("#job-status");
  st.textContent = j.status; st.className = `badge ${j.status}`;
  $("#job-stage").textContent = j.stage || "Waiting…";
  $("#job-elapsed").textContent = j.elapsed ? `⏱ ${j.elapsed}` : "";
  $("#job-detail").textContent = j.detail || "";
  const bar = $("#job-bar");
  bar.parentElement.classList.toggle("indeterminate", j.status === "running" && j.progress === null);
  bar.style.width = `${Math.round((j.progress ?? (j.status === "done" ? 1 : 0)) * 100)}%`;
  $("#job-cancel").classList.toggle("hidden", !["running", "queued"].includes(j.status));
  if (j.log.length) {
    const pre = $("#job-log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += j.log.join("\n") + "\n";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    logSeen = j.log_total;
  }
  $("#job-error").classList.toggle("hidden", !j.error);
  if (j.error) $("#job-error").innerHTML = errorHtml(j.error);
  const res = $("#job-results");
  res.classList.toggle("hidden", !j.files.length);
  if (j.files.length && res.dataset.job !== String(j.id)) {
    res.dataset.job = j.id;
    res.innerHTML = "<b>Your videos</b>";
    for (const f of [...j.files].reverse()) res.append(videoItem(f, f.split("/").pop(), f.includes("/montages/") ? "montage" : "clip"));
  }
  if (["done", "failed", "cancelled"].includes(j.status) && S?.current_job === null) {
    const next = S.jobs.find((x) => x.status === "running" || x.status === "queued");
    if (next && next.id !== jobId) watchJob(next.id);
  }
}
$("#job-cancel").onclick = () => api(`/api/jobs/${jobId}/cancel`, {}).then(() => toast("Cancelling…")).catch(fail);

// ---------------------------------------------------------------- steam login

function renderSteam() {
  const logged = !!S?.session;
  $("#steam-logged-in").classList.toggle("hidden", !logged);
  $("#steam-login").classList.toggle("hidden", logged);
  if (logged) $("#steam-name").textContent = S.session.account_name || S.session.steamid;
}

async function loginPoll() {
  clearTimeout(loginTimer);
  try {
    const st = await api("/api/login/status");
    $("#login-error").classList.toggle("hidden", st.state !== "error");
    if (st.state === "error") { $("#login-error").innerHTML = errorHtml(st.error); resetLoginForms(); return; }
    if (st.state === "done") {
      toast("Logged in to Steam", st.account_name || "");
      resetLoginForms();
      await refreshState(); renderSteam(); loadSteam(true);
      return;
    }
    if (st.qr) $("#qr-box").innerHTML = st.qr + `<span class="muted small">Waiting for you to approve in the Steam app…</span>`;
    if (st.needs_code) {
      $("#pw-form").classList.add("hidden");
      $("#code-form").classList.remove("hidden");
      $("#code-hint").textContent = st.needs_code === "app"
        ? "Enter the code from the Steam app (Steam Guard), or approve the login in the app."
        : "Steam sent a code to your e-mail. Enter it here.";
    }
    if (st.state === "waiting") loginTimer = setTimeout(loginPoll, 4000);
  } catch (e) { $("#login-error").classList.remove("hidden"); $("#login-error").innerHTML = errorHtml(e); }
}
function resetLoginForms() {
  $("#qr-box").innerHTML = `<button class="btn primary" id="btn-qr">Show QR code</button>`;
  $("#btn-qr").onclick = startQr;
  $("#pw-form").classList.remove("hidden");
  $("#code-form").classList.add("hidden");
  $("#pw-password").value = ""; $("#guard-code").value = "";
}
async function startQr() {
  $("#qr-box").innerHTML = `<span class="muted">Asking Steam for a code…</span>`;
  try { await api("/api/login/qr", {}); loginPoll(); } catch (e) { fail(e); resetLoginForms(); }
}
$("#btn-qr").onclick = startQr;
$("#pw-form").onsubmit = async (ev) => {
  ev.preventDefault();
  try { await api("/api/login/password", { account: $("#pw-account").value.trim(), password: $("#pw-password").value }); loginPoll(); }
  catch (e) { $("#login-error").classList.remove("hidden"); $("#login-error").innerHTML = errorHtml(e); }
};
$("#code-form").onsubmit = async (ev) => {
  ev.preventDefault();
  try { await api("/api/login/code", { code: $("#guard-code").value }); loginPoll(); } catch (e) { fail(e); }
};
$("#btn-logout").onclick = async () => {
  await api("/api/logout", {}).catch(fail);
  await refreshState(); renderSteam(); loadSteam();
};

// ---------------------------------------------------------------- boot

(async function boot() {
  if (!TOKEN) {
    document.body.innerHTML = `<div class="card" style="margin:40px">Open this app with the <code>cs2hl</code> command (or the CS2 Highlight Maker menu entry).</div>`;
    return;
  }
  history.replaceState(null, "", location.pathname);
  await refreshState();
  if (!S) return;
  fillSettings();
  renderSteam();
  loadSteam();
  if (HASH.get("page")) showPage(HASH.get("page"));
  setInterval(pollJob, 1000);
  setInterval(async () => { const before = S?.config?.mode; await refreshState(); if (S && S.config.mode !== before) fillSettings(); }, 5000);
})();
