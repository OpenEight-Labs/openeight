import test from "node:test";
import assert from "node:assert/strict";
import { isSemVer } from "../src/semver.ts";
test("SemVer preserves exact grammar and uint64 bounds", () => {
  for (const value of [
    "1.0.0",
    "1.2.3-alpha.1+build.01",
    "18446744073709551615.0.0",
  ])
    assert.equal(isSemVer(value), true, value);
  for (const value of [
    "v1.0.0",
    "01.0.0",
    "1.0.0-01",
    "1.2",
    "1.0.0\n",
    "18446744073709551616.0.0",
  ])
    assert.equal(isSemVer(value), false, value);
});
