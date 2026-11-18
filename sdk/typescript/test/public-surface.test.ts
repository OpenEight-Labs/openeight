import assert from "node:assert/strict";
import test from "node:test";
import * as sdk from "../src/index.ts";

test("public package exposes machine operations without an administration client", () => {
  assert.equal(typeof sdk.MachineClient, "function");
  assert.equal(Object.hasOwn(sdk, "HumanClient"), false);
  assert.equal(Object.hasOwn(sdk, "ApiError"), false);
});
