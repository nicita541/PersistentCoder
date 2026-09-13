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
exports.activate = activate;
exports.deactivate = deactivate;
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const PersistentCoderProcess_1 = require("./backend/PersistentCoderProcess");
const PersistentCoderViewProvider_1 = require("./PersistentCoderViewProvider");
function activate(context) {
    const backendRoot = path.dirname(context.extensionUri.fsPath);
    const output = vscode.window.createOutputChannel("PersistentCoder");
    const backend = new PersistentCoderProcess_1.PersistentCoderProcess(backendRoot, output);
    const provider = new PersistentCoderViewProvider_1.PersistentCoderViewProvider(context.extensionUri, backend);
    context.subscriptions.push(output, backend, provider);
    context.subscriptions.push(vscode.window
        .registerWebviewViewProvider(PersistentCoderViewProvider_1.PersistentCoderViewProvider
        .viewType, provider, {
        webviewOptions: {
            retainContextWhenHidden: true
        }
    }));
    try {
        backend.start();
    }
    catch (error) {
        const message = error instanceof Error
            ? error.message
            : String(error);
        output.appendLine("[backend startup] " +
            message);
        vscode.window
            .showErrorMessage("PersistentCoder backend: " +
            message);
    }
}
function deactivate() {
    // Resources are disposed through
    // context.subscriptions.
}
//# sourceMappingURL=extension.js.map