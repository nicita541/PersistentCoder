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
        value;

    workModeSelect.disabled =
        value;

    sendButton.textContent =
        value
            ? "…"
            : "↑";
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
        baseSuccess &&
        (
            mode === "sandbox" ||
            autoApply.applied === true
        );

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
                    Реальный результат AgentRuntime
                </div>

            </div>

        </div>


        <div class="run-result">

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

            ${patchHtml}

        </div>
    `;

    messages.appendChild(
        card
    );

    scrollToBottom();
}


/* SEND */

function sendMessage() {
    if (busy) {
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
        }
    }
);


resizeInput();