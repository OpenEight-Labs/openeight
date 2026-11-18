import { freezeDeep } from "./immutable.ts";
import {
  normalizeEvent,
  correlationUuid,
  canonicalFields,
  runFields,
} from "./telemetry-validation.ts";
import { ConfigurationError, ProtocolError } from "./errors.ts";
import {
  Transport,
  type CallOptions,
  type TransportOptions,
} from "./transport.ts";
import {
  compareUTF8,
  jsonSnapshot,
  prepareDecision,
  rfc3339Micros,
  requireUuid7,
  ToolRegistration,
  workflowFields,
  encodedObject,
  sameJSON,
  type PreparedDecision,
} from "./values.ts";
import {
  BufferedObserveWriter,
  GovernedRun,
  prepareRunIdentity,
  type RunCheckpoint,
  type RunIdentity,
  type ObserveLimits,
  type RunIdentityOptions,
  normalizeRunIdentity,
  runWrite,
  exportRunCheckpoint,
  importRunCheckpoint,
} from "./run.ts";

export type AgentIntegration = "otel" | "langchain" | "deepagents" | "other";
export interface MachineClientOptions extends TransportOptions {
  apiKey: string;
  agentName: string;
  integration?: AgentIntegration;
  maxOwnedRuns?: number;
  observe?: ObserveLimits;
}
export interface DecisionResult {
  readonly decisionId: string;
  readonly runId: string;
  readonly verdict: "allow" | "deny" | "approval_required" | "dry_run_only";
  readonly reason: string;
  readonly replayed: boolean;
}
export interface ToolHandle {
  readonly governanceRevisionId?: string;
  readonly currentVersionId?: string;
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly capability: string;
  readonly governed: boolean;
  readonly connector?: string;
  readonly action?: string;
  readonly toolVersionId: string;
  readonly version: string;
  readonly contentDigest: string;
  readonly toolContractDigest?: string;
  readonly inputSchema: Readonly<Record<string, unknown>>;
  readonly readiness?: ToolReadiness;
  readonly intent?: string | null;
  readonly intentDrafting?: ToolIntentDrafting | null;
}
export interface ToolIntentDrafting {
  readonly status:
    | "not_required"
    | "waiting_classification"
    | "queued"
    | "running"
    | "failed"
    | "complete";
  readonly deadlineAt?: string;
  readonly errorCode?: string;
}
export interface ToolReadiness {
  readonly governanceRevisionId?: string;
  readonly name: string;
  readonly registrationStatus: string;
  readonly governed: boolean;
  readonly policyReady: boolean;
  readonly eligible: boolean;
  readonly reason: string;
  readonly toolId?: string;
  readonly toolVersionId?: string;
  readonly policyContractSetDigest?: string;
  readonly policyInputContracts: readonly PolicyInputContract[];
}
export interface RegistrationResult {
  readonly requestIndex: number;
  readonly status: string;
  readonly reason: string;
  readonly tool?: ToolHandle;
  readonly readiness?: ToolReadiness;
}
export interface AgentReadiness {
  readonly tools: readonly ToolReadiness[];
  readonly fullyRegistered: boolean;
  readonly registrationReason: string;
  readonly fullyPolicyReady: boolean;
  readonly policyReason: string;
}
function record(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v))
    throw new ProtocolError();
  return v as Record<string, unknown>;
}
function str(v: unknown): string {
  if (typeof v !== "string") throw new ProtocolError();
  return v;
}
function bool(v: unknown): boolean {
  if (typeof v !== "boolean") throw new ProtocolError();
  return v;
}
function protoStr(v: unknown): string {
  return v === undefined ? "" : str(v);
}
function protoBool(v: unknown): boolean {
  return v === undefined ? false : bool(v);
}
export interface PolicyInputContract {
  readonly policyId: string;
  readonly policyVersionId: string;
  readonly bindingId: string;
  readonly inputDigest: string;
  readonly fieldSelectors: readonly { path: string; alias?: string }[];
}
function policySelectors(
  value: unknown,
): readonly { path: string; alias?: string }[] {
  if (!Array.isArray(value) || value.length > 64) throw new ProtocolError();
  const fields = value.map((value) => {
    const f = record(value),
      path = str(f.path),
      alias = f.alias === undefined ? undefined : str(f.alias);
    if (
      Object.keys(f).some((key) => key !== "path" && key !== "alias") ||
      !path.trim() ||
      new TextEncoder().encode(path).byteLength > 256 ||
      path.includes("*") ||
      path.split(".").some((part) => !part) ||
      (alias !== undefined &&
        (new TextEncoder().encode(alias).byteLength > 256 ||
          alias.includes(".") ||
          (alias !== "" && !alias.trim())))
    )
      throw new ProtocolError();
    try {
      jsonSnapshot(f, true);
    } catch {
      throw new ProtocolError();
    }
    return { path, ...(alias !== undefined ? { alias } : {}) };
  });
  const sources = fields.map((field) => field.path),
    outputs = fields.map((field) => field.alias || field.path);
  if (
    new Set(sources).size !== sources.length ||
    new Set(outputs).size !== outputs.length ||
    outputs.some((left) =>
      outputs.some((right) => right.startsWith(left + ".")),
    )
  )
    throw new ProtocolError();
  return fields;
}
function readiness(v: unknown): ToolReadiness {
  const r = record(v),
    token = r.policy_contract_set_digest,
    revision = r.governance_revision_id ?? undefined;
  if (
    revision !== undefined &&
    (typeof revision !== "string" || !isUUID(revision))
  )
    throw new ProtocolError();
  if (
    token !== undefined &&
    (typeof token !== "string" || !/^[0-9a-f]{64}$/.test(token))
  )
    throw new ProtocolError();
  const contracts = r.policy_input_contracts ?? [];
  if (!Array.isArray(contracts) || contracts.length > 32)
    throw new ProtocolError();
  const requirements = contracts.map((value) => {
    const c = record(value),
      fields = c.field_selectors ?? [];
    if (
      !Array.isArray(fields) ||
      fields.length > 64 ||
      !isUUID(str(c.binding_id)) ||
      !isUUID(str(c.policy_id)) ||
      !isUUID(str(c.policy_version_id)) ||
      typeof c.input_digest !== "string" ||
      !/^[0-9a-f]{64}$/.test(c.input_digest)
    )
      throw new ProtocolError();
    return freezeDeep({
      bindingId: str(c.binding_id),
      policyId: str(c.policy_id),
      policyVersionId: str(c.policy_version_id),
      inputDigest: c.input_digest,
      fieldSelectors: policySelectors(fields),
    });
  });
  const result = {
    ...(revision !== undefined ? { governanceRevisionId: revision } : {}),
    name: str(r.name),
    registrationStatus: protoStr(r.registration_status),
    governed: protoBool(r.governed),
    policyReady: protoBool(r.policy_ready),
    eligible: protoBool(r.eligible),
    reason: protoStr(r.reason),
    ...(typeof r.tool_id === "string" ? { toolId: r.tool_id } : {}),
    ...(typeof r.tool_version_id === "string"
      ? { toolVersionId: r.tool_version_id }
      : {}),
    ...(typeof token === "string" ? { policyContractSetDigest: token } : {}),
    policyInputContracts: requirements,
  };
  if (
    (token !== undefined &&
      (!result.eligible ||
        !result.policyReady ||
        result.reason !== "ready" ||
        !result.governed ||
        !result.toolId ||
        !isUUID(result.toolId) ||
        !result.toolVersionId ||
        !isUUID(result.toolVersionId))) ||
    (token === undefined && requirements.length)
  )
    throw new ProtocolError();
  if (
    new Set(requirements.map((item) => item.bindingId)).size !==
      requirements.length ||
    new Set(requirements.map((item) => item.policyVersionId)).size !==
      requirements.length
  )
    throw new ProtocolError();
  return freezeDeep(result);
}
/** Copy the minimal native-path union required by policy; aliases are server-owned. */
export function collectSelectedValues(
  arguments_: Record<string, unknown>,
  contract: ToolReadiness,
): readonly { path: string; value: unknown }[] {
  if (
    !contract.eligible ||
    !contract.policyReady ||
    contract.reason !== "ready" ||
    !contract.policyContractSetDigest
  )
    throw new ConfigurationError("invalid_input");
  const paths = [
    ...new Set(
      contract.policyInputContracts.flatMap((item) =>
        item.fieldSelectors.map((field) => field.path),
      ),
    ),
  ].sort(compareUTF8);
  const result: { path: string; value: unknown }[] = [];
  for (const path of paths) {
    if (result.some((item) => path.startsWith(item.path + "."))) continue;
    let value: unknown = arguments_;
    let found = true;
    for (const part of path.split(".")) {
      if (
        !value ||
        typeof value !== "object" ||
        Array.isArray(value) ||
        !Object.hasOwn(value, part)
      ) {
        found = false;
        break;
      }
      value = (value as Record<string, unknown>)[part];
    }
    if (found) result.push({ path, value });
  }
  return freezeDeep(
    jsonSnapshot(result).value as { path: string; value: unknown }[],
  );
}

function intentDrafting(v: unknown): ToolIntentDrafting | null {
  if (v == null) return null;
  const d = record(v),
    status = d.status;
  if (
    typeof status !== "string" ||
    ![
      "not_required",
      "waiting_classification",
      "queued",
      "running",
      "failed",
      "complete",
    ].includes(status)
  )
    throw new ProtocolError();
  if (
    d.deadline_at != null &&
    (typeof d.deadline_at !== "string" ||
      !Number.isFinite(Date.parse(d.deadline_at)))
  )
    throw new ProtocolError();
  if (d.error_code != null && typeof d.error_code !== "string")
    throw new ProtocolError();
  return Object.freeze({
    status: status as ToolIntentDrafting["status"],
    ...(typeof d.deadline_at === "string" ? { deadlineAt: d.deadline_at } : {}),
    ...(typeof d.error_code === "string" ? { errorCode: d.error_code } : {}),
  });
}
function toolHandle(v: unknown, ready?: ToolReadiness): ToolHandle {
  const wrapper = record(v),
    tool = record(wrapper.tool),
    version = record(wrapper.tool_version);
  if ("field_selectors" in version || "policy_input_digest" in version)
    throw new ProtocolError();
  let schema: unknown;
  try {
    schema = JSON.parse(str(version.input_schema_json));
  } catch {
    throw new ProtocolError();
  }
  const digest = str(version.content_digest),
    versionName = str(version.version),
    id = str(tool.id),
    versionId = str(version.id),
    name = str(tool.name),
    description = str(tool.description),
    capability = str(tool.capability),
    governed = bool(tool.governed);
  if (
    !isUUID(id) ||
    !isUUID(versionId) ||
    !/^sha256:[0-9a-f]{64}$/.test(digest)
  )
    throw new ProtocolError();
  const current = tool.current_version_id;
  if (
    current !== undefined &&
    (typeof current !== "string" || !isUUID(current) || current !== versionId)
  )
    throw new ProtocolError();
  const revision = version.governance_revision_id;
  if (revision != null && (typeof revision !== "string" || !isUUID(revision)))
    throw new ProtocolError();
  const policy = version.tool_contract_digest;
  if (
    policy != null &&
    (typeof policy !== "string" || !/^sha256:[0-9a-f]{64}$/.test(policy))
  )
    throw new ProtocolError();
  if (
    version.tool_id !== id ||
    (policy != null && !governed) ||
    (policy == null &&
      ready &&
      (ready.eligible || ready.policyReady) &&
      !(
        capability === "read" &&
        !governed &&
        ready.reason === "read_only" &&
        ready.eligible &&
        ready.policyReady
      ))
  )
    throw new ProtocolError();
  if (!schema || typeof schema !== "object" || Array.isArray(schema))
    throw new ProtocolError();
  try {
    jsonSnapshot(schema, true);
  } catch {
    throw new ProtocolError();
  }
  const intent = version.intent ?? null;
  if (
    intent !== null &&
    (typeof intent !== "string" ||
      !intent.trim() ||
      intent.includes("\0") ||
      new TextEncoder().encode(intent).length > 2000)
  )
    throw new ProtocolError();
  const drafting = intentDrafting(version.intent_drafting);
  const connector = tool.connector,
    action = tool.action;
  if (
    (connector !== undefined && typeof connector !== "string") ||
    (action !== undefined && typeof action !== "string")
  )
    throw new ProtocolError();
  return Object.freeze({
    id,
    name,
    description,
    capability,
    governed,
    intent,
    intentDrafting: drafting,
    ...(typeof revision === "string" ? { governanceRevisionId: revision } : {}),
    ...(typeof current === "string" ? { currentVersionId: current } : {}),
    ...(typeof connector === "string" ? { connector } : {}),
    ...(typeof action === "string" ? { action } : {}),
    toolVersionId: versionId,
    version: versionName,
    contentDigest: digest,
    ...(typeof policy === "string" ? { toolContractDigest: policy } : {}),
    inputSchema: freezeDeep(schema as Record<string, unknown>),
    ...(ready ? { readiness: ready } : {}),
  });
}
function headers(
  key: string,
  name: string,
  integration?: AgentIntegration,
): Record<string, string> {
  return {
    Authorization: `ApiKey ${key}`,
    "X-OpenEight-Agent-Name": name,
    ...(integration === undefined
      ? {}
      : { "X-OpenEight-Agent-Integration": integration }),
  };
}
function encodeConnectBody(value: unknown): string {
  return JSON.stringify(value);
}
/** Machine-authenticated agent operations with owned Run, buffer and transport cleanup. */
export class MachineClient {
  readonly #transport: Transport;
  get baseUrl(): string {
    return this.#transport.baseUrl;
  }
  get defaultTimeout(): number {
    return this.#transport.defaultTimeout;
  }
  private readonly apiKey: string;
  private readonly agentName: string;
  private readonly integration?: AgentIntegration;
  private readonly runFacts = new Map<string, RunIdentity>();
  private readonly runs = new Map<string, Promise<GovernedRun>>();
  private readonly completedRuns = new Set<string>();
  private readonly completionAttempted = new Set<string>();
  private readonly suspendedRuns = new Set<string>();
  private readonly options: ObserveLimits;
  private readonly maxOwnedRuns: number;
  private readonly writer: BufferedObserveWriter;
  private readonly standalone = new Set<BufferedObserveWriter>();
  private closing = false;
  private closeOperation?: Promise<void>;
  constructor(options: MachineClientOptions) {
    if (!options || typeof options !== "object")
      throw new ConfigurationError("invalid_auth");
    const raw = options as MachineClientOptions & Record<string, unknown>;
    if (
      [
        "agentCredential",
        "agent_credential",
        "agent",
        "credential",
        "auth",
        "token",
        "organizationId",
        "organization_id",
        "workspaceId",
        "workspace_id",
        "agentId",
        "agent_id",
        "apiKeyId",
        "api_key_id",
        "policyId",
        "policy_id",
        "policyVersionId",
        "policy_version_id",
      ].some((key) => key in raw)
    )
      throw new ConfigurationError("legacy_option");
    this.#transport = new Transport(options);
    if (
      typeof options.apiKey !== "string" ||
      !options.apiKey.startsWith("ctk_") ||
      options.apiKey.length < 8
    )
      throw new ConfigurationError("invalid_api_key");
    if (
      typeof options.agentName !== "string" ||
      !/^[a-z0-9][a-z0-9._/-]{0,127}$/.test(options.agentName)
    )
      throw new ConfigurationError("invalid_agent_name");
    if (
      options.integration !== undefined &&
      !["otel", "langchain", "deepagents", "other"].includes(
        options.integration,
      )
    )
      throw new ConfigurationError("invalid_agent_integration");
    this.integration = options.integration;
    this.apiKey = options.apiKey;
    this.agentName = options.agentName;
    this.options = options.observe ?? {};
    this.maxOwnedRuns = options.maxOwnedRuns ?? 1000;
    if (
      !Number.isSafeInteger(this.maxOwnedRuns) ||
      this.maxOwnedRuns < 1 ||
      this.maxOwnedRuns > 1000
    )
      throw new ConfigurationError("invalid_limit");
    this.writer = new BufferedObserveWriter(
      (payload, callOptions) =>
        this.writeTelemetryInternal(payload, callOptions),
      this.options,
      undefined,
      () => this.assertOpen(),
    );
  }
  private assertOpen(): void {
    if (this.closing) throw new ConfigurationError("client_closed");
  }
  private machineCall<T>(
    path: string,
    body: unknown,
    options?: CallOptions,
  ): Promise<T> {
    return this.#transport.send<T>(
      path,
      {
        method: "POST",
        headers: {
          ...headers(this.apiKey, this.agentName, this.integration),
          "Content-Type": "application/json",
        },
        body: encodeConnectBody(body),
      },
      options,
    );
  }
  /** Publish schema facts; acknowledgement does not grant execution authority. */
  async registerTools(
    registrations: readonly ToolRegistration[],
    options?: CallOptions,
  ): Promise<readonly RegistrationResult[]> {
    this.assertOpen();
    if (
      !Array.isArray(registrations) ||
      registrations.length < 1 ||
      registrations.length > 200 ||
      new Set(registrations.map((t) => t.name)).size !== registrations.length ||
      registrations.some((t) => !(t instanceof ToolRegistration))
    )
      throw new ConfigurationError("invalid_tool_registration");
    const tools = registrations.map((t) => ({
      name: t.name,
      description: t.description,
      input_schema_json: jsonSnapshot(t.inputSchema, true).text,
      ...(t.expectedToolId !== undefined
        ? {
            expected_tool_id: t.expectedToolId,
            expected_current_version_id: t.expectedCurrentVersionId,
          }
        : {}),
    }));
    if (
      new TextEncoder().encode(JSON.stringify({ tools })).byteLength > 1048576
    )
      throw new ConfigurationError("invalid_tool_registration");
    const response = record(
      await this.machineCall(
        "/api/v1/openeight.observe.v1.ObserveService/RegisterTools",
        { tools },
        options,
      ),
    );
    if (
      !Array.isArray(response.results) ||
      response.results.length !== tools.length
    )
      throw new ProtocolError();
    return Object.freeze(
      response.results.map((item, i) => {
        const r = record(item);
        if (r.request_index !== i) throw new ProtocolError();
        const status = str(r.status),
          reason = protoStr(r.reason);
        if (!["registered", "replayed", "invalid", "conflict"].includes(status))
          throw new ProtocolError();
        const ready =
          r.readiness === undefined ? undefined : readiness(r.readiness);
        if (
          ready &&
          (ready.policyContractSetDigest !== undefined ||
            ready.policyInputContracts.length)
        )
          throw new ProtocolError();
        const hasTool = r.tool !== undefined || r.tool_version !== undefined;
        if (
          ["registered", "replayed"].includes(status)
            ? !r.tool || !r.tool_version
            : hasTool
        )
          throw new ProtocolError();
        const handle = hasTool
          ? toolHandle({ tool: r.tool, tool_version: r.tool_version }, ready)
          : undefined;
        return Object.freeze({
          requestIndex: i,
          status,
          reason,
          ...(handle ? { tool: handle } : {}),
          ...(ready ? { readiness: ready } : {}),
        });
      }),
    );
  }
  /** Fetch policy readiness for exact versions; an empty selection requests the catalog. */
  async getToolReadiness(
    toolVersionIds: readonly string[],
    options?: CallOptions,
  ): Promise<AgentReadiness> {
    this.assertOpen();
    if (
      !Array.isArray(toolVersionIds) ||
      toolVersionIds.length > 200 ||
      new Set(toolVersionIds).size !== toolVersionIds.length ||
      toolVersionIds.some((id) => !isUUID(id))
    )
      throw new ConfigurationError("invalid_input");
    const r = record(
      await this.machineCall(
        "/api/v1/openeight.observe.v1.ObserveService/GetToolReadiness",
        { tool_version_ids: [...toolVersionIds] },
        options,
      ),
    );
    const a = record(r.readiness);
    if (
      !Array.isArray(a.tools) ||
      new TextEncoder().encode(JSON.stringify(r)).byteLength > 4 * 1024 * 1024
    )
      throw new ProtocolError();
    const tools = a.tools.map(readiness);
    if (
      toolVersionIds.length &&
      (tools.length !== toolVersionIds.length ||
        new Set(tools.map((item) => item.toolVersionId)).size !==
          tools.length ||
        tools.some(
          (item) =>
            !item.toolVersionId || !toolVersionIds.includes(item.toolVersionId),
        ))
    )
      throw new ProtocolError();
    return Object.freeze({
      tools: Object.freeze(tools),
      fullyRegistered: protoBool(a.fully_registered),
      registrationReason: protoStr(a.registration_reason),
      fullyPolicyReady: protoBool(a.fully_policy_ready),
      policyReason: protoStr(a.policy_reason),
    });
  }
  /** Freeze one attempt for unchanged retry and approval replay; no network call. */
  prepareDecision(
    input: Parameters<typeof prepareDecision>[0],
  ): PreparedDecision {
    return prepareDecision(input);
  }
  /** Request a verdict; approval replay requires separately observed approved state. */
  async decide(
    prepared: PreparedDecision,
    options?: CallOptions,
  ): Promise<DecisionResult> {
    this.assertOpen();
    const stable = prepareDecision({
      id: prepared.id,
      runId: prepared.runId,
      sourceTime: prepared.sourceTime,
      toolId: prepared.toolId,
      toolVersionId: prepared.toolVersionId,
      toolName: prepared.toolName,
      connector: prepared.connector,
      action: prepared.action,
      input: prepared.input,
      policyContractSetDigest: prepared.policyContractSetDigest,
      selectedValues: prepared.selectedValues,
      workflowId: prepared.workflowId,
      workflowVersion: prepared.workflowVersion,
    });
    const body = {
      decision_id: stable.id,
      run_id: stable.runId,
      source_time: stable.sourceTime,
      tool_id: stable.toolId,
      tool_version_id: stable.toolVersionId,
      input_json: jsonSnapshot(stable.input, true).text,
      policy_contract_set_digest: stable.policyContractSetDigest,
      selected_values_json: jsonSnapshot(stable.selectedValues).text,
      ...workflowFields(stable.workflowId, stable.workflowVersion),
    };
    const r = record(
      await this.machineCall(
        "/api/v1/openeight.govern.v1.GovernService/Decide",
        body,
        options,
      ),
    );
    if (
      r.decision_id !== stable.id ||
      r.run_id !== stable.runId ||
      !["allow", "deny", "approval_required", "dry_run_only"].includes(
        String(r.verdict),
      ) ||
      typeof r.reason !== "string" ||
      typeof r.replayed !== "boolean"
    )
      throw new ProtocolError();
    return Object.freeze({
      decisionId: str(r.decision_id),
      runId: str(r.run_id),
      verdict: r.verdict as DecisionResult["verdict"],
      reason: str(r.reason),
      replayed: r.replayed,
    });
  }
  /** Read human approval state; replay the Decision before executing an approved attempt. */
  async approvalState(
    decisionId: string,
    options?: CallOptions,
  ): Promise<"pending" | "approved" | "rejected" | "expired" | "none"> {
    this.assertOpen();
    requireUuid7(decisionId);
    const r = record(
      await this.machineCall(
        "/api/v1/openeight.govern.v1.GovernService/GetApprovalState",
        { decision_id: decisionId },
        options,
      ),
    );
    if (
      !["pending", "approved", "rejected", "expired", "none"].includes(
        String(r.state),
      )
    )
      throw new ProtocolError();
    return r.state as "pending" | "approved" | "rejected" | "expired" | "none";
  }
  /** Send caller-owned Run/Event identities and require complete acknowledgements. */
  async writeTelemetry(
    payload: TelemetryPayload,
    options?: CallOptions,
  ): Promise<void> {
    this.assertOpen();
    return this.writeTelemetryInternal(payload, options);
  }
  private async writeTelemetryInternal(
    payload: TelemetryPayload,
    options?: CallOptions,
  ): Promise<void> {
    const stable = normalizeTelemetry(payload);
    if (
      new TextEncoder().encode(JSON.stringify(stable)).byteLength >
        4 * 1024 * 1024 ||
      (stable.runs?.length ?? 0) + (stable.events?.length ?? 0) > 1000
    )
      throw new ConfigurationError("invalid_limit");
    const response = record(
      await this.machineCall(
        "/api/v1/openeight.observe.v1.ObserveService/WriteTelemetry",
        stable,
        options,
      ),
    );
    checkAck(stable.runs ?? [], response.runs);
    checkAck(stable.events ?? [], response.events);
  }
  /** Capture a Run identity before sending; retain it until the outcome is known. */
  prepareRunIdentity(options: RunIdentityOptions = {}): RunIdentity {
    return prepareRunIdentity(options);
  }
  /** Acknowledge an active Run before returning its owned event buffer handle. */
  async governedRun(
    identity: RunIdentity = this.prepareRunIdentity(),
    options?: CallOptions,
  ): Promise<GovernedRun> {
    identity = normalizeRunIdentity(identity);
    if (this.closing) throw new ConfigurationError("client_closed");
    if (
      this.completedRuns.has(identity.runId) ||
      this.completionAttempted.has(identity.runId) ||
      this.suspendedRuns.has(identity.runId)
    )
      throw new ConfigurationError("invalid_run");
    const existing = this.runs.get(identity.runId);
    if (existing) {
      const retained = this.runFacts.get(identity.runId);
      if (!retained || !sameJSON(runWrite(retained), runWrite(identity)))
        throw new ConfigurationError("invalid_run");
      return existing;
    }
    return this.reserveRun(identity, options);
  }

  /** Resume a suspended Run under exclusive caller ownership, before completion is attempted. */
  async resumeGovernedRun(
    checkpoint: RunCheckpoint,
    options?: CallOptions,
  ): Promise<GovernedRun> {
    if (
      !checkpoint ||
      checkpoint.version !== 1 ||
      checkpoint.state !== "suspended" ||
      !checkpoint.runId ||
      !checkpoint.startedAt ||
      !checkpoint.attributes
    )
      throw new ConfigurationError("invalid_run");
    if (
      this.completedRuns.has(checkpoint.runId) ||
      this.completionAttempted.has(checkpoint.runId) ||
      this.runs.has(checkpoint.runId)
    )
      throw new ConfigurationError("invalid_run");
    const identity = normalizeRunIdentity(
      importRunCheckpoint(exportRunCheckpoint(checkpoint)),
    );
    const run = await this.reserveRun(identity, options);
    this.suspendedRuns.delete(identity.runId);
    return run;
  }
  private async reserveRun(
    identity: RunIdentity,
    options?: CallOptions,
  ): Promise<GovernedRun> {
    if (this.closing) throw new ConfigurationError("client_closed");
    if (this.runs.size >= this.maxOwnedRuns)
      throw new ConfigurationError("invalid_limit");
    this.writer.admitRun(identity);
    this.runFacts.set(identity.runId, identity);
    const reservation = Promise.resolve().then(async () => {
      await this.writeTelemetry({ runs: [runWrite(identity)] }, options);
      return new GovernedRun(
        identity,
        this.writer,
        (payload, callOptions) =>
          this.writeTelemetryInternal(payload, callOptions),
        () => this.assertOpen(),
        (item, completed) => {
          this.runs.delete(item.runId);
          this.runFacts.delete(item.runId);
          if (completed) this.completedRuns.add(item.runId);
          else this.suspendedRuns.add(item.runId);
        },
        () => this.completionAttempted.add(identity.runId),
      );
    });
    this.runs.set(identity.runId, reservation);
    try {
      return await reservation;
    } catch (error) {
      this.releaseFailedReservation(identity.runId, reservation);
      throw error;
    }
  }
  private releaseFailedReservation(
    id: string,
    reservation: Promise<GovernedRun>,
  ): void {
    const release = () => {
      this.writer.releaseRun(id);
      if (this.runs.get(id) === reservation) {
        this.runs.delete(id);
        this.runFacts.delete(id);
      }
    };
    if (this.#transport.hasOutstandingTransportWork())
      void this.#transport.whenTransportIdle().then(release);
    else release();
  }
  /** Create a client-owned in-memory buffer; close it to drain accepted Events. */
  bufferedObserve(): BufferedObserveWriter {
    if (this.closing) throw new ConfigurationError("client_closed");
    let writer: BufferedObserveWriter;
    writer = new BufferedObserveWriter(
      (payload, callOptions) =>
        this.writeTelemetryInternal(payload, callOptions),
      this.options,
      () => this.standalone.delete(writer),
      () => this.assertOpen(),
    );
    this.standalone.add(writer);
    return writer;
  }
  /** Attempt owned cleanup and close transport even on failure; retryable settlement is not guaranteed. */
  close(options?: CallOptions): Promise<void> {
    if (this.closeOperation) return this.closeOperation;
    this.closing = true;
    let first: unknown;
    const operation = (async () => {
      for (const promise of [...this.runs.values()]) {
        try {
          const run = await promise;
          await run.close(options);
        } catch (e) {
          first ??= e;
        }
      }
      for (const writer of [...this.standalone]) {
        try {
          await writer.close(options);
        } catch (e) {
          first ??= e;
        }
      }
      try {
        await this.writer.close(options);
      } catch (e) {
        first ??= e;
      }
      try {
        await this.#transport.closeTransport();
      } catch (e) {
        first ??= e;
      }
      if (first) throw first;
    })();
    this.closeOperation = operation;
    void operation.catch(() => {
      if (this.closeOperation === operation) this.closeOperation = undefined;
    });
    return operation;
  }
}

export interface RunWrite {
  id: string;
  status: "active" | "completed";
  started_at: string;
  ended_at?: string;
  attributes_json?: string;
  workflow_id?: string;
  workflow_version?: string;
  parent_run_id?: string;
}
export interface EventWrite {
  id: string;
  run_id: string;
  name: string;
  observation_kind: "span" | "llm_call" | "tool_call" | "lifecycle";
  span_role?: "graph" | "node" | "retriever" | "internal" | null;
  operation_kind: "unspecified" | "invoke" | "stream";
  occurred_at: string;
  outcome: "unspecified" | "success" | "error" | "interrupted";
  input_json?: string;
  output_json?: string;
  error_json?: string;
  attributes_json?: string;
  latency_ms?: string | null;
  ttft_ms?: string | null;
  input_tokens?: string;
  output_tokens?: string;
  cache_creation_input_tokens?: string;
  cache_read_input_tokens?: string;
  decision_id?: string;
  tool_id?: string;
  tool_version_id?: string;
  tool_name?: string;
  provider?: string;
  model?: string;
  parent_event_id?: string;
}
export interface TelemetryPayload {
  runs?: readonly RunWrite[];
  events?: readonly EventWrite[];
}
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
function isUUID(v: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
    v,
  );
}
function checkForbidden(row: object): void {
  for (const key of Object.keys(row))
    if (forbidden.has(key)) throw new ConfigurationError("forbidden_selector");
}
function normalizeTelemetry(p: TelemetryPayload): TelemetryPayload {
  if (
    !p ||
    typeof p !== "object" ||
    Array.isArray(p) ||
    (p.runs !== undefined && !Array.isArray(p.runs)) ||
    (p.events !== undefined && !Array.isArray(p.events))
  )
    throw new ConfigurationError("invalid_event");
  const runs = (p.runs ?? []).map((raw) => {
    const input = canonicalFields(raw, runFields);
    workflowFields(input.workflow_id, input.workflow_version);
    if (input.parent_run_id !== undefined) correlationUuid(input.parent_run_id);
    checkForbidden(input);
    requireUuid7(input.id);
    if (input.status !== "active" && input.status !== "completed")
      throw new ConfigurationError("invalid_run");
    const run = {
      ...input,
      started_at: rfc3339Micros(input.started_at),
      ...(input.ended_at ? { ended_at: rfc3339Micros(input.ended_at) } : {}),
      attributes_json: input.attributes_json ?? "{}",
    };
    if (
      (run.status === "completed" && !run.ended_at) ||
      (run.status === "active" && run.ended_at)
    )
      throw new ConfigurationError("invalid_run");
    encodedObject(run.attributes_json);
    if (run.ended_at && run.ended_at < run.started_at)
      throw new ConfigurationError("invalid_run");
    return run;
  });
  const events = (p.events ?? []).map(normalizeEvent);
  return { runs, events };
}
function checkAck(sent: readonly { id: string }[], got: unknown): void {
  if (!Array.isArray(got)) throw new ProtocolError();
  const expected = new Set(sent.map((x) => x.id));
  const seen = new Set<string>();
  for (const row of got) {
    const id = record(row).id;
    if (typeof id !== "string" || !expected.has(id) || seen.has(id))
      throw new ProtocolError();
    seen.add(id);
  }
  if (seen.size !== expected.size) throw new ProtocolError();
}
