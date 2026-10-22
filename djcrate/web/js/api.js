// Fetch wrapper: every failure becomes an ApiError with a readable message.

export class ApiError extends Error {
  constructor(code, message, extra = {}, status = 0) {
    super(message);
    this.code = code;
    this.extra = extra;
    this.status = status;
  }
}

export async function api(method, path, body) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body !== undefined ? { "content-type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError("backend_unreachable", "Can't reach DJ Crate's engine.");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = data.error || {};
    let message = e.message;
    if (!message && Array.isArray(data.detail)) message = "Invalid request: " + data.detail.map((d) => d.msg).join(", ");
    throw new ApiError(e.code || `http_${r.status}`, message || `Request failed (${r.status})`, e, r.status);
  }
  return data;
}

export const get = (p) => api("GET", p);
export const post = (p, b = {}) => api("POST", p, b);
export const patch = (p, b) => api("PATCH", p, b);
export const put = (p, b) => api("PUT", p, b);
export const del = (p) => api("DELETE", p);

// Native bits from the pywebview window; prompts as a fallback in a plain browser.
const bridge = () => window.pywebview && window.pywebview.api;

export const native = {
  get available() { return !!bridge(); },
  async pickFolder(promptText = "Folder path:") {
    if (bridge()) return await bridge().pick_folder();
    return window.prompt(promptText) || null;
  },
  async pickFile(kind = "mp3", promptText = "File path:") {
    if (bridge()) return await bridge().pick_file(kind);
    return window.prompt(promptText) || null;
  },
  async restart() {
    if (bridge()) return await bridge().restart();
    location.reload();
  },
};
