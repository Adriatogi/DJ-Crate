// Shared app state + a tiny event bus.

export const store = {
  status: null,      // last /api/status
  sidebar: null,     // last /api/sidebar
  view: localStorage.getItem("djc.view") || "smart:all",
  tracks: [],        // tracks in the current view
};

const listeners = {};

export function on(evt, fn) {
  (listeners[evt] ||= []).push(fn);
}

export function emit(evt, data) {
  for (const fn of listeners[evt] || []) {
    try { fn(data); } catch (e) { console.error(e); }
  }
}

export const LINK_RE = /^https?:\/\/([a-z0-9-]+\.)*(youtube\.com|youtu\.be|soundcloud\.com|bandcamp\.com)(\/|$)/i;

export function readOnly() {
  return !!store.status?.library?.read_only;
}

export function flatCrates(nodes = store.sidebar?.tree || [], prefix = "") {
  const out = [];
  for (const n of nodes) {
    const path = prefix ? `${prefix} / ${n.name}` : n.name;
    out.push({ ...n, path });
    out.push(...flatCrates(n.children, path));
  }
  return out;
}

export function findCrate(id) {
  return flatCrates().find((c) => c.id === id) || null;
}
