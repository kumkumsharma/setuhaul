import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { shouldApplyInFlightChatResult } from "./chatRequestGuard.js";

describe("shouldApplyInFlightChatResult", () => {
  it("discards old in-flight response after driver switch", () => {
    const requestCtx = { epoch: 1, driverId: "DRV-027" };
    // User switched to Karan while the slow LLM response was still pending.
    const currentCtx = { epoch: 2, driverId: "DRV-MULTI" };
    assert.equal(shouldApplyInFlightChatResult(requestCtx, currentCtx), false);
  });

  it("applies response when driver/case context is unchanged", () => {
    const requestCtx = { epoch: 3, driverId: "DRV-MULTI" };
    const currentCtx = { epoch: 3, driverId: "DRV-MULTI" };
    assert.equal(shouldApplyInFlightChatResult(requestCtx, currentCtx), true);
  });

  it("discards when epoch bumped even if driver id matches", () => {
    // clearCaseUiState / new case without changing driver
    const requestCtx = { epoch: 4, driverId: "DRV-027" };
    const currentCtx = { epoch: 5, driverId: "DRV-027" };
    assert.equal(shouldApplyInFlightChatResult(requestCtx, currentCtx), false);
  });

  it("discards when driver id diverges even if epoch matches", () => {
    const requestCtx = { epoch: 1, driverId: "DRV-027" };
    const currentCtx = { epoch: 1, driverId: "DRV-MULTI" };
    assert.equal(shouldApplyInFlightChatResult(requestCtx, currentCtx), false);
  });
});
