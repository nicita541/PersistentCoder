import * as path from "path";
import * as vscode from "vscode";

import {
    PersistentCoderProcess
} from "./backend/PersistentCoderProcess";

import {
    PersistentCoderViewProvider
} from "./PersistentCoderViewProvider";


export function activate(
    context: vscode.ExtensionContext
): void {
    const backendRoot =
        path.dirname(
            context.extensionUri.fsPath
        );

    const output =
        vscode.window.createOutputChannel(
            "PersistentCoder"
        );

    const backend =
        new PersistentCoderProcess(
            backendRoot,
            output
        );

    const provider =
        new PersistentCoderViewProvider(
            context.extensionUri,
            backend
        );


    context.subscriptions.push(
        output,
        backend,
        provider
    );


    context.subscriptions.push(
        vscode.window
            .registerWebviewViewProvider(
                PersistentCoderViewProvider
                    .viewType,
                provider,
                {
                    webviewOptions: {
                        retainContextWhenHidden:
                            true
                    }
                }
            )
    );


    try {
        backend.start();

    } catch (error) {
        const message =
            error instanceof Error
                ? error.message
                : String(error);

        output.appendLine(
            "[backend startup] " +
            message
        );

        vscode.window
            .showErrorMessage(
                "PersistentCoder backend: " +
                message
            );
    }
}


export function deactivate():
    void
{
    // Resources are disposed through
    // context.subscriptions.
}