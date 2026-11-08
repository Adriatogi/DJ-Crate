// Docs tab: a plain HTML fragment (docs.html) shipped with the app, so it works offline.

import { api } from "./api.js";
import { emit } from "./store.js";
import { h } from "./ui.js";

const root = () => document.getElementById("view-docs");
let loaded = false;

export async function showDocs() {
  if (loaded) return;
  let html;
  try {
    const r = await fetch("/docs.html");
    if (!r.ok) throw new Error(r.status);
    html = await r.text();
  } catch {
    root().replaceChildren(h("div", { class: "docs-wrap" }, h("div", { class: "docs" }, h("p", {}, "Couldn't load the docs."))));
    return;
  }
  const article = h("div", { class: "docs" });
  article.innerHTML = html;
  const toc = h("nav", { class: "docs-toc" }, h("div", { class: "toc-title" }, "On this page"));
  article.querySelectorAll("h2").forEach((h2) => {
    toc.append(h("a", { href: `#${h2.id}`, onclick: (e) => { e.preventDefault(); h2.scrollIntoView({ behavior: "smooth", block: "start" }); } }, h2.textContent));
  });
  article.addEventListener("click", (e) => {
    const a = e.target.closest("a");
    if (!a) return;
    e.preventDefault();
    if (a.dataset.tab) emit("show-tab", a.dataset.tab);
    else if (/^https?:/.test(a.getAttribute("href") || "")) api("POST", "/api/open_url", { url: a.href });
  });
  root().replaceChildren(h("div", { class: "docs-wrap" }, toc, article));
  loaded = true;
}
