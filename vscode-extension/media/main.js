const vscode =
    acquireVsCodeApi();


const messages =
    document.getElementById(
        "messages"
    );

const welcome =
    document.getElementById(
        "welcome"
    );

const input =
    document.getElementById(
        "messageInput"
    );

const sendButton =
    document.getElementById(
        "sendButton"
    );

const newChatButton =
    document.getElementById(
        "newChatButton"
    );

const settingsButton =
    document.getElementById(
        "settingsButton"
    );

const attachButton =
    document.getElementById(
        "attachButton"
    );

const workModeSelect =
    document.getElementById(
        "workModeSelect"
    );

const backendStatusDot =
    document.getElementById(
        "backendStatusDot"
    );

const backendStatusText =
    document.getElementById(
        "backendStatusText"
    );


let busy = false;


/* HELPERS */

function hideWelcome() {
    if (welcome) {
        welcome.style.display =
            "none";
    }
}


function scrollToBottom() {
    messages.scrollTop =
        messages.scrollHeight;
}


function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function setBusy(value) {
    busy = value;

    input.disabled =
        value;

    sendButton.disabled =
        false;

    workModeSelect.disabled =
        value;

    sendButton.textContent =
        value
            ? "■"
            : "↑";

    sendButton.title =
        value
            ? "Отменить текущую операцию"
            : "Отправить";
}


function updateBackendStatus(
    status,
    text
) {
    backendStatusText.textContent =
        text;

    backendStatusDot.className =
        "status-dot " + status;
}


/* USER MESSAGE */

function addUserMessage(text) {
    hideWelcome();

    const row =
        document.createElement(
            "div"
        );

    row.className =
        "message-row user-row";

    const bubble =
        document.createElement(
            "div"
        );

    bubble.className =
        "user-message";

    bubble.textContent =
        text;

    row.appendChild(
        bubble
    );

    messages.appendChild(
        row
    );

    scrollToBottom();
}


/* AGENT STATUS */

function addAgentStatus(
    status,
    title,
    description
) {
    hideWelcome();

    const card =
        document.createElement(
            "section"
        );

    card.className =
        `agent-card ${status}`;

    const icon =
        status === "done"
            ? "✓"
            : status === "error"
                ? "!"
                : "●";

    card.innerHTML = `
        <div class="agent-card-header">

            <div class="agent-card-icon">
                ${icon}
            </div>

            <div class="agent-card-content">

                <div class="agent-card-title">
                    ${escapeHtml(title)}
                </div>

                <div class="agent-card-description">
                    ${escapeHtml(description)}
                </div>

            </div>

        </div>
    `;

    messages.appendChild(
        card
    );

    scrollToBottom();
}


/* RESULT */

function addRunResult(result) {
    hideWelcome();

    const verification =
        result.verification;

    const verificationOk =
        Boolean(
            verification &&
            verification.ok === true
        );

    const verificationStatus =
        verification
            ? verification.status
            : "N/A";

    const workflowStatus =
        result.workflow_status ||
        (result.phase === "DONE" ? "verified" : "failed");

    const workflowMessage =
        result.message ||
        "AgentRuntime finished";

    const mode =
        result.work_mode ===
            "auto_apply"
            ? "auto_apply"
            : "sandbox";

    const autoApply =
        result.auto_apply || {
            attempted: false,
            applied: false,
            files: [],
            reason: null
        };

    const baseSuccess =
        result.phase === "DONE" &&
        verificationOk;

    const finalSuccess =
        workflowStatus === "verified" ||
        workflowStatus === "applied";

    let title =
        "Task finished";

    if (
        finalSuccess &&
        mode === "sandbox"
    ) {
        title =
            "Task completed in Sandbox";
    }

    if (
        finalSuccess &&
        mode === "auto_apply"
    ) {
        title =
            "Task completed and applied";
    }

    if (
        baseSuccess &&
        mode === "auto_apply" &&
        !autoApply.applied
    ) {
        title =
            "Verification passed, apply failed";
    }

    const card =
        document.createElement(
            "section"
        );

    card.className =
        `agent-card ${
            finalSuccess
                ? "done"
                : "error"
        }`;

    const readFiles =
        Array.isArray(
            result.read_files
        )
            ? result.read_files
            : [];

    const changedFiles =
        Array.isArray(
            result.changed_files
        )
            ? result.changed_files
            : [];

    const changeEntries =
        Array.isArray(result.change_entries)
            ? result.change_entries
            : [];

    const changesHtml =
        changeEntries.length > 0
            ? `
                <div class="result-note">
                    ${changeEntries
                        .map((entry) => {
                            const reasons =
                                Array.isArray(entry.reasons) && entry.reasons.length > 0
                                    ? ` — ${entry.reasons.join(", ")}`
                                    : "";
                            const blocked = entry.apply_safe === true ? "" : " [BLOCKED]";
                            return `<div><code>${escapeHtml(
                                `${entry.operation} ${entry.path}${blocked}${reasons}`
                            )}</code></div>`;
                        })
                        .join("")}
                </div>
              `
            : "";

    const manifestHtml =
        result.manifest_id
            ? `
                <div class="result-row">
                    <span>Manifest</span>
                    <code>${escapeHtml(result.manifest_id)}</code>
                </div>
              `
            : "";

    const nextActions =
        Array.isArray(result.next_actions)
            ? result.next_actions
            : [];

    const actionsHtml =
        nextActions.length > 0
            ? `
                <div class="result-actions">
                    ${nextActions
                        .filter((action) => action === "apply" || action === "discard")
                        .map(
                            (action) => `
                                <button
                                    type="button"
                                    class="result-action ${action}"
                                    data-project-action="${action}"
                                >
                                    ${action === "apply" ? "Apply" : "Discard"}
                                </button>
                            `
                        )
                        .join("")}
                </div>
              `
            : "";

    const patchHtml =
        result.patch_path
            ? `
                <div class="result-row">
                    <span>Patch</span>

                    <code>
                        ${escapeHtml(
                            result.patch_path
                        )}
                    </code>
                </div>
              `
            : "";

    const verificationReason =
        verification &&
        verification.reason
            ? `
                <div class="result-reason">
                    ${escapeHtml(
                        verification.reason
                    )}
                </div>
              `
            : "";

    let modeResultHtml = "";

    if (mode === "sandbox") {
        modeResultHtml = `
            <div class="result-row">
                <span>Project</span>
                <strong>
                    Not applied
                </strong>
            </div>

            <div class="result-note">
                Изменения остались в sandbox.
                Реальный проект не изменён.
            </div>
        `;
    }

    if (mode === "auto_apply") {
        modeResultHtml = `
            <div class="result-row">
                <span>Applied</span>

                <strong>
                    ${
                        autoApply.applied
                            ? "YES"
                            : "NO"
                    }
                </strong>
            </div>

            ${
                autoApply.reason
                    ? `
                        <div class="result-note">
                            ${escapeHtml(
                                autoApply.reason
                            )}
                        </div>
                      `
                    : ""
            }
        `;
    }

    card.innerHTML = `
        <div class="agent-card-header">

            <div class="agent-card-icon">
                ${finalSuccess ? "✓" : "!"}
            </div>

            <div class="agent-card-content">

                <div class="agent-card-title">
                    ${escapeHtml(title)}
                </div>

                <div class="agent-card-description">
                    ${escapeHtml(workflowMessage)}
                </div>

            </div>

        </div>


        <div class="run-result">

            <div class="result-row">
                <span>Status</span>

                <strong>
                    ${escapeHtml(workflowStatus)}
                </strong>
            </div>


            <div class="result-row">
                <span>Mode</span>

                <strong>
                    ${
                        mode === "auto_apply"
                            ? "Direct"
                            : "Sandbox"
                    }
                </strong>
            </div>


            <div class="result-row">
                <span>Phase</span>

                <strong>
                    ${escapeHtml(
                        result.phase
                    )}
                </strong>
            </div>


            <div class="result-row">
                <span>Plan</span>

                <strong>
                    ${
                        result.plan_id ??
                        "—"
                    }
                </strong>
            </div>


            <div class="result-row">
                <span>Read files</span>

                <strong>
                    ${readFiles.length}
                </strong>
            </div>


            <div class="result-row">
                <span>Changed files</span>

                <strong>
                    ${changedFiles.length}
                </strong>
            </div>


            <div class="result-row">
                <span>Verification</span>

                <strong>
                    ${escapeHtml(
                        verificationStatus
                    )}
                </strong>
            </div>


            ${verificationReason}

            ${modeResultHtml}

            ${manifestHtml}

            ${changesHtml}

            ${patchHtml}

            ${actionsHtml}

        </div>
    `;

    card
        .querySelectorAll("[data-project-action]")
        .forEach((button) => {
            button.addEventListener("click", () => {
                const action = button.dataset.projectAction;
                if (
                    action !== "apply" &&
                    action !== "discard"
                ) {
                    return;
                }
                if (!window.confirm(`${action === "apply" ? "Apply" : "Discard"} verified changes?`)) {
                    return;
                }
                vscode.postMessage({type: action});
            });
        });

    messages.appendChild(
        card
    );

    scrollToBottom();
}


/* SEND */

function sendMessage() {
    if (busy) {
        vscode.postMessage({
            type: "cancelOperation"
        });
        return;
    }

    const text =
        input.value.trim();

    if (!text) {
        return;
    }

    const workMode =
        workModeSelect.value;

    addUserMessage(
        text
    );

    vscode.postMessage({
        type:
            "sendMessage",

        text,

        workMode
    });

    input.value =
        "";

    resizeInput();

    input.focus();
}


function resizeInput() {
    input.style.height =
        "auto";

    input.style.height =
        `${Math.min(
            input.scrollHeight,
            180
        )}px`;
}


/* CHAT */

function resetChat() {
    if (busy) {
        return;
    }

    messages.innerHTML =
        "";

    if (welcome) {
        welcome.style.display =
            "flex";

        messages.appendChild(
            welcome
        );
    }

    input.value =
        "";

    resizeInput();

    input.focus();
}


/* DOM */

sendButton.addEventListener(
    "click",
    sendMessage
);


input.addEventListener(
    "input",
    resizeInput
);


input.addEventListener(
    "keydown",
    (event) => {
        if (
            event.key ===
                "Enter" &&
            !event.shiftKey
        ) {
            event.preventDefault();

            sendMessage();
        }
    }
);


newChatButton.addEventListener(
    "click",
    resetChat
);


settingsButton.addEventListener(
    "click",
    () => {
        vscode.postMessage({
            type:
                "settings"
        });
    }
);


attachButton.addEventListener(
    "click",
    () => {
        addAgentStatus(
            "thinking",
            "Контекст проекта",
            "Выбор конкретных файлов подключим позже."
        );
    }
);


document
    .querySelectorAll(
        ".suggestion"
    )
    .forEach(
        (button) => {
            button.addEventListener(
                "click",
                () => {
                    input.value =
                        button.dataset
                            .prompt || "";

                    resizeInput();

                    input.focus();
                }
            );
        }
    );


/* BACKEND */

window.addEventListener(
    "message",
    (event) => {
        const message =
            event.data;

        if (
            message.type ===
            "backendStatus"
        ) {
            updateBackendStatus(
                message.status,
                message.text
            );

            return;
        }

        if (
            message.type ===
            "runStarted"
        ) {
            setBusy(
                true
            );

            addAgentStatus(
                "thinking",
                "AgentRuntime работает",
                "PersistentCoder выполняет задачу в изолированном sandbox."
            );

            return;
        }

        if (
            message.type ===
            "runCompleted"
        ) {
            setBusy(
                false
            );

            addRunResult(
                message.result
            );

            return;
        }

        if (message.type === "projectState") {
            setBusy(false);
            if (
                message.result &&
                message.result.workflow_status !== "idle"
            ) {
                addAgentStatus(
                    "thinking",
                    "Восстановлена предыдущая сессия",
                    message.result.message || "Project state recovered."
                );
                addRunResult(message.result);
            }
            return;
        }

        if (message.type === "actionStarted") {
            setBusy(true);
            document
                .querySelectorAll("[data-project-action]")
                .forEach((button) => {
                    button.disabled = true;
                });
            addAgentStatus(
                "thinking",
                message.action === "apply" ? "Applying changes" : "Discarding changes",
                "PersistentCoder is updating the durable project session."
            );
            return;
        }

        if (message.type === "actionCompleted") {
            setBusy(false);
            const result = message.result || {};
            addAgentStatus(
                result.ok === true ? "done" : "error",
                result.workflow_status || "completed",
                result.message || "Project action completed."
            );
            return;
        }

        if (
            message.type ===
            "runFailed"
        ) {
            setBusy(
                false
            );

            addAgentStatus(
                "error",
                "Ошибка",
                message.error ||
                    "Неизвестная ошибка backend."
            );

            return;
        }

        if (message.type === "operationCancelled") {
            setBusy(false);
            addAgentStatus(
                "error",
                "Операция отменена",
                message.message || "Backend перезапускается."
            );
        }
    }
);


resizeInput();
