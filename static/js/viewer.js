// Photo viewer: the lazy screen-fit image (generated on first view, then
// cached), an explicit original-download action, and keyboard navigation.

import { api } from "./api.js";

export async function showViewer(view, photoId, cards) {
  const card = cards.find((c) => c.photo_id === Number(photoId))
    || (await api(`/api/photos?limit=500&order=desc`).then((page) => page.photos.find(
      (c) => c.photo_id === Number(photoId)
    )));
  if (!card) {
    view.innerHTML = '<div class="loading">photo not found</div>';
    return;
  }

  const index = cards.indexOf(card);
  const viewer = document.createElement("div");
  viewer.id = "viewer";
  viewer.innerHTML = `
    <div class="viewer-bar">
      <a href="#/">close</a>
      <a href="${card.original}" download>download original</a>
    </div>
    <img class="screenfit" src="${card.large}" alt="photo ${card.photo_id}">
  `;
  view.innerHTML = "";
  view.appendChild(viewer);

  const step = (delta) => {
    const next = cards[index + delta];
    if (next) location.hash = `#/photo/${next.photo_id}`;
  };
  viewer.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight") step(1);
    if (event.key === "ArrowLeft") step(-1);
    if (event.key === "Escape") location.hash = "#/";
  });
  viewer.tabIndex = 0;
  viewer.focus();
}
