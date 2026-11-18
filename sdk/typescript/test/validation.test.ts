import assert from "node:assert/strict";
import test from "node:test";
import {
  ConfigurationError,
  MachineClient,
  ToolRegistration,
  rfc3339Micros,
  uuid7,
} from "../src/index.ts";

test("timestamps reject impossible calendar components instead of rolling forward", () => {
  for (const value of [
    "2026-02-30T12:00:00Z",
    "2025-02-29T12:00:00Z",
    "1900-02-29T12:00:00Z",
    "2026-04-31T12:00:00Z",
    "2026-01-01T24:00:00Z",
    "0000-01-01T00:00:00Z",
    "2026-00-01T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-01-00T00:00:00Z",
    "2026-01-01T00:60:00Z",
    "2026-01-01T00:00:60Z",
    "2026-01-01T00:00:00+24:00",
    "2026-01-01T00:00:00+00:60",
    "0001-01-01T00:00:00+01:00",
    "9999-12-31T23:59:59-01:00",
  ])
    assert.throws(() => rfc3339Micros(value), ConfigurationError, value);
  assert.equal(
    rfc3339Micros("2000-02-29T12:34:56.123456789+05:30"),
    "2000-02-29T07:04:56.123456Z",
  );
  assert.equal(
    rfc3339Micros("2024-03-01T00:15:00+01:00"),
    "2024-02-29T23:15:00.000000Z",
  );
  assert.equal(
    rfc3339Micros("0099-01-01T00:00:00Z"),
    "0099-01-01T00:00:00.000000Z",
  );
});

test("output token counts are validated before telemetry is sent", async () => {
  let calls = 0;
  const client = new MachineClient({
    baseUrl: "https://example.com",
    apiKey: "ctk_synthetic_example",
    agentName: "example",
    fetch: async (_, init) => {
      calls++;
      const body = JSON.parse(init!.body as string);
      return new Response(
        JSON.stringify({
          runs: [],
          events: body.events.map((event: { id: string }) => ({
            id: event.id,
          })),
        }),
        { status: 200 },
      );
    },
  });
  const event = {
    id: uuid7(),
    run_id: uuid7(),
    name: "example",
    observation_kind: "llm_call" as const,
    operation_kind: "invoke" as const,
    occurred_at: "2026-11-15T12:00:00Z",
    outcome: "success" as const,
  };
  try {
    for (const output_tokens of [
      "-1",
      "9223372036854775808",
      "1.5",
      "01",
      "NaN",
    ]) {
      await assert.rejects(
        client.writeTelemetry({ events: [{ ...event, output_tokens }] }),
        ConfigurationError,
      );
    }
    assert.equal(calls, 0);
    for (const output_tokens of ["0", "9223372036854775807"])
      await client.writeTelemetry({ events: [{ ...event, output_tokens }] });
    assert.equal(calls, 2);
  } finally {
    await client.close();
  }
});

test("patternProperties keys enforce the same restricted regex rules as pattern", () => {
  for (const pattern of ["(?=x)", "(x)\\1", "["]) {
    for (const inputSchema of [
      { type: "object", patternProperties: { [pattern]: { type: "string" } } },
      {
        type: "object",
        properties: {
          nested: {
            type: "object",
            patternProperties: { [pattern]: { type: "string" } },
          },
        },
      },
    ])
      assert.throws(
        () =>
          new ToolRegistration({
            name: "example",
            description: "",
            inputSchema,
          }),
        ConfigurationError,
        pattern,
      );
  }
  new ToolRegistration({
    name: "example",
    description: "",
    inputSchema: {
      type: "object",
      patternProperties: { "^[a-z]+$": { type: "string" } },
    },
  });
});
