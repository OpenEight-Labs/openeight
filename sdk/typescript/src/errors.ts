import { governanceReason } from "./connect-errors.ts";

export type ConfigurationReason =
  | "missing_base_url"
  | "invalid_base_url"
  | "missing_api_key"
  | "invalid_api_key"
  | "invalid_agent_integration"
  | "missing_agent_name"
  | "invalid_agent_name"
  | "invalid_auth"
  | "invalid_timeout"
  | "invalid_json"
  | "invalid_timestamp"
  | "invalid_uuid"
  | "invalid_tool_registration"
  | "invalid_input"
  | "invalid_limit"
  | "legacy_option"
  | "forbidden_selector"
  | "client_closed"
  | "client_busy"
  | "invalid_run"
  | "invalid_event";

export class SDKError extends Error {
  constructor(message = "SDK operation failed") {
    super(message);
    this.name = "SDKError";
  }
}

export class ConfigurationError extends SDKError {
  readonly reason: ConfigurationReason;
  constructor(reason: ConfigurationReason) {
    super("invalid SDK configuration");
    this.name = "ConfigurationError";
    this.reason = reason;
  }
}
export class TransportError extends SDKError {
  readonly reason: "timeout" | "canceled" | "unavailable";
  constructor(reason: "timeout" | "canceled" | "unavailable") {
    super("request unavailable");
    this.name = "TransportError";
    this.reason = reason;
  }
}
export class ConnectError extends SDKError {
  readonly status: number;
  readonly code: ConnectCode;
  readonly reason?: string;
  constructor(status: number, code: ConnectCode, reason?: string) {
    super("request failed");
    this.name = "ConnectError";
    this.status = status;
    this.code = code;
    this.reason = reason;
  }
}
export type ConnectCode =
  | "canceled"
  | "unknown"
  | "invalid_argument"
  | "deadline_exceeded"
  | "not_found"
  | "already_exists"
  | "permission_denied"
  | "resource_exhausted"
  | "failed_precondition"
  | "aborted"
  | "out_of_range"
  | "unavailable"
  | "data_loss"
  | "unauthenticated"
  | "internal";
const connectCodes = new Set<ConnectCode>([
  "canceled",
  "unknown",
  "invalid_argument",
  "deadline_exceeded",
  "not_found",
  "already_exists",
  "permission_denied",
  "resource_exhausted",
  "failed_precondition",
  "aborted",
  "out_of_range",
  "unavailable",
  "data_loss",
  "unauthenticated",
  "internal",
]);
export class ProtocolError extends SDKError {
  readonly status?: number;
  constructor(status?: number) {
    super("invalid server response");
    this.name = "ProtocolError";
    this.status = status;
  }
}
export class ObserveQueueFullError extends SDKError {
  readonly reason: "records" | "bytes" | "runs";
  constructor(reason: "records" | "bytes" | "runs") {
    super("Observe queue is full");
    this.name = "ObserveQueueFullError";
    this.reason = reason;
  }
}
export class ObserveDeliveryError extends SDKError {
  readonly reason: "timeout" | "rejected" | "unavailable" | "busy";
  constructor(reason: "timeout" | "rejected" | "unavailable" | "busy") {
    super("Observe delivery failed");
    this.name = "ObserveDeliveryError";
    this.reason = reason;
  }
}
export class ObserveRunCompletedError extends SDKError {
  readonly reason: "run_completed";
  constructor(reason: "run_completed") {
    super("Observe Run is completed");
    this.name = "ObserveRunCompletedError";
    this.reason = reason;
  }
}
export function connectError(status: number, value: unknown): ConnectError {
  const envelope =
    typeof value === "object" && value !== null
      ? (value as { code?: unknown; details?: unknown })
      : {};
  const code =
    typeof envelope.code === "string" &&
    connectCodes.has(envelope.code as ConnectCode)
      ? (envelope.code as ConnectCode)
      : "unknown";
  return new ConnectError(
    status,
    code,
    governanceReason(status, code, envelope.details),
  );
}
