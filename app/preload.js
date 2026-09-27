const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('llmwiki', {
  getStats: (workspace) => ipcRenderer.invoke('llmwiki:get-stats', workspace),
  getGraph: (workspace) => ipcRenderer.invoke('llmwiki:get-graph', workspace),
  search: (params) => ipcRenderer.invoke('llmwiki:search', params),
  getConcept: (conceptId) => ipcRenderer.invoke('llmwiki:get-concept', conceptId),
  listConcepts: (workspace) => ipcRenderer.invoke('llmwiki:list-concepts', workspace),
  listRaw: (workspace) => ipcRenderer.invoke('llmwiki:list-raw', workspace),
  getRaw: (docId) => ipcRenderer.invoke('llmwiki:get-raw', docId),
  deleteRaw: (docId) => ipcRenderer.invoke('llmwiki:delete-raw', docId),
  deleteConcept: (conceptId) => ipcRenderer.invoke('llmwiki:delete-concept', conceptId),
  getLogs: () => ipcRenderer.invoke('llmwiki:get-logs'),
  ingest: (sources, options = {}) => ipcRenderer.invoke('llmwiki:ingest', { sources, options }),
  deepDive: (conceptId, options = {}) => ipcRenderer.invoke('llmwiki:deep-dive', { conceptId, options }),
  selectFiles: () => ipcRenderer.invoke('llmwiki:select-files'),
  selectFile: () => ipcRenderer.invoke('llmwiki:select-files').then(files => files[0] || null),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  checkEnv: () => ipcRenderer.invoke('llmwiki:check-env'),
  installSkills: (params) => ipcRenderer.invoke('llmwiki:install-skills', params),
  saveConfig: (params) => ipcRenderer.invoke('llmwiki:save-config', params),
  listWorkspaces: () => ipcRenderer.invoke('llmwiki:list-workspaces'),
  createWorkspace: (params) => ipcRenderer.invoke('llmwiki:create-workspace', params),
  switchWorkspace: (id) => ipcRenderer.invoke('llmwiki:switch-workspace', id),
  deleteWorkspace: (id) => ipcRenderer.invoke('llmwiki:delete-workspace', id),
  currentWorkspace: () => ipcRenderer.invoke('llmwiki:current-workspace'),
  routeWorkspace: (query) => ipcRenderer.invoke('llmwiki:route-workspace', query)
});

