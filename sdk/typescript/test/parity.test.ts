import assert from "node:assert/strict";
import test from "node:test";
import {
  SDKError,
  ConfigurationError,
  TransportError,
  ConnectError,
  ProtocolError,
  ObserveDeliveryError,
  ObserveQueueFullError,
  ObserveRunCompletedError,
  prepareDecision,
  prepareRunIdentity,
  exportRunCheckpoint,
  importRunCheckpoint,
  MachineClient,
  GovernedRun,
  BufferedObserveWriter,
  uuid7,
} from "../src/index.ts";

const runId = uuid7(),
  toolId = uuid7(),
  workflowId = uuid7();
const sourceTime = "2026-09-01T12:00:00.123456789Z";
const decision = () => ({
  runId,
  toolId,
  toolVersionId: toolId,
  toolName: "example",
  connector: "test",
  action: "invoke",
  input: {},
  policyContractSetDigest: "a".repeat(64),
  selectedValues: [],
});

test("all SDK leaf errors share the exported base", () => {
  for (const e of [
    new ConfigurationError("invalid_run"),
    new TransportError("timeout"),
    new ConnectError(403, "permission_denied"),
    new ProtocolError(),
    new ObserveDeliveryError("rejected"),
    new ObserveQueueFullError("bytes"),
    new ObserveRunCompletedError("run_completed"),
  ]) {
    assert.ok(e instanceof SDKError);
    assert.ok(e instanceof Error);
    assert.notEqual(e.name, "SDKError");
  }
  assert.equal(new SDKError().message, "SDK operation failed");
});

test("workflow and timestamp bounds apply to prepared Decisions and Runs", () => {
  const p = prepareDecision({
    ...decision(),
    sourceTime,
    workflowId,
    workflowVersion: "1.0.0",
  });
  assert.equal(p.workflowId, workflowId);
  assert.equal(p.sourceTime, "2026-09-01T12:00:00.123456Z");
  for (const workflowId of [
    "slug",
    "00000000-0000-0000-0000-000000000000",
    "019a7000-7123-4abc-8def-0123456789ab",
  ])
    assert.throws(
      () => prepareRunIdentity({ workflowId, workflowVersion: "1.0.0" }),
      ConfigurationError,
    );
  assert.throws(
    () =>
      prepareDecision({ ...decision(), sourceTime: "0001-01-01T00:00:00Z" }),
    ConfigurationError,
  );
  assert.equal(
    prepareDecision({
      ...decision(),
      sourceTime: "0001-01-01T00:00:00.000001Z",
    }).sourceTime,
    "0001-01-01T00:00:00.000001Z",
  );
  assert.equal(
    prepareDecision({
      ...decision(),
      sourceTime: "9999-12-31T23:59:59.999999Z",
    }).sourceTime,
    "9999-12-31T23:59:59.999999Z",
  );
});

test("checkpoint preserves original Python numeric spelling and key order", () => {
  const raw = {
    version: 1,
    state: "suspended",
    run_id: runId,
    started_at: sourceTime,
    attributes: { a: 1, z: 2 },
    attributes_json: '{"z":2,"a":1.0}',
    workflow_id: workflowId,
    workflow_version: "1.0.0",
  };
  const cp = importRunCheckpoint(raw);
  raw.attributes.a = 4;
  assert.equal(exportRunCheckpoint(cp).attributes_json, '{"z":2,"a":1.0}');
  assert.equal(cp.attributes.a, 1);
  const legacy = exportRunCheckpoint(cp);
  delete (legacy as any).attributes_json;
  assert.equal(
    Object.hasOwn(
      exportRunCheckpoint(importRunCheckpoint(legacy)),
      "attributes_json",
    ),
    false,
  );
  for (const text of [
    '{"a":1,"a":1}',
    '{"a":NaN}',
    '{"a":2}',
    "[]",
    '{"a":9007199254740993}',
  ])
    assert.throws(
      () => importRunCheckpoint({ ...raw, attributes_json: text }),
      ConfigurationError,
    );
});

test("workflow and exact attributes survive suspend cycles, queueing and completion retry", async () => {
  const writes: any[] = [];
  let fail = true;
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch: async (_, init) => {
      const b = JSON.parse(init!.body as string);
      writes.push(b);
      if (b.runs?.[0]?.status === "completed" && fail) {
        fail = false;
        return new Response(JSON.stringify({ code: "unavailable" }), {
          status: 503,
        });
      }
      return new Response(
        JSON.stringify({
          runs: (b.runs ?? []).map((r: any) => ({ id: r.id })),
          events: (b.events ?? []).map((e: any) => ({ id: e.id })),
        }),
        { status: 200 },
      );
    },
  });
  let run = await client.resumeGovernedRun(
    importRunCheckpoint({
      version: 1,
      state: "suspended",
      run_id: runId,
      started_at: sourceTime,
      attributes: { n: 1 },
      attributes_json: '{"n":1.0}',
      workflow_id: workflowId,
      workflow_version: "1.0.0",
    }),
  );
  for (let i = 0; i < 2; i++) {
    const cp = await run.suspend();
    run = await client.resumeGovernedRun(
      importRunCheckpoint(exportRunCheckpoint(cp)),
    );
  }
  assert.throws(
    () =>
      run.submitEvent({
        name: "bad",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: sourceTime,
        outcome: "success",
        error: { message: "invalid" },
      }),
    ConfigurationError,
  );
  run.submitEvent({
    name: "valid",
    observation_kind: "span",
    operation_kind: "invoke",
    occurred_at: sourceTime,
    outcome: "success",
  });
  await assert.rejects(run.close());
  await run.close();
  await client.close();
  for (const b of writes)
    for (const r of b.runs) {
      assert.equal(r.attributes_json, '{"n":1.0}');
      assert.equal(r.workflow_id, workflowId);
    }
  const completed = writes.filter((b) => b.runs[0].status === "completed");
  assert.deepEqual(completed[0], completed[1]);
  assert.equal(completed.length, 2);
});

test("raw Event validation retains extension fields and edge correlation IDs", async () => {
  const edge = "6ba7b810-9dad-51d1-80b4-00c04fd430c8";
  const sent: any[] = [];
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch: async (_, init) => {
      const b = JSON.parse(init!.body as string);
      sent.push(b);
      return new Response(
        JSON.stringify({
          runs: [],
          events: b.events.map((e: any) => ({ id: e.id })),
        }),
        { status: 200 },
      );
    },
  });
  const e = {
    id: uuid7(),
    run_id: runId,
    name: "test",
    observation_kind: "llm_call" as const,
    operation_kind: "invoke" as const,
    outcome: "success" as const,
    occurred_at: sourceTime,
    input_tokens: "9223372036854775807",
    parent_event_id: edge,
    extension: { safe: 1 },
  };
  await client.writeTelemetry({ events: [e] });
  for (const patch of [
    { outcome: "invalid" },
    { outcome: "error" },
    { error_json: "{}" },
    { cache_read_input_tokens: "9223372036854775808" },
    { ttft_ms: "10", latency_ms: "9" },
    { tool_id: toolId },
    { attributes_json: "[]" },
  ])
    await assert.rejects(
      client.writeTelemetry({ events: [{ ...e, ...patch } as any] }),
      ConfigurationError,
    );
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].events[0].extension, { safe: 1 });
  await client.close();
});

test("prepared Decision preserves workflow and refuses changed-actor replay", async () => {
  const p = prepareDecision({
    ...decision(),
    sourceTime,
    workflowId,
    workflowVersion: "1.0.0",
  });
  const requests: string[] = [];
  const fetch = async (_: any, init: any) => {
    requests.push(init.body);
    const b = JSON.parse(init.body);
    if (
      new Headers(init.headers).get("Authorization") ===
      "ApiKey ctk_other_actor"
    )
      return new Response(JSON.stringify({ code: "already_exists" }), {
        status: 409,
      });
    return new Response(
      JSON.stringify({
        decision_id: b.decision_id,
        run_id: b.run_id,
        verdict: "approval_required",
        reason: "approval",
        replayed: true,
      }),
      { status: 200 },
    );
  };
  const a = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch,
  });
  const b = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_other_actor",
    agentName: "example",
    fetch,
  });
  const result = await a.decide(p);
  assert.equal(result.verdict, "approval_required");
  assert.equal(result.replayed, true);
  await a.decide(p);
  await assert.rejects(b.decide(p), ConnectError);
  assert.equal(requests[0], requests[1]);
  assert.equal(JSON.parse(requests[0]!).workflow_id, workflowId);
  await a.close();
  await b.close();
});

test("retained encoded attributes count against the Run memory limit", async () => {
  let calls = 0;
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    observe: { maxBytes: 1024, flushBytes: 512 },
    fetch: async () => {
      calls++;
      return new Response("{}");
    },
  });
  const cp = importRunCheckpoint({
    version: 1,
    state: "suspended",
    run_id: uuid7(),
    started_at: sourceTime,
    attributes: {},
    attributes_json: " ".repeat(8192) + "{}",
  });
  await assert.rejects(client.resumeGovernedRun(cp), ObserveQueueFullError);
  assert.equal(calls, 0);
  await client.close();
});

test("owned and in-flight startup deduplicate only matching immutable facts", async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  let calls = 0;
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch: async (_, init) => {
      calls++;
      const b = JSON.parse(init!.body as string);
      if (calls === 1) await gate;
      return new Response(
        JSON.stringify({
          runs: b.runs.map((r: any) => ({ id: r.id })),
          events: [],
        }),
      );
    },
  });
  const identity = prepareRunIdentity({
    startedAt: sourceTime,
    attributes: { x: 1 },
  });
  const first = client.governedRun(identity);
  const same = client.governedRun({ ...identity });
  for (const changed of [
    { attributes: { x: 2 }, attributesJson: '{"x":2}' },
    { workflowId, workflowVersion: "1.0.0" },
    { startedAt: "2026-09-02T00:00:00Z" },
    { attributesJson: '{ "x":1 }' },
  ])
    await assert.rejects(
      client.governedRun({ ...identity, ...changed }),
      ConfigurationError,
    );
  release();
  const run = await first;
  assert.equal(await same, run);
  assert.equal(calls, 1);
  assert.equal(await client.governedRun(identity), run);
  await assert.rejects(
    client.governedRun({ ...identity, workflowId, workflowVersion: "1.0.0" }),
    ConfigurationError,
  );
  await run.close();
  await client.close();
  assert.equal((client as any).runFacts.size, 0);
});

test("acknowledged completion is not resent after a local cleanup error", async () => {
  const identity = prepareRunIdentity({ startedAt: sourceTime });
  const writer = new BufferedObserveWriter(async () => {});
  writer.admitRun(identity);
  let completions = 0;
  const failure = new ObserveDeliveryError("unavailable");
  const run = new GovernedRun(
    identity,
    writer,
    async () => {
      completions++;
    },
    () => {},
    () => {
      throw failure;
    },
  );
  await assert.rejects(run.close(), (e) => e === failure);
  await assert.rejects(run.close(), (e) => e === failure);
  assert.equal(completions, 1);
  assert.throws(
    () =>
      run.submitEvent({
        name: "late",
        observation_kind: "span",
        operation_kind: "invoke",
        outcome: "success",
        occurred_at: sourceTime,
      }),
    ObserveRunCompletedError,
  );
  await writer.close();
});

test("completion clock skew retains an end timestamp at or after startup", async () => {
  const writes: any[] = [];
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch: async (_, init) => {
      const b = JSON.parse(init!.body as string);
      writes.push(b);
      return new Response(
        JSON.stringify({
          runs: b.runs.map((r: any) => ({ id: r.id })),
          events: [],
        }),
      );
    },
  });
  const identity = prepareRunIdentity({
    startedAt: new Date(Date.now() + 3600000).toISOString(),
  });
  const run = await client.governedRun(identity);
  await run.close();
  await client.close();
  assert.equal(writes[1].runs[0].ended_at, identity.startedAt);
});
