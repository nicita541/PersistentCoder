"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.PersistentCoderViewProvider = void 0;
const vscode = __importStar(require("vscode"));
class PersistentCoderViewProvider {
    extensionUri;
    backend;
    static viewType = "persistentCoder.chatView";
    view;
    currentRequestId;
    disposables = [];
    constructor(extensionUri, backend) {
        this.extensionUri = extensionUri;
        this.backend = backend;
        this.disposables.push(this.backend.onMessage((message) => this.handleBackendMessage(message)));
        this.disposables.push(this.backend.onStatus((event) => this.handleBackendStatus(event)));
    }
    dispose() {
        for (const disposable of this.disposables) {
            disposable.dispose();
        }
    }
    resolveWebviewView(webviewView) {
        this.view =
            webviewView;
        const webview = webviewView.webview;
        webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this.extensionUri, "media")
            ]
        };
        webview.html =
            this.getHtml(webview);
        webview.onDidReceiveMessage(async (message) => {
            await this
                .handleWebviewMessage(message);
        });
        this.postBackendStatus(this.backend.status, this.backend.status ===
            "ready"
            ? "Local"
            : "Starting...");
    }
    activeProjectRoot() {
        const activeDocument = vscode.window
            .activeTextEditor
            ?.document.uri;
        if (activeDocument) {
            const folder = vscode.workspace
                .getWorkspaceFolder(activeDocument);
            if (folder) {
                return folder.uri.fsPath;
            }
        }
        const folders = vscode.workspace
            .workspaceFolders;
        if (folders &&
            folders.length > 0) {
            return folders[0].uri.fsPath;
        }
        return null;
    }
    async handleWebviewMessage(message) {
        if (typeof message !==
            "object" ||
            message === null) {
            return;
        }
        const data = message;
        if (data.type ===
            "settings") {
            void vscode.window
                .showInformationMessage("Настройки PersistentCoder подключим позже.");
            return;
        }
        if (data.type !==
            "sendMessage" ||
            typeof data.text !==
                "string") {
            return;
        }
        const text = data.text.trim();
        if (!text) {
            return;
        }
        if (this.currentRequestId) {
            this.post({
                type: "runFailed",
                error: "Сейчас уже выполняется одна задача."
            });
            return;
        }
        const projectRoot = this.activeProjectRoot();
        if (!projectRoot) {
            this.post({
                type: "runFailed",
                error: "Откройте папку проекта в VS Code перед запуском PersistentCoder."
            });
            return;
        }
        let workMode;
        if (data.workMode ===
            "sandbox") {
            workMode =
                "sandbox";
        }
        else if (data.workMode ===
            "auto_apply") {
            workMode =
                "auto_apply";
        }
        else {
            this.post({
                type: "runFailed",
                error: "Неизвестный режим работы."
            });
            return;
        }
        try {
            this.currentRequestId =
                this.backend.run(text, projectRoot, workMode);
        }
        catch (error) {
            const errorText = error instanceof Error
                ? error.message
                : String(error);
            this.post({
                type: "runFailed",
                error: errorText
            });
        }
    }
    handleBackendStatus(event) {
        this.postBackendStatus(event.status, event.message);
    }
    postBackendStatus(status, text) {
        this.post({
            type: "backendStatus",
            status,
            text
        });
    }
    handleBackendMessage(message) {
        if (message.type ===
            "ready") {
            this.postBackendStatus("ready", "Local");
            return;
        }
        if (message.type ===
            "run_started") {
            if (message.request_id !==
                this.currentRequestId) {
                return;
            }
            this.post({
                type: "runStarted",
                requestId: message.request_id
            });
            return;
        }
        if (message.type ===
            "run_completed") {
            if (message.request_id !==
                this.currentRequestId) {
                return;
            }
            this.currentRequestId =
                undefined;
            this.post({
                type: "runCompleted",
                result: message.result
            });
            return;
        }
        if (message.type ===
            "run_failed") {
            if (message.request_id !==
                this.currentRequestId) {
                return;
            }
            this.currentRequestId =
                undefined;
            this.post({
                type: "runFailed",
                error: message.error
            });
            return;
        }
        if (message.type ===
            "protocol_error") {
            this.currentRequestId =
                undefined;
            this.post({
                type: "runFailed",
                error: "Backend protocol error: " +
                    message.error
            });
        }
    }
    post(message) {
        void this.view
            ?.webview
            .postMessage(message);
    }
    getHtml(webview) {
        const cssUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "main.css"));
        const jsUri = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "main.js"));
        const nonce = getNonce();
        return /* html */ `
<!DOCTYPE html>
<html lang="ru">

<head>

    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >

    <meta
        http-equiv="Content-Security-Policy"
        content="
            default-src 'none';
            img-src ${webview.cspSource} data:;
            style-src ${webview.cspSource} 'unsafe-inline';
            script-src 'nonce-${nonce}';
        "
    >

    <link
        rel="stylesheet"
        href="${cssUri}"
    >

    <title>
        PersistentCoder
    </title>

</head>


<body>

<div class="app">


    <header class="topbar">

        <div class="topbar-left">

            <div class="brand-icon">
                P
            </div>

            <div>

                <div class="brand-title">
                    PersistentCoder
                </div>

                <div class="brand-status">

                    <span
                        id="backendStatusDot"
                        class="status-dot starting"
                    ></span>

                    <span
                        id="backendStatusText"
                    >
                        Starting...
                    </span>

                </div>

            </div>

        </div>


        <div class="topbar-actions">

            <button
                id="newChatButton"
                class="icon-button"
                title="Новый чат"
            >
                ＋
            </button>

            <button
                id="settingsButton"
                class="icon-button"
                title="Настройки"
            >
                ⚙
            </button>

        </div>

    </header>


    <main
        id="messages"
        class="messages"
    >

        <section
            id="welcome"
            class="welcome"
        >

            <div class="hero-logo">
                P
            </div>

            <h1>
                PersistentCoder
            </h1>

            <p class="hero-subtitle">
                Локальный автономный coding agent
            </p>


            <div class="local-card">

                <div class="local-card-title">
                    ● Локальный backend
                </div>

                <div class="local-card-text">
                    VS Code общается с PersistentCoder
                    через локальный stdin/stdout JSON protocol.
                    Внешний сервер не используется.
                </div>

            </div>


            <div class="suggestions">

                <button
                    class="suggestion"
                    data-prompt="Посмотри проект и найди возможную ошибку"
                >
                    Найти ошибку
                </button>

                <button
                    class="suggestion"
                    data-prompt="Добавь новую функцию и тесты"
                >
                    Добавить функцию
                </button>

                <button
                    class="suggestion"
                    data-prompt="Проанализируй архитектуру проекта"
                >
                    Анализ проекта
                </button>

            </div>

        </section>

    </main>


    <footer class="composer-area">

        <div class="composer">

            <textarea
                id="messageInput"
                rows="1"
                placeholder="Напишите задачу..."
                aria-label="Сообщение"
            ></textarea>


            <div class="composer-toolbar">

                <div class="toolbar-left">

                    <button
                        class="small-action"
                        id="attachButton"
                        title="Добавить контекст"
                    >
                        ＋
                    </button>

                    <select
                        id="workModeSelect"
                        class="mode-selector"
                        title="Режим работы"
                    >
                        <option value="sandbox">
                            Sandbox
                        </option>

                        <option value="auto_apply">
                            Direct
                        </option>
                    </select>

                    <div class="model-selector">
                        Qwen Local
                    </div>

                </div>


                <button
                    id="sendButton"
                    class="send-button"
                    title="Отправить"
                >
                    ↑
                </button>

            </div>

        </div>


        <div class="composer-hint">
            Enter — отправить · Shift+Enter — новая строка
        </div>

    </footer>

</div>


<script
    nonce="${nonce}"
    src="${jsUri}"
></script>


</body>
</html>
`;
    }
}
exports.PersistentCoderViewProvider = PersistentCoderViewProvider;
function getNonce() {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" +
        "abcdefghijklmnopqrstuvwxyz" +
        "0123456789";
    let result = "";
    for (let i = 0; i < 32; i += 1) {
        result +=
            chars.charAt(Math.floor(Math.random() *
                chars.length));
    }
    return result;
}
//# sourceMappingURL=PersistentCoderViewProvider.js.map