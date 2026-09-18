// Sequential OLD36 missing-RAW recovery orchestration.
//
// Pure logic module (no React, no DOM) so it can be unit tested with
// plain Jest. The UI component (Old36MissingRawPanel.jsx) wires this
// to the real chunked-upload client and the /api/reference/old36
// endpoint; tests here wire it to mocks instead.
//
// Contract enforced by runSequentialOld36Import:
// - files are processed ONE AT A TIME (sequential await, never
//   Promise.all / concurrent uploads)
// - after every RETAINED or EXACT_DUPLICATE outcome, the caller's
//   `fetchState()` (GET /api/reference/old36) is re-awaited so the
//   backend stays the single source of truth for raw_ready
// - the sequence STOPS immediately on the first blocking failure
//   (BLOCKED_DISK_SPACE, OLD36_IDENTITY_REJECTED, QA_FAILED, or any
//   other generic failure) and does not touch subsequent files

export const OLD36_CHECKPOINT_HINT = "OLD36_REFERENCE";
export const OLD36_RETAIN_RAW = true;

export const OUTCOME = {
  RETAINED: "RETAINED",
  EXACT_DUPLICATE: "EXACT_DUPLICATE",
  BLOCKED_DISK_SPACE: "BLOCKED_DISK_SPACE",
  OLD36_IDENTITY_REJECTED: "OLD36_IDENTITY_REJECTED",
  QA_FAILED: "QA_FAILED",
  GENERIC_FAILURE: "GENERIC_FAILURE",
};

// Outcomes after which the sequence is allowed to continue to the
// next file. Everything else stops the batch.
const CONTINUE_OUTCOMES = new Set([OUTCOME.RETAINED, OUTCOME.EXACT_DUPLICATE]);

// Outcomes that trigger an /api/reference/old36 refresh. Per spec,
// this fires "after every successful retention or EXACT_DUPLICATE" —
// i.e. exactly the CONTINUE_OUTCOMES set. Kept as a separate export
// (even though currently identical) so the two concerns don't get
// silently coupled if either policy changes later.
const REFRESH_OUTCOMES = CONTINUE_OUTCOMES;

/**
 * Classify the result of a single uploadChunked() call into one of
 * the OUTCOME values. Never throws.
 *
 * @param {object|null} result - resolved payload from uploadChunked
 *   on success (server's /api/uploads/{id}/complete response body).
 * @param {Error|null} error - the error uploadChunked threw, if any.
 */
export function classifyUploadResult(result, error) {
  if (error) {
    const detail =
      error?.response?.data?.detail || error?.message || String(error || "");
    if (detail.includes("BLOCKED_DISK_SPACE")) return OUTCOME.BLOCKED_DISK_SPACE;
    if (detail.includes("OLD36_IDENTITY_REJECTED")) {
      return OUTCOME.OLD36_IDENTITY_REJECTED;
    }
    return OUTCOME.GENERIC_FAILURE;
  }
  if (!result) return OUTCOME.GENERIC_FAILURE;
  if (result.duplicate_status === "EXACT_DUPLICATE") return OUTCOME.EXACT_DUPLICATE;
  if (result.verdict && result.verdict !== "PASS" && result.verdict !== "PASS_WITH_WARNING") {
    return OUTCOME.QA_FAILED;
  }
  return OUTCOME.RETAINED;
}

/**
 * Derive the authoritative missing-RAW display state from a raw
 * GET /api/reference/old36 payload. Never recomputes raw_ready
 * itself — it is passed through verbatim from the backend, per the
 * "authoritative state" requirement (frontend counters must not be
 * the source of truth for raw_ready).
 */
export function deriveOld36MissingState(referenceState) {
  const sessions = Array.isArray(referenceState?.sessions)
    ? referenceState.sessions
    : [];
  const missingSessionIds = sessions
    .filter((s) => !s.raw_retained)
    .map((s) => s.session_id);
  return {
    presentSessions: referenceState?.present_sessions ?? 0,
    expectedSessions: referenceState?.expected_sessions ?? 11,
    rawRetainedSessions: referenceState?.raw_retained_sessions ?? 0,
    rawRetainedNominalHours: referenceState?.raw_retained_nominal_hours ?? 0,
    // Passed through unchanged from the backend response. This is
    // intentionally NOT derived from missingSessionIds.length on the
    // client, so a frontend bug can never fake "ready".
    rawReady: referenceState?.raw_ready ?? false,
    missingSessionIds,
  };
}

/**
 * Best-effort, non-authoritative association between a selected
 * filename and one of the currently-missing OLD36 session ids.
 * Used ONLY to help the operator sanity-check their file selection
 * before upload; the server-side QA-derived identity gate
 * (OLD36_IDENTITY_REJECTED) remains the sole security boundary.
 */
export function matchFileToMissingSession(filename, missingSessionIds) {
  if (!filename || !Array.isArray(missingSessionIds)) return null;
  return missingSessionIds.find((sid) => filename.includes(sid)) || null;
}

/**
 * Drive the sequential one-at-a-time OLD36 missing-RAW import.
 *
 * @param {File[]} files - locally selected File objects (already
 *   held by the caller; never sent as a single multipart batch).
 * @param {(file: File) => Promise<object>} uploadOne - uploads ONE
 *   file and resolves with the server's completion payload, or
 *   rejects. The caller is responsible for fixing
 *   checkpointHint=OLD36_CHECKPOINT_HINT and retainRaw=OLD36_RETAIN_RAW
 *   on every call.
 * @param {() => Promise<object>} fetchState - re-fetches
 *   GET /api/reference/old36 and resolves with the parsed body.
 * @param {(event: object) => void} [onEvent] - progress callback.
 */
export async function runSequentialOld36Import({
  files,
  uploadOne,
  fetchState,
  onEvent = () => {},
}) {
  const results = [];
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    onEvent({ type: "file_start", index: i, total: files.length, filename: file.name });

    let outcome;
    let payload = null;
    let error = null;
    try {
      // eslint-disable-next-line no-await-in-loop
      payload = await uploadOne(file);
      outcome = classifyUploadResult(payload, null);
    } catch (err) {
      error = err;
      outcome = classifyUploadResult(null, err);
    }

    results.push({ index: i, filename: file.name, outcome, payload, error });
    onEvent({ type: "file_result", index: i, filename: file.name, outcome, payload, error });

    if (REFRESH_OUTCOMES.has(outcome)) {
      // eslint-disable-next-line no-await-in-loop
      const state = await fetchState();
      onEvent({ type: "state_refresh", state });
    }

    if (!CONTINUE_OUTCOMES.has(outcome)) {
      onEvent({ type: "sequence_stopped", index: i, outcome });
      return { results, stoppedAt: i, stoppedOutcome: outcome };
    }
  }
  onEvent({ type: "sequence_complete" });
  return { results, stoppedAt: null, stoppedOutcome: null };
}
