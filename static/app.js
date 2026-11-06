"use strict";

const state = {
  session: null,
  languages: [],
  browsePath: "",
  selections: { browse: new Set(), scan: new Set(), search: new Set() },
  pollTimer: null,
  openJobs: new Set(),
  settings: null,
  settingsMediaPath: "",
};

const STATUS_LABELS = {
  pending: "pending", running: "translating…", done: "done", skipped: "skipped",
  error: "error", cancelled: "cancelled", queued: "queued",
};

function source() { return ST.$("source").value; }
function target() { return ST.$("target").value; }

// --- Tabs ---------------------------------------------------------------------------

function showTab(name) {
  for (const btn of document.querySelectorAll(".tab")) btn.classList.toggle("active", btn.dataset.tab === name);
  for (const panel of document.querySelectorAll(".panel")) panel.classList.toggle("hidden", panel.id !== `tab-${name}`);
  if (name === "jobs") loadJobs();
  if (name === "settings") loadSettings();
}
for (const btn of document.querySelectorAll(".tab")) btn.addEventListener("click", () => showTab(btn.dataset.tab));

// --- File tables --------------------------------------------------------------------

function renderFileTable(container, entries, key, opts) {
  const selection = state.selections[key];
  selection.clear();
  const button = ST.$(`${key}-translate`);
  const update = () => {
    button.disabled = selection.size === 0;
    button.textContent = selection.size ? `Translate selected (${selection.size})` : "Translate selected";
  };
  ST.clear(container);
  const files = entries.filter((e) => e.type === "file");
  if (!entries.length) {
    container.append(ST.el("p", { class: "hint", text: opts.empty || "Nothing here." }));
    update();
    return;
  }
  const boxes = [];
  const all = ST.el("input", { type: "checkbox", title: "Select all files", "aria-label": "Select all files" });
  all.disabled = files.length === 0;
  all.addEventListener("change", () => {
    for (const box of boxes) {
      box.checked = all.checked;
      if (all.checked) selection.add(box.value); else selection.delete(box.value);
    }
    update();
  });
  const table = ST.el("table", { class: "files" },
    ST.el("thead", {}, ST.el("tr", {},
      ST.el("th", { class: "sel" }, all),
      ST.el("th", { text: opts.showPath ? "Path" : "Name" }),
      ST.el("th", { text: "Lang" }),
      ST.el("th", { text: "Size" }),
      ST.el("th", { text: "Status" }))));
  const tbody = ST.el("tbody");
  for (const entry of entries) {
    if (entry.type === "dir") {
      tbody.append(ST.el("tr", { class: "dir" },
        ST.el("td"),
        ST.el("td", {}, ST.el("button", { class: "link", text: "📁 " + entry.name, onclick: () => opts.onDir(entry.path) })),
        ST.el("td"), ST.el("td"), ST.el("td")));
      continue;
    }
    const box = ST.el("input", { type: "checkbox", value: entry.path, "aria-label": `Select ${entry.name}` });
    box.addEventListener("change", () => {
      if (box.checked) selection.add(entry.path); else selection.delete(entry.path);
      all.checked = selection.size === boxes.length;
      update();
    });
    boxes.push(box);
    let status = "";
    let statusClass = "";
    if (entry.is_target) { status = `already ${target()}`; statusClass = "muted"; }
    else if (entry.translated) { status = `${target()} exists`; statusClass = "ok"; }
    tbody.append(ST.el("tr", {},
      ST.el("td", { class: "sel" }, box),
      ST.el("td", { class: "name", text: opts.showPath ? entry.path : entry.name }),
      ST.el("td", { text: entry.lang || (entry.detected ? `${entry.detected}?` : "—") }),
      ST.el("td", { class: "num", text: ST.formatSize(entry.size) }),
      ST.el("td", {}, status ? ST.el("span", { class: `badge ${statusClass}`, text: status }) : "")));
  }
  table.append(tbody);
  container.append(table);
  update();
}

async function startJob(key) {
  const files = [...state.selections[key]];
  if (!files.length) return;
  const msgId = `${key}-msg`;
  try {
    const job = await ST.api("POST", "/api/jobs", {
      files, source: source(), target: target(), overwrite: ST.$("overwrite").checked,
    });
    ST.msg(msgId, `Job queued with ${job.total} file(s). See the Jobs tab.`, "ok");
    state.openJobs.add(job.id);
    schedulePoll(500);
  } catch (e) {
    ST.msg(msgId, e.message, "error");
  }
}
for (const key of ["browse", "scan", "search"]) ST.$(`${key}-translate`).addEventListener("click", () => startJob(key));

// --- Browse -------------------------------------------------------------------------

async function browse(path) {
  ST.msg("browse-msg", "Loading…");
  try {
    const params = new URLSearchParams({ path, target: target() });
    const data = await ST.api("GET", `/api/files?${params}`);
    state.browsePath = data.path;
    const crumbs = ST.clear(ST.$("crumbs"));
    data.breadcrumbs.forEach((c, i) => {
      if (i) crumbs.append(" / ");
      crumbs.append(ST.el("button", { class: "link", text: c.name, onclick: () => browse(c.path) }));
    });
    renderFileTable(ST.$("browse-list"), data.entries, "browse", { onDir: browse, empty: "No folders or subtitle files here." });
    ST.msg("browse-msg", "");
  } catch (e) {
    ST.msg("browse-msg", e.message, "error");
  }
}
ST.$("browse-refresh").addEventListener("click", () => browse(state.browsePath));

// --- Scan ---------------------------------------------------------------------------

ST.$("scan-use-current").addEventListener("click", () => { ST.$("scan-path").value = state.browsePath; });
ST.$("scan-btn").addEventListener("click", async () => {
  const button = ST.$("scan-btn");
  button.disabled = true;
  ST.msg("scan-msg", "Scanning…");
  try {
    const data = await ST.api("POST", "/api/scan", {
      path: ST.$("scan-path").value.trim(),
      source: source() === "auto" ? state.session.default_source : source(),
      target: target(),
      mode: ST.$("scan-mode").value,
      include_existing: ST.$("scan-existing").checked,
    });
    renderFileTable(ST.$("scan-list"), data.results, "scan", { showPath: true, empty: "No matching files found." });
    let text = `${data.results.length} file(s) found (${data.scanned} subtitle files scanned).`;
    if (data.truncated) text += " Result list was capped.";
    if (data.detect_skipped) text += ` ${data.detect_skipped} file(s) not checked (detection limit).`;
    ST.msg("scan-msg", text, "ok");
  } catch (e) {
    ST.msg("scan-msg", e.message, "error");
  } finally {
    button.disabled = false;
  }
});

// --- Search -------------------------------------------------------------------------

ST.$("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  ST.msg("search-msg", "Searching…");
  try {
    const params = new URLSearchParams({ q: ST.$("search-q").value, target: target() });
    const data = await ST.api("GET", `/api/search?${params}`);
    renderFileTable(ST.$("search-list"), data.results, "search", { showPath: true, empty: "No matches." });
    ST.msg("search-msg", `${data.results.length} match(es)${data.truncated ? " (capped)" : ""}.`, "ok");
  } catch (e) {
    ST.msg("search-msg", e.message, "error");
  }
});

// --- Jobs ---------------------------------------------------------------------------

function schedulePoll(delay) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(loadJobs, delay);
}

function jobProgress(job) {
  const finished = job.total - (job.counts.pending || 0) - (job.counts.running || 0);
  return job.total ? finished / job.total : 0;
}

async function loadJob(job, container) {
  try {
    const full = await ST.api("GET", `/api/jobs/${encodeURIComponent(job.id)}`);
    const rows = full.files.map((f) => ST.el("tr", {},
      ST.el("td", { class: "name", text: f.path }),
      ST.el("td", {}, ST.el("span", { class: `badge s-${f.status}`, text: STATUS_LABELS[f.status] || f.status })),
      ST.el("td", { text: f.output && f.status === "done" ? `→ ${f.output}` : f.message })));
    ST.clear(container).append(ST.el("table", { class: "files" }, ST.el("tbody", {}, rows)));
  } catch (e) {
    ST.clear(container).append(ST.el("p", { class: "msg error", text: e.message }));
  }
}

async function loadJobs() {
  let data;
  try {
    data = await ST.api("GET", "/api/jobs");
  } catch (e) {
    schedulePoll(10000);
    return;
  }
  const active = data.jobs.filter((j) => j.status === "queued" || j.status === "running");
  const badge = ST.$("jobs-badge");
  badge.textContent = String(active.length);
  badge.classList.toggle("hidden", active.length === 0);

  const list = ST.clear(ST.$("jobs-list"));
  if (!data.jobs.length) list.append(ST.el("p", { class: "hint", text: "No jobs yet." }));
  for (const job of data.jobs) {
    const pct = Math.round(jobProgress(job) * 100);
    const counts = Object.entries(job.counts).map(([k, v]) => `${v} ${STATUS_LABELS[k] || k}`).join(", ");
    const details = ST.el("div", { class: "job-files" });
    const toggle = ST.el("button", { class: "link", text: state.openJobs.has(job.id) ? "Hide files" : "Show files" });
    toggle.addEventListener("click", () => {
      if (state.openJobs.has(job.id)) state.openJobs.delete(job.id); else state.openJobs.add(job.id);
      loadJobs();
    });
    const cancel = (job.status === "queued" || job.status === "running")
      ? ST.el("button", { text: "Cancel", onclick: async () => {
          try { await ST.api("POST", `/api/jobs/${encodeURIComponent(job.id)}/cancel`, {}); } catch (e) { /* shown on refresh */ }
          loadJobs();
        } })
      : null;
    const current = job.status === "running" && job.current.total
      ? ` — current file ${job.current.done}/${job.current.total} lines` : "";
    const bar = ST.el("progress", { class: "bar", max: 100, value: pct });
    list.append(ST.el("div", { class: "job" },
      ST.el("div", { class: "job-head" },
        ST.el("strong", { text: `${job.source} → ${job.target}` }),
        ST.el("span", { class: `badge s-${job.status}`, text: STATUS_LABELS[job.status] || job.status }),
        ST.el("span", { class: "hint", text: `${new Date(job.created * 1000).toLocaleString()} · ${job.total} file(s) · ${counts}${current}` }),
        ST.el("div", { class: "spacer" }), cancel, toggle),
      bar, details));
    if (state.openJobs.has(job.id)) loadJob(job, details);
  }
  if (active.length) schedulePoll(2000);
}
ST.$("jobs-refresh").addEventListener("click", loadJobs);

// --- Settings -----------------------------------------------------------------------

async function loadSettingsFolders(path) {
  try {
    const data = await ST.api("GET", `/api/settings/folders?${new URLSearchParams({ path })}`);
    state.settingsMediaPath = data.path;
    ST.$("s-media-selected").textContent = "/" + data.path;
    const crumbs = ST.clear(ST.$("s-media-crumbs"));
    crumbs.append(ST.el("button", { type: "button", class: "link", text: "media", onclick: () => loadSettingsFolders("") }));
    let acc = "";
    for (const part of data.path ? data.path.split("/") : []) {
      acc = acc ? `${acc}/${part}` : part;
      const p = acc;
      crumbs.append(" / ", ST.el("button", { type: "button", class: "link", text: part, onclick: () => loadSettingsFolders(p) }));
    }
    const list = ST.clear(ST.$("s-media-folders"));
    for (const name of data.folders) {
      const next = data.path ? `${data.path}/${name}` : name;
      list.append(ST.el("li", {}, ST.el("button", { type: "button", class: "link", text: "📁 " + name, onclick: () => loadSettingsFolders(next) })));
    }
  } catch (e) {
    ST.msg("settings-msg", e.message, "error");
  }
}

async function loadSettings() {
  try {
    const s = await ST.api("GET", "/api/settings");
    state.settings = s;
    ST.$("s-url").value = s.libretranslate_url;
    ST.$("s-key").value = "";
    ST.$("s-key").placeholder = s.api_key_set ? "(unchanged — key stored)" : "(none)";
    ST.$("s-key-clear").checked = false;
    ST.$("s-batch").value = s.batch_size;
    ST.$("s-maxmb").value = s.max_file_mb;
    ST.$("s-maxfiles").value = s.max_files_per_job;
    ST.$("s-fps").value = s.microdvd_fps;
    ST.$("s-idle").value = s.session_idle_hours;
    ST.$("s-abs").value = s.session_absolute_hours;
    ST.$("pw-username").value = s.username;
    const langs = state.languages.length ? state.languages : [{ code: s.default_source, name: s.default_source }, { code: s.default_target, name: s.default_target }];
    ST.fillLanguages(ST.$("s-source"), langs, s.default_source, false);
    ST.fillLanguages(ST.$("s-target"), langs, s.default_target, false);
    loadSettingsFolders(s.media_root);
  } catch (e) {
    ST.msg("settings-msg", e.message, "error");
  }
}

ST.$("s-test").addEventListener("click", async () => {
  ST.msg("s-test-msg", "Testing…");
  const body = { url: ST.$("s-url").value.trim() };
  if (ST.$("s-key").value) body.api_key = ST.$("s-key").value;
  else if (ST.$("s-key-clear").checked) body.api_key = "";
  try {
    const data = await ST.api("POST", "/api/settings/test", body);
    if (!data.ok) { ST.msg("s-test-msg", "Connection failed: " + data.error, "error"); return; }
    let text = `Connected — ${data.languages.length} languages.`;
    if (data.insecure) text += " Warning: plain http to a remote host.";
    ST.msg("s-test-msg", text, data.insecure ? "warn" : "ok");
  } catch (e) {
    ST.msg("s-test-msg", e.message, "error");
  }
});

ST.$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    libretranslate_url: ST.$("s-url").value.trim(),
    media_root: state.settingsMediaPath,
    default_source: ST.$("s-source").value,
    default_target: ST.$("s-target").value,
    batch_size: Number(ST.$("s-batch").value),
    max_file_mb: Number(ST.$("s-maxmb").value),
    max_files_per_job: Number(ST.$("s-maxfiles").value),
    microdvd_fps: Number(ST.$("s-fps").value),
    session_idle_hours: Number(ST.$("s-idle").value),
    session_absolute_hours: Number(ST.$("s-abs").value),
  };
  if (ST.$("s-key").value) body.libretranslate_api_key = ST.$("s-key").value;
  else if (ST.$("s-key-clear").checked) body.libretranslate_api_key = "";
  try {
    const s = await ST.api("PUT", "/api/settings", body);
    ST.msg("settings-msg", "Settings saved.", "ok");
    ST.$("insecure-banner").classList.toggle("hidden", !s.insecure_translator_url);
    state.session.default_source = s.default_source;
    state.session.default_target = s.default_target;
    state.session.max_files_per_job = s.max_files_per_job;
    await loadLanguages();
    loadSettings();
    browse("");
  } catch (e) {
    ST.msg("settings-msg", e.message, "error");
  }
});

ST.$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await ST.api("POST", "/api/settings/password", {
      current_password: ST.$("pw-current").value,
      new_password: ST.$("pw-new").value,
      new_password_confirm: ST.$("pw-new2").value,
    });
    ST.msg("password-msg", "Password changed. Other sessions were signed out.", "ok");
  } catch (e) {
    ST.msg("password-msg", e.message, "error");
  } finally {
    for (const id of ["pw-current", "pw-new", "pw-new2"]) ST.$(id).value = "";
  }
});

// --- Startup ------------------------------------------------------------------------

async function loadLanguages() {
  const banner = ST.$("lang-banner");
  try {
    const data = await ST.api("GET", "/api/languages");
    state.languages = data.languages;
    banner.classList.add("hidden");
  } catch (e) {
    state.languages = [];
    banner.textContent = `Could not load languages from LibreTranslate: ${e.message}. Check Settings.`;
    banner.classList.remove("hidden");
  }
  const langs = state.languages.length ? state.languages
    : [state.session.default_source, state.session.default_target].map((c) => ({ code: c, name: c }));
  const curSource = ST.$("source").value || state.session.default_source;
  const curTarget = ST.$("target").value || state.session.default_target;
  ST.fillLanguages(ST.$("source"), langs, curSource, true);
  ST.fillLanguages(ST.$("target"), langs, curTarget, false);
}

ST.$("target").addEventListener("change", () => browse(state.browsePath));

ST.$("logout-btn").addEventListener("click", async () => {
  try { await ST.api("POST", "/api/logout", {}); } finally { window.location.href = "/login"; }
});

(async function init() {
  try {
    state.session = await ST.api("GET", "/api/session");
  } catch (e) {
    return;
  }
  ST.csrf = state.session.csrf_token;
  ST.$("user").textContent = state.session.username;
  ST.$("insecure-banner").classList.toggle("hidden", !state.session.insecure_translator_url);
  await loadLanguages();
  browse("");
  loadJobs();
})();
