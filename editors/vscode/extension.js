const fs = require("fs");
const path = require("path");
const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

function serverCommand(cfg, cwd) {
  const custom = cfg.get("serverCommand");
  if (custom && custom.length) return custom;
  const local = cwd && path.join(cwd, ".venv", "bin", "byname");
  return [local && fs.existsSync(local) ? local : "byname", "lsp"];
}

async function start() {
  const cfg = vscode.workspace.getConfiguration("byname");
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const [command, ...args] = serverCommand(cfg, cwd);
  const checker = cfg.get("checker");
  if (checker && checker.length) args.push("--", ...checker);

  client = new LanguageClient(
    "byname",
    "byname",
    { command, args, options: { cwd } },
    {
      documentSelector: [{ scheme: "file", language: "pyn" }],
      synchronize: { fileEvents: vscode.workspace.createFileSystemWatcher("**/*.pyn") },
      initializationOptions: {
        outputOnSave: cfg.get("outputOnSave"),
        outputStripMain: cfg.get("outputStripMain"),
        diagnosticsOnSave: cfg.get("diagnosticsOnSave"),
      },
    }
  );
  await client.start();
}

// Alt+C: write <name>.pyn.py for the active file now (the server renders it, honouring outputStripMain)
async function writeOutput() {
  const editor = vscode.window.activeTextEditor;
  if (!client || !editor || editor.document.languageId !== "pyn") return;
  try {
    const out = await client.sendRequest("workspace/executeCommand", {
      command: "byname.server.writeOutput",
      arguments: [editor.document.uri.toString()],
    });
    vscode.window.setStatusBarMessage(`byname: wrote ${path.basename(out)}`, 3000);
  } catch (e) {
    vscode.window.showErrorMessage(e.message);
  }
}

exports.activate = async (context) => {
  context.subscriptions.push(vscode.commands.registerCommand("byname.writeOutput", writeOutput));
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(async (e) => {
      if (!e.affectsConfiguration("byname")) return;
      await client?.stop();
      await start();
    })
  );
  await start();
};

exports.deactivate = () => client?.stop();
