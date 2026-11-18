import {
  ConfigurationError,
  ConnectError,
  ProtocolError,
  TransportError,
  connectError,
} from "./errors.ts";

export interface TransportOptions {
  baseUrl: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
  closeTimeoutMs?: number;
}
/** Per-request cancellation and timeout in milliseconds; abort does not prove no effect. */
export interface CallOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}
function origin(value: string): string {
  let u: URL;
  try {
    u = new URL(value);
  } catch {
    throw new ConfigurationError("invalid_base_url");
  }
  if (
    !/^https?:$/.test(u.protocol) ||
    !u.hostname ||
    u.username ||
    u.password ||
    u.search ||
    u.hash ||
    u.pathname !== "/" ||
    /\s/.test(value)
  )
    throw new ConfigurationError("invalid_base_url");
  if (
    u.protocol !== "https:" &&
    !(
      u.hostname === "localhost" ||
      u.hostname === "127.0.0.1" ||
      u.hostname === "[::1]"
    )
  )
    throw new ConfigurationError("invalid_base_url");
  return u.origin;
}
/** Origin-bound HTTP transport with deadlines, redirect rejection and bounded cleanup. */
export class Transport {
  readonly baseUrl: string;
  protected readonly fetchImpl: typeof fetch;
  readonly defaultTimeout: number;
  private readonly closeTimeout: number;
  private closed = false;
  private active = new Set<AbortController>();
  private pending = new Set<Promise<unknown>>();
  private idleWaiters = new Set<() => void>();
  constructor(options: TransportOptions) {
    if (!options || typeof options !== "object")
      throw new ConfigurationError("missing_base_url");
    this.baseUrl = origin(options.baseUrl);
    this.fetchImpl = options.fetch ?? globalThis.fetch?.bind(globalThis);
    this.defaultTimeout = options.timeoutMs ?? 30_000;
    if (!Number.isFinite(this.defaultTimeout) || this.defaultTimeout <= 0)
      throw new ConfigurationError("invalid_timeout");
    this.closeTimeout = options.closeTimeoutMs ?? 10_000;
    if (!Number.isFinite(this.closeTimeout) || this.closeTimeout <= 0)
      throw new ConfigurationError("invalid_timeout");
    if (typeof this.fetchImpl !== "function")
      throw new ConfigurationError("invalid_auth");
  }
  async send<T>(
    path: string,
    init: RequestInit,
    options: CallOptions = {},
  ): Promise<T> {
    if (this.closed) throw new ConfigurationError("client_closed");
    if (
      !path.startsWith("/") ||
      path.startsWith("//") ||
      /^[a-z]+:/i.test(path)
    )
      throw new ConfigurationError("invalid_base_url");
    const timeout = options.timeoutMs ?? this.defaultTimeout;
    if (!Number.isFinite(timeout) || timeout <= 0)
      throw new ConfigurationError("invalid_timeout");
    const ctl = new AbortController();
    this.active.add(ctl);
    let rejectDeadline: (reason: unknown) => void = () => {};
    let rejectCancel: (reason: unknown) => void = () => {};
    const deadline = new Promise<never>((_, reject) => {
      rejectDeadline = reject;
    });
    const canceled = new Promise<never>((_, reject) => {
      rejectCancel = reject;
    });
    const timer = setTimeout(() => {
      ctl.abort("deadline");
      rejectDeadline(new TransportError("timeout"));
    }, timeout);
    const external = options.signal;
    const onAbort = () => {
      ctl.abort("caller");
      rejectCancel(new TransportError("canceled"));
    };
    external?.addEventListener("abort", onAbort, { once: true });
    if (external?.aborted) onAbort();
    const task = (async () => {
      if (ctl.signal.aborted)
        throw new TransportError(external?.aborted ? "canceled" : "timeout");
      const headers = new Headers(init.headers);
      const response = await this.fetchImpl(this.baseUrl + path, {
        ...init,
        headers,
        signal: ctl.signal,
        redirect: "manual",
      });
      if (ctl.signal.aborted)
        throw new TransportError(external?.aborted ? "canceled" : "timeout");
      if (
        response.type === "opaqueredirect" ||
        (response.status >= 300 && response.status < 400)
      )
        throw new TransportError("unavailable");
      if (response.status === 204) return undefined as T;
      let body: unknown;
      try {
        const text = await response.text();
        body = text === "" ? undefined : JSON.parse(text);
      } catch {
        if (!response.ok) throw new ConnectError(response.status, "unknown");
        throw new ProtocolError(response.status);
      }
      if (!response.ok) {
        throw connectError(response.status, body);
      }
      return body as T;
    })().catch((error) => {
      if (
        error instanceof ConnectError ||
        error instanceof ProtocolError ||
        error instanceof TransportError ||
        error instanceof ConfigurationError
      )
        throw error;
      if (ctl.signal.aborted)
        throw new TransportError(external?.aborted ? "canceled" : "timeout");
      throw new TransportError("unavailable");
    });
    this.pending.add(task);
    void task
      .finally(() => {
        this.pending.delete(task);
        this.active.delete(ctl);
        if (this.pending.size === 0) {
          for (const resolve of this.idleWaiters) resolve();
          this.idleWaiters.clear();
        }
      })
      .catch(() => {});
    try {
      return await Promise.race([task, deadline, canceled]);
    } finally {
      clearTimeout(timer);
      external?.removeEventListener("abort", onAbort);
    }
  }
  /** Abort owned calls and wait for settlement; report client_busy if cleanup times out. */
  async closeTransport(): Promise<void> {
    this.closed = true;
    for (const ctl of this.active) ctl.abort("caller");
    const pending = [...this.pending];
    if (!pending.length) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timed = new Promise<"busy">((resolve) => {
      timer = setTimeout(() => resolve("busy"), this.closeTimeout);
    });
    const outcome = await Promise.race([
      Promise.allSettled(pending).then(() => "done" as const),
      timed,
    ]);
    if (timer) clearTimeout(timer);
    if (outcome === "busy") throw new ConfigurationError("client_busy");
  }
  hasOutstandingTransportWork(): boolean {
    return this.pending.size > 0;
  }
  whenTransportIdle(): Promise<void> {
    if (this.pending.size === 0) return Promise.resolve();
    return new Promise((resolve) => this.idleWaiters.add(resolve));
  }
}

/** Require an origin without credentials or path; allow plain HTTP only on loopback. */
export function validateBaseUrl(value: string): string {
  return origin(value);
}
