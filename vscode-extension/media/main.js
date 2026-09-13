const vscode = acquireVsCodeApi();

const messages =
    document.getElementById("messages");

const welcome =
    document.getElementById("welcome");

const input =
    document.getElementById("messageInput");

const sendButton =
    document.getElementById("sendButton");

const newChatButton =
    document.getElementById("newChatButton");

const settingsButton =
    document.getElementById("settingsButton");

const attachButton =
    document.getElementById("attachButton");

let demoGeneration = 0;


/* =========================================
   GENERAL
========================================= */

function hideWelcome() {
    if (welcome) {
        welcome.style.display = "none";
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


function wait(ms) {
    return new Promise(
        (resolve) => setTimeout(resolve, ms)
    );
}


/* =========================================
   USER MESSAGE
========================================= */

function addUserMessage(text) {
    hideWelcome();

    const row =
        document.createElement("div");

    row.className =
        "message-row user-row";

    const bubble =
        document.createElement("div");

    bubble.className =
        "user-message";

    bubble.textContent = text;

    row.appendChild(bubble);
    messages.appendChild(row);

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
        document.createElement("div");

    card.className =
        `agent-card ${status}`;

    const icon =
        status === "done"
            ? "✓"
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

    messages.appendChild(card);

    scrollToBottom();
}


/* =========================================
   PLAN
========================================= */

function addPlan(tasks) {
    hideWelcome();

    const card =
        document.createElement("section");

    card.className =
        "plan-card";

    const header =
        document.createElement("div");

    header.className =
        "plan-header";

    header.innerHTML = `
        <div class="plan-header-left">
            <span class="plan-symbol">◇</span>
            <span>PLAN</span>
        </div>

        <span class="plan-count">
            ${tasks.length} tasks
        </span>
    `;

    card.appendChild(header);

    const list =
        document.createElement("div");

    list.className =
        "plan-list";

    tasks.forEach(
        (task, index) => {
            const item =
                document.createElement("div");

            item.className =
                `plan-item ${task.status}`;

            let icon = "○";

            if (task.status === "done") {
                icon = "✓";
            }

            if (task.status === "active") {
                icon = "›";
            }

            item.innerHTML = `
                <span class="plan-status">
                    ${icon}
                </span>

                <div class="plan-task-body">
                    <span class="plan-title">
                        ${escapeHtml(task.title)}
                    </span>

                    <span class="plan-number">
                        ${index + 1}
                    </span>
                </div>
            `;

            list.appendChild(item);
        }
    );

    card.appendChild(list);

    messages.appendChild(card);

    scrollToBottom();
}


/* =========================================
   TOOL CARDS
========================================= */

const toolIcons = {
    search: "⌕",
    read: "↗",
    edit: "✎",
    terminal: ">_",
    verification: "✓"
};


const toolLabels = {
    search: "Search",
    read: "Read",
    edit: "Edit",
    terminal: "Terminal",
    verification: "Verification"
};


function addToolCard({
    type,
    title,
    subtitle = "",
    detail = "",
    state = "done",
    badges = []
}) {
    hideWelcome();

    const card =
        document.createElement("section");

    card.className =
        `tool-card tool-${type} ${state}`;

    const badgesHtml =
        badges
            .map(
                (badge) => `
                    <span class="
                        tool-badge
                        ${escapeHtml(
                            badge.kind || ""
                        )}
                    ">
                        ${escapeHtml(badge.text)}
                    </span>
                `
            )
            .join("");

    const hasDetail =
        Boolean(detail);

    card.innerHTML = `
        <button
            class="tool-card-header"
            type="button"
        >

            <div class="tool-main">

                <div class="tool-icon">
                    ${toolIcons[type] || "•"}
                </div>

                <div class="tool-info">

                    <div class="tool-name-row">

                        <span class="tool-name">
                            ${toolLabels[type] || type}
                        </span>

                        <div class="tool-badges">
                            ${badgesHtml}
                        </div>

                    </div>

                    <div class="tool-title">
                        ${escapeHtml(title)}
                    </div>

                    ${
                        subtitle
                            ? `
                                <div class="tool-subtitle">
                                    ${escapeHtml(subtitle)}
                                </div>
                              `
                            : ""
                    }

                </div>

            </div>

            ${
                hasDetail
                    ? `
                        <span class="tool-chevron">
                            ›
                        </span>
                      `
                    : ""
            }

        </button>

        ${
            hasDetail
                ? `
                    <div class="tool-detail">
                        <pre>${escapeHtml(detail)}</pre>
                    </div>
                  `
                : ""
        }
    `;

    if (hasDetail) {
        const header =
            card.querySelector(
                ".tool-card-header"
            );

        header.addEventListener(
            "click",
            () => {
                card.classList.toggle(
                    "expanded"
                );
            }
        );
    }

    messages.appendChild(card);

    scrollToBottom();

    return card;
}


/* =========================================
   DEMO SEQUENCE
========================================= */

async function runDemoToolSequence() {
    const generation =
        ++demoGeneration;

    await wait(500);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "search",
        title: "LoginService",
        subtitle:
            "Поиск символа по проекту",
        state: "done",
        badges: [
            {
                text: "3 results"
            }
        ],
        detail:
`app/auth/service.py:18
tests/test_auth.py:24
app/api/login.py:11`
    });


    await wait(650);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "read",
        title: "app/auth/service.py",
        subtitle:
            "Прочитан существующий файл",
        state: "done",
        badges: [
            {
                text: "142 lines"
            }
        ],
        detail:
`class LoginService:
    def authenticate(self, user, password):
        ...`
    });


    await wait(600);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "read",
        title: "tests/test_auth.py",
        subtitle:
            "Найдены связанные тесты",
        state: "done",
        badges: [
            {
                text: "tests"
            }
        ]
    });


    await wait(700);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "edit",
        title: "app/auth/service.py",
        subtitle:
            "Обновлена логика авторизации",
        state: "done",
        badges: [
            {
                text: "+12",
                kind: "positive"
            },
            {
                text: "-4",
                kind: "negative"
            }
        ],
        detail:
`@@ LoginService.authenticate

- old validation
+ corrected validation
+ regression handling`
    });


    await wait(650);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "edit",
        title: "tests/test_auth.py",
        subtitle:
            "Добавлен regression test",
        state: "done",
        badges: [
            {
                text: "+18",
                kind: "positive"
            }
        ]
    });


    await wait(700);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "terminal",
        title:
            "python -m pytest -q tests/test_auth.py",
        subtitle:
            "Команда выполнена в sandbox",
        state: "done",
        badges: [
            {
                text: "exit 0",
                kind: "positive"
            }
        ],
        detail:
`..............
14 passed in 1.28s`
    });


    await wait(650);

    if (generation !== demoGeneration) {
        return;
    }

    addToolCard({
        type: "verification",
        title: "Verification PASS",
        subtitle:
            "Все success criteria подтверждены",
        state: "success",
        badges: [
            {
                text: "14 passed",
                kind: "positive"
            }
        ],
        detail:
`✓ Existing file was read before edit
✓ Python syntax valid
✓ Regression test added
✓ Related pytest tests passed`
    });
}


/* =========================================
   SEND MESSAGE
========================================= */

function sendMessage() {
    const text =
        input.value.trim();

    if (!text) {
        return;
    }

    demoGeneration += 1;

    addUserMessage(text);

    vscode.postMessage({
        type: "sendMessage",
        text
    });

    input.value = "";

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
   NEW CHAT
========================================= */

function resetChat() {
    demoGeneration += 1;

    messages.innerHTML = "";

    if (welcome) {
        welcome.style.display =
            "flex";

        messages.appendChild(
            welcome
        );
    }

    input.value = "";

    resizeInput();

    input.focus();
}


/* =========================================
   EVENTS
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
            event.key === "Enter" &&
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
            type: "settings"
        });
    }
);


attachButton.addEventListener(
    "click",
    () => {
        addAgentStatus(
            "thinking",
            "Контекст проекта",
            "Позже здесь будет выбор файлов, папок и текущего редактора."
        );
    }
);


document
    .querySelectorAll(".suggestion")
    .forEach(
        (button) => {
            button.addEventListener(
                "click",
                () => {
                    input.value =
                        button.dataset.prompt ||
                        "";

                    resizeInput();

                    input.focus();
                }
            );
        }
    );


window.addEventListener(
    "message",
    (event) => {
        const message =
            event.data;

        if (
            message.type ===
            "agentStatus"
        ) {
            addAgentStatus(
                message.status,
                message.title,
                message.description
            );

            return;
        }

        if (
            message.type ===
            "demoPlan"
        ) {
            addPlan(
                message.tasks || []
            );

            runDemoToolSequence();
        }
    }
);


resizeInput();