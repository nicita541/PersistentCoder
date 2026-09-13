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


const backendStatusDot =
    document.getElementById(
        "backendStatusDot"
    );


const backendStatusText =
    document.getElementById(
        "backendStatusText"
    );


let busy =
    false;


/* =========================================
   HELPERS
========================================= */

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
        .replaceAll(
            "&",
            "&amp;"
        )
        .replaceAll(
            "<",
            "&lt;"
        )
        .replaceAll(
            ">",
            "&gt;"
        )
        .replaceAll(
            '"',
            "&quot;"
        )
        .replaceAll(
            "'",
            "&#039;"
        );
}


function setBusy(value) {
    busy =
        value;

    input.disabled =
        value;

    sendButton.disabled =
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


/* =========================================
   USER MESSAGE
========================================= */

function addUserMessage(
    text
) {
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


/* =========================================
   AGENT STATUS
========================================= */

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


/* =========================================
   REAL FINAL RESULT
========================================= */

function addRunResult(
    result
) {
    hideWelcome();


    const card =
        document.createElement(
            "section"
        );


    const verification =
        result.verification;


    const verificationStatus =
        verification
            ? verification.status
            : "N/A";


    const success =
        result.phase === "DONE" &&
        verification &&
        verification.ok === true;


    card.className =
        `agent-card ${
            success
                ? "done"
                : "error"
        }`;


    const changedFiles =
        Array.isArray(
            result.changed_files
        )
            ? result.changed_files
            : [];


    const readFiles =
        Array.isArray(
            result.read_files
        )
            ? result.read_files
            : [];


    const patch =
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


    card.innerHTML = `
        <div class="agent-card-header">

            <div class="agent-card-icon">
                ${success ? "✓" : "!"}
            </div>

            <div class="agent-card-content">

                <div class="agent-card-title">
                    ${
                        success
                            ? "Task completed"
                            : "Task finished"
                    }
                </div>

                <div class="agent-card-description">
                    Реальный результат AgentRuntime
                </div>

            </div>

        </div>


        <div class="run-result">

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

            ${patch}

        </div>
    `;


    messages.appendChild(
        card
    );


    scrollToBottom();
}


/* =========================================
   SEND
========================================= */

function sendMessage() {
    if (busy) {
        return;
    }


    const text =
        input.value.trim();


    if (!text) {
        return;
    }


    addUserMessage(
        text
    );


    vscode.postMessage({
        type:
            "sendMessage",

        text
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


/* =========================================
   CHAT
========================================= */

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


/* =========================================
   DOM EVENTS
========================================= */

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


/* =========================================
   BACKEND MESSAGES
========================================= */

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
                "PersistentCoder получил задачу. Детальный live-stream подключим на следующем этапе."
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