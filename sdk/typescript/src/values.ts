import { freezeDeep } from "./immutable.ts";
import { isSemVer } from "./semver.ts";
import { ConfigurationError } from "./errors.ts";
export function compareUTF8(a: string, b: string): number {
  const left = new TextEncoder().encode(a),
    right = new TextEncoder().encode(b);
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    if (left[i] !== right[i]) return left[i]! - right[i]!;
  }
  return left.length - right.length;
}
export type JSONValue =
  | null
  | boolean
  | number
  | string
  | JSONValue[]
  | { [key: string]: JSONValue };
/** Copy strict JSON and encode it; reject cycles, unsafe numbers and non-JSON objects. */
export function jsonSnapshot(
  value: unknown,
  objectRoot = false,
): { bytes: Uint8Array; text: string; value: JSONValue } {
  try {
    const active = new Set<object>();
    const copy = (v: unknown, depth = 0): JSONValue => {
      if (v === null || typeof v === "boolean") return v as JSONValue;
      if (typeof v === "string") {
        if (!validUnicode(v) || new TextEncoder().encode(v).byteLength > 65536)
          throw 0;
        return v;
      }
      if (typeof v === "number") {
        if (
          !Number.isFinite(v) ||
          (Number.isInteger(v) && !Number.isSafeInteger(v))
        )
          throw 0;
        return v;
      }
      if (typeof v !== "object" || depth >= 32 || active.has(v)) throw 0;
      active.add(v);
      try {
        if (Array.isArray(v)) return v.map((x) => copy(x, depth + 1));
        if (
          Object.getPrototypeOf(v) !== Object.prototype &&
          Object.getPrototypeOf(v) !== null
        )
          throw 0;
        const out: Record<string, JSONValue> = {};
        for (const [k, x] of Object.entries(v)) {
          if (
            !validUnicode(k) ||
            new TextEncoder().encode(k).byteLength > 65536
          )
            throw 0;
          Object.defineProperty(out, k, {
            value: copy(x, depth + 1),
            enumerable: true,
            writable: true,
            configurable: true,
          });
        }
        return out;
      } finally {
        active.delete(v);
      }
    };
    if (
      objectRoot &&
      (!value || typeof value !== "object" || Array.isArray(value))
    )
      throw 0;
    const normalized = copy(value);
    const text = JSON.stringify(normalized);
    const bytes = new TextEncoder().encode(text);
    if (bytes.byteLength > 1_048_576) throw 0;
    return { bytes, text, value: normalized };
  } catch {
    throw new ConfigurationError("invalid_json");
  }
}
function validUnicode(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const n = s.charCodeAt(i + 1);
      if (!(n >= 0xdc00 && n <= 0xdfff)) return false;
      i++;
    } else if (c >= 0xdc00 && c <= 0xdfff) return false;
  }
  return true;
}
/** Allocate a time-based correlation ID; this does not establish server authority. */
export function uuid7(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const ms = BigInt(Date.now());
  for (let i = 5; i >= 0; i--) {
    bytes[i] = Number((ms >> BigInt((5 - i) * 8)) & 255n);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const h = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
export function requireUuid7(value: string): string {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  )
    throw new ConfigurationError("invalid_uuid");
  return value;
}
function requireUuid(value: string): string {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  )
    throw new ConfigurationError("invalid_uuid");
  return value;
}
/** Normalize an explicit timestamp to UTC, truncating sub-microsecond precision. */
export function rfc3339Micros(value: string): string {
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(
      value,
    );
  if (!match) throw new ConfigurationError("invalid_timestamp");
  const [year, month, day, hour, minute, second] = match
    .slice(1, 7)
    .map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    year < 1 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > days[month - 1]! ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    throw new ConfigurationError("invalid_timestamp");
  }
  const zone = match[8]!;
  if (
    zone !== "Z" &&
    (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4, 6)) > 59)
  )
    throw new ConfigurationError("invalid_timestamp");
  const base = Date.parse(
    `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}${zone}`,
  );
  if (!Number.isFinite(base)) throw new ConfigurationError("invalid_timestamp");
  const normalized = new Date(base);
  if (normalized.getUTCFullYear() < 1 || normalized.getUTCFullYear() > 9999)
    throw new ConfigurationError("invalid_timestamp");
  const fraction = (match[7] ?? "").padEnd(6, "0").slice(0, 6);
  return normalized.toISOString().slice(0, 19) + `.${fraction}Z`;
}
export interface FieldSelector {
  readonly path: string;
  readonly alias?: string;
}
export interface ToolRegistrationOptions {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  expectedToolId?: string;
  expectedCurrentVersionId?: string;
}
/** Snapshot schema facts; schema successors require both acknowledged precondition IDs. */
export class ToolRegistration {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: Record<string, unknown>;
  readonly expectedToolId?: string;
  readonly expectedCurrentVersionId?: string;
  constructor(o: ToolRegistrationOptions) {
    if (
      !o ||
      Object.keys(o).some(
        (k) =>
          ![
            "name",
            "description",
            "inputSchema",
            "expectedToolId",
            "expectedCurrentVersionId",
          ].includes(k),
      ) ||
      typeof o.name !== "string" ||
      !o.name.trim() ||
      new TextEncoder().encode(o.name).byteLength > 256 ||
      typeof o.description !== "string" ||
      new TextEncoder().encode(o.description).byteLength > 4096
    )
      throw new ConfigurationError("invalid_tool_registration");
    if (
      (o.expectedToolId === undefined) !==
      (o.expectedCurrentVersionId === undefined)
    )
      throw new ConfigurationError("invalid_tool_registration");
    if (o.expectedToolId !== undefined) {
      this.expectedToolId = requireUuid(o.expectedToolId);
      this.expectedCurrentVersionId = requireUuid(o.expectedCurrentVersionId!);
    }
    const schema = jsonSnapshot(o.inputSchema, true);
    if (new TextEncoder().encode(schema.text).byteLength > 256 * 1024)
      throw new ConfigurationError("invalid_tool_registration");
    this.name = o.name;
    this.description = o.description;
    this.inputSchema = freezeDeep(schema.value as Record<string, unknown>);
    validateToolSchema(this.inputSchema);
    Object.freeze(this);
  }
}

function validateToolPattern(pattern: string): void {
  if (/\(\?|\\[1-9]|\\g<|\\k</.test(pattern))
    throw new ConfigurationError("invalid_tool_registration");
  try {
    new RegExp(pattern);
  } catch {
    throw new ConfigurationError("invalid_tool_registration");
  }
}

function validateToolSchema(schema: Record<string, unknown>): void {
  const defs = schema.$defs ?? {};
  if (
    !defs ||
    typeof defs !== "object" ||
    Array.isArray(defs) ||
    (schema.$schema !== undefined &&
      schema.$schema !== "https://json-schema.org/draft/2020-12/schema") ||
    (schema.type !== undefined && schema.type !== "object")
  )
    throw new ConfigurationError("invalid_tool_registration");
  let nodes = 0;
  const walk = (v: unknown, depth: number): void => {
    if (++nodes > 4096 || depth > 32)
      throw new ConfigurationError("invalid_tool_registration");
    if (Array.isArray(v)) {
      for (const x of v) walk(x, depth + 1);
      return;
    }
    if (v && typeof v === "object") {
      for (const [k, x] of Object.entries(v)) {
        if (["$id", "$anchor", "$dynamicAnchor", "$dynamicRef"].includes(k))
          throw new ConfigurationError("invalid_tool_registration");
        if (k === "$ref") {
          if (typeof x !== "string" || !x.startsWith("#/$defs/"))
            throw new ConfigurationError("invalid_tool_registration");
          const name = x.slice(8).replace(/~1/g, "/").replace(/~0/g, "~");
          if (!Object.hasOwn(defs, name))
            throw new ConfigurationError("invalid_tool_registration");
        }
        if (
          k === "properties" &&
          x &&
          typeof x === "object" &&
          Object.keys(x).some((key) => key.includes("."))
        )
          throw new ConfigurationError("invalid_tool_registration");
        if (
          (k === "pattern" || k === "patternProperties") &&
          typeof x === "string"
        )
          validateToolPattern(x);
        if (
          k === "patternProperties" &&
          x &&
          typeof x === "object" &&
          !Array.isArray(x)
        ) {
          for (const pattern of Object.keys(x)) validateToolPattern(pattern);
        }
        walk(x, depth + 1);
      }
    }
  };
  walk(schema, 0);
}
/** Immutable attempt identity and policy-selected input retained for exact replay. */
export interface PreparedDecision {
  readonly id: string;
  readonly runId: string;
  readonly sourceTime: string;
  readonly toolId: string;
  readonly toolVersionId: string;
  readonly toolName: string;
  readonly connector: string;
  readonly action: string;
  readonly input: Readonly<Record<string, JSONValue>>;
  readonly policyContractSetDigest: string;
  readonly selectedValues: readonly { path: string; value: JSONValue }[];
  readonly workflowId?: string;
  readonly workflowVersion?: string;
}
export type PrepareDecisionOptions = Omit<
  PreparedDecision,
  "id" | "sourceTime" | "input"
> & { input: Record<string, unknown>; id?: string; sourceTime?: string };
/** Freeze validated input and identity once; reuse this result unchanged on retry. */
export function prepareDecision(i: PrepareDecisionOptions): PreparedDecision {
  const input = jsonSnapshot(i.input, true).value as Record<string, JSONValue>;
  if (
    Object.keys(input).length ||
    typeof i.policyContractSetDigest !== "string" ||
    !/^[0-9a-f]{64}$/.test(i.policyContractSetDigest) ||
    !Array.isArray(i.selectedValues)
  )
    throw new ConfigurationError("invalid_input");
  const selected = jsonSnapshot(i.selectedValues).value as {
    path: string;
    value: JSONValue;
  }[];
  const paths = new Set<string>();
  for (const item of selected) {
    if (
      !item ||
      typeof item !== "object" ||
      Object.keys(item).length !== 2 ||
      typeof item.path !== "string" ||
      !item.path.trim() ||
      new TextEncoder().encode(item.path).byteLength > 256 ||
      item.path.includes("*") ||
      item.path.split(".").some((part) => !part) ||
      !Object.hasOwn(item, "value") ||
      paths.has(item.path)
    )
      throw new ConfigurationError("invalid_input");
    paths.add(item.path);
  }
  selected.sort((a, b) => compareUTF8(a.path, b.path));
  const envelope = jsonSnapshot(
    {
      input,
      policy_contract_set_digest: i.policyContractSetDigest,
      selected_values: selected,
    },
    true,
  );
  if (envelope.bytes.byteLength > 1_044_480)
    throw new ConfigurationError("invalid_input");
  const id = requireUuid7(i.id ?? uuid7()),
    runId = requireUuid7(i.runId);
  if (
    typeof i.toolName !== "string" ||
    !i.toolName.trim() ||
    typeof i.connector !== "string" ||
    !i.connector.trim() ||
    typeof i.action !== "string" ||
    !i.action.trim()
  )
    throw new ConfigurationError("invalid_input");
  const sourceTime = rfc3339Micros(i.sourceTime ?? new Date().toISOString());
  if (sourceTime === "0001-01-01T00:00:00.000000Z")
    throw new ConfigurationError("invalid_timestamp");
  const wf = workflowFields(i.workflowId, i.workflowVersion);
  return Object.freeze({
    id,
    runId,
    sourceTime,
    ...(wf.workflow_id
      ? { workflowId: wf.workflow_id, workflowVersion: wf.workflow_version }
      : {}),
    toolId: requireUuid(i.toolId),
    toolVersionId: requireUuid(i.toolVersionId),
    toolName: i.toolName,
    connector: i.connector,
    action: i.action,
    input: freezeDeep(input),
    policyContractSetDigest: i.policyContractSetDigest,
    selectedValues: freezeDeep(selected),
  });
}

/** Validate optional workflow correlation without assigning authority. */
export function workflowFields(
  id?: string,
  version?: string,
): { workflow_id?: string; workflow_version?: string } {
  if (id === undefined && version === undefined) return {};
  if (
    typeof id !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[57][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      id,
    ) ||
    typeof version !== "string" ||
    !isSemVer(version) ||
    new TextEncoder().encode(version).byteLength > 128
  )
    throw new ConfigurationError("invalid_input");
  return { workflow_id: id, workflow_version: version };
}

/** Compare JSON projections independently of object insertion order and number spelling. */
export function sameJSON(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (
    a === null ||
    b === null ||
    typeof a !== "object" ||
    typeof b !== "object" ||
    Array.isArray(a) !== Array.isArray(b)
  )
    return false;
  const x = a as Record<string, unknown>,
    y = b as Record<string, unknown>;
  return (
    Object.keys(x).length === Object.keys(y).length &&
    Object.keys(x).every((k) => Object.hasOwn(y, k) && sameJSON(x[k], y[k]))
  );
}

/** Validate strict JSON without discarding duplicate keys or original encoded bytes. */
export function encodedObject(text: string): Record<string, JSONValue> {
  try {
    if (
      typeof text !== "string" ||
      new TextEncoder().encode(text).byteLength > 1_048_576
    )
      throw 0;
    const value = JSON.parse(text);
    jsonSnapshot(value, true);
    let at = 0;
    const space = () => {
      while (/\s/.test(text[at] ?? "") && at < text.length) at++;
    };
    const string = (): string => {
      const begin = at++;
      while (at < text.length) {
        const c = text[at++];
        if (c === '"') return JSON.parse(text.slice(begin, at));
        if (c === "\\") at++;
      }
      throw 0;
    };
    const read = (depth: number): void => {
      if (depth > 32) throw 0;
      space();
      if (text[at] === "{") {
        at++;
        space();
        const keys = new Set<string>();
        if (text[at] === "}") {
          at++;
          return;
        }
        while (true) {
          space();
          const key = string();
          if (keys.has(key)) throw 0;
          keys.add(key);
          space();
          at++;
          read(depth + 1);
          space();
          if (text[at++] === "}") break;
        }
      } else if (text[at] === "[") {
        at++;
        space();
        if (text[at] === "]") {
          at++;
          return;
        }
        while (true) {
          read(depth + 1);
          space();
          if (text[at++] === "]") break;
        }
      } else if (text[at] === '"') string();
      else {
        while (at < text.length && !/[\s,}\]]/.test(text[at]!)) at++;
      }
    };
    read(0);
    space();
    if (at !== text.length) throw 0;
    return value;
  } catch {
    throw new ConfigurationError("invalid_json");
  }
}
