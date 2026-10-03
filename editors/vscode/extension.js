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
    }
  );
  await client.start();
}

exports.activate = async (context) => {
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
