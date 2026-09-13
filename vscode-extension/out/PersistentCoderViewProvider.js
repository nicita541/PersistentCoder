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
    static viewType = "persistentCoder.chatView";
    constructor(extensionUri) {
        this.extensionUri = extensionUri;
    }
    resolveWebviewView(webviewView) {
        const webview = webviewView.webview;
        webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(this.extensionUri, "media")
            ]
        };
        webview.html = this.getHtml(webview);
        webview.onDidReceiveMessage(async (message) => {
            if (typeof message !== "object" ||
                message === null) {
                return;
            }
            const data = message;
            if (data.type === "settings") {
                vscode.window.showInformationMessage("Настройки PersistentCoder подключим позже.");
                return;
            }
            if (data.type !== "sendMessage" ||
                typeof data.text !== "string") {
                return;
            }
            const text = data.text.trim();
            if (!text) {
                return;
            }
            // -----------------------------
            // DEMO / UI PROTOTYPE ONLY
            // -----------------------------
            await delay(450);
            webview.postMessage({
                type: "agentStatus",
                status: "thinking",
                title: "Анализирую задачу...",
                description: "Изучаю запрос и структуру проекта."
            });
            await delay(700);
            webview.postMessage({
                type: "demoPlan",
                tasks: [
                    {
                        title: "Изучить связанные файлы",
                        status: "done"
                    },
                    {
                        title: "Подготовить изменения",
                        status: "active"
                    },
                    {
                        title: "Запустить проверки",
                        status: "pending"
                    }
                ]
            });
            await delay(900);
            webview.postMessage({
                type: "agentStatus",
                status: "done",
                title: "Прототип интерфейса",
                description: "Backend пока не подключён. " +
                    "Позже здесь будет настоящий AgentRuntime."
            });
        });
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

    <title>PersistentCoder</title>
</head>

<body>

<div class="app">

    <!-- HEADER -->

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
                    <span class="status-dot"></span>
                    Local
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


    <!-- CONTENT -->

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
                    ● Работает локально
                </div>

                <div class="local-card-text">
                    Модель, память, sandbox и Docker
                    будут работать на вашем ПК.
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


    <!-- INPUT -->

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

                    <div class="model-selector">
                        Qwen Local
                        <span class="chevron">
                           ⌄
                        </span>
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
        result += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return result;
}
function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}
//# sourceMappingURL=PersistentCoderViewProvider.js.map