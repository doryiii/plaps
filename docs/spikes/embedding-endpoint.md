# Spike: EmbeddingGemma 2 embedding endpoint contract

* **Date:** 2026-10-30
* **Status:** Verified end-to-end on this machine
* **Feeds:** `plaps/embed/adapter.py`, `tests/test_adapter_contract.py`

## Environment

* `llama-server` **0.8.0-dev** (build 0; the build reports an empty commit string — the adapter should not depend on version detection)
* Models: `ggmlorg-embeddinggemma-2-BF16.gguf` (558 MB) + `ggmlorg-mmproj-embeddinggemma-2-BF16.gguf` (982 MB)
* Launch flags: `--embeddings --mmproj <mmproj.gguf> --host 127.0.0.1 --port 8095 --device Vulkan1 -ngl 99` (Ryzen 5 5700G iGPU, RADV RENOIR)
* Server self-reports capabilities `["completion","multimodal"]` — mmproj confirmed loaded via `/v1/models`.

## Endpoint contract

`POST /v1/embeddings`

**Text** — plain string works:

```json
{"input": "a dog on the beach"}
```

**Image — the only working shape** (typed parts nested in a `content` array inside each `input` item):

```json
{"input": [
  {"content": [
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<...>"}}
  ]}
]}
```

**Response:** OpenAI-shaped. `data[i].embedding` (order preserved), `usage.prompt_tokens` includes image tokens. Vectors are **768-dim** for both modalities, **L2-normalized by the server** (measured norm exactly 1.000000). The adapter still normalizes defensively per the design.

## Failure modes (all reproduced live)

1. **Flat OpenAI shape** (`{"input": [{"type": "image_url", ...}]}`) → `400` with `"prompt" elements must be a string, a list of tokens, ...`. The parts must be one level deeper, inside `content`.
2. **Data URI as a plain input string** → the base64 is **tokenized as text**. A 4 MB JPEG produced `input (430215 tokens) is too large to process` (`500`). A *small* image would silently succeed and return a text-embedding-of-the-base64 — a garbage vector. **The adapter must never send data URIs as plain strings.**
3. **Legacy `POST /embedding` with an `image_data` field** returns `200` with a *text-only* vector (per ggml-org/llama.cpp#30082, not re-tested here). Avoid the legacy endpoint entirely.

## Batching

* An array of `input` items is accepted; **order is preserved**; per-item vectors match serial embeddings to cos ≥ 0.999997 (float drift only).
* **Mixed batches** (text + image items in one request) work.
* Batching is a transport convenience, **not** a throughput win: 8 images in one request took ~20.2 s vs ~2.5 s each serially on this host.

## Latency (this host — dev box, not the production AI box)

* ~2.4–2.7 s per image (first request 2.85 s cold).
* ~234–260 prompt tokens per image after mmproj resize (independent of source resolution).
* Text adds no measurable cost (mixed text+image batch ≈ image alone).
* The production AI box is expected faster (owner observed 0.1–1 s/photo there). First full embed of 150K photos: **re-estimate on the AI box before promising a window**; at this dev box's rate it would be ~4.2 days, not the 4–42 h in the design doc.

## Payload sizes

* Originals up to 4.3 MB (base64 ≈ 5.7 MB) accepted; an 8-image request (~20 MB total) accepted.
* Since mmproj resizes to the model's input resolution (~250 tokens regardless), full-resolution upload is mostly wasted bandwidth. **Adapter-level option:** feed the cached ~2048 px screen-fit image instead of the original. Flagged as an adapter decision during implementation (trade-off: bandwidth vs fidelity; default: original bytes until measured otherwise).

## Semantic verification (real photos)

8 photos from `/tmp/2021/` (ground-truth descriptions obtained from the Gemma-4 vision endpoint, not folder names — the "Hawaii" folder opens with a bowl of noodle soup), 8 natural-language queries:

* **8/8 top-1 retrieval.** Match cosines 0.62–0.75; runner-up cosines 0.53–0.68 — clean separation.
* Cross-modal joint space confirmed: text and image vectors are directly comparable.

## Related infrastructure on this machine

* A Qwen3-Embedding-8B server already runs at `127.0.0.1:8091` (text-only) — a candidate swap target proving the adapter's pluggability requirement.
* Gemma-4 31B vision server at `192.168.1.213:8090` (chat-completions; `messages[].content[]` with `image_url` parts) was used for ground truth.
