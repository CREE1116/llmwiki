const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const { execFile, execFileSync, spawn } = require('child_process');

let mainWindow;
let activeIngestProcess = null;
const allChildProcesses = new Set(); // tracks every spawned child for cleanup on quit
const ROOT_DIR = path.resolve(__dirname, '..');

// Detect the correct python3 binary once at startup (handles python3.14, etc.)
// Electron apps often have a stripped PATH that lacks /opt/homebrew/bin, so we expand it.
let resolvedPython3 = null;
function getResolvedPython3() {
  if (resolvedPython3) return resolvedPython3;
  // Augment PATH with common Python/Homebrew install locations
  const extraPaths = [
    '/opt/homebrew/bin',
    '/usr/local/bin',
    '/usr/bin',
    '/opt/homebrew/opt/python3/bin',
    process.env.HOME ? `${process.env.HOME}/.pyenv/shims` : '',
    process.env.HOME ? `${process.env.HOME}/.local/bin` : '',
  ].filter(Boolean);
  const augmentedPath = [...extraPaths, process.env.PATH || ''].join(':');

  const candidates = ['python3', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python'];
  for (const candidate of candidates) {
    try {
      const out = execFileSync('which', [candidate], {
        encoding: 'utf8',
        timeout: 3000,
        env: { ...process.env, PATH: augmentedPath }
      }).trim();
      if (out) { resolvedPython3 = out; console.log('[llmwiki] Python resolved to:', out); return resolvedPython3; }
    } catch (e) { /* try next */ }
  }
  resolvedPython3 = 'python3';
  return resolvedPython3;
}

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
  // Use auto-detected python3 path to handle python3.14, etc.
  return { command: getResolvedPython3(), args: ['-m', 'llmwiki', ...args] };
}

function getCLIEnv(isPackaged) {
  // Prepend common tool paths so python3, pip, etc. are reachable even when
  // Electron strips the shell PATH (common on macOS app bundles).
  const extraPaths = [
    '/opt/homebrew/bin',
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
  ].join(':');
  const augmentedPath = `${extraPaths}:${process.env.PATH || ''}`;
  return {
    ...process.env,
    PATH: augmentedPath,
    ...(isPackaged ? {} : { PYTHONPATH: ROOT_DIR })
  };
}

function runCLI(args) {
  return new Promise((resolve) => {
    const invocation = getCLIInvocation(args);
    const options = {
      cwd: app.isPackaged ? app.getPath('userData') : ROOT_DIR,
      env: getCLIEnv(app.isPackaged),
      maxBuffer: 10 * 1024 * 1024
    };

    const proc = execFile(invocation.command, invocation.args, options, (error, stdout, stderr) => {
      allChildProcesses.delete(proc);
      if (error) {
        console.error(`[llmwiki CLI] Error (${args.join(' ')}):`, stderr || error.message);
        resolve({ error: stderr || error.message, stdout: stdout || '' });
      } else {
        if (stderr && stderr.trim()) console.warn(`[llmwiki CLI] stderr (${args[0]}):`, stderr.trim());
        resolve({ stdout: stdout.trim() });
      }
    });
    allChildProcesses.add(proc);
  });
}

function runCLIStreaming(args, onEvent) {
  return new Promise((resolve) => {
    const invocation = getCLIInvocation(args);
    const options = {
      cwd: app.isPackaged ? app.getPath('userData') : ROOT_DIR,
      env: getCLIEnv(app.isPackaged)
    };

    const proc = spawn(invocation.command, invocation.args, options);
    activeIngestProcess = proc;
    allChildProcesses.add(proc);

    let stdoutBuffer = '';
    let lastResult = null;
    let stderrBuffer = '';

    proc.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString();
      const lines = stdoutBuffer.split('\n');
      stdoutBuffer = lines.pop(); // keep last partial line

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const parsed = parseJsonSafe(trimmed);
        if (parsed && parsed.event) {
          if (onEvent) onEvent(parsed);
          if (parsed.event === 'completed') {
            lastResult = parsed;
          }
        }
      }
    });

    proc.stderr.on('data', (chunk) => {
      stderrBuffer += chunk.toString();
    });

    proc.on('close', (code) => {
      allChildProcesses.delete(proc);
      if (activeIngestProcess === proc) {
        activeIngestProcess = null;
      }
      if (stderrBuffer && stderrBuffer.trim()) {
        console.warn('[llmwiki streaming] stderr:', stderrBuffer.trim());
      }
      if (stdoutBuffer.trim()) {
        const parsed = parseJsonSafe(stdoutBuffer.trim());
        if (parsed && parsed.event) {
          if (onEvent) onEvent(parsed);
          if (parsed.event === 'completed') lastResult = parsed;
        }
      }
      if (lastResult) {
        resolve(lastResult);
      } else {
        const fallbackParsed = parseJsonSafe(stdoutBuffer);
        if (fallbackParsed) {
          resolve(fallbackParsed);
        } else {
          resolve({ error: stderrBuffer || `Process exited with code ${code}`, stdout: stdoutBuffer });
        }
      }
    });

    proc.on('error', (err) => {
      allChildProcesses.delete(proc);
      if (activeIngestProcess === proc) {
        activeIngestProcess = null;
      }
      console.error('[llmwiki streaming] spawn error:', err.message);
      resolve({ error: err.message });
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

  if (process.env.LLMWIKI_CAPTURE === '1') {
    mainWindow.webContents.on('did-finish-load', async () => {
      await new Promise(r => setTimeout(r, 2500));
      const assetsDir = path.join(ROOT_DIR, 'assets');
      if (!fs.existsSync(assetsDir)) fs.mkdirSync(assetsDir, { recursive: true });

      let img = await mainWindow.capturePage();
      fs.writeFileSync(path.join(assetsDir, 'screenshot-graph.png'), img.toPNG());

      await mainWindow.webContents.executeJavaScript(`
        document.getElementById('modal-workspaces').classList.remove('hidden');
      `);
      await new Promise(r => setTimeout(r, 800));
      img = await mainWindow.capturePage();
      fs.writeFileSync(path.join(assetsDir, 'screenshot-workspaces.png'), img.toPNG());

      await mainWindow.webContents.executeJavaScript(`
        document.getElementById('modal-workspaces').classList.add('hidden');
        document.getElementById('modal-settings').classList.remove('hidden');
      `);
      await new Promise(r => setTimeout(r, 800));
      img = await mainWindow.capturePage();
      fs.writeFileSync(path.join(assetsDir, 'screenshot-settings.png'), img.toPNG());

      await mainWindow.webContents.executeJavaScript(`
        document.getElementById('modal-settings').classList.add('hidden');
        document.querySelector('.nav-btn[data-tab="tab-ingest"]').click();
      `);
      await new Promise(r => setTimeout(r, 800));
      img = await mainWindow.capturePage();
      fs.writeFileSync(path.join(assetsDir, 'screenshot-ingest.png'), img.toPNG());

      console.log('All screenshots captured into assets/');
      app.quit();
    });
  }

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

// Kill all spawned Python child processes when the app exits
// This prevents zombie llmwiki / python3 processes staying alive after quit
app.on('before-quit', () => {
  if (allChildProcesses.size > 0) {
    console.log(`[llmwiki] Cleaning up ${allChildProcesses.size} child process(es) before quit...`);
    for (const proc of allChildProcesses) {
      try { proc.kill('SIGTERM'); } catch (e) { /* already dead */ }
    }
    allChildProcesses.clear();
  }
});


// ----------------- IPC Handlers -----------------

ipcMain.handle('llmwiki:get-stats', async (event, workspace) => {
  const args = ['stats'];
  if (workspace && workspace !== 'all') args.push('--workspace', workspace);
  args.push('--json');
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to parse stats' };
});

ipcMain.handle('llmwiki:get-graph', async (event, workspace) => {
  const args = ['graph-data'];
  if (workspace && workspace !== 'all') args.push('--workspace', workspace);
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || { nodes: [], edges: [], error: res.error };
});

ipcMain.handle('llmwiki:search', async (event, { query, mode, limit, workspace }) => {
  const args = ['search', query || '', '--mode', mode || 'hybrid', '--limit', String(limit || 10), '--caller', 'desktop_app'];
  if (workspace && workspace !== 'all') args.push('--workspace', workspace);
  args.push('--json');
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:get-concept', async (event, conceptId) => {
  const res = await runCLI(['get', conceptId, '--neighbors', '--raw', '--caller', 'desktop_app', '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Concept not found' };
});

ipcMain.handle('llmwiki:list-concepts', async (event, workspace) => {
  const args = ['list-concepts', '--limit', '200'];
  if (workspace && workspace !== 'all') args.push('--workspace', workspace);
  args.push('--json');
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:list-raw', async (event, workspace) => {
  const args = ['list-raw', '--limit', '200'];
  if (workspace && workspace !== 'all') args.push('--workspace', workspace);
  args.push('--json');
  const res = await runCLI(args);
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

// Workspace Handlers
ipcMain.handle('llmwiki:list-workspaces', async () => {
  const res = await runCLI(['workspace', 'list', '--json']);
  return parseJsonSafe(res.stdout) || [];
});

ipcMain.handle('llmwiki:create-workspace', async (event, { id, name, desc }) => {
  const args = ['workspace', 'create', id];
  if (name) args.push('--name', name);
  if (desc) args.push('--desc', desc);
  args.push('--json');
  const res = await runCLI(args);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to create workspace' };
});

ipcMain.handle('llmwiki:switch-workspace', async (event, id) => {
  const res = await runCLI(['workspace', 'use', id, '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to switch workspace' };
});

ipcMain.handle('llmwiki:delete-workspace', async (event, id) => {
  const res = await runCLI(['workspace', 'delete', id, '--json']);
  return parseJsonSafe(res.stdout) || { error: res.error || 'Failed to delete workspace' };
});

ipcMain.handle('llmwiki:current-workspace', async () => {
  const res = await runCLI(['workspace', 'current', '--json']);
  return parseJsonSafe(res.stdout) || { active_workspace: 'default' };
});

ipcMain.handle('llmwiki:route-workspace', async (event, query) => {
  const res = await runCLI(['workspace', 'route', query, '--json']);
  return parseJsonSafe(res.stdout) || null;
});

ipcMain.handle('llmwiki:abort-ingest', async () => {
  if (activeIngestProcess) {
    try {
      activeIngestProcess.kill('SIGINT');
      setTimeout(() => {
        if (activeIngestProcess) {
          try { activeIngestProcess.kill('SIGTERM'); } catch (e) {}
          activeIngestProcess = null;
        }
      }, 1500);
      return { aborted: true };
    } catch (e) {
      return { error: e.message };
    }
  }
  return { aborted: false };
});

ipcMain.handle('llmwiki:ingest', async (event, request) => {
  const legacyRequest = Array.isArray(request) || typeof request === 'string';
  const sources = legacyRequest ? request : request?.sources;
  const options = legacyRequest ? {} : (request?.options || {});
  const srcList = Array.isArray(sources) ? sources : [sources].filter(Boolean);
  const args = ['ingest', ...srcList];
  if (options.workspace && options.workspace !== 'all') {
    args.push('--workspace', options.workspace);
  }
  if (options.explore) {
    const depth = Math.max(0, Math.min(20, Number(options.depth) || 1));
    const maxPages = options.greedy ? 0 : Math.max(1, Math.min(2000, Number(options.maxPages) || 8));
    args.push('--explore', '--depth', String(depth), '--max-pages', String(maxPages));
    if (options.greedy) {
      args.push('--greedy');
    }
  }
  args.push('--stream');
  const res = await runCLIStreaming(args, (streamEvent) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('llmwiki:ingest-event', streamEvent);
    }
  });
  if (res && !res.error) return res;
  return { error: res?.error || 'Ingestion failed' };
});

ipcMain.handle('llmwiki:deep-dive', async (event, request) => {
  const conceptId = typeof request === 'string' ? request : request?.conceptId;
  const options = typeof request === 'object' ? (request.options || {}) : {};
  if (!conceptId) return { error: 'Concept ID is required for deep-dive' };

  const args = ['deep-dive', conceptId];
  if (options.depth) args.push('--depth', String(Math.max(1, Math.min(20, Number(options.depth)))));
  if (options.maxPages) args.push('--max-pages', String(Math.max(1, Math.min(2000, Number(options.maxPages)))));
  if (options.greedy) args.push('--greedy');
  if (options.query) args.push('--query', String(options.query));
  args.push('--stream');

  const res = await runCLIStreaming(args, (streamEvent) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('llmwiki:ingest-event', streamEvent);
    }
  });
  if (res && !res.error) return res;
  return { error: res?.error || 'Deep dive failed' };
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
