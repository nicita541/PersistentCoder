import * as fs from "fs";
import * as path from "path";
import * as readline from "readline";
import * as vscode from "vscode";

import {
    ChildProcessWithoutNullStreams,
    spawn
} from "child_process";

import {
    randomUUID
} from "crypto";

import {
    BackendMessage,
    makeInspectRequest,
    makeProjectActionRequest,
    makeRunRequest,
    parseBackendMessage,
    ProjectAction,
    PROTOCOL_VERSION,
    WorkMode
} from "./protocol";


export type BackendStatus =
    | "stopped"
    | "starting"
    | "ready"
    | "error";


export interface BackendStatusEvent {
    status: BackendStatus;
    message: string;
}


export class PersistentCoderProcess
    implements vscode.Disposable
{
    private child:
        | ChildProcessWithoutNullStreams
        | undefined;

    private lines:
        | readline.Interface
        | undefined;

    private stopping = false;

    private cancelling = false;

    private currentStatus:
        BackendStatus = "stopped";


    private readonly messageEmitter =
        new vscode.EventEmitter<
            BackendMessage
        >();


    private readonly statusEmitter =
        new vscode.EventEmitter<
            BackendStatusEvent
        >();


    public readonly onMessage =
        this.messageEmitter.event;


    public readonly onStatus =
        this.statusEmitter.event;


    public constructor(
        private readonly backendRoot: string,
        private readonly output:
            vscode.OutputChannel
    ) {}


    public get status():
        BackendStatus
    {
        return this.currentStatus;
    }


    private setStatus(
        status: BackendStatus,
        message: string
    ): void {
        this.currentStatus =
            status;

        this.statusEmitter.fire({
            status,
            message
        });
    }


    private pythonExecutable():
        string
    {
        const windowsPython =
            path.join(
                this.backendRoot,
                ".venv",
                "Scripts",
                "python.exe"
            );

        const posixPython =
            path.join(
                this.backendRoot,
                ".venv",
                "bin",
                "python"
            );

        const candidate =
            process.platform === "win32"
                ? windowsPython
                : posixPython;

        if (!fs.existsSync(candidate)) {
            throw new Error(
                "PersistentCoder Python not found: " +
                candidate
            );
        }

        return candidate;
    }


    public start(): void {
        if (this.child) {
            return;
        }

        this.stopping =
            false;

        this.setStatus(
            "starting",
            "Запуск локального backend..."
        );

        const python =
            this.pythonExecutable();

        this.output.appendLine(
            `[backend] root: ${this.backendRoot}`
        );

        this.output.appendLine(
            `[backend] python: ${python}`
        );

        this.child = spawn(
            python,
            [
                "-u",
                "-m",
                "app.vscode_backend"
            ],
            {
                cwd:
                    this.backendRoot,

                shell:
                    false,

                windowsHide:
                    true,

                env: {
                    ...process.env,
                    PYTHONUNBUFFERED:
                        "1"
                }
            }
        );


        this.lines =
            readline.createInterface({
                input:
                    this.child.stdout
            });


        this.lines.on(
            "line",
            (line) => {
                const trimmed =
                    line.trim();

                if (!trimmed) {
                    return;
                }

                try {
                    const message =
                        parseBackendMessage(
                            trimmed
                        );

                    if (
                        message.type ===
                        "ready"
                    ) {
                        if (
                            message.protocol_version
                            !== PROTOCOL_VERSION
                        ) {
                            this.setStatus(
                                "error",
                                "Несовместимая версия backend protocol."
                            );

                            return;
                        }

                        this.setStatus(
                            "ready",
                            "Local"
                        );
                    }

                    this.messageEmitter.fire(
                        message
                    );

                } catch (error) {
                    const message =
                        error instanceof Error
                            ? error.message
                            : String(error);

                    this.output.appendLine(
                        "[backend stdout parse error] " +
                        message
                    );

                    this.output.appendLine(
                        trimmed
                    );

                    this.setStatus(
                        "error",
                        "Ошибка протокола backend."
                    );
                }
            }
        );


        this.child.stderr.on(
            "data",
            (chunk: Buffer) => {
                this.output.append(
                    chunk.toString(
                        "utf8"
                    )
                );
            }
        );


        this.child.on(
            "error",
            (error) => {
                this.output.appendLine(
                    "[backend process error] " +
                    error.message
                );

                this.setStatus(
                    "error",
                    error.message
                );
            }
        );


        this.child.on(
            "exit",
            (
                code,
                signal
            ) => {
                this.output.appendLine(
                    `[backend] exit code=${String(code)} signal=${String(signal)}`
                );

                this.lines?.close();

                this.lines =
                    undefined;

                this.child =
                    undefined;

                if (this.stopping) {
                    this.setStatus(
                        "stopped",
                        "Backend остановлен."
                    );

                    return;
                }

                if (this.cancelling) {
                    this.cancelling = false;
                    this.setStatus(
                        "starting",
                        "Операция отменена. Восстанавливаем backend..."
                    );

                    try {
                        this.start();
                    } catch (error) {
                        this.setStatus(
                            "error",
                            error instanceof Error
                                ? error.message
                                : String(error)
                        );
                    }

                    return;
                }

                this.setStatus(
                    "error",
                    `Backend завершился неожиданно (code=${String(code)}).`
                );
            }
        );
    }


    private write(
        payload: string
    ): void {
        if (
            !this.child ||
            !this.child.stdin.writable
        ) {
            throw new Error(
                "PersistentCoder backend is not running."
            );
        }

        this.child.stdin.write(
            payload + "\n",
            "utf8"
        );
    }


    public run(
    request: string,
    projectRoot: string,
    workMode: WorkMode
    ): string {
        if (
            this.currentStatus !==
            "ready"
        ) {
            throw new Error(
                "PersistentCoder backend ещё не готов."
            );
        }

        const requestId =
            randomUUID();

        this.write(
            makeRunRequest(
                requestId,
                request,
                projectRoot,
                workMode
            )
        );

        return requestId;
    }

    public projectAction(
        action: ProjectAction,
        projectRoot: string
    ): string {
        if (this.currentStatus !== "ready") {
            throw new Error(
                "PersistentCoder backend ещё не готов."
            );
        }

        const requestId = randomUUID();
        this.write(
            makeProjectActionRequest(
                requestId,
                action,
                projectRoot
            )
        );
        return requestId;
    }

    public inspect(projectRoot: string): string {
        if (this.currentStatus !== "ready") {
            throw new Error(
                "PersistentCoder backend ещё не готов."
            );
        }

        const requestId = randomUUID();
        this.write(
            makeInspectRequest(
                requestId,
                projectRoot
            )
        );
        return requestId;
    }

    public cancelCurrentOperation(): boolean {
        const child = this.child;
        if (!child || child.exitCode !== null) {
            return false;
        }

        this.cancelling = true;
        this.setStatus(
            "starting",
            "Отмена операции..."
        );

        if (!child.kill()) {
            this.cancelling = false;
            throw new Error(
                "Не удалось остановить текущую операцию."
            );
        }

        return true;
    }

    public dispose(): void {
        this.stopping =
            true;

        this.cancelling =
            false;

        if (
            this.child &&
            this.child.stdin.writable
        ) {
            try {
                this.child.stdin.write(
                    JSON.stringify({
                        type: "shutdown"
                    }) + "\n"
                );
            } catch {
                // Extension is shutting down.
            }
        }

        const child =
            this.child;

        if (child) {
            setTimeout(
                () => {
                    if (
                        child.exitCode ===
                        null
                    ) {
                        child.kill();
                    }
                },
                500
            );
        }

        this.lines?.close();

        this.messageEmitter.dispose();
        this.statusEmitter.dispose();
    }
}
