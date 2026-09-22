export const PROTOCOL_VERSION = 4;


export type WorkMode =
    | "sandbox"
    | "auto_apply";


export interface AutoApplyResult {
    attempted: boolean;
    applied: boolean;
    files: string[];
    reason: string | null;
}


export interface RunVerification {
    ok: boolean;
    status: string;
    reason: string;

    criteria: Array<{
        criterion: string;
        status: string;
        check: string;
        reason: string;
        evidence: string[];
    }>;
}


export interface ChangeEntry {
    path: string;
    operation: string;
    before_size: number | null;
    after_size: number | null;
    reviewable: boolean;
    apply_safe: boolean;
    reasons: string[];
}


export type ProjectAction =
    | "apply"
    | "discard";


export interface ActionResult {
    action: ProjectAction;
    ok: boolean;
    workflow_status: string;
    message: string;
    files: string[];
}


export interface RunResult {
    run_id: number | null;

    phase: string;

    workflow_status: string;

    message: string;

    next_actions: string[];

    plan_id: number | null;

    global_goal: string | null;

    completion: string | null;

    patch_path: string | null;

    manifest_id: string | null;

    can_apply: boolean | null;

    apply_block_reason: string | null;

    change_entries: ChangeEntry[];

    read_files: string[];

    changed_files: string[];

    commands: Array<{
        command: string;
        returncode: number;
    }>;

    verification:
        | RunVerification
        | null;

    repair:
        | {
              required: boolean;
              action: string | null;
              scope: string | null;
              reason: string | null;
          }
        | null;

    work_mode: WorkMode;

    auto_apply: AutoApplyResult;
}


export type BackendMessage =
    | {
          type: "ready";
          protocol_version: number;
          pid: number;
      }
    | {
          type: "pong";
          request_id: string;
      }
    | {
          type: "run_started";
          request_id: string;
      }
    | {
          type: "run_completed";
          request_id: string;
          result: RunResult;
      }
    | {
          type: "project_state";
          request_id: string;
          result: RunResult;
      }
    | {
          type: "run_failed";
          request_id: string;
          error: string;
      }
    | {
          type: "action_started";
          request_id: string;
          action: ProjectAction;
      }
    | {
          type: "action_completed";
          request_id: string;
          result: ActionResult;
      }
    | {
          type: "action_failed";
          request_id: string;
          action: ProjectAction;
          error: string;
      }
    | {
          type: "protocol_error";
          request_id?: string | null;
          error: string;
      }
    | {
          type: "shutdown_complete";
      };


function isRecord(
    value: unknown
): value is Record<string, unknown> {
    return (
        typeof value === "object" &&
        value !== null &&
        !Array.isArray(value)
    );
}


function readString(
    value: unknown,
    fallback = ""
): string {
    return (
        typeof value === "string"
            ? value
            : fallback
    );
}


function readNullableString(
    value: unknown
): string | null {
    return (
        typeof value === "string"
            ? value
            : null
    );
}


function readNullableNumber(
    value: unknown
): number | null {
    return (
        typeof value === "number" &&
        Number.isFinite(value)
            ? value
            : null
    );
}


function readStringArray(
    value: unknown
): string[] {
    if (!Array.isArray(value)) {
        return [];
    }

    return value.filter(
        (item): item is string =>
            typeof item === "string"
    );
}


function readWorkMode(
    value: unknown
): WorkMode {
    if (
        value === "sandbox" ||
        value === "auto_apply"
    ) {
        return value;
    }

    throw new Error(
        "Invalid work_mode from backend"
    );
}


function readProjectAction(
    value: unknown
): ProjectAction {
    if (
        value === "apply" ||
        value === "discard"
    ) {
        return value;
    }

    throw new Error(
        "Invalid project action from backend"
    );
}


function parseAutoApply(
    value: unknown
): AutoApplyResult {
    if (!isRecord(value)) {
        throw new Error(
            "Invalid auto_apply result"
        );
    }

    return {
        attempted:
            value.attempted === true,

        applied:
            value.applied === true,

        files:
            readStringArray(
                value.files
            ),

        reason:
            readNullableString(
                value.reason
            )
    };
}


function parseActionResult(
    value: unknown
): ActionResult {
    if (!isRecord(value)) {
        throw new Error(
            "Invalid action result"
        );
    }

    return {
        action:
            readProjectAction(value.action),
        ok:
            value.ok === true,
        workflow_status:
            readString(value.workflow_status),
        message:
            readString(value.message),
        files:
            readStringArray(value.files)
    };
}


function parseRunResult(
    value: unknown
): RunResult {
    if (!isRecord(value)) {
        throw new Error(
            "run_completed.result must be an object"
        );
    }

    let verification:
        | RunVerification
        | null = null;

    if (
        value.verification !== null &&
        isRecord(value.verification)
    ) {
        const rawCriteria =
            Array.isArray(
                value.verification.criteria
            )
                ? value.verification.criteria
                : [];

        verification = {
            ok:
                value.verification.ok === true,

            status:
                readString(
                    value.verification.status
                ),

            reason:
                readString(
                    value.verification.reason
                ),

            criteria:
                rawCriteria
                    .filter(isRecord)
                    .map(
                        (criterion) => ({
                            criterion:
                                readString(
                                    criterion.criterion
                                ),

                            status:
                                readString(
                                    criterion.status
                                ),

                            check:
                                readString(
                                    criterion.check
                                ),

                            reason:
                                readString(
                                    criterion.reason
                                ),

                            evidence:
                                readStringArray(
                                    criterion.evidence
                                )
                        })
                    )
        };
    }

    const commands =
        Array.isArray(value.commands)
            ? value.commands
            : [];

    const changeEntries =
        Array.isArray(value.change_entries)
            ? value.change_entries
                  .filter(isRecord)
                  .map(
                      (entry) => ({
                          path:
                              readString(entry.path),
                          operation:
                              readString(entry.operation),
                          before_size:
                              readNullableNumber(entry.before_size),
                          after_size:
                              readNullableNumber(entry.after_size),
                          reviewable:
                              entry.reviewable === true,
                          apply_safe:
                              entry.apply_safe === true,
                          reasons:
                              readStringArray(entry.reasons)
                      })
                  )
            : [];

    return {
        run_id:
            readNullableNumber(
                value.run_id
            ),

        phase:
            readString(
                value.phase,
                "UNKNOWN"
            ),

        workflow_status:
            readString(
                value.workflow_status,
                "failed"
            ),

        message:
            readString(
                value.message
            ),

        next_actions:
            readStringArray(
                value.next_actions
            ),

        plan_id:
            readNullableNumber(
                value.plan_id
            ),

        global_goal:
            readNullableString(
                value.global_goal
            ),

        completion:
            readNullableString(
                value.completion
            ),

        patch_path:
            readNullableString(
                value.patch_path
            ),

        manifest_id:
            readNullableString(
                value.manifest_id
            ),

        can_apply:
            typeof value.can_apply === "boolean"
                ? value.can_apply
                : null,

        apply_block_reason:
            readNullableString(
                value.apply_block_reason
            ),

        change_entries:
            changeEntries,

        read_files:
            readStringArray(
                value.read_files
            ),

        changed_files:
            readStringArray(
                value.changed_files
            ),

        commands:
            commands
                .filter(isRecord)
                .map(
                    (command) => ({
                        command:
                            readString(
                                command.command
                            ),

                        returncode:
                            typeof command.returncode
                                === "number"
                                ? command.returncode
                                : -1
                    })
                ),

        verification,

        repair:
            isRecord(value.repair)
                ? {
                      required:
                          value.repair
                              .required === true,

                      action:
                          readNullableString(
                              value.repair.action
                          ),

                      scope:
                          readNullableString(
                              value.repair.scope
                          ),

                      reason:
                          readNullableString(
                              value.repair.reason
                          )
                  }
                : null,

        work_mode:
            readWorkMode(
                value.work_mode
            ),

        auto_apply:
            parseAutoApply(
                value.auto_apply
            )
    };
}


export function parseBackendMessage(
    line: string
): BackendMessage {
    const value:
        unknown = JSON.parse(line);

    if (!isRecord(value)) {
        throw new Error(
            "Backend message must be an object"
        );
    }

    const type =
        value.type;

    if (type === "ready") {
        if (
            typeof value.protocol_version !==
                "number" ||
            typeof value.pid !==
                "number"
        ) {
            throw new Error(
                "Invalid ready message"
            );
        }

        return {
            type,
            protocol_version:
                value.protocol_version,
            pid:
                value.pid
        };
    }

    if (type === "pong") {
        return {
            type,

            request_id:
                readString(
                    value.request_id
                )
        };
    }

    if (type === "run_started") {
        return {
            type,

            request_id:
                readString(
                    value.request_id
                )
        };
    }

    if (type === "run_completed") {
        return {
            type,

            request_id:
                readString(
                    value.request_id
                ),

            result:
                parseRunResult(
                    value.result
                )
        };
    }

    if (type === "run_failed") {
        return {
            type,

            request_id:
                readString(
                    value.request_id
                ),

            error:
                readString(
                    value.error,
                    "Unknown backend error"
                )
        };
    }

    if (type === "project_state") {
        return {
            type,
            request_id:
                readString(value.request_id),
            result:
                parseRunResult(value.result)
        };
    }

    if (type === "action_started") {
        return {
            type,
            request_id:
                readString(value.request_id),
            action:
                readProjectAction(value.action)
        };
    }

    if (type === "action_completed") {
        return {
            type,
            request_id:
                readString(value.request_id),
            result:
                parseActionResult(value.result)
        };
    }

    if (type === "action_failed") {
        return {
            type,
            request_id:
                readString(value.request_id),
            action:
                readProjectAction(value.action),
            error:
                readString(value.error, "Project action failed")
        };
    }

    if (type === "protocol_error") {
        return {
            type,

            request_id:
                readNullableString(
                    value.request_id
                ),

            error:
                readString(
                    value.error,
                    "Protocol error"
                )
        };
    }

    if (
        type ===
        "shutdown_complete"
    ) {
        return {
            type
        };
    }

    throw new Error(
        `Unknown backend message: ${String(type)}`
    );
}


export function makeRunRequest(
    requestId: string,
    request: string,
    projectRoot: string,
    workMode: WorkMode
): string {
    return JSON.stringify({
        type: "run",
        request_id: requestId,
        request,
        project_root: projectRoot,
        work_mode: workMode
    });
}


export function makePingRequest(
    requestId: string
): string {
    return JSON.stringify({
        type: "ping",
        request_id: requestId
    });
}


export function makeInspectRequest(
    requestId: string,
    projectRoot: string
): string {
    return JSON.stringify({
        type: "inspect",
        request_id: requestId,
        project_root: projectRoot
    });
}


export function makeProjectActionRequest(
    requestId: string,
    action: ProjectAction,
    projectRoot: string
): string {
    return JSON.stringify({
        type: action,
        request_id: requestId,
        project_root: projectRoot
    });
}
