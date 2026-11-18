import { ConfigurationError } from "./errors.ts";
import { jsonSnapshot, requireUuid7, rfc3339Micros } from "./values.ts";
import type { EventWrite } from "./machine.ts";

const forbidden = new Set([
  "organization_id",
  "workspace_id",
  "agent_id",
  "api_key_id",
  "policy_id",
  "policy_version_id",
  "trust_level",
  "tenant_id",
  "principal_id",
  "user_id",
  "session_id",
]);
const metrics = [
  "input_tokens",
  "output_tokens",
  "cache_creation_input_tokens",
  "cache_read_input_tokens",
  "latency_ms",
  "ttft_ms",
] as const;
const eventFields = [
  "id",
  "run_id",
  "name",
  "observation_kind",
  "operation_kind",
  "outcome",
  "occurred_at",
  "span_role",
  "parent_event_id",
  "decision_id",
  "provider",
  "model",
  "tool_id",
  "tool_version_id",
  "tool_name",
  "input_json",
  "output_json",
  "error_json",
  "attributes_json",
  ...metrics,
];
export const runFields = [
  "id",
  "status",
  "started_at",
  "ended_at",
  "attributes_json",
  "workflow_id",
  "workflow_version",
  "parent_run_id",
];
/** Recognize ProtoJSON aliases before validation; duplicate spellings are ambiguous. */
export function canonicalFields<T extends object>(
  input: T,
  fields: readonly string[],
): T {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const row = { ...input } as Record<string, unknown>;
  for (const field of fields) {
    const alias = field.replace(/_([a-z])/g, (_, letter: string) =>
      letter.toUpperCase(),
    );
    if (alias === field || !Object.hasOwn(row, alias)) continue;
    if (Object.hasOwn(row, field)) invalid();
    row[field] = row[alias];
    delete row[alias];
  }
  return row as T;
}
function invalid(): never {
  throw new ConfigurationError("invalid_event");
}
function name(v: unknown, max: number): void {
  if (
    typeof v !== "string" ||
    !v.trim() ||
    new TextEncoder().encode(v).byteLength > max
  )
    invalid();
}
function uuid(v: unknown): void {
  if (
    typeof v !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      v,
    )
  )
    invalid();
}
export function correlationUuid(v: unknown): void {
  if (
    typeof v !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[57][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      v,
    )
  )
    invalid();
}
/** Validate before queue ownership; retain accepted unknown raw fields for wire compatibility. */
export function normalizeEvent(input: EventWrite): EventWrite {
  if (!input || typeof input !== "object" || Array.isArray(input)) invalid();
  const event = canonicalFields(input, eventFields);
  event.occurred_at = rfc3339Micros(event.occurred_at);
  const row = event as unknown as Record<string, unknown>;
  if (Object.keys(row).some((k) => forbidden.has(k)))
    throw new ConfigurationError("forbidden_selector");
  requireUuid7(event.id);
  requireUuid7(event.run_id);
  if (event.parent_event_id !== undefined)
    correlationUuid(event.parent_event_id);
  if (event.decision_id !== undefined) correlationUuid(event.decision_id);
  name(event.name, 128);
  if (
    !["span", "llm_call", "tool_call", "lifecycle"].includes(
      event.observation_kind,
    ) ||
    !["unspecified", "invoke", "stream"].includes(event.operation_kind) ||
    !["unspecified", "success", "error", "interrupted"].includes(event.outcome)
  )
    invalid();
  if (
    event.span_role != null &&
    (event.observation_kind !== "span" ||
      !["graph", "node", "retriever", "internal"].includes(event.span_role))
  )
    invalid();
  for (const key of ["provider", "model", "tool_name"] as const)
    if (event[key] !== undefined) name(event[key], 256);
  if (event.tool_id !== undefined) uuid(event.tool_id);
  if (event.tool_version_id !== undefined) uuid(event.tool_version_id);
  if (
    (event.tool_id === undefined) !== (event.tool_version_id === undefined) ||
    (event.tool_version_id !== undefined && event.tool_name === undefined)
  )
    invalid();
  const values: Record<string, unknown> = {};
  let size = 0;
  for (const key of [
    "input_json",
    "output_json",
    "error_json",
    "attributes_json",
  ]) {
    const text = row[key];
    if (text === undefined) continue;
    if (typeof text !== "string") invalid();
    try {
      values[key] = JSON.parse(text);
    } catch {
      invalid();
    }
    const snap = jsonSnapshot(values[key], key === "attributes_json");
    row[key] = snap.text;
    size += snap.bytes.byteLength;
  }
  if (size > 1_048_576) throw new ConfigurationError("invalid_limit");
  for (const key of metrics) {
    const v = row[key];
    if (v === undefined || v === null) continue;
    if (
      typeof v !== "string" ||
      !/^(?:0|[1-9]\d{0,18})$/.test(v) ||
      BigInt(v) > 9223372036854775807n
    )
      invalid();
  }
  // Encoded JSON null remains present for payload restrictions.
  const present = (key: string) => Object.hasOwn(values, key);
  if (
    (event.outcome === "success" && present("error_json")) ||
    (event.outcome === "error" &&
      (!present("error_json") || present("output_json")))
  )
    invalid();
  if (
    event.outcome === "interrupted" &&
    (!["span", "lifecycle"].includes(event.observation_kind) ||
      present("error_json") ||
      present("output_json"))
  )
    invalid();
  if (
    event.observation_kind === "lifecycle" &&
    (event.operation_kind !== "unspecified" ||
      !["interrupted", "unspecified"].includes(event.outcome) ||
      ["input_json", "output_json", "error_json"].some(present) ||
      ["provider", "model", ...metrics].some((k) => row[k] != null))
  )
    invalid();
  if (
    event.ttft_ms != null &&
    (event.observation_kind !== "llm_call" ||
      (event.latency_ms != null &&
        BigInt(event.ttft_ms) > BigInt(event.latency_ms)))
  )
    invalid();
  if (
    (event.cache_creation_input_tokens != null ||
      event.cache_read_input_tokens != null) &&
    event.input_tokens == null
  )
    invalid();
  if (
    event.input_tokens != null &&
    BigInt(event.cache_creation_input_tokens ?? "0") +
      BigInt(event.cache_read_input_tokens ?? "0") >
      BigInt(event.input_tokens)
  )
    invalid();
  const hasIdentity = [
    event.tool_id,
    event.tool_version_id,
    event.tool_name,
    event.decision_id,
  ].some((value) => value !== undefined);
  const registered =
    event.tool_id !== undefined || event.tool_version_id !== undefined;
  if (
    event.observation_kind !== "tool_call" &&
    event.observation_kind !== "lifecycle"
  ) {
    if (hasIdentity) invalid();
  } else if (event.observation_kind === "tool_call" || hasIdentity) {
    if (
      event.tool_name === undefined ||
      (event.observation_kind === "lifecycle" && !registered)
    )
      invalid();
    if (event.decision_id !== undefined) {
      if (!registered) invalid();
      if (
        event.observation_kind === "tool_call" &&
        (!["success", "error"].includes(event.outcome) ||
          !present("input_json") ||
          values.input_json === null ||
          typeof values.input_json !== "object" ||
          Array.isArray(values.input_json))
      )
        invalid();
    }
  }
  return event;
}
