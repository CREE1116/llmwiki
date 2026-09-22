const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('llmwiki', {
  getStats: () => ipcRenderer.invoke('llmwiki:get-stats'),
  getGraph: () => ipcRenderer.invoke('llmwiki:get-graph'),
  search: (params) => ipcRenderer.invoke('llmwiki:search', params),
  getConcept: (conceptId) => ipcRenderer.invoke('llmwiki:get-concept', conceptId),
  listConcepts: () => ipcRenderer.invoke('llmwiki:list-concepts'),
  listRaw: () => ipcRenderer.invoke('llmwiki:list-raw'),
  getRaw: (docId) => ipcRenderer.invoke('llmwiki:get-raw', docId),
  deleteRaw: (docId) => ipcRenderer.invoke('llmwiki:delete-raw', docId),
  deleteConcept: (conceptId) => ipcRenderer.invoke('llmwiki:delete-concept', conceptId),
  getLogs: () => ipcRenderer.invoke('llmwiki:get-logs'),
  ingest: (sources, options = {}) => ipcRenderer.invoke('llmwiki:ingest', { sources, options }),
  selectFiles: () => ipcRenderer.invoke('llmwiki:select-files'),
  selectFile: () => ipcRenderer.invoke('llmwiki:select-files').then(files => files[0] || null),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  checkEnv: () => ipcRenderer.invoke('llmwiki:check-env'),
  installSkills: (params) => ipcRenderer.invoke('llmwiki:install-skills', params),
  saveConfig: (params) => ipcRenderer.invoke('llmwiki:save-config', params)
});
