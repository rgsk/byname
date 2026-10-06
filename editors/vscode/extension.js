const fs = require("fs");
const path = require("path");
const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

function onPath(name) {
  return (process.env.PATH || "").split(path.delimiter).some((d) => d && fs.existsSync(path.join(d, name)));
}

// null: no byname here (the extension now wakes for any Python file, and a plain project shouldn't get errors)
function serverCommand(cfg, cwd) {
  const custom = cfg.get("serverCommand");
  if (custom && custom.length) return custom;
  const local = cwd && path.join(cwd, ".venv", "bin", "byname");
  if (local && fs.existsSync(local)) return [local, "lsp"];
  return onPath("byname") ? ["byname", "lsp"] : null;
}

async function start() {
  const cfg = vscode.workspace.getConfiguration("byname");
  const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const cmd = serverCommand(cfg, cwd);
  if (!cmd) return;
  const [command, ...args] = cmd;
  const checker = cfg.get("checker");
  if (checker && checker.length) args.push("--", ...checker);

  client = new LanguageClient(
    "byname",
    "byname",
    { command, args, options: { cwd } },
    {
      // .pyn, and .py and notebooks too: byname is the workspace's Python server (turn the BasedPyright
      // extension off here). .py files and notebooks without `%load_ext byname` pass through untranslated
      documentSelector: [
        { scheme: "file", language: "pyn" },
        { scheme: "file", language: "python" },
        { scheme: "untitled", language: "python" },
        { notebook: "jupyter-notebook", language: "python" },
      ],
      synchronize: {
        fileEvents: vscode.workspace.createFileSystemWatcher("**/*.pyn"),
        configurationSection: ["python", "basedpyright"],
      },
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

// Notebook actions on save (notebook.format, notebook.source.fixAll / organizeImports): VS Code asks for them
// once, on the notebook's first cell (notebook.defaultFormatter picks among `notebook.format` ones), which
// may be markdown, outside the server's selector. So they're offered here for every cell, and asked of the
// server on the first code cell; the edit covers every cell
const NOTEBOOK_KINDS = ["format", "source.fixAll", "source.organizeImports"].map((k) => vscode.CodeActionKind.Notebook.append(k));

async function notebookAction(document, range, context, token) {
  const kind = client && context.only && NOTEBOOK_KINDS.find((k) => context.only.contains(k));
  if (!kind) return [];
  const uri = document.uri.toString();
  const nb = vscode.workspace.notebookDocuments.find((n) => n.getCells().some((c) => c.document.uri.toString() === uri));
  const cell = nb?.getCells().find((c) => c.kind === vscode.NotebookCellKind.Code);
  if (!cell) return [];
  const zero = { line: 0, character: 0 };
  const result = await client.sendRequest(
    "textDocument/codeAction",
    {
      textDocument: { uri: client.code2ProtocolConverter.asUri(cell.document.uri) },
      range: { start: zero, end: zero },
      context: { diagnostics: [], only: [kind.value], triggerKind: 2 },
    },
    token
  );
  return client.protocol2CodeConverter.asCodeActionResult(result || [], token);
}

exports.activate = async (context) => {
  context.subscriptions.push(vscode.commands.registerCommand("byname.writeOutput", writeOutput));
  context.subscriptions.push(
    vscode.languages.registerCodeActionsProvider(
      { notebookType: "jupyter-notebook" },
      { provideCodeActions: notebookAction },
      { providedCodeActionKinds: NOTEBOOK_KINDS }
    )
  );
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
