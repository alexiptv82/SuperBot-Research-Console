// Multipart OLD36 bundle upload (5 raw binary parts).
//
// Client reconstructs nothing locally \u2014 it just uploads the 5 parts to
// their server-side .part slots via the existing /api/bundles/{id}/chunk
// endpoints, then triggers assemble which:
//   1) concatenates parts (streaming, server-side)
//   2) verifies SHA256
//   3) hands the reassembled ZIP to the existing single-bundle import
//      pipeline (inner ZIP validation + per-session QA + retention).

import { api } from "@/lib/api";

const DEFAULT_CHUNK = 8 * 1024 * 1024;

export async function fetchMultipartManifest() {
  const r = await api.get("/bundles/multipart/manifest");
  return r.data;
}

async function postChunk(uploadId, index, blob, signal) {
  const buf = await blob.arrayBuffer();
  const r = await api.post(`/bundles/${uploadId}/chunk/${index}`, buf, {
    headers: { "Content-Type": "application/octet-stream" },
    timeout: 300_000,
    signal,
  });
  return r.data;
}

async function uploadOnePart(slot, file, { onProgress, signal, maxRetries = 3 }) {
  const chunkSize = slot.chunk_size || DEFAULT_CHUNK;
  const totalChunks = slot.total_chunks || Math.ceil(file.size / chunkSize);
  let uploaded = 0;
  for (let i = 0; i < totalChunks; i++) {
    const start = i * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    const blob = file.slice(start, end);
    let attempt = 0;
    while (true) {
      try {
        if (signal?.aborted) throw new Error("aborted");
        await postChunk(slot.upload_id, i, blob, signal);
        break;
      } catch (err) {
        attempt += 1;
        if (attempt > maxRetries) {
          const detail = err?.response?.data?.detail || err?.message || "chunk upload failed";
          throw new Error(`Part ${slot.part_name} chunk ${i}: ${detail}`);
        }
        await new Promise((res) => setTimeout(res, 500 * Math.pow(2, attempt)));
      }
    }
    uploaded = end;
    onProgress?.({ partIndex: slot.part_index, uploadedBytes: uploaded, totalBytes: file.size });
  }
}

export async function uploadMultipartBundle({
  files,        // File[] length 5, in order part-00..part-04
  onProgress = () => {},
  signal,
}) {
  onProgress({ phase: "manifest" });
  const manifest = await fetchMultipartManifest();

  // 1. Local validation.
  if (files.length !== manifest.parts.length) {
    throw new Error(
      `Expected ${manifest.parts.length} parts, got ${files.length}`,
    );
  }
  // Sort by filename to enforce lexical order regardless of drop order.
  const orderedFiles = [...files].sort((a, b) => a.name.localeCompare(b.name));
  manifest.parts.forEach((p, i) => {
    if (orderedFiles[i].name !== p.name) {
      throw new Error(
        `Slot ${i}: expected file name ${p.name}, got ${orderedFiles[i].name}`,
      );
    }
    if (orderedFiles[i].size !== p.size) {
      throw new Error(
        `Slot ${i} (${p.name}): expected size ${p.size} bytes, got ${orderedFiles[i].size}`,
      );
    }
  });

  onProgress({ phase: "init" });
  const init = await api.post("/bundles/multipart/init", {
    parts: manifest.parts.map((p) => ({ name: p.name, size: p.size })),
  });
  const multipartId = init.data.multipart_id;
  const slots = init.data.slots;

  const partUploaded = new Array(slots.length).fill(0);
  const totalBytes = manifest.bundle_total_size;

  const emit = () => {
    const uploaded = partUploaded.reduce((a, b) => a + b, 0);
    onProgress({
      phase: "uploading",
      uploaded,
      total: totalBytes,
      partProgress: partUploaded.map((b, i) => ({
        partIndex: i,
        uploadedBytes: b,
        totalBytes: manifest.parts[i].size,
      })),
    });
  };
  emit();

  try {
    // 2. Upload parts sequentially. Parallel would race disk IO for
    //    2 GB across the same volume; sequential keeps memory + IO
    //    predictable.
    for (let i = 0; i < slots.length; i++) {
      await uploadOnePart(slots[i], orderedFiles[i], {
        signal,
        onProgress: (p) => {
          partUploaded[i] = p.uploadedBytes;
          emit();
        },
      });
    }

    // 3. Assemble.
    onProgress({ phase: "assembling", uploaded: totalBytes, total: totalBytes });
    const assemble = await api.post(
      `/bundles/multipart/${multipartId}/assemble`,
      {},
      { timeout: 900_000 },
    );
    onProgress({ phase: "done", uploaded: totalBytes, total: totalBytes, result: assemble.data });
    return assemble.data;
  } catch (err) {
    try { await api.delete(`/bundles/multipart/${multipartId}`); } catch (_) {}
    throw err;
  }
}
