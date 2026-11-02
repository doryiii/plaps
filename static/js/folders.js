// Folder tree navigation: fetches the derived tree once per scan generation
// (the server memoizes it) and renders expandable nodes. Entering a folder
// scopes the timeline via #/folder/<id>.

import { api } from "./api.js";

let cached = null; // the tree, cached until the hash route changes away

export async function showFolders(view) {
  view.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "folders";
  const loading = document.createElement("div");
  loading.className = "loading";
  loading.textContent = "loading folders…";
  wrap.appendChild(loading);
  view.appendChild(wrap);

  let root;
  try {
    root = await api("/api/folders");
  } catch (err) {
    loading.textContent = err.message;
    return;
  }
  cached = root;
  wrap.innerHTML = "";
  wrap.appendChild(renderNode(root, 0));
}

function renderNode(node, depth) {
  const details = document.createElement("details");
  details.open = depth < 1; // top level expanded, the rest collapsed
  const summary = document.createElement("summary");
  const name = document.createElement("span");
  name.textContent = node.name;
  summary.appendChild(name);
  const meta = document.createElement("span");
  meta.className = "folder-meta";
  const when = node.newest_date_taken ? node.newest_date_taken.slice(0, 10) : "";
  meta.textContent = `${node.photo_count} photos${when ? " · newest " + when : ""}`;
  summary.appendChild(meta);
  details.appendChild(summary);

  const open = document.createElement("button");
  open.className = "folder-open";
  open.textContent = "open";
  open.addEventListener("click", (event) => {
    event.preventDefault();
    location.hash = `#/folder/${node.id}`;
  });
  summary.appendChild(open);

  for (const child of node.children) {
    details.appendChild(renderNode(child, depth + 1));
  }
  return details;
}

export const folderName = (id) => id; // the route only needs the id; names come from the tree
