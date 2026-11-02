// Search view: query box, ranked results grid, the three result states
// (endpoint down / nothing embedded in this filter / nothing reached the
// similarity floor) with filter-scoped coverage and per-card scores.

import { api, getSettings, mediaUrls } from "./api.js";

export async function showSearch(view, params) {
  const query = params.get("q") || "";
  const folder = params.get("folder");
  const after = params.get("after");
  const before = params.get("before");

  view.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "search";
  const form = document.createElement("form");
  form.className = "search-form";
  const input = document.createElement("input");
  input.type = "search";
  input.name = "q";
  input.value = query;
  input.placeholder = "search photos…";
  const submit = document.createElement("button");
  submit.textContent = "search";
  form.append(input, submit);
  wrap.appendChild(form);
  const results = document.createElement("div");
  results.className = "search-results";
  wrap.appendChild(results);
  view.appendChild(wrap);
  input.focus();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const next = new URLSearchParams({ q: input.value });
    for (const key of ["folder", "after", "before"]) {
      if (params.get(key)) next.set(key, params.get(key));
    }
    location.hash = `#/search?${next}`;
  });

  if (!query) return;
  results.innerHTML = '<div class="loading">searching…</div>';

  const searchParams = new URLSearchParams({ q: query, limit: "100" });
  if (folder) searchParams.set("folder", folder);
  if (after) searchParams.set("after", after);
  if (before) searchParams.set("before", before);

  let body;
  try {
    body = await api(`/api/search?${searchParams}`);
  } catch (err) {
    results.innerHTML = "";
    if (err.code === "endpoint_down") {
      results.appendChild(message("search is unavailable — the embedding endpoint is down", true));
    } else {
      results.appendChild(message(err.message, true));
    }
    return;
  }

  results.innerHTML = "";
  const coverage = body.coverage;
  const line = document.createElement("div");
  line.className = "coverage-line";
  line.textContent =
    `search covers ${coverage.embedded} of ${coverage.total} photos in this view`;
  results.appendChild(line);

  if (body.state === "empty_filter") {
    results.appendChild(message("nothing is embedded in this view yet — try again later"));
    return;
  }
  if (body.state === "no_matches") {
    results.appendChild(
      message("no strong matches — showing the closest photos anyway")
    );
  }

  const settings = getSettings();
  const grid = document.createElement("div");
  grid.className = "grid-sq search-grid";
  for (const card of body.photos) {
    const cell = document.createElement("div");
    cell.className = "photo";
    const img = document.createElement("img");
    img.src = mediaUrls(card)[settings.gridStyle === "sq" ? "sq" : "as"];
    img.loading = "lazy";
    img.alt = `${card.date_taken} (score ${card.score.toFixed(2)})`;
    img.addEventListener("click", () => {
      location.hash = `#/photo/${card.photo_id}`;
    });
    cell.appendChild(img);
    const score = document.createElement("div");
    score.className = "score";
    score.textContent = card.score.toFixed(2);
    cell.appendChild(score);
    grid.appendChild(cell);
  }
  results.appendChild(grid);
}

function message(text, isError = false) {
  const el = document.createElement("div");
  el.className = isError ? "loading error" : "loading";
  el.textContent = text;
  return el;
}
