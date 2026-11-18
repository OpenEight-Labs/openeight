import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { connectError } from "../src/errors.ts";
import { governanceReason } from "../src/connect-errors.ts";

const cases: {
  reason: string;
  domain: string;
  connect_code: string;
  http_status: number;
}[] = JSON.parse(
  readFileSync(new URL("../../contracts/errors.json", import.meta.url), "utf8"),
).errors;
function errorInfo(reason: string, domain: string) {
  // Independent test encoding of ErrorInfo's length-delimited strings.
  const a = Buffer.from(reason),
    b = Buffer.from(domain);
  return [
    {
      type: "google.rpc.ErrorInfo",
      value: Buffer.concat([
        Buffer.from([10, a.length]),
        a,
        Buffer.from([18, b.length]),
        b,
      ])
        .toString("base64")
        .replace(/=+$/, ""),
    },
  ];
}
for (const entry of cases)
  test(`public ErrorInfo ${entry.reason}`, () => {
    const error = connectError(entry.http_status, {
      code: entry.connect_code,
      message: "private server detail must not escape",
      details: errorInfo(entry.reason, entry.domain),
    });
    assert.equal(error.code, entry.connect_code);
    assert.equal(error.reason, entry.reason);
    assert.equal(error.message, "request failed");
  });
test("known wire bytes decode independently of catalog generation", () => {
  const details = [
    {
      type: "google.rpc.ErrorInfo",
      value: "ChFUT09MX05PVF9HT1ZFUk5FRBIQb3BlbmVpZ2h0LmdvdmVybg",
    },
  ];
  assert.equal(
    governanceReason(400, "failed_precondition", details),
    "TOOL_NOT_GOVERNED",
  );
});
test("malformed or mismatched details retain only the protocol code", () => {
  const details = errorInfo("TOOL_NOT_GOVERNED", "openeight.govern"),
    encoded = details[0]!.value;
  const raw = Buffer.from(encoded, "base64");
  const invalid: unknown[] = [
    undefined,
    {},
    [],
    [...details, ...details],
    Array(9).fill(details[0]),
    [{ type: "other", value: encoded }],
    [{ type: "google.rpc.ErrorInfo", value: "?" }],
    [{ type: "google.rpc.ErrorInfo", value: "a".repeat(1025) }],
    errorInfo("UNKNOWN_REASON", "openeight.govern"),
    errorInfo("TOOL_NOT_GOVERNED", "openeight.access"),
  ];
  for (const bytes of [
    Buffer.concat([
      Buffer.from([10, raw[1]! + 3, 239, 187, 191]),
      raw.subarray(2),
    ]),
    raw.subarray(0, raw.length - 1),
    Buffer.concat([raw, raw]),
    Buffer.from([10, 128]),
    Buffer.from([10, 1, 255]),
    Buffer.from(raw.toString().replace("openeight.govern", "untrusted.domain")),
  ])
    invalid.push([
      { type: "google.rpc.ErrorInfo", value: bytes.toString("base64") },
    ]);
  for (const value of invalid)
    assert.equal(
      governanceReason(400, "failed_precondition", value),
      undefined,
    );
  assert.equal(
    governanceReason(409, "failed_precondition", details),
    undefined,
  );
  assert.equal(governanceReason(400, "invalid_argument", details), undefined);
  assert.equal(
    governanceReason(400, "failed_precondition", [
      { type: "google.rpc.ErrorInfo", value: raw.toString("base64") },
    ]),
    "TOOL_NOT_GOVERNED",
  );
});
test("ErrorInfo metadata is ignored", () => {
  const details = errorInfo("TOOL_NOT_GOVERNED", "openeight.govern");
  const entry = Buffer.from([
    10, 3, 115, 113, 108, 18, 7, 112, 114, 105, 118, 97, 116, 101,
  ]);
  details[0]!.value = Buffer.concat([
    Buffer.from(details[0]!.value, "base64"),
    Buffer.from([26, entry.length]),
    entry,
  ]).toString("base64");
  assert.equal(
    governanceReason(400, "failed_precondition", details),
    "TOOL_NOT_GOVERNED",
  );
});
