"use strict";

const FALLBACK_LANGUAGES = [
  { code: "en", name: "English" },
  { code: "fr", name: "French" },
  { code: "de", name: "German" },
  { code: "es", name: "Spanish" },
  { code: "it", name: "Italian" },
  { code: "nl", name: "Dutch" },
  { code: "pt", name: "Portuguese" },
];

let setupToken = "";
let mediaPath = "";

function fillDefaults(languages) {
  const source = ST.$("def-source").value || "en";
  const target = ST.$("def-target").value || "fr";
  ST.fillLanguages(ST.$("def-source"), languages, source, false);
  ST.fillLanguages(ST.$("def-target"), languages, target, false);
}

async function loadFolders(path) {
  ST.msg("media-msg", "");
  try {
    const data = await ST.api("POST", "/api/setup/folders", { token: setupToken, path });
    mediaPath = data.path;
    ST.$("media-selected").textContent = "/" + mediaPath;
    const crumbs = ST.clear(ST.$("media-crumbs"));
    crumbs.append(ST.el("button", { type: "button", class: "link", text: "media", onclick: () => loadFolders("") }));
    let acc = "";
    for (const part of mediaPath ? mediaPath.split("/") : []) {
      acc = acc ? `${acc}/${part}` : part;
      const target = acc;
      crumbs.append(" / ", ST.el("button", { type: "button", class: "link", text: part, onclick: () => loadFolders(target) }));
    }
    const list = ST.clear(ST.$("media-folders"));
    if (!data.folders.length) list.append(ST.el("li", { class: "hint", text: "No sub-folders" }));
    for (const name of data.folders) {
      const next = mediaPath ? `${mediaPath}/${name}` : name;
      list.append(ST.el("li", {}, ST.el("button", { type: "button", class: "link", text: "📁 " + name, onclick: () => loadFolders(next) })));
    }
  } catch (e) {
    ST.msg("media-msg", e.message, "error");
  }
}

ST.$("token-btn").addEventListener("click", async () => {
  const token = ST.$("token").value.trim();
  ST.msg("token-msg", "");
  try {
    const data = await ST.api("POST", "/api/setup/check", { token });
    setupToken = token;
    ST.$("step-token").classList.add("hidden");
    ST.$("setup-form").classList.remove("hidden");
    fillDefaults(FALLBACK_LANGUAGES);
    if (!data.media_base_exists) ST.msg("media-msg", "The /media mount does not exist — check docker-compose.yml.", "error");
    else loadFolders("");
  } catch (e) {
    ST.msg("token-msg", e.message, "error");
  }
});

ST.$("test-btn").addEventListener("click", async () => {
  ST.msg("test-msg", "Testing…");
  try {
    const data = await ST.api("POST", "/api/setup/test", {
      token: setupToken,
      url: ST.$("lt-url").value.trim(),
      api_key: ST.$("lt-key").value,
    });
    if (!data.ok) {
      ST.msg("test-msg", "Connection failed: " + data.error, "error");
      return;
    }
    fillDefaults(data.languages);
    let text = `Connected — ${data.languages.length} languages available.`;
    if (data.insecure) text += " Warning: plain http to a remote host; subtitle text travels unencrypted.";
    ST.msg("test-msg", text, data.insecure ? "warn" : "ok");
  } catch (e) {
    ST.msg("test-msg", e.message, "error");
  }
});

ST.$("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  ST.msg("setup-msg", "");
  if (ST.$("password").value !== ST.$("password2").value) {
    ST.msg("setup-msg", "Passwords do not match", "error");
    return;
  }
  try {
    await ST.api("POST", "/api/setup", {
      token: setupToken,
      username: ST.$("username").value.trim(),
      password: ST.$("password").value,
      password_confirm: ST.$("password2").value,
      libretranslate_url: ST.$("lt-url").value.trim(),
      libretranslate_api_key: ST.$("lt-key").value,
      media_root: mediaPath,
      default_source: ST.$("def-source").value,
      default_target: ST.$("def-target").value,
    });
    window.location.href = "/login";
  } catch (e) {
    ST.msg("setup-msg", e.message, "error");
  }
});
