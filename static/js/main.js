// Hash-based router wiring the views together: #/ (timeline) and
// #/photo/<id> (viewer). Grid style and timeline order are client-side
// settings persisted in localStorage — no server keys, no settings endpoint.

import { getSettings, saveSettings } from "./api.js";
import { Timeline } from "./timeline.js";
import { StatusBanner } from "./status.js";
import { showViewer } from "./viewer.js";
import { showFolders } from "./folders.js";

const view = document.getElementById("view");
const bannerElement = document.getElementById("banner");
const timeline = new Timeline();
const banner = new StatusBanner(bannerElement);

const gridButton = document.getElementById("grid-style");
const orderButton = document.getElementById("order");
const dateJump = document.getElementById("date-jump");

function reflectSettings() {
  const settings = getSettings();
  gridButton.textContent = settings.gridStyle === "sq" ? "squares" : "rows";
  orderButton.textContent = settings.order === "desc" ? "newest" : "oldest";
}

gridButton.addEventListener("click", () => {
  const settings = getSettings();
  saveSettings({ gridStyle: settings.gridStyle === "sq" ? "as" : "sq" });
  reflectSettings();
  timeline.setStyle();
});

orderButton.addEventListener("click", () => {
  const settings = getSettings();
  saveSettings({ order: settings.order === "desc" ? "asc" : "desc" });
  reflectSettings();
  timeline.reset();
  timeline.loadMore().then(() => timeline._virtualize());
});

dateJump.addEventListener("change", () => {
  if (dateJump.value) timeline.jumpTo(dateJump.value);
});

async function route() {
  const hash = location.hash || "#/";
  if (hash.startsWith("#/photo/")) {
    await showViewer(view, hash.slice("#/photo/".length), timeline.cards);
    return;
  }
  if (hash.startsWith("#/folders")) {
    await showFolders(view);
    return;
  }
  if (hash.startsWith("#/folder/")) {
    const folderId = hash.slice("#/folder/".length);
    view.innerHTML = "";
    view.appendChild(timeline.container);
    timeline.container.id = "timeline";
    if (timeline.scope !== folderId) {
      timeline.scope = folderId; // entering a folder scopes the timeline
      timeline.reset();
    }
    if (timeline.cards.length === 0) await timeline.loadMore();
    timeline._virtualize();
    return;
  }
  view.innerHTML = "";
  view.appendChild(timeline.container);
  timeline.container.id = "timeline";
  if (timeline.scope !== null) {
    timeline.scope = null;
    timeline.reset();
  }
  if (timeline.cards.length === 0) await timeline.loadMore();
  timeline._virtualize();
}

window.addEventListener("hashchange", route);
reflectSettings();
banner.start();
route();
