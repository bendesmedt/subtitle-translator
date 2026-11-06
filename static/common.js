/* Shared helpers: fetch wrapper with CSRF header, DOM builder. No innerHTML. */
"use strict";

const ST = {
  csrf: null,

  async api(method, url, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      if (ST.csrf) opts.headers["X-CSRF-Token"] = ST.csrf;
      opts.body = JSON.stringify(body || {});
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      throw new Error("Network error: server unreachable");
    }
    if (resp.status === 401 && !url.startsWith("/api/login")) {
      window.location.href = "/login";
      throw new Error("Not logged in");
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      const msg = data && typeof data.detail === "string" ? data.detail : `Request failed (HTTP ${resp.status})`;
      throw new Error(msg);
    }
    return data;
  },

  el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  },

  clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  },

  $(id) {
    return document.getElementById(id);
  },

  msg(id, text, kind) {
    const node = ST.$(id);
    if (!node) return;
    node.textContent = text || "";
    node.className = "msg" + (kind ? " " + kind : "");
  },

  fillLanguages(select, languages, selected, withAuto) {
    ST.clear(select);
    if (withAuto) select.append(ST.el("option", { value: "auto", text: "Auto-detect" }));
    for (const lang of languages) {
      select.append(ST.el("option", { value: lang.code, text: `${lang.name} (${lang.code})` }));
    }
    if (selected && [...select.options].some((o) => o.value === selected)) select.value = selected;
  },

  formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  },
};
