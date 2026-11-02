// Virtualized timeline grid: windowed fetching via IntersectionObserver, both
// grid styles (sq uniform square, as justified rows), date headers, and DOM
// virtualization — off-screen row blocks are unmounted and replaced with
// measured-height spacers, so tens of thousands of <img> elements are never
// live at once. Hand-rolled; no vendored virtualization library.

import { getSettings, photosPage, mediaUrls } from "./api.js";

const VIEWPORT_MARGIN = "1200px";
const UNMOUNT_MARGIN = "3000px";
const ROW_TARGET_HEIGHT = 200;

export class Timeline {
  constructor(container = null) {
    this.container = container || document.createElement("div");
    this.cards = [];          // all loaded photo cards, in fetch order
    this.cursor = null;
    this.exhausted = false;
    this.fetching = false;
    this.blocks = new Map();  // index -> { mounted: bool, height: number, el: Element }

    this.observer = new IntersectionObserver(
      (entries) => this._onObserve(entries),
      { rootMargin: VIEWPORT_MARGIN }
    );
    this.unmountObserver = new IntersectionObserver(
      (entries) => this._onUnmountCheck(entries),
      { rootMargin: UNMOUNT_MARGIN }
    );
  }

  reset() {
    this.cards = [];
    this.cursor = null;
    this.exhausted = false;
    this.blocks.clear();
    this.container.innerHTML = "";
  }

  async loadMore() {
    if (this.fetching || this.exhausted) return;
    this.fetching = true;
    try {
      const settings = getSettings();
      const page = await photosPage({ cursor: this.cursor, order: settings.order });
      this.cursor = page.next_cursor;
      if (!page.next_cursor) this.exhausted = true;
      this._appendCards(page.photos);
    } finally {
      this.fetching = false;
    }
  }

  async jumpTo(dateIso) {
    // One date-bounded request reaches the target year; no cursor paging needed.
    const settings = getSettings();
    const page = await photosPage({ order: settings.order, before: dateIso });
    this.reset();
    this.cursor = page.next_cursor;
    if (!page.next_cursor) this.exhausted = true;
    this._appendCards(page.photos);
    this._virtualize();
  }

  // -- layout --------------------------------------------------------------

  _appendCards(newCards) {
    const start = this.cards.length;
    this.cards.push(...newCards);
    for (let i = start; i < this.cards.length; i += 40) {
      this.blocks.set(i, { mounted: false, height: 0, el: null });
    }
  }

  _render() {
    this.container.innerHTML = "";
    for (const block of this.blocks.values()) block.el = null;
    for (const [index] of this.blocks) this.blocks.get(index).mounted = false;
    this._virtualize();
  }

  _virtualize() {
    const fragment = document.createDocumentFragment();
    let pending = [];
    for (const [index, block] of this.blocks) {
      const chunk = this.cards.slice(index, index + 40);
      if (block.mounted) {
        fragment.appendChild(block.el);
        continue;
      }
      const spacer = document.createElement("div");
      spacer.className = "spacer";
      spacer.style.height = `${block.height}px`;
      if (block.height === 0) {
        // not yet measured: mount it so the observer can measure and prune
        const el = this._buildBlock(index, chunk);
        this.blocks.get(index).mounted = true;
        this.blocks.get(index).el = el;
        fragment.appendChild(el);
        this.observer.observe(el);
        this.unmountObserver.observe(el);
      } else {
        fragment.appendChild(spacer);
      }
      pending = null;
    }
    const sentinel = document.createElement("div");
    sentinel.className = "sentinel";
    sentinel.id = "timeline-sentinel";
    fragment.appendChild(sentinel);
    this.container.appendChild(fragment);
  }

  _buildBlock(index, chunk) {
    const settings = getSettings();
    const block = document.createElement("div");
    block.dataset.block = String(index);
    if (settings.gridStyle === "sq") {
      const grid = document.createElement("div");
      grid.className = "grid-sq";
      for (const card of chunk) grid.appendChild(this._photoCell(card));
      block.appendChild(grid);
    } else {
      block.classList.add("grid-as");
      this._layoutJustifiedRows(block, chunk);
    }
    return block;
  }

  _layoutJustifiedRows(block, chunk) {
    // justified rows: pack photos into rows of equal display height using the
    // card's stored (post-orientation) dimensions.
    let row = [];
    let rowAspect = 0;
    const containerWidth = this.container.clientWidth || 1000;
    const flush = () => {
      if (!row.length) return;
      const rowEl = document.createElement("div");
      rowEl.className = "row";
      const height = Math.round((containerWidth - (row.length - 1) * 4) / rowAspect);
      for (const card of row) {
        const cell = this._photoCell(card);
        const aspect = card.width / card.height || 1;
        cell.style.flex = `0 0 ${Math.round(height * aspect)}px`;
        cell.style.height = `${height}px`;
        rowEl.appendChild(cell);
      }
      block.appendChild(rowEl);
      row = [];
      rowAspect = 0;
    };
    for (const card of chunk) {
      const aspect = card.width / card.height || 1;
      row.push(card);
      rowAspect += aspect;
      if (containerWidth / rowAspect < ROW_TARGET_HEIGHT) flush();
    }
    flush();
  }

  _photoCell(card) {
    const cell = document.createElement("div");
    cell.className = "photo";
    const img = document.createElement("img");
    const settings = getSettings();
    img.src = mediaUrls(card)[settings.gridStyle === "sq" ? "sq" : "as"];
    img.loading = "lazy";
    img.alt = card.date_taken;
    img.addEventListener("click", () => {
      location.hash = `#/photo/${card.photo_id}`;
    });
    cell.appendChild(img);
    return cell;
  }

  // -- observers -----------------------------------------------------------

  _onObserve(entries) {
    let changed = false;
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      if (entry.target.id === "timeline-sentinel") {
        this.loadMore().then(() => this._virtualize());
        continue;
      }
      const block = this.blocks.get(Number(entry.target.dataset.block));
      if (block && block.height === 0 && entry.target.offsetHeight > 0) {
        // measured: record and rebuild so far-away blocks can become spacers
        block.height = entry.target.offsetHeight;
        changed = true;
      }
    }
    if (changed) this._virtualize();
  }

  _onUnmountCheck(entries) {
    for (const entry of entries) {
      if (entry.isIntersecting) continue;
      const index = Number(entry.target.dataset.block);
      const block = this.blocks.get(index);
      if (block && block.height > 0) {
        // off-screen by more than the margin: unmount behind a measured spacer
        this.observer.unobserve(entry.target);
        this.unmountObserver.unobserve(entry.target);
        block.mounted = false;
        block.el = null;
        this._virtualize();
      }
    }
  }

  setStyle() {
    this._render();
  }
}
