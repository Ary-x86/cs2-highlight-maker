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
let modeTab = null;      // gcpd tab shown under "Steam matches"
let Q = null;            // last /api/queue
const selected = new Map();  // key -> queue item ({kind, match|path})
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

function showPage(name, focus) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
  if (name === "videos") loadVideos(focus || []);
  if (name === "queue") pollQueue();
  if (name === "steam") renderSteam();
}
$$("nav button").forEach((b) => (b.onclick = () => showPage(b.dataset.page)));
$("#btn-steam-page").onclick = () => api("/api/open_url", { url: S.match_tab_urls[modeTab] })
  .then(() => toast("Opened in your browser", "Put downloaded replays in the CS2 replays folder or your Downloads folder; the app will use them."))
  .catch(fail);

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
  S.output_path ??= S.clips_dir.replace(/[\\/]clips$/, "");  // older server still running after an update
  S.replays_dir ??= S.cs2_dir ? S.cs2_dir + "/game/csgo/replays" : null;
  $("#replays-dir").textContent = S.replays_dir || "(CS2 not found)";
  $("#out-dir").textContent = S.output_path;
  $("#work-dir").textContent = S.work_dir;
  $("#btn-latest").classList.toggle("disabled", !S.session);
  $("#latest-hint").innerHTML = S.session
    ? "Finds your newest match of any mode on Steam, downloads the replay, renders it, then deletes the replay."
    : `Log in on the <a href="#" onclick="showPage('steam');return false">Steam account</a> page first, or use a demo from this PC below.`;
  if (!modeTab) { modeTab = Object.keys(S.match_tabs)[0]; renderModeTabs(); }
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
  $$("#round-types input[type=checkbox]").forEach((el) => (el.checked = c.round_highlights.includes(el.value)));
  $("#clutch-min").value = String(Math.min(3, c.clutch_min_enemies));
  fillSpecials();
  $("#pre-seconds").value = c.pre_seconds;
  $("#post-seconds").value = c.post_seconds;
  $("#max-gap").value = c.max_gap_seconds;
  $("#resolution").value = `${c.width}x${c.height}`;
  $("#output-height-field").classList.toggle("hidden", c.height <= 1080);
  $("#output-height").value = String(c.output_height && c.output_height < c.height ? c.output_height : 0);
  $("#graphics").value = c.graphics;
  S.codecs ??= { auto: "", h264_nvenc: "", h264_vaapi: "", libx264: "" };
  $("#codec").innerHTML = Object.keys(S.codecs).map((k) => `<option value="${k}">${esc(CODEC_LABELS[k] || S.codecs[k])}</option>`).join("");
  $("#codec").value = c.video_codec;
  $("#codec-hint").textContent = c.video_codec === "auto" ? "NVIDIA → AMD/Intel → CPU, whichever works first." : "";
  $("#quality").value = c.quality;
  qualityLabel();
  $("#steamids").value = c.steamids.join(", ");
  $("#accounts").innerHTML = S.accounts.length
    ? S.accounts.map((a) => `<span class="tag">${esc(a.name)} · ${esc(a.steamid)}</span>`).join(" ")
    : `<span class="muted">No Steam accounts found on this PC.</span>`;
}

const CODEC_LABELS = {
  auto: "Automatic (recommended)", h264_nvenc: "NVIDIA GPU (NVENC)", h264_vaapi: "AMD / Intel GPU (VAAPI, experimental)",
  h264_amf: "AMD GPU (AMF, experimental)", h264_qsv: "Intel GPU (Quick Sync, experimental)", libx264: "CPU (works everywhere)",
};

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
$$("#round-types input[type=checkbox]").forEach((el) => (el.onchange = () =>
  save({ round_highlights: $$("#round-types input[type=checkbox]:checked").map((x) => x.value) })));
$("#clutch-min").onclick = (e) => e.stopPropagation();
$("#clutch-min").onchange = (e) => save({ clutch_min_enemies: +e.target.value });

// Special shots dropdown with "Select all"
const specialBoxes = () => $$("#special-dd .dd-panel input[value]");
function fillSpecials() {
  const chosen = S.config.special_kills;
  specialBoxes().forEach((el) => (el.checked = chosen.includes(el.value)));
  const all = $("#special-all");
  all.checked = chosen.length === specialBoxes().length;
  all.indeterminate = chosen.length > 0 && !all.checked;
  const names = specialBoxes().filter((el) => el.checked).map((el) => $("b", el.parentElement).textContent);
  $("#special-summary").textContent = !names.length ? "None (only the rounds above)"
    : all.checked ? "All special shots" : names.length <= 3 ? names.join(", ") : `${names.length} selected`;
}
const saveSpecials = () => save({ special_kills: specialBoxes().filter((el) => el.checked).map((el) => el.value) }, true);
specialBoxes().forEach((el) => (el.onchange = saveSpecials));
$("#special-all").onchange = (e) => { specialBoxes().forEach((el) => (el.checked = e.target.checked)); saveSpecials(); };
document.addEventListener("click", (e) => { const dd = $("#special-dd"); if (dd.open && !dd.contains(e.target)) dd.open = false; });
$("#pre-seconds").onchange = (e) => save({ pre_seconds: +e.target.value });
$("#post-seconds").onchange = (e) => save({ post_seconds: +e.target.value });
$("#max-gap").onchange = (e) => save({ max_gap_seconds: +e.target.value });
$("#resolution").onchange = (e) => { const [w, h] = e.target.value.split("x").map(Number); save({ width: w, height: h }); };
$("#output-height").onchange = (e) => save({ output_height: +e.target.value });
$("#graphics").onchange = (e) => save({ graphics: e.target.value });
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
    $("#picker-roots").innerHTML = d.roots.map((r) => `<button class="btn small" data-p="${esc(r)}">${esc(r.split(/[\\/]/).filter(Boolean).pop() || r)}</button>`).join("");
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
$("#btn-open-out").onclick = () => api("/api/open", { path: S.output_path }).catch(fail);

// ---------------------------------------------------------------- matches

const fmtTime = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";

function renderModeTabs() {
  $("#mode-tabs").innerHTML = Object.entries(S.match_tabs)
    .map(([tab, label]) => `<button data-mt="${tab}" class="${tab === modeTab ? "active" : ""}">${esc(label)}</button>`).join("");
  $$("#mode-tabs button").forEach((b) => (b.onclick = () => { modeTab = b.dataset.mt; renderModeTabs(); loadSteam(); }));
}

// A list row with a selection checkbox. `item` is what gets queued; null means it can't be queued.
function pickRow(key, item, metaHtml, status, files = []) {
  const el = document.createElement("div");
  el.className = "item" + (selected.has(key) ? " sel" : "");
  el.innerHTML = `<div class="pick">${item ? `<input type="checkbox" ${selected.has(key) ? "checked" : ""}>` : `<span style="width:17px"></span>`}
      <div class="meta">${metaHtml}</div></div>
    <div class="actions">${item ? (status === "queued" ? `<span class="tag done">in queue</span>`
      : `<button class="btn small primary">+ Add to queue</button>`) : `<span class="muted small">no replay</span>`}</div>`;
  const made = $(".made", el);
  if (made) made.onclick = () => showVideos(files);
  if (item) {
    const box = $("input", el);
    box.onchange = () => { box.checked ? selected.set(key, item) : selected.delete(key); el.classList.toggle("sel", box.checked); renderFab(); };
    const btn = $(".actions button", el);
    if (btn) btn.onclick = () => addToQueue([item]);
  }
  return el;
}

async function loadSteam(refresh = false, more = false, quiet = false) {
  const box = $("#steam-list");
  if (!S?.session) {
    box.innerHTML = `<div class="empty">Log in on the <a href="#" onclick="showPage('steam');return false">Steam account</a> page to see your matches.</div>`;
    $("#btn-more").classList.add("hidden");
    $("#steam-reached").textContent = "";
    return;
  }
  const tab = modeTab;
  if (!more && !quiet) box.innerHTML = `<div class="empty">Loading ${esc(S.match_tabs[tab])} matches from Steam…</div>`;
  $("#btn-more").classList.add("disabled");
  try {
    const r = await api(`/api/matches?tab=${tab}${refresh ? "&refresh=1" : ""}${more ? "&more=1" : ""}`);
    if (tab !== modeTab) return;
    $("#btn-more").classList.toggle("hidden", r.exhausted);
    $("#btn-more").classList.remove("disabled");
    $("#steam-reached").textContent = r.reached ? `Showing matches back to ${r.reached}` : "";
    if (!r.matches.length) {
      box.innerHTML = `<div class="empty">No ${esc(S.match_tabs[tab])} matches in this period.${r.exhausted ? "" : " Try <b>Load older matches</b>."}</div>`;
      return;
    }
    box.innerHTML = "";
    for (const m of r.matches) {
      const meta = `<span class="map">${esc(m.map)}</span>
        <span class="tag ${m.result}">${esc(m.score || "–")} ${m.result ? esc(m.result) : ""}</span>
        <span class="muted small">${esc(m.kills)} / ${esc(m.assists)} / ${esc(m.deaths)} K/A/D</span>
        <span class="muted small">${esc(fmtTime(m.time))}${m.duration ? " · " + esc(m.duration) : ""}</span>
        ${m.rendered ? madeTag() : ""}
        ${m.local ? `<span class="tag" title="${esc(m.local)}">💾 on this PC</span>` : ""}`;
      // A replay you downloaded yourself can still be rendered after Valve's link expired.
      const item = m.url || m.local ? { kind: "match", match: m } : null;
      box.append(pickRow(`match:${m.id}`, item, meta, m.queued ? "queued" : "", m.files));
    }
  } catch (e) {
    box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`;
    $("#btn-more").classList.remove("disabled");
  }
}
$("#btn-more").onclick = () => loadSteam(false, true);
$("#btn-refresh").onclick = () => { $("#tab-local").classList.contains("active") ? loadLocal() : loadSteam(true); };

async function loadLocal() {
  const box = $("#local-list");
  try {
    const list = await api("/api/local");
    if (!list.length) { box.innerHTML = `<div class="empty">No .dem files in the replays folder.</div>`; return; }
    box.innerHTML = "";
    for (const d of list) {
      const meta = `<span class="map">${esc(d.name)}</span>
        <span class="muted small">${esc(d.size_mb)} MB · ${esc(fmtTime(d.modified))}</span>
        ${d.compressed ? `<span class="tag" title="Unpacked automatically when rendered">.bz2</span>` : ""}
        ${d.rendered ? madeTag() : ""}${d.skipped ? `<span class="tag">not your match</span>` : ""}`;
      box.append(pickRow(`local:${d.path}`, { kind: "local", path: d.path }, meta, d.queued ? "queued" : "", d.files));
    }
  } catch (e) { box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`; }
}

const madeTag = () => `<button class="tag done made" title="Show these videos in My videos">✔ made ›</button>`;

// Jump to My videos and flash the given files (a match's clips and montage).
function showVideos(files) {
  showPage("videos", files);
}

async function loadVideos(focus = []) {
  const box = $("#videos-list");
  try {
    const list = await api("/api/outputs");
    if (!list.length) { box.innerHTML = `<div class="empty">No videos yet. Make some highlights first!</div>`; return; }
    box.innerHTML = "";
    for (const v of list) box.append(videoItem(v.path, `${v.kind === "montage" ? "🎞 " : ""}${v.name}`, `${v.group} · ${v.size_mb} MB`));
    const hits = $$("#videos-list .item").filter((el) => focus.includes(el.dataset.path));
    if (focus.length && !hits.length) toast("Videos not found", "They were moved or deleted.", true);
    hits.forEach((el) => { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); });
    hits[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (e) { box.innerHTML = `<div class="error-card">${errorHtml(e)}</div>`; }
}

function videoItem(path, title, sub) {
  const el = document.createElement("div");
  el.className = "item";
  el.dataset.path = path;
  el.innerHTML = `<div class="meta"><span class="map">${esc(title)}</span><span class="muted small">${esc(sub)}</span></div>
    <div class="actions"><button class="btn small primary">▶ Play</button><button class="btn small">Show in folder</button></div>`;
  const [play, show] = $$("button", el);
  play.onclick = () => api("/api/open", { path }).catch(fail);
  show.onclick = () => api("/api/open", { path, reveal: true }).catch(fail);
  return el;
}

// ---------------------------------------------------------------- selection + queue

function renderFab() {
  $("#fab").classList.toggle("hidden", selected.size === 0);
  $("#fab-text").textContent = `${selected.size} selected`;
}
$("#fab-add").onclick = async () => {
  if (await addToQueue([...selected.values()])) {
    selected.clear();
    renderFab();
    $$(".item.sel").forEach((el) => { el.classList.remove("sel"); const c = $("input", el); if (c) c.checked = false; });
  }
};
$("#fab-clear").onclick = () => {
  selected.clear(); renderFab();
  $$(".item.sel").forEach((el) => { el.classList.remove("sel"); const c = $("input", el); if (c) c.checked = false; });
};

async function addToQueue(items) {
  try {
    const r = await api("/api/queue/add", { items, mode: S.config.mode });
    toast(`Added ${r.ids.length} to the queue`, r.paused ? "The queue is paused: press Start on the Queue page." : "Rendering starts automatically, one at a time.");
    await pollQueue();  // also refreshes the list rows ("in queue")
    return true;
  } catch (e) { fail(e); return false; }
}
$("#btn-latest").onclick = () => addToQueue([{ kind: "latest" }]);

const STATUS_ICON = { queued: "⏳", running: "🎬", done: "✅", failed: "⚠️", cancelled: "✖" };

let activeSig = null;  // which jobs are waiting/running; a change means list rows need a new status

function reloadMatchList() {
  if ($("#tab-local").classList.contains("active")) loadLocal(); else if (S?.session) loadSteam(false, false, true);
}

async function pollQueue() {
  try { Q = await api("/api/queue"); } catch { return; }
  const sig = Q.items.filter((j) => ["queued", "running"].includes(j.status)).map((j) => j.id).join(",");
  if (activeSig !== null && sig !== activeSig) reloadMatchList();
  activeSig = sig;
  const waiting = Q.items.filter((j) => j.status === "queued").length;
  const running = Q.items.find((j) => j.status === "running");
  $("#queue-count").textContent = waiting + (running ? 1 : 0);
  $("#queue-count").classList.toggle("hidden", !waiting && !running);
  $("#btn-pause").textContent = Q.paused ? "▶ Start" : "⏸ Pause";
  $("#paused-banner").classList.toggle("hidden", !(Q.paused && waiting));

  // "now rendering" strip on the Highlights page
  $("#now-bar").classList.toggle("hidden", !running);
  if (running) {
    $("#now-title").textContent = running.title;
    $("#now-stage").textContent = `· ${running.stage}${running.detail ? " · " + running.detail : ""}`;
    $("#now-progress").style.width = `${Math.round((running.progress ?? 0) * 100)}%`;
  }

  if (jobId === null || !Q.items.some((j) => j.id === jobId)) {
    const pick = running || Q.items.find((j) => j.status === "queued") || Q.items[Q.items.length - 1];
    if (pick) watchJob(pick.id); else { jobId = null; $("#job").classList.add("hidden"); }
  }

  const box = $("#queue-list");
  if (!$("#page-queue").classList.contains("active")) return;
  if (!Q.items.length) { box.innerHTML = `<div class="empty">The queue is empty. Tick matches on the Highlights page and press <b>+ Add to queue</b>.</div>`; return; }
  box.innerHTML = "";
  const order = [...Q.items.filter((j) => j.status === "running"), ...Q.items.filter((j) => j.status === "queued"),
                 ...Q.items.filter((j) => !["running", "queued"].includes(j.status)).reverse()];
  for (const j of order) {
    const el = document.createElement("div");
    el.className = "item qitem" + (j.id === jobId ? " sel" : "");
    const pct = Math.round((j.progress ?? 0) * 100);
    el.innerHTML = `<div class="icon">${STATUS_ICON[j.status] || ""}</div>
      <div><div class="meta"><span class="map">${esc(j.title)}</span>
        <span class="tag">${j.mode === "blur60" ? "Motion blur 60" : "Smooth 120"}</span>
        <span class="muted small">${esc(j.status === "queued" ? (j.stage || "waiting") : j.stage)}${j.detail && j.status === "running" ? " · " + esc(j.detail) : ""}${j.elapsed ? " · ⏱ " + esc(j.elapsed) : ""}</span>
        ${j.error ? `<span class="tag loss">${esc(j.error.title)}</span>` : ""}${j.files.length ? `<span class="tag done">${j.files.length} video(s)</span>` : ""}</div>
        ${j.status === "running" ? `<div class="mini"><div style="width:${pct}%"></div></div>` : ""}</div>
      <div class="actions">
        ${j.status === "queued" ? `<button class="btn small" data-a="up" title="Move up">↑</button><button class="btn small" data-a="down" title="Move down">↓</button>` : ""}
        ${["failed", "cancelled"].includes(j.status) ? `<button class="btn small" data-a="retry">Retry</button>` : ""}
        <button class="btn small" data-a="view">Details</button>
        <button class="btn small danger" data-a="remove" title="${j.status === "running" ? "Cancel" : "Remove"}">${j.status === "running" ? "Cancel" : "✕"}</button>
      </div>`;
    $$("[data-a]", el).forEach((b) => (b.onclick = () => queueAction(j, b.dataset.a)));
    box.append(el);
  }
}

async function queueAction(j, action) {
  try {
    if (action === "up" || action === "down") Q = await api(`/api/queue/${j.id}/move`, { delta: action === "up" ? -1 : 1 });
    else if (action === "remove") {
      if (j.status === "running" && !confirm("Stop rendering this match? CS2 will be closed.")) return;
      Q = await api(`/api/queue/${j.id}/remove`, {});
    } else if (action === "retry") {
      const full = await api(`/api/jobs/${j.id}`);
      await api(`/api/queue/${j.id}/remove`, {});
      await api("/api/queue/add", { items: [full.spec], mode: j.mode });
    } else if (action === "view") { watchJob(j.id); $("#job").scrollIntoView({ behavior: "smooth" }); }
    pollQueue();
  } catch (e) { fail(e); }
}

$("#btn-pause").onclick = () => api("/api/queue/pause", { paused: !Q.paused }).then(pollQueue).catch(fail);
$("#btn-resume").onclick = () => api("/api/queue/pause", { paused: false }).then(pollQueue).catch(fail);
$("#btn-clear").onclick = () => api("/api/queue/clear", {}).then(pollQueue).catch(fail);

// ---------------------------------------------------------------- job details

function watchJob(id) {
  if (jobId === id) return;
  jobId = id;
  logSeen = 0;
  $("#job-log").textContent = "";
  $("#job-results").dataset.job = "";
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
  $("#job-cancel").textContent = j.status === "running" ? "Cancel" : "Remove";
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
}
$("#job-cancel").onclick = () => {
  const j = Q?.items.find((x) => x.id === jobId);
  if (j) queueAction(j, "remove");
};

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

// ---------------------------------------------------------------- first-run setup

const INSTALL_FFMPEG = {
  linux: "Install it with your package manager, e.g. <code>sudo apt install ffmpeg</code>, <code>sudo pacman -S ffmpeg</code> or <code>sudo dnf install ffmpeg</code>, then reopen this window.",
  mac: "Install it with <code>brew install ffmpeg</code>.",
};
const PLUGIN_FIX = {
  linux: "Run <code>./install.sh</code> in the app's folder; it builds the plugin (one time, needs <code>g++</code>).",
  windows: "The plugin comes inside the app. Download the newest release again and start that.",
  mac: "Recording happens inside CS2, which Valve doesn't make for macOS.",
};

function setupSteps() {
  const st = S.setup, steps = [];
  steps.push(S.cs2_dir
    ? { ok: "ok", title: "Counter-Strike 2", body: `Found in <code>${esc(S.cs2_dir)}</code>` }
    : { ok: "bad", title: "Counter-Strike 2", body: esc(S.cs2_error ? `${S.cs2_error.why} ${S.cs2_error.fix}` : "Not found."),
        action: st.platform === "mac" ? null : ["Choose folder…", async () => {
          const p = await pickFolder("Choose the 'Counter-Strike Global Offensive' folder", "");
          if (p) await save({ cs2_dir: p }, true);
        }] });
  steps.push(S.accounts.length
    ? { ok: "ok", title: "Your Steam accounts", body: `You will be recognised in demos as ${S.accounts.map((a) => `<b>${esc(a.name)}</b>`).join(", ")}.` }
    : { ok: "warn", title: "Your Steam accounts", body: "No Steam account has logged in on this PC yet. Start Steam once, or add your SteamID64 under Settings → Accounts." });
  steps.push(S.plugin_ok
    ? { ok: "ok", title: "Recording plugin", body: "Ready. It is added to CS2 only while recording and removed right after." }
    : { ok: "bad", title: "Recording plugin", body: PLUGIN_FIX[st.platform] });
  const dl = st.ffmpeg_download;
  steps.push(st.ffmpeg_ok
    ? { ok: "ok", title: "FFmpeg (turns frames into videos)", body: "Installed." }
    : st.platform === "windows"
      ? { ok: "bad", title: "FFmpeg (turns frames into videos)",
          body: dl.running ? `Downloading… ${Math.round(dl.progress * 100)}%` : dl.error ? esc(`${dl.error.title}: ${dl.error.why} ${dl.error.fix}`) : "Not found. The app can download it for you (about 200 MB, from the BtbN/FFmpeg-Builds project on GitHub).",
          action: dl.running ? null : ["Download FFmpeg", () => api("/api/setup/ffmpeg", {}).then((r) => { S = r; renderSetup(); }).catch(fail)] }
      : { ok: "bad", title: "FFmpeg (turns frames into videos)", body: INSTALL_FFMPEG[st.platform] });
  steps.push({ ok: "ok", title: "Where videos are saved", body: `<code>${esc(S.output_path)}</code>`,
    action: ["Change…", async () => {
      const p = await pickFolder("Choose where videos are saved", S.config.output_dir || S.work_dir);
      if (p) await save({ output_dir: p }, true);
    }] });
  steps.push(S.session
    ? { ok: "ok", title: "Steam login (optional)", body: `Logged in as <b>${esc(S.session.account_name || S.session.steamid)}</b>.` }
    : { ok: "warn", title: "Steam login (optional)", body: "Log in to list your matches and download their replays. Demos already on this PC work without it.",
        action: ["Log in…", async () => { await finishSetup(); showPage("steam"); }] });
  return steps;
}

function renderSetup() {
  if (!S || !$("#setup").open) return;
  const steps = setupSteps();
  $("#setup-steps").innerHTML = steps.map((st, i) => `
    <li class="${st.ok}"><span class="dot"></span><div><b>${esc(st.title)}</b><p class="muted small">${st.body}</p></div>
    ${st.action ? `<button class="btn small" data-step="${i}">${esc(st.action[0])}</button>` : ""}</li>`).join("");
  $$("#setup-steps [data-step]").forEach((b) => (b.onclick = async () => {
    await steps[+b.dataset.step].action[1]();
    await refreshState(); fillSettings(); renderSetup();
  }));
  const blocking = steps.filter((st) => st.ok === "bad").length;
  $("#setup-note").textContent = blocking ? `${blocking} thing(s) still need attention before recording works. You can finish now and fix them later.` : "All set.";
  if (S.setup.ffmpeg_download.running) setTimeout(() => refreshState().then(renderSetup), 1000);
}

async function finishSetup() {
  try { S = await api("/api/setup/done", {}); } catch (e) { fail(e); }
  $("#setup").close();
}

function openSetup() {
  if (!S.setup) return toast("Restart the app to use the setup screen", "It was updated while running.");
  $("#setup").showModal();
  renderSetup();
}
$("#setup-done").onclick = finishSetup;
$$("[data-open-setup]").forEach((b) => (b.onclick = openSetup));

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
  if (S.setup?.needed) openSetup();
  await pollQueue();
  setInterval(pollJob, 1000);
  setInterval(pollQueue, 2000);
  setInterval(async () => { const before = S?.config?.mode; await refreshState(); if (S && S.config.mode !== before) fillSettings(); }, 5000);
})();
