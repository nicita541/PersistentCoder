import * as vscode from "vscode";

export class PersistentCoderViewProvider
    implements vscode.WebviewViewProvider
{
    public static readonly viewType =
        "persistentCoder.chatView";

    public constructor(
        private readonly extensionUri: vscode.Uri
    ) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView
    ): void {
        const webview = webviewView.webview;

        webview.options = {
            enableScripts: true,
            localResourceRoots: [
                vscode.Uri.joinPath(
                    this.extensionUri,
                    "media"
                )
            ]
        };

        webview.html = this.getHtml(webview);

        webview.onDidReceiveMessage(
            async (message: unknown) => {
                if (
                    typeof message !== "object" ||
                    message === null
                ) {
                    return;
                }

                const data = message as {
                    type?: string;
                    text?: string;
                };

                if (data.type === "settings") {
                    vscode.window.showInformationMessage(
                        "Настройки PersistentCoder подключим позже."
                    );

                    return;
                }

                if (
                    data.type !== "sendMessage" ||
                    typeof data.text !== "string"
                ) {
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
                    description:
                        "Изучаю запрос и структуру проекта."
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
                    description:
                        "Backend пока не подключён. " +
                        "Позже здесь будет настоящий AgentRuntime."
                });
            }
        );
    }

    private getHtml(
        webview: vscode.Webview
    ): string {
        const cssUri = webview.asWebviewUri(
            vscode.Uri.joinPath(
                this.extensionUri,
                "media",
                "main.css"
            )
        );

        const jsUri = webview.asWebviewUri(
            vscode.Uri.joinPath(
                this.extensionUri,
                "media",
                "main.js"
            )
        );

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

function getNonce(): string {
    const chars =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ" +
        "abcdefghijklmnopqrstuvwxyz" +
        "0123456789";

    let result = "";

    for (let i = 0; i < 32; i += 1) {
        result += chars.charAt(
            Math.floor(
                Math.random() * chars.length
            )
        );
    }

    return result;
}

function delay(ms: number): Promise<void> {
    return new Promise(
        (resolve) => setTimeout(resolve, ms)
    );
}