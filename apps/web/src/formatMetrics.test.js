import test from "node:test";
import assert from "node:assert/strict";
import { formatRateAsPercent } from "./formatMetrics.js";

test("formatRateAsPercent shows 0.3 as 30%", () => {
  assert.equal(formatRateAsPercent(0.3), "30%");
});

test("formatRateAsPercent shows 0.7 as 70%", () => {
  assert.equal(formatRateAsPercent(0.7), "70%");
});

test("formatRateAsPercent shows 1 as 100%", () => {
  assert.equal(formatRateAsPercent(1), "100%");
});

test("formatRateAsPercent shows 0 as 0%", () => {
  assert.equal(formatRateAsPercent(0), "0%");
});

test("formatRateAsPercent handles nullish", () => {
  assert.equal(formatRateAsPercent(null), "—");
  assert.equal(formatRateAsPercent(undefined), "—");
});
