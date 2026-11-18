import { freezeDeep } from "./immutable.ts";
import {
  ConfigurationError,
  ConnectError,
  ObserveDeliveryError,
  ObserveQueueFullError,
  ObserveRunCompletedError,
  ProtocolError,
  TransportError,
} from "./errors.ts";
import {
  jsonSnapshot,
  requireUuid7,
  rfc3339Micros,
  uuid7,
  workflowFields,
  encodedObject,
  sameJSON,
  type JSONValue,
} from "./values.ts";
import type { CallOptions } from "./transport.ts";
import { normalizeEvent } from "./telemetry-validation.ts";
import type { EventWrite, RunWrite } from "./machine.ts";

export interface RunIdentity {
  readonly runId: string;
  readonly startedAt: string;
  readonly attributes: Readonly<Record<string, unknown>>;
  readonly attributesJson?: string;
  readonly workflowId?: string;
  readonly workflowVersion?: string;
}
export interface RunIdentityOptions {
  runId?: string;
  startedAt?: string;
  attributes?: Record<string, unknown>;
  workflowId?: string;
  workflowVersion?: string;
}
/** Application-owned suspension data; it provides neither durable storage nor fencing. */
export interface RunCheckpoint extends RunIdentity {
  readonly version: 1;
  readonly state: "suspended";
}
export interface RunCheckpointData {
  readonly version: 1;
  readonly state: "suspended";
  readonly run_id: string;
  readonly started_at: string;
  readonly attributes: Readonly<Record<string, JSONValue>>;
  readonly attributes_json?: string;
  readonly workflow_id?: string;
  readonly workflow_version?: string;
}

/** Normalize a received identity while preserving retained text and legacy absence. */
export function normalizeRunIdentity(value: RunIdentity): RunIdentity {
  if (!value || typeof value !== "object")
    throw new ConfigurationError("invalid_run");
  const attrs = jsonSnapshot(value.attributes, true).value as Record<
    string,
    unknown
  >;
  if (
    value.attributesJson !== undefined &&
    !sameJSON(encodedObject(value.attributesJson), attrs)
  )
    throw new ConfigurationError("invalid_run");
  const wf = workflowFields(value.workflowId, value.workflowVersion);
  return Object.freeze({
    runId: requireUuid7(value.runId),
    startedAt: rfc3339Micros(value.startedAt),
    attributes: freezeDeep(attrs),
    ...(value.attributesJson !== undefined
      ? { attributesJson: value.attributesJson }
      : {}),
    ...(wf.workflow_id
      ? { workflowId: wf.workflow_id, workflowVersion: wf.workflow_version }
      : {}),
  });
}
/** Preserve startup encoding in every Run row, including buffered Event envelopes. */
export function runWrite(identity: RunIdentity): RunWrite {
  return {
    id: identity.runId,
    status: "active",
    started_at: identity.startedAt,
    attributes_json:
      identity.attributesJson ?? jsonSnapshot(identity.attributes, true).text,
    ...workflowFields(identity.workflowId, identity.workflowVersion),
  };
}
/** Export validated suspension facts, omitting unknown legacy encoding. */
export function exportRunCheckpoint(
  checkpoint: RunCheckpoint,
): RunCheckpointData {
  if (
    !checkpoint ||
    checkpoint.version !== 1 ||
    checkpoint.state !== "suspended"
  )
    throw new ConfigurationError("invalid_run");
  const i = normalizeRunIdentity(checkpoint);
  return {
    version: 1,
    state: "suspended",
    run_id: i.runId,
    started_at: i.startedAt,
    attributes: jsonSnapshot(i.attributes, true).value as Record<
      string,
      JSONValue
    >,
    ...(i.attributesJson !== undefined
      ? { attributes_json: i.attributesJson }
      : {}),
    ...workflowFields(i.workflowId, i.workflowVersion),
  };
}
/** Import untrusted checkpoint data without making network or authority claims. */
export function importRunCheckpoint(value: unknown): RunCheckpoint {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new ConfigurationError("invalid_run");
  const v = value as Record<string, unknown>;
  if (
    Object.keys(v).some(
      (k) =>
        ![
          "version",
          "state",
          "run_id",
          "started_at",
          "attributes",
          "attributes_json",
          "workflow_id",
          "workflow_version",
        ].includes(k),
    ) ||
    v.version !== 1 ||
    v.state !== "suspended" ||
    (Object.hasOwn(v, "attributes_json") &&
      typeof v.attributes_json !== "string") ||
    (Object.hasOwn(v, "workflow_id") && typeof v.workflow_id !== "string") ||
    (Object.hasOwn(v, "workflow_version") &&
      typeof v.workflow_version !== "string")
  )
    throw new ConfigurationError("invalid_run");
  const identity = normalizeRunIdentity({
    runId: v.run_id as string,
    startedAt: v.started_at as string,
    attributes: v.attributes as Record<string, unknown>,
    attributesJson: v.attributes_json as string | undefined,
    workflowId: v.workflow_id as string | undefined,
    workflowVersion: v.workflow_version as string | undefined,
  });
  return Object.freeze({ version: 1, state: "suspended", ...identity });
}
export type GovernedEventInput = Omit<
  EventWrite,
  | "id"
  | "run_id"
  | "input_json"
  | "output_json"
  | "error_json"
  | "attributes_json"
> & {
  id?: string;
  input?: unknown;
  output?: unknown;
  error?: unknown;
  attributes?: Record<string, unknown>;
};
export interface ObserveLimits {
  maxRecords?: number;
  maxBytes?: number;
  flushRecords?: number;
  flushBytes?: number;
  flushIntervalMs?: number;
  maxRequestRecords?: number;
  maxRequestBytes?: number;
}
interface Pending {
  run: RunIdentity;
  event: EventWrite;
  bytes: number;
}
const defaults = {
  maxRecords: 1000,
  maxBytes: 8 * 1024 * 1024,
  flushRecords: 100,
  flushBytes: 3 * 1024 * 1024,
  flushIntervalMs: 1000,
  maxRequestRecords: 1000,
  maxRequestBytes: 4 * 1024 * 1024,
};
function cleanupOptions(options?: CallOptions): CallOptions {
  return options?.timeoutMs === undefined
    ? { ...options, timeoutMs: 10_000 }
    : options;
}
function deliveryError(error: unknown): unknown {
  if (
    error instanceof ObserveDeliveryError ||
    (error instanceof TransportError && error.reason === "canceled")
  )
    return error;
  const reason =
    error instanceof TransportError
      ? error.reason === "timeout"
        ? "timeout"
        : "unavailable"
      : error instanceof ConnectError || error instanceof ProtocolError
        ? "rejected"
        : "unavailable";
  return new ObserveDeliveryError(reason);
}
/** Freeze caller facts and allocate missing identity fields without a network request. */
export function prepareRunIdentity(
  options: RunIdentityOptions = {},
): RunIdentity {
  const attrs = jsonSnapshot(options.attributes ?? {}, true);
  return normalizeRunIdentity({
    runId: options.runId ?? uuid7(),
    startedAt: options.startedAt ?? new Date().toISOString(),
    attributes: attrs.value as Record<string, unknown>,
    attributesJson: attrs.text,
    workflowId: options.workflowId,
    workflowVersion: options.workflowVersion,
  });
}
/** Bounded in-memory delivery; accepted Events remain queued until acknowledged. */
export class BufferedObserveWriter {
  private pending: Pending[] = [];
  private records = 0;
  private bytes = 0;
  private closed = false;
  private closing = false;
  private closeOperation?: Promise<void>;
  private inFlight?: Promise<void>;
  private timer?: ReturnType<typeof setTimeout>;
  private ownedRuns = new Map<string, number>();
  private readonly limits: typeof defaults;
  private readonly write: (
    payload: import("./machine.ts").TelemetryPayload,
    options?: CallOptions,
  ) => Promise<void>;
  private readonly onClose?: () => void;
  private readonly assertWritable?: () => void;
  constructor(
    write: (
      payload: import("./machine.ts").TelemetryPayload,
      options?: CallOptions,
    ) => Promise<void>,
    limits: ObserveLimits = {},
    onClose?: () => void,
    assertWritable?: () => void,
  ) {
    this.write = write;
    this.onClose = onClose;
    this.assertWritable = assertWritable;
    this.limits = { ...defaults, ...limits };
    if (
      Object.values(this.limits).some(
        (x) => !Number.isSafeInteger(x) || x <= 0,
      ) ||
      this.limits.maxRequestRecords < 2 ||
      this.limits.maxRequestRecords > 1000 ||
      this.limits.maxRequestBytes > 4 * 1024 * 1024 ||
      this.limits.maxRecords > 1000 ||
      this.limits.maxBytes > 8 * 1024 * 1024 ||
      (this.limits.flushRecords > this.limits.maxRecords &&
        this.limits.flushBytes > this.limits.maxBytes)
    )
      throw new ConfigurationError("invalid_limit");
  }
  admitRun(run: RunIdentity): void {
    if (this.ownedRuns.has(run.runId)) return;
    const size = new TextEncoder().encode(JSON.stringify(run)).byteLength;
    if (this.ownedRuns.size >= this.limits.maxRecords)
      throw new ObserveQueueFullError("runs");
    if (this.records + this.ownedRuns.size + 1 > this.limits.maxRecords)
      throw new ObserveQueueFullError("records");
    if (this.bytes + size > this.limits.maxBytes)
      throw new ObserveQueueFullError("bytes");
    this.ownedRuns.set(run.runId, size);
    this.bytes += size;
  }
  releaseRun(runId: string): void {
    const size = this.ownedRuns.get(runId);
    if (size !== undefined) {
      this.ownedRuns.delete(runId);
      this.bytes -= size;
    }
  }
  submit(run: RunIdentity, event: EventWrite): void {
    this.assertWritable?.();
    if (this.closed || this.closing)
      throw new ObserveRunCompletedError("run_completed");
    if (event.run_id !== run.runId)
      throw new RangeError("invalid SDK configuration");
    const snap = jsonSnapshot(normalizeEvent(event)).text;
    const bytes = new TextEncoder().encode(snap).byteLength;
    if (!this.ownedRuns.has(run.runId)) this.admitRun(run);
    if (this.records + this.ownedRuns.size + 1 > this.limits.maxRecords)
      throw new ObserveQueueFullError("records");
    if (this.bytes + bytes > this.limits.maxBytes)
      throw new ObserveQueueFullError("bytes");
    const copied = JSON.parse(snap) as EventWrite;
    this.pending.push({ run, event: copied, bytes });
    this.records++;
    this.bytes += bytes;
    if (
      this.records >= this.limits.flushRecords ||
      this.bytes >= this.limits.flushBytes
    )
      void this.flush().catch(() => {});
    else if (!this.timer)
      this.timer = setTimeout(() => {
        this.timer = undefined;
        void this.flush().catch(() => {});
      }, this.limits.flushIntervalMs);
  }
  /** Send pending Events; failed delivery retains them for an explicit retry. */
  async flush(options?: CallOptions): Promise<void> {
    if (this.closing || this.closed)
      throw new ObserveRunCompletedError("run_completed");
    return this.flushInternal(options);
  }
  private async flushInternal(options?: CallOptions): Promise<void> {
    if (this.inFlight) return this.inFlight;
    const operation = this.flushNow(cleanupOptions(options));
    this.inFlight = operation;
    try {
      await operation;
    } finally {
      if (this.inFlight === operation) this.inFlight = undefined;
    }
  }
  private async flushNow(options?: CallOptions): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    while (this.pending.length) {
      const batch: Pending[] = [];
      let events = 0;
      for (const item of this.pending) {
        const proposedBatch = [...batch, item];
        const proposedRuns = [
          ...new Map(proposedBatch.map((x) => [x.run.runId, x.run])).values(),
        ].map((x) => runWrite(x));
        const proposed = {
          runs: proposedRuns,
          events: proposedBatch.map((x) => x.event),
        };
        const encoded = new TextEncoder().encode(
          JSON.stringify(proposed),
        ).byteLength;
        if (
          batch.length &&
          (proposedRuns.length + proposedBatch.length >
            this.limits.maxRequestRecords ||
            encoded > this.limits.maxRequestBytes)
        )
          break;
        if (
          !batch.length &&
          (proposedRuns.length + proposedBatch.length >
            this.limits.maxRequestRecords ||
            encoded > this.limits.maxRequestBytes)
        )
          throw new ObserveQueueFullError("bytes");
        batch.push(item);
        events++;
      }
      if (!batch.length) throw new ObserveQueueFullError("records");
      const runs = [
        ...new Map(batch.map((x) => [x.run.runId, x.run])).values(),
      ].map((x) => runWrite(x));
      const payload = { runs, events: batch.map((x) => x.event) };
      // Recheck exact encoded envelope including repeated Run rows.
      if (
        new TextEncoder().encode(JSON.stringify(payload)).byteLength >
          this.limits.maxRequestBytes ||
        runs.length + batch.length > this.limits.maxRequestRecords
      )
        throw new ObserveQueueFullError("bytes");
      try {
        await this.write(payload, options);
      } catch (error) {
        throw deliveryError(error);
      }
      this.pending.splice(0, batch.length);
      this.records -= batch.length;
      this.bytes -= batch.reduce((n, x) => n + x.bytes, 0);
    }
  }
  /** Drain before closing; a failed drain retains queued Events for retry. */
  close(options?: CallOptions): Promise<void> {
    if (this.closeOperation) return this.closeOperation;
    if (this.closed) return Promise.resolve();
    this.closing = true;
    const operation = (async () => {
      try {
        await this.flushInternal(cleanupOptions(options));
        this.closed = true;
        this.ownedRuns.clear();
        this.records = 0;
        this.bytes = 0;
        this.onClose?.();
      } catch (error) {
        this.closing = false;
        throw error;
      }
    })();
    this.closeOperation = operation;
    void operation.catch(() => {
      if (this.closeOperation === operation) this.closeOperation = undefined;
    });
    return operation;
  }
  get isClosed(): boolean {
    return this.closed;
  }
}

/** Own Event submission and completion for one acknowledged Run identity. */
export class GovernedRun {
  private state:
    | "active"
    | "suspending"
    | "suspended"
    | "closing"
    | "completed" = "active";
  private transition?: Promise<unknown>;
  private transitionKind?: "suspend" | "close";
  private completionAt?: string;
  private cleanupError?: unknown;
  readonly identity: RunIdentity;
  private readonly writer: BufferedObserveWriter;
  private readonly write: (
    payload: import("./machine.ts").TelemetryPayload,
    options?: CallOptions,
  ) => Promise<void>;
  private readonly assertOpen: () => void;
  private readonly release: (run: GovernedRun, completed: boolean) => void;
  private readonly markCompletionAttempt: () => void;
  constructor(
    identity: RunIdentity,
    writer: BufferedObserveWriter,
    write: (
      payload: import("./machine.ts").TelemetryPayload,
      options?: CallOptions,
    ) => Promise<void>,
    assertOpen: () => void,
    release: (run: GovernedRun, completed: boolean) => void,
    markCompletionAttempt: () => void = () => {},
  ) {
    this.identity = identity;
    this.writer = writer;
    this.write = write;
    this.assertOpen = assertOpen;
    this.release = release;
    this.markCompletionAttempt = markCompletionAttempt;
  }
  get runId(): string {
    return this.identity.runId;
  }
  /** Snapshot and queue an Event, returning its ID; queue admission is not acknowledgement. */
  submitEvent(event: GovernedEventInput): string {
    this.assertOpen();
    if (this.state !== "active")
      throw new ObserveRunCompletedError("run_completed");
    const id = requireUuid7(event.id ?? uuid7());
    const { input, output, error, attributes, ...fields } = event;
    const submitted = {
      ...fields,
      id,
      run_id: this.runId,
      ...(Object.hasOwn(event, "input")
        ? { input_json: jsonSnapshot(input).text }
        : {}),
      ...(Object.hasOwn(event, "output")
        ? { output_json: jsonSnapshot(output).text }
        : {}),
      ...(Object.hasOwn(event, "error")
        ? { error_json: jsonSnapshot(error).text }
        : {}),
      ...(Object.hasOwn(event, "attributes")
        ? { attributes_json: jsonSnapshot(attributes, true).text }
        : {}),
    } as EventWrite;
    this.writer.submit(this.identity, submitted);
    return id;
  }
  /** Flush this client-shared writer without completing the Run. */
  flush(options?: CallOptions): Promise<void> {
    this.assertOpen();
    if (this.state !== "active")
      throw new ObserveRunCompletedError("run_completed");
    return this.writer.flush(options);
  }
  /** Flush and release this handle without completing the Run; caller owns safe resumption. */
  suspend(options?: CallOptions): Promise<RunCheckpoint> {
    this.assertOpen();
    if (this.transition)
      return this.transitionKind === "suspend"
        ? (this.transition as Promise<RunCheckpoint>)
        : Promise.reject(new ObserveRunCompletedError("run_completed"));
    if (this.state !== "active")
      throw new ObserveRunCompletedError("run_completed");
    this.state = "suspending";
    const op = (async () => {
      try {
        await this.writer.flush(cleanupOptions(options));
        this.writer.releaseRun(this.runId);
        this.state = "suspended";
        this.release(this, false);
        return Object.freeze({
          version: 1 as const,
          state: "suspended" as const,
          ...this.identity,
        });
      } catch (error) {
        this.state = "active";
        throw error;
      }
    })();
    this.transition = op;
    this.transitionKind = "suspend";
    return op.finally(() => {
      if (this.transition === op) {
        this.transition = undefined;
        this.transitionKind = undefined;
      }
    });
  }
  /** Flush Events then complete with a retained timestamp; retry on this handle before client shutdown. */
  close(options?: CallOptions): Promise<void> {
    if (this.transition)
      return this.transitionKind === "close"
        ? (this.transition as Promise<void>)
        : Promise.reject(new ObserveRunCompletedError("run_completed"));
    if (this.state === "completed")
      return this.cleanupError === undefined
        ? Promise.resolve()
        : Promise.reject(this.cleanupError);
    if (this.state !== "active" && this.state !== "closing")
      throw new ObserveRunCompletedError("run_completed");
    if (this.state === "active") {
      this.state = "closing";
      this.completionAt ??= [
        rfc3339Micros(new Date().toISOString()),
        this.identity.startedAt,
      ]
        .sort()
        .at(-1)!;
      this.markCompletionAttempt();
    }
    const op = (async () => {
      await this.writer.flush(cleanupOptions(options));
      try {
        await this.write(
          {
            runs: [
              {
                ...runWrite(this.identity),
                status: "completed",
                ended_at: this.completionAt,
              },
            ],
          },
          cleanupOptions(options),
        );
      } catch (error) {
        throw deliveryError(error);
      }
      this.state = "completed";
      try {
        this.writer.releaseRun(this.runId);
        this.release(this, true);
      } catch (error) {
        this.cleanupError = error;
        throw error;
      }
    })();
    this.transition = op;
    this.transitionKind = "close";
    return op.finally(() => {
      if (this.transition === op) {
        this.transition = undefined;
        this.transitionKind = undefined;
      }
    });
  }
}
