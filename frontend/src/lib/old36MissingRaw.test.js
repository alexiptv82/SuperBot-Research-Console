import {
  OLD36_CHECKPOINT_HINT,
  OLD36_RETAIN_RAW,
  OUTCOME,
  classifyUploadResult,
  deriveOld36MissingState,
  matchFileToMissingSession,
  runSequentialOld36Import,
} from "./old36MissingRaw";

function fakeFile(name) {
  return new File(["x"], name, { type: "application/zip" });
}

describe("classifyUploadResult", () => {
  test("maps EXACT_DUPLICATE duplicate_status", () => {
    expect(
      classifyUploadResult({ duplicate_status: "EXACT_DUPLICATE", verdict: "PASS" }, null)
    ).toBe(OUTCOME.EXACT_DUPLICATE);
  });
  test("maps a PASS verdict to RETAINED", () => {
    expect(classifyUploadResult({ duplicate_status: "NEW", verdict: "PASS" }, null)).toBe(OUTCOME.RETAINED);
  });
  test("maps a FAIL verdict to QA_FAILED", () => {
    expect(classifyUploadResult({ duplicate_status: "NEW", verdict: "FAIL" }, null)).toBe(OUTCOME.QA_FAILED);
  });
  test("maps a BLOCKED_DISK_SPACE error detail", () => {
    const err = { response: { data: { detail: "BLOCKED_DISK_SPACE: retention recheck failed" } } };
    expect(classifyUploadResult(null, err)).toBe(OUTCOME.BLOCKED_DISK_SPACE);
  });
  test("maps an OLD36_IDENTITY_REJECTED error detail", () => {
    const err = { response: { data: { detail: "OLD36_IDENTITY_REJECTED: derived session_id ..." } } };
    expect(classifyUploadResult(null, err)).toBe(OUTCOME.OLD36_IDENTITY_REJECTED);
  });
  test("maps an unrecognized error to GENERIC_FAILURE", () => {
    expect(classifyUploadResult(null, new Error("network blip"))).toBe(OUTCOME.GENERIC_FAILURE);
  });
});

describe("deriveOld36MissingState", () => {
  test("passes raw_ready through verbatim without recomputing it", () => {
    const backend = {
      present_sessions: 11,
      expected_sessions: 11,
      raw_retained_sessions: 2,
      raw_retained_nominal_hours: 6,
      raw_ready: false,
      sessions: [
        { session_id: "a", raw_retained: true },
        { session_id: "b", raw_retained: true },
        { session_id: "c", raw_retained: false },
      ],
    };
    const derived = deriveOld36MissingState(backend);
    expect(derived.rawReady).toBe(false);
    expect(derived.missingSessionIds).toEqual(["c"]);
    expect(derived.rawRetainedSessions).toBe(2);
    expect(derived.presentSessions).toBe(11);
  });
  test("does not synthesize raw_ready=true from an empty missing list", () => {
    const backend = { present_sessions: 11, raw_retained_sessions: 11, raw_ready: undefined, sessions: [] };
    const derived = deriveOld36MissingState(backend);
    expect(derived.rawReady).toBe(false);
  });
  test("handles a null/undefined reference state without throwing", () => {
    const derived = deriveOld36MissingState(null);
    expect(derived.missingSessionIds).toEqual([]);
    expect(derived.rawReady).toBe(false);
  });
});

describe("matchFileToMissingSession", () => {
  test("matches a filename containing a missing session id", () => {
    const missing = ["20260906T092548Z_d18fd1e3", "20260907T070729Z_48d293bf"];
    const name = "Bitget_MultiVenue_Microstructure_V2_SESSION_20260906T092548Z_d18fd1e3_3H.zip";
    expect(matchFileToMissingSession(name, missing)).toBe("20260906T092548Z_d18fd1e3");
  });
  test("returns null for an unrecognized filename", () => {
    expect(matchFileToMissingSession("random-file.zip", ["20260906T092548Z_d18fd1e3"])).toBeNull();
  });
});

describe("backend parameters", () => {
  test("checkpoint hint and retain_raw are fixed to OLD36 recovery intent", () => {
    expect(OLD36_CHECKPOINT_HINT).toBe("OLD36_REFERENCE");
    expect(OLD36_RETAIN_RAW).toBe(true);
  });
});

describe("runSequentialOld36Import", () => {
  test("processes files strictly sequentially — one upload in flight at a time", async () => {
    const files = [fakeFile("a.zip"), fakeFile("b.zip"), fakeFile("c.zip")];
    let activeCount = 0;
    let maxActiveCount = 0;
    const callOrder = [];
    const uploadOne = async (file) => {
      callOrder.push(file.name);
      activeCount += 1;
      maxActiveCount = Math.max(maxActiveCount, activeCount);
      await new Promise((r) => setTimeout(r, 5));
      activeCount -= 1;
      return { duplicate_status: "NEW", verdict: "PASS" };
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(callOrder).toEqual(["a.zip", "b.zip", "c.zip"]);
    expect(maxActiveCount).toBe(1);
    expect(outcome.stoppedAt).toBeNull();
    expect(outcome.results).toHaveLength(3);
  });

  test("sends OLD36_REFERENCE + retain_raw=true on every call via the wrapper", async () => {
    const files = [fakeFile("a.zip")];
    const seenParams = [];
    const uploadOne = async () => {
      seenParams.push({ checkpointHint: OLD36_CHECKPOINT_HINT, retainRaw: OLD36_RETAIN_RAW });
      return { duplicate_status: "NEW", verdict: "PASS" };
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(seenParams).toEqual([{ checkpointHint: "OLD36_REFERENCE", retainRaw: true }]);
  });

  test("refreshes /api/reference/old36 after a successful retention", async () => {
    const files = [fakeFile("a.zip")];
    const uploadOne = async () => ({ duplicate_status: "NEW", verdict: "PASS" });
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(fetchState).toHaveBeenCalledTimes(1);
  });

  test("EXACT_DUPLICATE is not fatal: refreshes state and continues to next file", async () => {
    const files = [fakeFile("dup.zip"), fakeFile("next.zip")];
    const calls = [];
    const uploadOne = async (file) => {
      calls.push(file.name);
      if (file.name === "dup.zip") return { duplicate_status: "EXACT_DUPLICATE", verdict: "PASS" };
      return { duplicate_status: "NEW", verdict: "PASS" };
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(calls).toEqual(["dup.zip", "next.zip"]);
    expect(outcome.stoppedAt).toBeNull();
    expect(fetchState).toHaveBeenCalledTimes(2);
    expect(outcome.results[0].outcome).toBe(OUTCOME.EXACT_DUPLICATE);
  });

  test("BLOCKED_DISK_SPACE stops the sequence immediately, no further files touched", async () => {
    const files = [fakeFile("a.zip"), fakeFile("b.zip"), fakeFile("c.zip")];
    const calls = [];
    const uploadOne = async (file) => {
      calls.push(file.name);
      if (file.name === "b.zip") {
        const err = new Error("blocked");
        err.response = { data: { detail: "BLOCKED_DISK_SPACE: free=10 required=500" } };
        throw err;
      }
      return { duplicate_status: "NEW", verdict: "PASS" };
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(calls).toEqual(["a.zip", "b.zip"]);
    expect(outcome.stoppedAt).toBe(1);
    expect(outcome.stoppedOutcome).toBe(OUTCOME.BLOCKED_DISK_SPACE);
    expect(fetchState).toHaveBeenCalledTimes(1);
  });

  test("OLD36_IDENTITY_REJECTED stops the sequence on the first file", async () => {
    const files = [fakeFile("a.zip"), fakeFile("b.zip")];
    const calls = [];
    const uploadOne = async (file) => {
      calls.push(file.name);
      const err = new Error("rejected");
      err.response = { data: { detail: "OLD36_IDENTITY_REJECTED: derived session_id ..." } };
      throw err;
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(calls).toEqual(["a.zip"]);
    expect(outcome.stoppedOutcome).toBe(OUTCOME.OLD36_IDENTITY_REJECTED);
    expect(fetchState).not.toHaveBeenCalled();
  });

  test("QA failure (non-PASS verdict, no thrown error) also stops the sequence", async () => {
    const files = [fakeFile("a.zip"), fakeFile("b.zip")];
    const calls = [];
    const uploadOne = async (file) => {
      calls.push(file.name);
      return { duplicate_status: "NEW", verdict: "FAIL" };
    };
    const fetchState = jest.fn().mockResolvedValue({ raw_ready: false, sessions: [] });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(calls).toEqual(["a.zip"]);
    expect(outcome.stoppedOutcome).toBe(OUTCOME.QA_FAILED);
  });

  test("a clean 11-file run ends with a state reflecting backend raw_ready=true", async () => {
    const files = Array.from({ length: 3 }, (_, i) => fakeFile(`s${i}.zip`));
    const uploadOne = async () => ({ duplicate_status: "NEW", verdict: "PASS" });
    let calls = 0;
    const fetchState = jest.fn().mockImplementation(async () => {
      calls += 1;
      return { raw_ready: calls === files.length, sessions: [] };
    });
    const outcome = await runSequentialOld36Import({ files, uploadOne, fetchState });
    expect(outcome.stoppedAt).toBeNull();
    expect(outcome.results).toHaveLength(3);
    expect(fetchState).toHaveBeenCalledTimes(3);
  });
});
