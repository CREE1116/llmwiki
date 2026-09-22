const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFile } = require('child_process');

let mainWindow;
const ROOT_DIR = path.resolve(__dirname, '..');

function getPackagedCLIPath() {
  const binary = process.platform === 'win32' ? 'llmwiki.exe' : 'llmwiki';
  return path.join(process.resourcesPath, 'runtime', binary);
}

function getCLIInvocation(args) {
  if (app.isPackaged) {
    const cliPath = getPackagedCLIPath();
    if (process.platform !== 'win32') {
      try {
        fs.chmodSync(cliPath, 0o755);
      } catch (e) {
        // electron-builder normally preserves executable mode; continue and
        // let execFile report a useful error if the package is malformed.
      }
    }
    return { command: cliPath, args };
  }
  return { command: 'python3', args: ['-m', 'llmwiki', ...args] };
}

function runCLI(args) {
  return new Promise((resolve, reject) => {
    const invocation = getCLIInvocation(args);
    const options = {
      cwd: app.isPackaged ? app.getPath('userData') : ROOT_DIR,
      env: {
        ...process.env,
        ...(app.isPackaged ? {} : { PYTHONPATH: ROOT_DIR })
      },
      maxBuffer: 10 * 1024 * 1024
    };

    execFile(invocation.command, invocation.args, options, (error, stdout, stderr) => {
      if (error) {
        console.error(`CLI Error (${invocation.args.join(' ')}):`, stderr || error.message);
        resolve({ error: stderr || error.message, stdout: stdout });
      } else {
        resolve({ stdout: stdout.trim() });
      }
    });
  });
}

function parseJsonSafe(text) {
  if (!text || typeof text !== 'string') return null;
  const trimmed = text.trim();
  try {
    return JSON.parse(trimmed);
  } catch (e) {
    const firstBracket = trimmed.search(/[\[{]/);
    const lastBracket = Math.max(trimmed.lastIndexOf(']'), trimmed.lastIndexOf('}'));
    if (firstBracket !== -1 && lastBracket > firstBracket) {
      try {
        const sub = trimmed.slice(firstBracket, lastBracket + 1);
        return JSON.parse(sub);
      } catch (err) {
        // ignore
      }
    }
    return null;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1300,
    height: 850,
    minWidth: 1000,
    minHeight: 650,
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#0f172a',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  if (app.isPackaged) {
    const bootstrap = await runCLI(['bootstrap', '--json']);
    if (bootstrap.error) {
      console.warn('LLMWiki first-run setup did not fully complete:', bootstrap.error);
    }
  }
  await runCLI(['reindex', '--json']);
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// ----------------- IPC Handlers -----------------

ipcMain.handle('llmwiki:get-stats', async () => {
  const res = await runCLI(['stats', '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to parse stats' };
});

ipcMain.handle('llmwiki:get-graph', async () => {
  const res = await runCLI(['graph-data']);
  return parseJsonSafe(res.stdout) || { nodes: [], edges: [], error: res.error };
});

ipcMain.handle('llmwiki:search', async (event, { query, mode, limit }) => {
  const args = ['search', query || '', '--mode', mode || 'hybrid', '--limit', String(limit || 10), '--caller', 'desktop_app', '--json'];
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:get-concept', async (event, conceptId) => {
  const res = await runCLI(['get', conceptId, '--neighbors', '--raw', '--caller', 'desktop_app', '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Concept not found' };
});

ipcMain.handle('llmwiki:list-concepts', async () => {
  const res = await runCLI(['list-concepts', '--limit', '200', '--json']);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:list-raw', async () => {
  const res = await runCLI(['list-raw', '--limit', '200', '--json']);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:get-raw', async (event, docId) => {
  const res = await runCLI(['raw', docId, '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Raw doc not found' };
});

ipcMain.handle('llmwiki:delete-raw', async (event, docId) => {
  const res = await runCLI(['delete-raw', docId, '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to delete raw document' };
});

ipcMain.handle('llmwiki:delete-concept', async (event, conceptId) => {
  const res = await runCLI(['delete-concept', conceptId, '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to delete concept' };
});

ipcMain.handle('llmwiki:get-logs', async () => {
  const res = await runCLI(['logs', '--limit', '100', '--json']);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:ingest', async (event, request) => {
  const legacyRequest = Array.isArray(request) || typeof request === 'string';
  const sources = legacyRequest ? request : request?.sources;
  const options = legacyRequest ? {} : (request?.options || {});
  const srcList = Array.isArray(sources) ? sources : [sources].filter(Boolean);
  const args = ['ingest', ...srcList];
  if (options.explore) {
    const depth = Math.max(0, Math.min(3, Number(options.depth) || 1));
    const maxPages = Math.max(1, Math.min(40, Number(options.maxPages) || 8));
    args.push('--explore', '--depth', String(depth), '--max-pages', String(maxPages));
  }
  args.push('--json');
  const res = await runCLI(args);
  const parsed = parseJsonSafe(res.stdout);
  if (parsed) return parsed;
  return { error: res.error || res.stdout || 'Ingestion failed' };
});

ipcMain.handle('llmwiki:check-env', async () => {
  const res = await runCLI(['check-env', '--json']);
  try {
    return JSON.parse(res.stdout);
  } catch (e) {
    return { error: res.error || 'Environment check failed' };
  }
});

ipcMain.handle('llmwiki:install-skills', async (event, { antigravity, claude, codex, symlink }) => {
  const args = ['install-skills'];
  if (antigravity) args.push('--antigravity');
  if (claude) args.push('--claude');
  if (codex) args.push('--codex');
  if (symlink) args.push('--symlink');
  args.push('--json');

  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Installation failed' };
});

ipcMain.handle('llmwiki:save-config', async (event, { provider, model, initialized }) => {
  const args = ['save-config', '--json'];
  if (provider) args.push('--provider', provider);
  if (model) args.push('--model', model);
  if (initialized) args.push('--initialized');

  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Config save failed' };
});

ipcMain.handle('llmwiki:select-files', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: 'Documents', extensions: ['pdf', 'txt', 'md', 'markdown', 'json'] },
      { name: 'All Files', extensions: ['*'] }
    ]
  });
  if (result.canceled || result.filePaths.length === 0) {
    return [];
  }
  return result.filePaths;
});
