// First-run progress indicator and the stalled-scan error banner: polls
// /api/status and keys the banner on its scan_stalled field.

import { status } from "./api.js";

export class StatusBanner {
  constructor(element) {
    this.element = element;
    this.timer = null;
  }

  start() {
    this.timer = setInterval(() => this.refresh(), 5000);
    this.refresh();
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
  }

  async refresh() {
    let info;
    try {
      info = await status();
    } catch {
      this._show("service unreachable", false);
      return;
    }
    const scan = info.scan || {};
    if (scan.scan_stalled) {
      this._show("scan stalled — no progress for 15 minutes; check the service logs", true);
      return;
    }
    const stats = scan.stats || {};
    const queue = (info.queue && info.queue.thumb && info.queue.thumb.pending) || 0;
    const total = Object.values(info.queue || {}).reduce(
      (sum, kinds) => sum + Object.values(kinds).reduce((a, b) => a + b, 0), 0
    );
    if (total > 0) {
      const indexed = (scan.checkpoint && scan.checkpoint.scanned) || 0;
      this._show(`indexing… ${indexed} photos scanned, ${queue} thumbnails queued`, false);
    } else if (this.element.dataset.sticky !== "1") {
      this.element.hidden = true;
    }
  }

  _show(message, isError) {
    this.element.hidden = false;
    this.element.className = isError ? "error" : "";
    this.element.innerHTML = "";
    this.element.append(message);
    const dismiss = document.createElement("span");
    dismiss.className = "dismiss";
    dismiss.textContent = "×";
    dismiss.addEventListener("click", () => {
      this.element.hidden = true;
      this.element.dataset.sticky = "1";
    });
    this.element.appendChild(dismiss);
  }
}
