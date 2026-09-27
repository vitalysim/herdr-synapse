// The page's only door to the server: same-origin fetches with the session cookie,
// and the CSRF token (from GET /api/session) on every write.

let csrfToken = "";

export function setCsrf(token) {
  csrfToken = token || "";
}

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.message) || `HTTP ${status}`);
    this.status = status;
    this.code = (body && body.code) || `http_${status}`;
    this.body = body || {};
  }
}

async function parse(res) {
  let body = null;
  const type = res.headers.get("Content-Type") || "";
  if (type.startsWith("application/json")) {
    try {
      body = await res.json();
    } catch {
      body = null;
    }
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

export const teamPath = (team, rest) => `/api/teams/${encodeURIComponent(team)}/${rest}`;

export async function getJSON(path) {
  const res = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  return parse(res);
}

export async function getText(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new ApiError(res.status, null);
  return res.text();
}

export async function getBlob(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) throw new ApiError(res.status, null);
  return res.blob();
}

export async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-Synapse-CSRF": csrfToken },
    body: JSON.stringify(body),
  });
  return parse(res);
}

export async function postBytes(path, blob, contentType) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": contentType, Accept: "application/json", "X-Synapse-CSRF": csrfToken },
    body: blob,
  });
  return parse(res);
}

export function blobToDataURL(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

export async function dataURLToBlob(dataURL) {
  const res = await fetch(dataURL);
  return res.blob();
}
