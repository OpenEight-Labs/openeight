import assert from "node:assert/strict";
import test from "node:test";
import {
  ConfigurationError,
  MachineClient,
  prepareRunIdentity,
  uuid7,
} from "../src/index.ts";

const runId = uuid7(),
  id = uuid7(),
  toolId = uuid7(),
  edge = "6ba7b810-9dad-51d1-80b4-00c04fd430c8",
  time = "2026-09-01T00:00:00.000000Z";
const registered = {
  tool_id: toolId,
  tool_version_id: toolId,
  tool_name: "example",
};
const base = {
  id,
  run_id: runId,
  name: "example",
  observation_kind: "span",
  operation_kind: "invoke",
  outcome: "success",
  occurred_at: time,
};
function client(writes: any[]) {
  return new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_admission_canary",
    agentName: "example",
    fetch: async (_, init) => {
      const b = JSON.parse(String(init?.body));
      writes.push(b);
      return new Response(
        JSON.stringify({
          runs: (b.runs ?? []).map((r: any) => ({ id: r.id })),
          events: (b.events ?? []).map((e: any) => ({ id: e.id })),
        }),
      );
    },
  });
}
const invalid = [
  { error_json: "null" },
  { outcome: "error" },
  { outcome: "error", error_json: "null", output_json: "null" },
  { outcome: "error", error_json: "{}", output_json: "{}" },
  { outcome: "interrupted", output_json: " null " },
  { outcome: "interrupted", error_json: "null" },
  ...["input_json", "output_json", "error_json"].map((field) => ({
    observation_kind: "lifecycle",
    operation_kind: "unspecified",
    outcome: "interrupted",
    [field]: "null",
  })),
  ...["span", "llm_call"].flatMap((observation_kind) =>
    [{ tool_name: "example" }, registered, { decision_id: toolId }].map(
      (identity) => ({ observation_kind, ...identity }),
    ),
  ),
  { observation_kind: "tool_call" },
  { observation_kind: "tool_call", tool_id: toolId, tool_name: "example" },
  {
    observation_kind: "tool_call",
    tool_name: "example",
    decision_id: edge,
    input_json: "{}",
  },
  { observation_kind: "tool_call", ...registered, decision_id: edge },
  {
    observation_kind: "tool_call",
    ...registered,
    decision_id: edge,
    input_json: "null",
  },
  {
    observation_kind: "tool_call",
    ...registered,
    decision_id: edge,
    input_json: "[]",
  },
  {
    observation_kind: "tool_call",
    ...registered,
    decision_id: edge,
    input_json: "{}",
    outcome: "unspecified",
  },
  {
    observation_kind: "lifecycle",
    operation_kind: "unspecified",
    outcome: "interrupted",
    tool_name: "example",
  },
];
for (const [index, patch] of invalid.entries())
  test(`invalid Event combination ${index} is rejected before dispatch`, async () => {
    const writes: any[] = [];
    const c = client(writes);
    try {
      await assert.rejects(
        c.writeTelemetry({ events: [{ ...base, ...patch } as any] }),
        ConfigurationError,
      );
      assert.equal(writes.length, 0);
    } finally {
      await c.close();
    }
  });
const valid = [
  { input_json: "null", output_json: "null" },
  { outcome: "error", error_json: "null" },
  { outcome: "interrupted" },
  {
    observation_kind: "lifecycle",
    operation_kind: "unspecified",
    outcome: "interrupted",
  },
  { observation_kind: "tool_call", tool_name: "example" },
  { observation_kind: "tool_call", ...registered },
  {
    observation_kind: "tool_call",
    ...registered,
    decision_id: edge,
    parent_event_id: edge,
    input_json: "{}",
  },
  {
    observation_kind: "lifecycle",
    operation_kind: "unspecified",
    outcome: "interrupted",
    ...registered,
    decision_id: edge,
  },
  {
    observation_kind: "llm_call",
    parent_event_id: edge,
    input_tokens: "9223372036854775807",
    output_tokens: "0",
    extension: { safe: true },
  },
];
for (const [index, patch] of valid.entries())
  test(`valid Event combination ${index} preserves wire fields`, async () => {
    const writes: any[] = [];
    const c = client(writes);
    const row = { ...base, ...patch };
    try {
      await c.writeTelemetry({ events: [row as any] });
      assert.deepEqual(writes[0].events[0], row);
    } finally {
      await c.close();
    }
  });
test("rejected high-level Events cannot poison the queue or completion", async () => {
  const writes: any[] = [];
  const c = client(writes);
  try {
    const run = await c.governedRun(prepareRunIdentity({ startedAt: time }));
    for (const patch of [
      { error: null },
      { outcome: "error", error: {}, output: {} },
      { ...registered },
      { observation_kind: "tool_call" },
    ])
      assert.throws(
        () =>
          run.submitEvent({
            name: "invalid",
            observation_kind: "span",
            operation_kind: "invoke",
            occurred_at: time,
            outcome: "success",
            ...patch,
          } as any),
        ConfigurationError,
      );
    run.submitEvent({
      name: "valid",
      observation_kind: "span",
      operation_kind: "invoke",
      occurred_at: time,
      outcome: "success",
    });
    await run.close();
    const events = writes.flatMap((b) => b.events ?? []);
    assert.equal(events.length, 1);
    assert.equal(events[0].name, "valid");
    assert.equal(writes.at(-1).runs[0].status, "completed");
  } finally {
    await c.close();
  }
});

test("protocol aliases are validated before raw dispatch and queue admission", async () => {
  const writes: any[] = [];
  const c = client(writes);
  try {
    for (const patch of [
      { errorJson: "{}" },
      { toolId },
      { decisionId: toolId },
    ])
      await assert.rejects(
        c.writeTelemetry({ events: [{ ...base, ...patch } as any] }),
        ConfigurationError,
      );
    assert.equal(writes.length, 0);
    const run = await c.governedRun(prepareRunIdentity({ startedAt: time }));
    for (const patch of [
      { errorJson: "{}" },
      { toolId },
      { decisionId: toolId },
      { runId: run.runId },
    ])
      assert.throws(
        () =>
          run.submitEvent({
            name: "invalid",
            observation_kind: "span",
            operation_kind: "invoke",
            occurred_at: time,
            outcome: "success",
            ...patch,
          } as any),
        ConfigurationError,
      );
    run.submitEvent({
      name: "valid",
      observation_kind: "span",
      operation_kind: "invoke",
      occurred_at: time,
      outcome: "success",
    });
    await run.close();
    assert.equal(writes.flatMap((b) => b.events ?? []).length, 1);
  } finally {
    await c.close();
  }
});
test("valid protocol aliases normalize without losing fields and duplicates reject", async () => {
  const writes: any[] = [];
  const c = client(writes);
  try {
    await c.writeTelemetry({
      runs: [
        {
          id: runId,
          status: "active",
          startedAt: time,
          attributesJson: "{}",
        } as any,
      ],
      events: [
        {
          id,
          runId,
          name: "example",
          observationKind: "tool_call",
          operationKind: "invoke",
          outcome: "error",
          occurredAt: time,
          errorJson: "null",
          toolId,
          toolVersionId: toolId,
          toolName: "example",
          decisionId: edge,
          inputJson: "{}",
          extension: { safe: true },
        } as any,
      ],
    });
    assert.equal(writes[0].events[0].error_json, "null");
    assert.equal(writes[0].events[0].decision_id, edge);
    assert.deepEqual(writes[0].events[0].extension, { safe: true });
    assert.equal(writes[0].runs[0].started_at, time);
    await assert.rejects(
      c.writeTelemetry({
        events: [{ ...base, output_json: "null", outputJson: "null" } as any],
      }),
      ConfigurationError,
    );
  } finally {
    await c.close();
  }
});
