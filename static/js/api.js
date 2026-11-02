// Fetch wrapper: cursor handling mirroring the server format, photo-card
// consumption, error-shape surfacing, and localStorage persistence for the two
// client-side settings (grid style, timeline order).

const SETTINGS_KEY = "plaps.settings";

export function getSettings() {
  try {
    return { gridStyle: "sq", order: "desc", ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return { gridStyle: "sq", order: "desc" };
  }
}

export function saveSettings(patch) {
  const next = { ...getSettings(), ...patch };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
  return next;
}

export async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = body && body.error ? body.error : { code: "error", message: response.statusText };
    const err = new Error(error.message);
    err.code = error.code;
    err.status = response.status;
    throw err;
  }
  return body;
}

export function photosPage({ cursor = null, order = "desc", after = null, before = null } = {}) {
  const params = new URLSearchParams({ limit: "100", order });
  if (cursor) params.set("cursor", cursor);
  if (after) params.set("after", after);
  if (before) params.set("before", before);
  return api(`/api/photos?${params}`);
}

export async function status() {
  return api("/api/status");
}

// The three media fields are prebuilt URLs on the card; nothing to construct.
export const mediaUrls = (card) => ({ sq: card.thumb_sq, as: card.thumb_as, large: card.large });
