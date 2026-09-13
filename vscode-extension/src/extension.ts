import * as vscode from "vscode";

import { PersistentCoderViewProvider } from "./PersistentCoderViewProvider";

export function activate(context: vscode.ExtensionContext): void {
    const provider = new PersistentCoderViewProvider(
        context.extensionUri
    );

    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            PersistentCoderViewProvider.viewType,
            provider,
            {
                webviewOptions: {
                    retainContextWhenHidden: true
                }
            }
        )
    );
}

export function deactivate(): void {
    // Пока ничего очищать вручную не требуется.
}