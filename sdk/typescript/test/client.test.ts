import assert from "node:assert/strict";
import test from "node:test";
import {
  ConfigurationError,
  ConnectError,
  MachineClient,
  ObserveDeliveryError,
  ObserveQueueFullError,
  ObserveRunCompletedError,
  ProtocolError,
  jsonSnapshot,
  ToolRegistration,
  TransportError,
  prepareRunIdentity,
  collectSelectedValues,
} from "../src/index.ts";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
const machine = (
  fetch: typeof globalThis.fetch,
  more: Record<string, unknown> = {},
) =>
  new MachineClient({
    baseUrl: "http://localhost:8080",
    apiKey: "ctk_0123456789abcdef",
    agentName: "orders-agent",
    fetch,
    ...more,
  });

test("machine operations use current identity headers and no caller authority selector", async () => {
  const seen: Request[] = [];
  const c = machine(async (input, init) => {
    seen.push(new Request(input, init));
    const path = new URL(String(input)).pathname;
    if (path.endsWith("RegisterTools")) return json({ results: [] });
    if (path.endsWith("GetToolReadiness"))
      return json({
        readiness: {
          tools: [],
          fully_registered: true,
          registration_reason: "",
          fully_policy_ready: true,
          policy_reason: "",
        },
      });
    if (path.endsWith("WriteTelemetry")) return json({ runs: [], events: [] });
    return json({ state: "none" });
  });
  await c.getToolReadiness([]);
  await c.writeTelemetry({});
  await c.approvalState("0198f6cb-0300-7000-8000-000000000001");
  for (const request of seen) {
    assert.equal(
      request.headers.get("authorization"),
      "ApiKey ctk_0123456789abcdef",
    );
    assert.equal(request.headers.get("x-openeight-agent-name"), "orders-agent");
    assert.equal(request.headers.has("x-workspace-id"), false);
  }
  assert.equal(
    new URL(seen[0].url).pathname,
    "/api/v1/openeight.observe.v1.ObserveService/GetToolReadiness",
  );
  assert.equal(
    new URL(seen[2].url).pathname,
    "/api/v1/openeight.govern.v1.GovernService/GetApprovalState",
  );
  assert.equal(seen[2].method, "POST");
  assert.deepEqual(await seen[2].json(), {
    decision_id: "0198f6cb-0300-7000-8000-000000000001",
  });
  await c.close();
});

test("tool registration reports immutable facts without authoring governance", async () => {
  let request: Request | undefined;
  const c = machine(async (input, init) => {
    request = new Request(input, init);
    return json({
      results: [
        { request_index: 0, status: "invalid", reason: "invalid_registration" },
      ],
    });
  });
  const schema = { type: "object", properties: { amount: { type: "number" } } };
  const registration = new ToolRegistration({
    name: "refund",
    description: "refund tool",
    inputSchema: schema,
  });
  schema.properties.amount.type = "string";
  assert.deepEqual(registration.inputSchema, {
    type: "object",
    properties: { amount: { type: "number" } },
  });
  const results = await c.registerTools([registration]);
  assert.equal(results[0]?.status, "invalid");
  const body = (await request!.json()) as { tools: unknown[] };
  assert.deepEqual(body.tools[0], {
    name: "refund",
    description: "refund tool",
    input_schema_json: jsonSnapshot(registration.inputSchema, true).text,
  });
  assert.throws(
    () =>
      new ToolRegistration({
        ...{ name: "x", description: "x", inputSchema: {}, governed: true },
      }),
    ConfigurationError,
  );
  await c.close();
});

test("prepared Decisions retain IDs, selected input and microseconds on an explicit retry", async () => {
  const calls: unknown[] = [];
  const c = machine(async (_input, init) => {
    assert.equal(
      new URL(String(_input)).pathname,
      "/api/v1/openeight.govern.v1.GovernService/Decide",
    );
    calls.push(JSON.parse(String(init?.body)));
    return json({
      decision_id: "0198f6cb-0300-7000-8000-000000000001",
      run_id: "0198f6cb-0300-7000-8000-000000000002",
      verdict: "allow",
      reason: "",
      replayed: calls.length > 1,
    });
  });
  const prepared = c.prepareDecision({
    id: "0198f6cb-0300-7000-8000-000000000001",
    runId: "0198f6cb-0300-7000-8000-000000000002",
    sourceTime: "2026-08-20T02:00:00.123456+02:00",
    toolId: "00000000-0000-4000-8000-000000000011",
    toolVersionId: "00000000-0000-4000-8000-000000000012",
    toolName: "refund",
    connector: "payments",
    action: "create",
    input: {},
    policyContractSetDigest: "a".repeat(64),
    selectedValues: [{ path: "amount", value: 100 }],
  });
  await c.decide(prepared);
  await c.decide(prepared);
  assert.equal((calls[0] as { input_json: string }).input_json, "{}");
  assert.deepEqual(
    JSON.parse(
      (calls[0] as { selected_values_json: string }).selected_values_json,
    ),
    [{ path: "amount", value: 100 }],
  );
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(
    (calls[0] as { source_time: string }).source_time,
    "2026-08-20T00:00:00.123456Z",
  );
  await c.close();
});

test("Decision response IDs must match the prepared Decision", async () => {
  const c = machine(async () =>
    json({
      decision_id: "0198f6cb-0300-7000-8000-000000000099",
      run_id: "0198f6cb-0300-7000-8000-000000000002",
      verdict: "allow",
      reason: "",
      replayed: false,
    }),
  );
  const prepared = c.prepareDecision({
    id: "0198f6cb-0300-7000-8000-000000000001",
    runId: "0198f6cb-0300-7000-8000-000000000002",
    toolId: "00000000-0000-4000-8000-000000000011",
    toolVersionId: "00000000-0000-4000-8000-000000000012",
    toolName: "refund",
    connector: "payments",
    action: "create",
    input: {},
    policyContractSetDigest: "a".repeat(64),
    selectedValues: [],
  });
  await assert.rejects(c.decide(prepared), ProtocolError);
  await c.close();
});

test("JSON snapshots preserve an own __proto__ key without polluting prototypes", () => {
  const input = JSON.parse(
    '{"outer":{"__proto__":{"polluted":true}}}',
  ) as Record<string, unknown>;
  const selected = jsonSnapshot(input, true).value as Record<string, unknown>;
  assert.equal(Object.hasOwn(selected.outer as object, "__proto__"), true);
  assert.equal(({} as Record<string, unknown>).polluted, undefined);
});

test("malformed machine success and Connect failures have safe typed errors", async () => {
  const malformed = machine(async () => json({ results: [] }));
  await assert.rejects(
    malformed.registerTools([
      new ToolRegistration({
        name: "x",
        description: "valid",
        inputSchema: { type: "object" },
      }),
    ]),
    ProtocolError,
  );
  await malformed.close();
  const failed = machine(async () =>
    json(
      { code: "permission_denied", message: "secret body should not escape" },
      403,
    ),
  );
  await assert.rejects(
    failed.getToolReadiness([]),
    (e: unknown) =>
      e instanceof ConnectError &&
      e.code === "permission_denied" &&
      !e.message.includes("secret"),
  );
  await failed.close();
  const rest = machine(async () =>
    json({ code: "failed_precondition", message: "db detail" }, 400),
  );
  await assert.rejects(
    rest.approvalState("0198f6cb-0300-7000-8000-000000000001"),
    (e: unknown) =>
      e instanceof ConnectError &&
      e.code === "failed_precondition" &&
      e.reason === undefined &&
      e.message === "request failed",
  );
  await rest.close();
});

test("redirects are refused and transport exceptions never leak", async () => {
  const redirect = machine(
    async () =>
      new Response(null, {
        status: 302,
        headers: { location: "https://attacker.invalid/" },
      }),
  );
  await assert.rejects(
    redirect.getToolReadiness([]),
    (e: unknown) => e instanceof Error && !e.message.includes("attacker"),
  );
  await redirect.close();
  const thrown = machine(async () => {
    throw new Error("credential=ctk_SECRET provider response detail");
  });
  await assert.rejects(
    thrown.getToolReadiness([]),
    (e: unknown) =>
      e instanceof Error &&
      e.message === "request unavailable" &&
      !e.message.includes("ctk_SECRET"),
  );
  await thrown.close();
});

test("old credentials fail before network activity", async () => {
  let called = false;
  assert.throws(
    () =>
      new MachineClient({
        baseUrl: "http://localhost:8080",
        apiKey: "ctp_legacy",
        agentName: "agent",
        fetch: async () => {
          called = true;
          return json({});
        },
      }),
    ConfigurationError,
  );
  assert.equal(called, false);
});

test("telemetry distinguishes absent JSON from explicit null and validates acknowledgements", async () => {
  const requests: Request[] = [];
  const c = machine(async (input, init) => {
    const req = new Request(input, init);
    requests.push(req.clone());
    const body = (await req.json()) as {
      events: Array<{ id: string }>;
      runs: Array<{ id: string }>;
    };
    return json({
      runs: body.runs.map((x) => ({ id: x.id, replayed: false })),
      events: body.events.map((x) => ({ id: x.id, replayed: false })),
    });
  });
  await c.writeTelemetry({
    runs: [
      {
        id: "0198f6cb-0300-7000-8000-000000000002",
        status: "active",
        started_at: "2026-09-01T00:00:00Z",
        attributes_json: "{}",
      },
    ],
    events: [
      {
        id: "0198f6cb-0300-7000-8000-000000000003",
        run_id: "0198f6cb-0300-7000-8000-000000000002",
        name: "work",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: "2026-09-01T00:00:00Z",
        outcome: "success",
        input_json: "null",
      },
    ],
  });
  const body = (await requests[0]!.json()) as {
    events: Array<Record<string, unknown>>;
  };
  assert.equal(body.events[0]?.input_json, "null");
  assert.equal("output_json" in body.events[0]!, false);
  await c.close();
});

test("Run identity is explicit and active Run creation is acknowledged before return", async () => {
  const requests: unknown[] = [];
  const c = machine(async (_i, init) => {
    requests.push(JSON.parse(String(init?.body)));
    return json({
      runs: [{ id: "0198f6cb-0300-7000-8000-000000000002", replayed: false }],
      events: [],
    });
  });
  const identity = prepareRunIdentity({
    runId: "0198f6cb-0300-7000-8000-000000000002",
  });
  const run = await c.governedRun(identity);
  assert.equal(run.runId, identity.runId);
  assert.equal(requests.length, 1);
  await c.close();
});

test("Observe requests use protobuf JSON names and split batches by encoded request record limits", async () => {
  const sent: Array<{
    runs: Array<Record<string, unknown>>;
    events: Array<Record<string, unknown>>;
  }> = [];
  const c = machine(
    async (_input, init) => {
      const payload = JSON.parse(String(init?.body)) as {
        runs: Array<Record<string, unknown>>;
        events: Array<Record<string, unknown>>;
      };
      sent.push(payload);
      return json({
        runs: payload.runs.map((r) => ({ id: r.id, replayed: false })),
        events: payload.events.map((e) => ({ id: e.id, replayed: false })),
      });
    },
    {
      observe: {
        maxRecords: 8,
        maxBytes: 64 * 1024,
        flushRecords: 8,
        flushBytes: 64 * 1024,
        flushIntervalMs: 1000,
        maxRequestRecords: 2,
        maxRequestBytes: 64 * 1024,
      },
    },
  );
  const writer = c.bufferedObserve(),
    run = prepareRunIdentity({
      runId: "0198f6cb-0300-7000-8000-000000000002",
      startedAt: "2026-09-01T00:00:00.123Z",
    });
  for (const id of [
    "0198f6cb-0300-7000-8000-000000000003",
    "0198f6cb-0300-7000-8000-000000000004",
  ])
    writer.submit(run, {
      id,
      run_id: run.runId,
      name: "event",
      observation_kind: "span",
      operation_kind: "invoke",
      occurred_at: "2026-09-01T00:00:00Z",
      outcome: "success",
    });
  await writer.flush();
  assert.equal(sent.length, 2);
  assert.equal(
    sent[0]?.runs[0]?.started_at,
    run.startedAt.replace(/\.\d{3}Z$/, ".123000Z"),
  );
  assert.equal(sent[0]?.events[0]?.run_id, run.runId);
  assert.deepEqual(
    sent.flatMap((x) => x.events.map((e) => e.id)),
    [
      "0198f6cb-0300-7000-8000-000000000003",
      "0198f6cb-0300-7000-8000-000000000004",
    ],
  );
  await writer.close();
  await c.close();
});

test("Run lifecycle rejects competing transitions and replays exact completion after lost acknowledgement", async () => {
  let unblock!: (value: Response) => void, started!: () => void;
  const entered = new Promise<void>((resolve) => {
      started = resolve;
    }),
    gate = new Promise<Response>((resolve) => {
      unblock = resolve;
    });
  let holdEvents = false,
    loseCompletion = true;
  const completions: string[] = [];
  const c = machine(async (_input, init) => {
    const body = JSON.parse(String(init?.body)) as {
      runs: Array<{ id: string; status: string }>;
      events: Array<{ id: string }>;
    };
    if (body.events.length && holdEvents) {
      started();
      return gate;
    }
    if (body.runs.some((r) => r.status === "completed")) {
      completions.push(JSON.stringify(body));
      if (loseCompletion) {
        loseCompletion = false;
        throw new Error("ambiguous delivery");
      }
    }
    return json({
      runs: body.runs.map((r) => ({ id: r.id, replayed: false })),
      events: body.events.map((e) => ({ id: e.id, replayed: false })),
    });
  });
  const identity = prepareRunIdentity({
      runId: "0198f6cb-0300-7000-8000-000000000002",
    }),
    run = await c.governedRun(identity);
  const eventId = run.submitEvent({
    name: "write",
    tool_name: "write",
    observation_kind: "tool_call",
    operation_kind: "invoke",
    occurred_at: "2026-09-01T00:00:00Z",
    outcome: "success",
  });
  holdEvents = true;
  const suspended = run.suspend();
  await entered;
  await assert.rejects(run.close(), ObserveRunCompletedError);
  unblock(
    json({
      runs: [{ id: identity.runId, replayed: false }],
      events: [{ id: eventId, replayed: false }],
    }),
  );
  const checkpoint = await suspended;
  assert.throws(() => run.close(), ObserveRunCompletedError);
  const resumed = await c.resumeGovernedRun(checkpoint);
  const closeA = resumed.close(),
    closeB = resumed.close();
  await Promise.all([
    assert.rejects(closeA, ObserveDeliveryError),
    assert.rejects(closeB, ObserveDeliveryError),
  ]);
  assert.equal(completions.length, 1);
  await resumed.close();
  assert.equal(completions.length, 2);
  assert.equal(completions[0], completions[1]);
  await assert.rejects(c.resumeGovernedRun(checkpoint), ConfigurationError);
  await c.close();
});

test("failed telemetry acknowledgement retains and replays the identical request", async () => {
  const requests: string[] = [];
  let first = true;
  const c = machine(async (_input, init) => {
    const body = String(init?.body);
    requests.push(body);
    if (first) {
      first = false;
      throw new Error("ambiguous response");
    }
    const p = JSON.parse(body) as {
      runs: Array<{ id: string }>;
      events: Array<{ id: string }>;
    };
    return json({
      runs: p.runs.map((r) => ({ id: r.id, replayed: true })),
      events: p.events.map((e) => ({ id: e.id, replayed: true })),
    });
  });
  const writer = c.bufferedObserve(),
    run = prepareRunIdentity({
      runId: "0198f6cb-0300-7000-8000-000000000002",
      startedAt: "2026-09-01T00:00:00.123456Z",
    });
  writer.submit(run, {
    id: "0198f6cb-0300-7000-8000-000000000003",
    run_id: run.runId,
    name: "work",
    observation_kind: "span",
    operation_kind: "invoke",
    occurred_at: "2026-09-01T00:00:00.123456Z",
    outcome: "success",
  });
  await assert.rejects(writer.flush(), ObserveDeliveryError);
  await writer.flush();
  assert.equal(requests.length, 2);
  assert.equal(requests[0], requests[1]);
  await writer.close();
  await c.close();
});

test("settled Run initialization failure releases its reservation for exact identity retry", async () => {
  let calls = 0;
  const c = machine(async (_input, init) => {
    calls++;
    if (calls === 1) throw new Error("response lost");
    const body = JSON.parse(String(init?.body)) as {
      runs: Array<{ id: string }>;
    };
    return json({
      runs: body.runs.map((r) => ({ id: r.id, replayed: true })),
      events: [],
    });
  });
  const identity = prepareRunIdentity({
    runId: "0198f6cb-0300-7000-8000-000000000002",
  });
  await assert.rejects(c.governedRun(identity), TransportError);
  const run = await c.governedRun(identity);
  assert.equal(run.runId, identity.runId);
  assert.equal(calls, 2);
  await c.close();
});

test("timed-out unsettled Run initialization stays reserved until its transport settles", async () => {
  let finish!: (r: Response) => void;
  const gate = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  let calls = 0;
  const c = machine(
    async (_input, init) => {
      calls++;
      if (calls === 1) return gate;
      const body = JSON.parse(String(init?.body)) as {
        runs: Array<{ id: string }>;
      };
      return json({
        runs: body.runs.map((r) => ({ id: r.id, replayed: true })),
        events: [],
      });
    },
    { timeoutMs: 10, closeTimeoutMs: 1000 },
  );
  const identity = prepareRunIdentity({
    runId: "0198f6cb-0300-7000-8000-000000000002",
  });
  await assert.rejects(
    c.governedRun(identity),
    (e: unknown) => e instanceof TransportError && e.reason === "timeout",
  );
  await assert.rejects(
    c.governedRun(identity),
    (e: unknown) => e instanceof TransportError && e.reason === "timeout",
  );
  assert.equal(calls, 1);
  finish(json({ runs: [{ id: identity.runId, replayed: false }], events: [] }));
  for (let i = 0; i < 20 && calls === 1; i++)
    await new Promise((resolve) => setTimeout(resolve, 1));
  const run = await c.governedRun(identity);
  assert.equal(run.runId, identity.runId);
  assert.equal(calls, 2);
  await c.close();
});

test("Run admission limits count empty owned Runs and reject Events without eviction", async () => {
  const c = machine(
    async (_input, init) => {
      const body = JSON.parse(String(init?.body)) as {
        runs: Array<{ id: string }>;
      };
      return json({
        runs: body.runs.map((r) => ({ id: r.id, replayed: false })),
        events: [],
      });
    },
    {
      observe: {
        maxRecords: 1,
        maxBytes: 4096,
        flushRecords: 1,
        flushBytes: 1024,
        flushIntervalMs: 1000,
        maxRequestRecords: 2,
        maxRequestBytes: 4096,
      },
      maxOwnedRuns: 1,
    },
  );
  const run = await c.governedRun(
    prepareRunIdentity({ runId: "0198f6cb-0300-7000-8000-000000000002" }),
  );
  assert.throws(
    () =>
      run.submitEvent({
        name: "overflow",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: "2026-09-01T00:00:00Z",
        outcome: "success",
      }),
    ObserveQueueFullError,
  );
  await assert.rejects(
    c.governedRun(
      prepareRunIdentity({ runId: "0198f6cb-0300-7000-8000-000000000003" }),
    ),
    ConfigurationError,
  );
  await c.close();
});

test("client close refuses new machine work while it drains acknowledged Events before completion", async () => {
  let unblock!: (r: Response) => void,
    entered!: () => void,
    eventId = "";
  const held = new Promise<Response>((resolve) => {
      unblock = resolve;
    }),
    seen = new Promise<void>((resolve) => {
      entered = resolve;
    });
  let block = true;
  const calls: string[] = [];
  const c = machine(async (_input, init) => {
    const body = JSON.parse(String(init?.body)) as {
      runs: Array<{ id: string; status: string }>;
      events: Array<{ id: string }>;
    };
    calls.push(
      body.events.length ? "events" : (body.runs[0]?.status ?? "empty"),
    );
    if (body.events.length && block) {
      entered();
      return held;
    }
    return json({
      runs: body.runs.map((x) => ({ id: x.id, replayed: false })),
      events: body.events.map((x) => ({ id: x.id, replayed: false })),
    });
  });
  const writer = c.bufferedObserve();
  const run = await c.governedRun(
    prepareRunIdentity({ runId: "0198f6cb-0300-7000-8000-000000000002" }),
  );
  eventId = run.submitEvent({
    name: "work",
    observation_kind: "span",
    operation_kind: "invoke",
    occurred_at: "2026-09-01T00:00:00Z",
    outcome: "success",
  });
  const closing = c.close();
  await seen;
  await assert.rejects(c.getToolReadiness([]), ConfigurationError);
  await assert.rejects(c.registerTools([]), ConfigurationError);
  await assert.rejects(
    c.approvalState("0198f6cb-0300-7000-8000-000000000001"),
    ConfigurationError,
  );
  await assert.rejects(c.writeTelemetry({}), ConfigurationError);
  assert.throws(
    () =>
      run.submitEvent({
        name: "late",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: "2026-09-01T00:00:00Z",
        outcome: "success",
      }),
    ConfigurationError,
  );
  assert.throws(
    () =>
      writer.submit(prepareRunIdentity(), {
        id: "0198f6cb-0300-7000-8000-000000000004",
        run_id: "0198f6cb-0300-7000-8000-000000000005",
        name: "late",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: "2026-09-01T00:00:00Z",
        outcome: "success",
      }),
    ConfigurationError,
  );
  block = false;
  unblock(
    json({
      runs: [{ id: run.runId, replayed: false }],
      events: [{ id: eventId, replayed: false }],
    }),
  );
  await closing;
  assert.deepEqual(calls, ["active", "events", "completed"]);
});

test("writer close closes admission before yielding and never discards a late Event", async () => {
  let calls = 0;
  const c = machine(async () => {
    calls++;
    return json({ runs: [], events: [] });
  });
  const writer = c.bufferedObserve();
  const closing = writer.close();
  assert.throws(
    () =>
      writer.submit(prepareRunIdentity(), {
        id: "0198f6cb-0300-7000-8000-000000000003",
        run_id: "0198f6cb-0300-7000-8000-000000000002",
        name: "late",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: "2026-09-01T00:00:00Z",
        outcome: "success",
      }),
    ObserveRunCompletedError,
  );
  await closing;
  assert.equal(calls, 0);
  await c.close();
});

test("conditional registration keeps both UUID conditions on an identical retry", async () => {
  const toolId = "00000000-0000-4000-8000-000000000011";
  const versionId = "00000000-0000-4000-8000-000000000012";
  for (const conditions of [
    { expectedToolId: toolId },
    { expectedCurrentVersionId: versionId },
    { expectedToolId: "", expectedCurrentVersionId: "" },
    {
      expectedToolId: "00000000-0000-0000-0000-000000000000",
      expectedCurrentVersionId: versionId,
    },
  ]) {
    assert.throws(
      () =>
        new ToolRegistration({
          name: "refund",
          description: "",
          inputSchema: {},
          ...conditions,
        }),
      ConfigurationError,
    );
  }
  const requests: unknown[] = [];
  const client = machine(async (_, init) => {
    requests.push(JSON.parse(String(init?.body)));
    return json({
      results: [
        {
          request_index: 0,
          status: "conflict",
          reason: "current_version_changed",
        },
      ],
    });
  });
  const facts = new ToolRegistration({
    name: "refund",
    description: "",
    inputSchema: {},
    expectedToolId: toolId,
    expectedCurrentVersionId: versionId,
  });
  await client.registerTools([facts]);
  await client.registerTools([facts]);
  assert.deepEqual(requests, [
    {
      tools: [
        {
          name: "refund",
          description: "",
          input_schema_json: "{}",
          expected_tool_id: toolId,
          expected_current_version_id: versionId,
        },
      ],
    },
    {
      tools: [
        {
          name: "refund",
          description: "",
          input_schema_json: "{}",
          expected_tool_id: toolId,
          expected_current_version_id: versionId,
        },
      ],
    },
  ]);
  await client.close();
});

test("machine registration retains nullable saved intent and frozen drafting status", async () => {
  const id = "00000000-0000-4000-8000-000000000001",
    versionId = "00000000-0000-4000-8000-000000000002";
  for (const fields of [
    {},
    { intent: null },
    {
      intent: "  Require approval.\n",
      intent_drafting: { status: "complete" },
    },
  ]) {
    const c = machine(async () =>
      json({
        results: [
          {
            request_index: 0,
            status: "registered",
            reason: "",
            tool: {
              id,
              current_version_id: versionId,
              name: "write",
              description: "",
              capability: "write",
              governed: false,
            },
            tool_version: {
              id: versionId,
              tool_id: id,
              version: "1",
              content_digest: "sha256:" + "a".repeat(64),
              input_schema_json: "{}",
              ...fields,
            },
          },
        ],
      }),
    );
    const result = await c.registerTools([
      new ToolRegistration({ name: "write", description: "", inputSchema: {} }),
    ]);
    assert.equal(
      result[0]?.tool?.intent,
      "intent" in fields ? fields.intent : null,
    );
    if ("intent_drafting" in fields) {
      assert.equal(result[0]?.tool?.intentDrafting?.status, "complete");
      assert.equal(Object.isFrozen(result[0]?.tool?.intentDrafting), true);
    }
    await c.close();
  }
});

test("prepared envelopes freeze selected values in the server UTF-8 path order", async () => {
  const values = [
    { path: "😀", value: { label: "before" } },
    { path: "\ue000", value: null },
  ];
  const c = machine(async () => json({}));
  const base = {
    runId: "0198f6cb-0300-7000-8000-000000000002",
    toolId: "00000000-0000-4000-8000-000000000011",
    toolVersionId: "00000000-0000-4000-8000-000000000012",
    toolName: "unicode",
    connector: "test",
    action: "write",
    input: {},
    policyContractSetDigest: "a".repeat(64),
  };
  const frozen = c.prepareDecision({ ...base, selectedValues: values });
  (values[0]!.value as { label: string }).label = "after";
  assert.deepEqual(frozen.selectedValues, [
    { path: "\ue000", value: null },
    { path: "😀", value: { label: "before" } },
  ]);
  const readiness = {
    name: "unicode",
    registrationStatus: "registered",
    governed: true,
    policyReady: true,
    eligible: true,
    reason: "ready",
    policyContractSetDigest: "a".repeat(64),
    policyInputContracts: [
      {
        bindingId: base.toolId,
        policyId: base.toolId,
        policyVersionId: base.toolVersionId,
        inputDigest: "b".repeat(64),
        fieldSelectors: [{ path: "😀" }, { path: "\ue000" }],
      },
    ],
  };
  assert.deepEqual(
    collectSelectedValues({ "😀": 1, "\ue000": null }, readiness),
    [
      { path: "\ue000", value: null },
      { path: "😀", value: 1 },
    ],
  );
  assert.throws(
    () =>
      c.prepareDecision({ ...base, input: { secret: 1 }, selectedValues: [] }),
    ConfigurationError,
  );
  for (const path of [" ", "я".repeat(129)])
    assert.throws(
      () =>
        c.prepareDecision({ ...base, selectedValues: [{ path, value: 1 }] }),
      ConfigurationError,
    );
  const big = Array.from({ length: 16 }, () => "x".repeat(65500));
  assert.throws(
    () =>
      c.prepareDecision({
        ...base,
        selectedValues: [{ path: "payload", value: big }],
      }),
    ConfigurationError,
  );
  await c.close();
});

test("readiness rejects foreign exact-version responses and oversized requirement sets", async () => {
  const id = "00000000-0000-4000-8000-000000000012";
  for (const tools of [
    [],
    [
      {
        name: "other",
        tool_version_id: "00000000-0000-4000-8000-000000000099",
      },
    ],
  ]) {
    const c = machine(async () => json({ readiness: { tools } }));
    await assert.rejects(c.getToolReadiness([id]), ProtocolError);
    await c.close();
  }
  const requirements = Array.from({ length: 33 }, () => ({
    binding_id: id,
    policy_id: id,
    policy_version_id: id,
    input_digest: "a".repeat(64),
    field_selectors: [],
  }));
  const c = machine(async () =>
    json({
      readiness: {
        tools: [
          {
            name: "write",
            tool_version_id: id,
            policy_input_contracts: requirements,
          },
        ],
      },
    }),
  );
  await assert.rejects(c.getToolReadiness([id]), ProtocolError);
  await c.close();
});

test("native Read publication accepts read-only readiness without Write configuration", async () => {
  const id = "00000000-0000-4000-8000-000000000011",
    version = "00000000-0000-4000-8000-000000000012";
  const c = machine(async () =>
    json({
      results: [
        {
          request_index: 0,
          status: "registered",
          reason: "",
          tool: {
            id,
            name: "lookup",
            description: "Read fixture",
            capability: "read",
            governed: false,
            current_version_id: version,
          },
          tool_version: {
            id: version,
            tool_id: id,
            governance_revision_id: version,
            version: "v1",
            content_digest: "sha256:" + "a".repeat(64),
            input_schema_json: "{}",
          },
          readiness: {
            name: "lookup",
            tool_id: id,
            tool_version_id: version,
            registration_status: "registered",
            governed: false,
            eligible: true,
            policy_ready: true,
            reason: "read_only",
          },
        },
      ],
    }),
  );
  const result = await c.registerTools([
    new ToolRegistration({
      name: "lookup",
      description: "Read fixture",
      inputSchema: {},
    }),
  ]);
  assert.equal(result[0]!.tool!.toolContractDigest, undefined);
  assert.equal(result[0]!.tool!.governanceRevisionId, version);
  assert.equal(result[0]!.readiness!.reason, "read_only");
  await c.close();
});

test("integration is copied before first read and telemetry; omission stays absent", async () => {
  for (const integration of [
    undefined,
    "otel",
    "langchain",
    "deepagents",
    "other",
  ] as const) {
    const seen: Request[] = [];
    const options = { integration };
    const client = machine(async (input, init) => {
      seen.push(new Request(input, init));
      return String(input).endsWith("WriteTelemetry")
        ? json({ runs: [], events: [] })
        : json({ state: "none" });
    }, options);
    options.integration = "other";
    await client.approvalState("0198f6cb-0300-7000-8000-000000000001");
    await client.writeTelemetry({});
    for (const request of seen)
      assert.equal(
        request.headers.get("X-OpenEight-Agent-Integration"),
        integration ?? null,
      );
    await client.close();
  }
  assert.throws(
    () => machine(async () => json({}), { integration: "unknown" }),
    ConfigurationError,
  );
});

test("integration Connect details retain only the stable identity reason", async () => {
  const reason = "agent_integration_mismatch",
    domain = "openeight.identity";
  const value = Buffer.concat([
    Buffer.from([10, reason.length]),
    Buffer.from(reason),
    Buffer.from([18, domain.length]),
    Buffer.from(domain),
  ]).toString("base64");
  const client = machine(async () =>
    json(
      {
        code: "failed_precondition",
        message: "private detail",
        details: [{ type: "google.rpc.ErrorInfo", value }],
      },
      400,
    ),
  );
  await assert.rejects(
    client.approvalState("0198f6cb-0300-7000-8000-000000000001"),
    (e: unknown) =>
      e instanceof ConnectError &&
      e.reason === reason &&
      e.message === "request failed",
  );
  await client.close();
});

test("readiness preserves genuine revision metadata and rejects malformed stamps", async () => {
  const revision = "11111111-1111-4111-8111-111111111111";
  for (const value of [
    undefined,
    revision,
    "",
    "malformed",
    "00000000-0000-0000-0000-000000000000",
    false,
  ]) {
    const c = machine(async () =>
      json({
        readiness: {
          tools: [
            {
              name: "lookup",
              registration_status: "registered",
              policy_ready: true,
              eligible: true,
              reason: "read_only",
              governance_revision_id: value,
            },
          ],
        },
      }),
    );
    if (value === undefined || value === revision)
      assert.equal(
        (await c.getToolReadiness([])).tools[0]?.governanceRevisionId,
        value,
      );
    else await assert.rejects(c.getToolReadiness([]), ProtocolError);
    await c.close();
  }
});

test("client shutdown owns transport and drains accepted events before completion", async () => {
  const writes: Array<{ runs: Array<{ status: string }>; events: unknown[] }> =
    [];
  const client = machine(async (_url, init) => {
    const body = JSON.parse(String(init?.body));
    writes.push(body);
    return json({
      runs: (body.runs ?? []).map((row: { id: string }) => ({ id: row.id })),
      events: (body.events ?? []).map((row: { id: string }) => ({
        id: row.id,
      })),
    });
  });
  assert.equal("closeTransport" in client, false);
  assert.equal("send" in client, false);
  const run = await client.governedRun();
  run.submitEvent({
    name: "work",
    observation_kind: "span",
    operation_kind: "invoke",
    occurred_at: new Date().toISOString(),
    outcome: "success",
  });
  await client.close();
  assert.equal(writes.flatMap((body) => body.events ?? []).length, 1);
  assert.equal(writes.at(-1)?.runs[0]?.status, "completed");
  assert.throws(
    () =>
      run.submitEvent({
        name: "late",
        observation_kind: "span",
        operation_kind: "invoke",
        occurred_at: new Date().toISOString(),
        outcome: "success",
      }),
    ConfigurationError,
  );
});
