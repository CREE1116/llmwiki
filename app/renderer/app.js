const DEFAULT_PHYSICS = {
  repulsion: 1350,
  linkDistance: 135,
  linkStrength: 0.008,
  centerPull: 0.0018
};

function loadSavedPhysics() {
  try {
    const saved = localStorage.getItem('llmwiki_physics');
    if (saved) return { ...DEFAULT_PHYSICS, ...JSON.parse(saved) };
  } catch (e) {}
  return { ...DEFAULT_PHYSICS };
}

const state = {
  activeTab: 'tab-graph',
  currentWorkspace: 'default',
  workspaces: [],
  graph: { nodes: [], edges: [] },
  transform: { x: 0, y: 0, scale: 1 },
  selectedNode: null,
  hoveredNode: null,
  draggedNode: null,
  panning: false,
  lastPointer: { x: 0, y: 0 },
  query: '',
  canvasWidth: 0,
  canvasHeight: 0,
  selectedFiles: [],
  environment: null,
  selectedProvider: 'ollama',
  libraryMode: 'concepts',
  concepts: [],
  rawDocs: [],
  librarySelection: { concepts: new Set(), raw: new Set() },
  libraryVisible: { concepts: [], raw: [] },
  physics: loadSavedPhysics(),
  currentRawDoc: null,
};

const canvas = document.getElementById('graph-canvas');
const ctx = canvas.getContext('2d');

window.addEventListener('DOMContentLoaded', async () => {
  setupNavigation();
  setupTopBar();
  setupWorkspaces();
  setupGraph();
  setupPhysicsControls();
  setupLibrary();
  setupActivity();
  setupIngest();
  setupModals();
  setupSettings();
  await loadWorkspaces();
  await refreshEnvironment();
  await refreshAll();
  setInterval(async () => {
    await loadStats();
    if (state.activeTab === 'tab-activity') await loadLogs();
  }, 8000);
});

async function refreshAll() {
  await Promise.all([loadStats(), loadGraph(), loadConcepts(), loadRawDocs(), loadLogs()]);
}

function setupNavigation() {
  document.querySelectorAll('.nav-btn[data-tab]').forEach(button => {
    button.addEventListener('click', () => {
      activateTab(button.dataset.tab);
    });
  });
}

function activateTab(tabId, { fit = true } = {}) {
  document.querySelectorAll('.nav-btn[data-tab]').forEach(item => {
    item.classList.toggle('active', item.dataset.tab === tabId);
  });
  document.querySelectorAll('.view-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === tabId);
  });
  state.activeTab = tabId;
  if (tabId === 'tab-graph') {
    requestAnimationFrame(() => {
      resizeCanvas();
      if (fit) fitGraph();
    });
  }
}

async function loadStats(ws = state.currentWorkspace) {
  const stats = await window.llmwiki.getStats(ws);
  if (!stats || stats.error) return;
  document.getElementById('stat-raw').textContent = formatNumber(stats.total_sources);
  document.getElementById('stat-concepts').textContent = formatNumber(stats.total_concepts);
  const visibleConnections = state.graph.edges.length || stats.total_relations;
  document.getElementById('stat-relations').textContent = formatNumber(visibleConnections);
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString('ko-KR');
}

// Graph
function setupGraph() {
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  document.getElementById('btn-reload-graph').addEventListener('click', async () => {
    await loadGraph();
    fitGraph();
  });
  document.getElementById('btn-fit-graph').addEventListener('click', fitGraph);
  document.getElementById('graph-search-input').addEventListener('input', event => {
    state.query = event.target.value.trim().toLowerCase();
  });
  document.getElementById('btn-close-inspector').addEventListener('click', closeInspector);
  document.getElementById('btn-delete-current-concept').addEventListener('click', () => {
    if (state.selectedNode?.id) {
      deleteConcept(state.selectedNode.id, state.selectedNode.name || state.selectedNode.id);
    }
  });

  const deepDiveBtn = document.getElementById('btn-deep-dive-concept');
  if (deepDiveBtn) {
    deepDiveBtn.addEventListener('click', async () => {
      const node = state.selectedNode;
      if (!node?.id) return;
      deepDiveBtn.disabled = true;
      const originalText = deepDiveBtn.textContent;
      deepDiveBtn.textContent = '🌐 탐색 중…';

      setProgress({
        title: `'${node.name || node.id}' 딥다이브 탐색 중…`,
        percent: 30,
        detail: '웹에서 관련 키워드와 문서를 크롤링하여 지식 그래프를 확장합니다.',
        count: '딥다이브 중',
        indeterminate: true,
        state: 'running'
      });

      const abortBtn = document.getElementById('btn-abort-ingest');
      if (abortBtn) {
        abortBtn.classList.remove('hidden');
        abortBtn.disabled = false;
        abortBtn.textContent = '딥다이브 중단 (Stop)';
      }

      beginLiveGraphRefresh();
      try {
        const res = await window.llmwiki.deepDive(node.id, { depth: 1, maxPages: 5, greedy: false });
        if (res && !res.error) {
          const count = res.new_concepts?.length || 0;
          const relCount = res.new_relations || 0;
          const interruptedNotice = res.interrupted ? ' (중단 시점까지 보존됨)' : '';
          setProgress({
            title: res.interrupted ? '딥다이브 중단 완료' : '딥다이브 완료',
            percent: 100,
            detail: `${count}개 신규 개념과 ${relCount}개 연결 관계를 지식 그래프에 자동 추가했습니다.${interruptedNotice}`,
            count: '완료',
            state: 'success'
          });
          await refreshAll();
          if (state.selectedNode?.id === node.id) {
            await showInspector(state.selectedNode);
          }
        } else {
          setProgress({
            title: '딥다이브 실패',
            percent: 100,
            detail: res?.error || '웹 탐색 중 지식을 증류하지 못했습니다.',
            state: 'error'
          });
        }
      } catch (err) {
        setProgress({
          title: '오류 발생',
          percent: 100,
          detail: err.message || '딥다이브 요청 처리 중 오류가 발생했습니다.',
          state: 'error'
        });
      } finally {
        await endLiveGraphRefresh();
        if (abortBtn) {
          abortBtn.classList.add('hidden');
          abortBtn.textContent = '탐색 중단 (Stop)';
        }
        deepDiveBtn.disabled = false;
        deepDiveBtn.textContent = originalText;
      }
    });
  }

  canvas.addEventListener('mousedown', event => {

    const point = screenToWorld(event.clientX, event.clientY);
    const node = hitNode(point.x, point.y);
    if (node) {
      state.draggedNode = node;
      state.selectedNode = node;
      showInspector(node);
    } else {
      state.panning = true;
      state.lastPointer = { x: event.clientX, y: event.clientY };
    }
  });
  canvas.addEventListener('mousemove', event => {
    const point = screenToWorld(event.clientX, event.clientY);
    if (state.draggedNode) {
      state.draggedNode.x = point.x;
      state.draggedNode.y = point.y;
      state.draggedNode.vx = 0;
      state.draggedNode.vy = 0;
      return;
    }
    if (state.panning) {
      state.transform.x += event.clientX - state.lastPointer.x;
      state.transform.y += event.clientY - state.lastPointer.y;
      state.lastPointer = { x: event.clientX, y: event.clientY };
      return;
    }
    state.hoveredNode = hitNode(point.x, point.y);
    canvas.style.cursor = state.hoveredNode ? 'pointer' : 'grab';
  });
  window.addEventListener('mouseup', () => {
    state.draggedNode = null;
    state.panning = false;
  });
  canvas.addEventListener('wheel', event => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const nextScale = clamp(state.transform.scale * (event.deltaY < 0 ? 1.1 : .9), .18, 2.8);
    const ratio = nextScale / state.transform.scale;
    state.transform.x = x - (x - state.transform.x) * ratio;
    state.transform.y = y - (y - state.transform.y) * ratio;
    state.transform.scale = nextScale;
  }, { passive: false });
  requestAnimationFrame(renderGraph);
}

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  state.canvasWidth = Math.max(1, rect.width);
  state.canvasHeight = Math.max(1, rect.height);
  canvas.width = Math.round(state.canvasWidth * dpr);
  canvas.height = Math.round(state.canvasHeight * dpr);
}

async function loadGraph(ws = state.currentWorkspace) {
  const data = await window.llmwiki.getGraph(ws);
  if (!data || data.error || !Array.isArray(data.nodes)) return;
  const existing = new Map(state.graph.nodes.map(node => [node.id, node]));
  const count = data.nodes.length;
  const nodes = data.nodes.map((node, index) => {
    const previous = existing.get(node.id);
    const angle = index * 2.399963229728653;
    const initialDistance = 70 + Math.sqrt(index) * 64;
    const baseRadius = 16;
    const sourcesCount = node.sources_count || 1;
    const radius = baseRadius + Math.min((sourcesCount - 1) * 3.5, 14);
    return {
      ...node,
      x: previous?.x ?? Math.cos(angle) * initialDistance,
      y: previous?.y ?? Math.sin(angle) * initialDistance,
      vx: previous?.vx ?? 0,
      vy: previous?.vy ?? 0,
      radius,
    };
  });
  const nodeMap = new Map(nodes.map(node => [node.id, node]));
  const edges = (data.edges || []).map(edge => ({
    ...edge,
    source: nodeMap.get(edge.source),
    target: nodeMap.get(edge.target),
  })).filter(edge => edge.source && edge.target);
  state.graph = { nodes, edges };
  document.getElementById('stat-relations').textContent = formatNumber(edges.length);
  document.getElementById('graph-empty').classList.toggle('hidden', nodes.length > 0);
  if (!existing.size) requestAnimationFrame(fitGraph);
}

const liveGraphRefresh = {
  users: 0,
  timer: null,
  busy: false,
};

async function tickLiveGraphRefresh() {
  if (liveGraphRefresh.busy) return;
  liveGraphRefresh.busy = true;
  try {
    await Promise.all([loadGraph(), loadStats()]);
  } catch (err) {
    console.warn('Live graph refresh failed:', err);
  } finally {
    liveGraphRefresh.busy = false;
  }
}

function beginLiveGraphRefresh() {
  liveGraphRefresh.users += 1;
  if (liveGraphRefresh.timer) return;
  tickLiveGraphRefresh();
  liveGraphRefresh.timer = setInterval(tickLiveGraphRefresh, 600);
}

async function endLiveGraphRefresh() {
  liveGraphRefresh.users = Math.max(0, liveGraphRefresh.users - 1);
  if (liveGraphRefresh.users > 0) return;
  if (liveGraphRefresh.timer) {
    clearInterval(liveGraphRefresh.timer);
    liveGraphRefresh.timer = null;
  }
  await tickLiveGraphRefresh();
}

function fitGraph() {
  const nodes = state.graph.nodes;
  if (!nodes.length || !state.canvasWidth || !state.canvasHeight) {
    state.transform = { x: state.canvasWidth / 2, y: state.canvasHeight / 2, scale: 1 };
    return;
  }
  const minX = Math.min(...nodes.map(node => node.x - 55));
  const maxX = Math.max(...nodes.map(node => node.x + 55));
  const minY = Math.min(...nodes.map(node => node.y - 45));
  const maxY = Math.max(...nodes.map(node => node.y + 45));
  const graphWidth = Math.max(160, maxX - minX);
  const graphHeight = Math.max(120, maxY - minY);
  const scale = clamp(Math.min((state.canvasWidth - 100) / graphWidth, (state.canvasHeight - 100) / graphHeight), .18, 1.35);
  state.transform.scale = scale;
  state.transform.x = state.canvasWidth / 2 - ((minX + maxX) / 2) * scale;
  state.transform.y = state.canvasHeight / 2 - ((minY + maxY) / 2) * scale;
}

function setupPhysicsControls() {
  const toggleBtn = document.getElementById('btn-toggle-physics');
  const panel = document.getElementById('physics-panel');
  const resetBtn = document.getElementById('btn-reset-physics');

  const sRepulsion = document.getElementById('slider-repulsion');
  const lRepulsion = document.getElementById('label-repulsion');

  const sLinkDist = document.getElementById('slider-link-distance');
  const lLinkDist = document.getElementById('label-link-distance');

  const sLinkStr = document.getElementById('slider-link-strength');
  const lLinkStr = document.getElementById('label-link-strength');

  const sCenterPull = document.getElementById('slider-center-pull');
  const lCenterPull = document.getElementById('label-center-pull');

  function syncUI() {
    sRepulsion.value = state.physics.repulsion;
    lRepulsion.textContent = state.physics.repulsion;

    sLinkDist.value = state.physics.linkDistance;
    lLinkDist.textContent = state.physics.linkDistance;

    sLinkStr.value = state.physics.linkStrength;
    lLinkStr.textContent = Number(state.physics.linkStrength).toFixed(3);

    sCenterPull.value = state.physics.centerPull;
    lCenterPull.textContent = Number(state.physics.centerPull).toFixed(4);
  }

  function save() {
    try {
      localStorage.setItem('llmwiki_physics', JSON.stringify(state.physics));
    } catch (e) {}
  }

  syncUI();

  toggleBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    panel.classList.toggle('hidden');
  });

  panel.addEventListener('click', (e) => e.stopPropagation());

  document.addEventListener('click', (e) => {
    if (!panel.contains(e.target) && e.target !== toggleBtn) {
      panel.classList.add('hidden');
    }
  });

  sRepulsion.addEventListener('input', (e) => {
    state.physics.repulsion = Number(e.target.value);
    lRepulsion.textContent = state.physics.repulsion;
    save();
  });

  sLinkDist.addEventListener('input', (e) => {
    state.physics.linkDistance = Number(e.target.value);
    lLinkDist.textContent = state.physics.linkDistance;
    save();
  });

  sLinkStr.addEventListener('input', (e) => {
    state.physics.linkStrength = Number(e.target.value);
    lLinkStr.textContent = Number(state.physics.linkStrength).toFixed(3);
    save();
  });

  sCenterPull.addEventListener('input', (e) => {
    state.physics.centerPull = Number(e.target.value);
    lCenterPull.textContent = Number(state.physics.centerPull).toFixed(4);
    save();
  });

  resetBtn.addEventListener('click', () => {
    state.physics = { ...DEFAULT_PHYSICS };
    syncUI();
    save();
  });
}

function updatePhysics() {
  const { nodes, edges } = state.graph;
  const p = state.physics;
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const dx = nodes[j].x - nodes[i].x;
      const dy = nodes[j].y - nodes[i].y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = Math.min(2.0, (p.repulsion || 1350) / (distance * distance));
      nodes[i].vx -= (dx / distance) * force;
      nodes[i].vy -= (dy / distance) * force;
      nodes[j].vx += (dx / distance) * force;
      nodes[j].vy += (dy / distance) * force;
    }
  }
  edges.forEach(edge => {
    const dx = edge.target.x - edge.source.x;
    const dy = edge.target.y - edge.source.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const targetDistance = edge.inferred ? (p.linkDistance || 135) * 1.25 : (p.linkDistance || 135);
    const force = (distance - targetDistance) * (p.linkStrength || 0.008);
    edge.source.vx += (dx / distance) * force;
    edge.source.vy += (dy / distance) * force;
    edge.target.vx -= (dx / distance) * force;
    edge.target.vy -= (dy / distance) * force;
  });
  nodes.forEach(node => {
    if (node === state.draggedNode) return;
    node.vx += -node.x * (p.centerPull || 0.0018);
    node.vy += -node.y * (p.centerPull || 0.0018);
    node.vx *= .88;
    node.vy *= .88;
    node.x += node.vx;
    node.y += node.vy;
  });
}

function renderGraph() {
  updatePhysics();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, state.canvasWidth, state.canvasHeight);
  ctx.save();
  ctx.translate(state.transform.x, state.transform.y);
  ctx.scale(state.transform.scale, state.transform.scale);
  drawEdges();
  drawNodes();
  ctx.restore();
  requestAnimationFrame(renderGraph);
}

function drawEdges() {
  const focusId = state.selectedNode?.id || state.hoveredNode?.id || '';
  state.graph.edges.forEach(edge => {
    const sourceMatches = !state.query || `${edge.source.name} ${edge.source.id} ${(edge.source.tags || []).join(' ')}`.toLowerCase().includes(state.query);
    const targetMatches = !state.query || `${edge.target.name} ${edge.target.id} ${(edge.target.tags || []).join(' ')}`.toLowerCase().includes(state.query);
    const queryMatches = sourceMatches || targetMatches;
    const focused = Boolean(focusId && (edge.source.id === focusId || edge.target.id === focusId));
    const dx = edge.target.x - edge.source.x;
    const dy = edge.target.y - edge.source.y;
    const distance = Math.max(1, Math.hypot(dx, dy));
    const ux = dx / distance;
    const uy = dy / distance;
    const startX = edge.source.x + ux * edge.source.radius;
    const startY = edge.source.y + uy * edge.source.radius;
    const endX = edge.target.x - ux * (edge.target.radius + 3);
    const endY = edge.target.y - uy * (edge.target.radius + 3);
    ctx.save();
    ctx.globalAlpha = state.query && !queryMatches
      ? .05
      : (focusId ? (focused ? 1 : .08) : (edge.inferred ? .55 : .78));
    ctx.beginPath();
    if (edge.inferred) ctx.setLineDash([5, 5]);
    ctx.moveTo(startX, startY);
    ctx.lineTo(endX, endY);
    ctx.strokeStyle = focused
      ? (edge.inferred ? 'rgba(154,174,214,.86)' : 'rgba(164,186,255,.96)')
      : (edge.inferred ? 'rgba(111,129,158,.48)' : 'rgba(133,154,205,.72)');
    ctx.lineWidth = focused ? 1.9 : (edge.inferred ? 1 : 1.35);
    ctx.stroke();
    if (!edge.inferred) {
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(endX, endY);
      ctx.lineTo(endX - ux * 8 + uy * 4, endY - uy * 8 - ux * 4);
      ctx.lineTo(endX - ux * 8 - uy * 4, endY - uy * 8 + ux * 4);
      ctx.closePath();
      ctx.fillStyle = focused ? 'rgba(164,186,255,.96)' : 'rgba(133,154,205,.72)';
      ctx.fill();
    }
    const showLabel = focused || (state.graph.edges.length <= 32 && state.transform.scale >= .72);
    if (showLabel) {
      const label = edge.inferred ? '유사' : (edge.relation || '관련');
      ctx.font = focused ? '600 9px -apple-system, sans-serif' : '9px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillStyle = focused ? '#afc0ee' : (edge.inferred ? '#687487' : '#8394b6');
      ctx.fillText(label, (edge.source.x + edge.target.x) / 2, (edge.source.y + edge.target.y) / 2 - 6);
    }
    ctx.restore();
  });
}

function drawNodes() {
  const focusId = state.selectedNode?.id || state.hoveredNode?.id || '';
  state.graph.nodes.forEach(node => {
    const matched = !state.query || `${node.name} ${node.id} ${(node.tags || []).join(' ')}`.toLowerCase().includes(state.query);
    const selected = state.selectedNode?.id === node.id;
    const hovered = state.hoveredNode?.id === node.id;
    const related = focusId && state.graph.edges.some(edge =>
      (edge.source.id === focusId && edge.target.id === node.id) ||
      (edge.target.id === focusId && edge.source.id === node.id)
    );
    ctx.save();
    ctx.globalAlpha = matched ? (focusId && !selected && !hovered && !related ? .28 : 1) : .12;
    if (selected || hovered) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, node.radius + 7, 0, Math.PI * 2);
      ctx.fillStyle = selected ? 'rgba(140,168,255,.18)' : 'rgba(255,255,255,.08)';
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
    const gradient = ctx.createRadialGradient(node.x - 5, node.y - 6, 2, node.x, node.y, node.radius);
    gradient.addColorStop(0, nodeColor(node.type, true));
    gradient.addColorStop(1, nodeColor(node.type, false));
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.strokeStyle = selected ? '#dbe3ff' : '#2b3441';
    ctx.lineWidth = selected ? 1.5 : 1;
    ctx.stroke();
    const showLabel = selected || hovered || (state.query && matched) || state.graph.nodes.length <= 44 || state.transform.scale >= 1.05;
    if (showLabel) {
      ctx.font = selected || hovered ? '600 10px -apple-system, sans-serif' : '500 10px -apple-system, sans-serif';
      ctx.fillStyle = selected || hovered ? '#f2f5ff' : '#d9dee7';
      ctx.textAlign = 'center';
      ctx.fillText(shortLabel(node.name || node.id), node.x, node.y + node.radius + 15);
    }
    ctx.restore();
  });
}

function nodeColor(type, light) {
  const colors = {
    algorithm: ['#9eb8ff', '#526aa8'],
    architecture: ['#bea4ff', '#6b55a5'],
    technique: ['#83d7ad', '#427a61'],
    theory: ['#e4bd7e', '#8a663b'],
    tool: ['#ee9fb9', '#96536b'],
  };
  return (colors[type] || ['#a5b5d6', '#53627e'])[light ? 0 : 1];
}

function shortLabel(value) {
  const text = String(value || '');
  return text.length > 34 ? `${text.slice(0, 31)}…` : text;
}

function screenToWorld(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.transform.x) / state.transform.scale,
    y: (clientY - rect.top - state.transform.y) / state.transform.scale,
  };
}

function hitNode(x, y) {
  return [...state.graph.nodes].reverse().find(node => Math.hypot(node.x - x, node.y - y) <= node.radius + 6) || null;
}

function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

async function showInspector(node) {
  const drawer = document.getElementById('graph-inspector');
  drawer.classList.remove('hidden');
  document.getElementById('inspector-title').textContent = node.name || node.id;
  document.getElementById('inspector-type').textContent = node.type || 'concept';
  const tags = document.getElementById('inspector-tags');
  tags.replaceChildren(...(node.tags || []).map(tag => {
    const element = document.createElement('span');
    element.className = 'badge badge-tag';
    element.textContent = tag;
    return element;
  }));
  const full = await window.llmwiki.getConcept(node.id);
  document.getElementById('inspector-summary').textContent = full?.summary || '아직 작성된 요약이 없습니다.';
  renderList('inspector-mechanisms', full?.mechanisms || [], '정리된 메커니즘이 없습니다.');
  const links = (full?.graph_connections || []).map(link => {
    const target = link.related_id || link.target_id || link.source_id || '';
    return `${link.relation_type || link.relation || '관련'} · ${target}${link.reason ? ` — ${link.reason}` : ''}`;
  });
  renderList('inspector-links', links, '명시된 연결이 없습니다.');

  const sourcesContainer = document.getElementById('inspector-sources-container');
  const sourcesList = document.getElementById('inspector-sources-list');
  const rawSnippets = full?.raw_snippets || [];
  if (rawSnippets.length) {
    sourcesContainer.classList.remove('hidden');
    sourcesList.replaceChildren(...rawSnippets.map(snip => {
      const li = document.createElement('li');
      li.className = 'source-chip';
      const titleSpan = document.createElement('span');
      titleSpan.className = 'source-chip-title';
      titleSpan.textContent = snip.title || snip.doc_id;
      titleSpan.title = snip.source_uri || '';
      const actionSpan = document.createElement('span');
      actionSpan.className = 'source-chip-action';
      actionSpan.textContent = '원문 보기 ›';
      li.append(titleSpan, actionSpan);
      li.addEventListener('click', async () => {
        const rawDoc = await window.llmwiki.getRaw(snip.doc_id);
        if (rawDoc && !rawDoc.error) {
          openRawModal(rawDoc);
        }
      });
      return li;
    }));
  } else {
    sourcesContainer.classList.add('hidden');
  }

  const rawSection = document.getElementById('inspector-raw-container');
  const snippet = full?.raw_snippets?.[0];
  rawSection.classList.toggle('hidden', !snippet);
  document.getElementById('inspector-raw-text').textContent = snippet?.content || '';
}

function renderList(id, items, emptyCopy) {
  const list = document.getElementById(id);
  const values = items.length ? items : [emptyCopy];
  list.replaceChildren(...values.map(value => {
    const item = document.createElement('li');
    item.textContent = value;
    return item;
  }));
}

function closeInspector() {
  document.getElementById('graph-inspector').classList.add('hidden');
  state.selectedNode = null;
}

// Library
function setupLibrary() {
  const conceptsButton = document.getElementById('btn-db-tab-concepts');
  const rawButton = document.getElementById('btn-db-tab-raw');
  conceptsButton.addEventListener('click', () => setLibraryMode('concepts'));
  rawButton.addEventListener('click', () => setLibraryMode('raw'));
  document.getElementById('btn-select-visible').addEventListener('click', selectAllVisibleLibraryItems);
  document.getElementById('btn-clear-selection').addEventListener('click', clearLibrarySelection);
  document.getElementById('btn-delete-selected').addEventListener('click', deleteSelectedLibraryItems);
  document.getElementById('concept-select-all').addEventListener('change', event => {
    setAllVisibleLibraryItems('concepts', event.target.checked);
  });
  document.getElementById('raw-select-all').addEventListener('change', event => {
    setAllVisibleLibraryItems('raw', event.target.checked);
  });
  document.getElementById('btn-refresh-library').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = '새로고침 중…';
    try {
      await Promise.all([loadConcepts(), loadRawDocs(), loadGraph(), loadStats()]);
    } finally {
      button.textContent = previous;
      button.disabled = false;
    }
  });
  let searchTimer;
  document.getElementById('db-search-input').addEventListener('input', event => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const query = event.target.value.trim();
      if (state.libraryMode === 'raw') {
        renderRawDocs(filterRawDocs(query));
        return;
      }
      if (!query) return renderConcepts(state.concepts);
      const hits = await window.llmwiki.search({ query, mode: 'hybrid', limit: 50 });
      renderConcepts(hits || []);
    }, 180);
  });
}

function setLibraryMode(mode) {
  state.libraryMode = mode;
  const concepts = mode === 'concepts';
  document.getElementById('btn-db-tab-concepts').classList.toggle('active', concepts);
  document.getElementById('btn-db-tab-raw').classList.toggle('active', !concepts);
  document.getElementById('db-concepts-panel').classList.toggle('hidden', !concepts);
  document.getElementById('db-raw-panel').classList.toggle('hidden', concepts);
  const searchInput = document.getElementById('db-search-input');
  searchInput.placeholder = concepts ? '개념 검색' : '원문 검색';
  const query = searchInput.value.trim();
  if (concepts) {
    if (!query) renderConcepts(state.concepts);
    else window.llmwiki.search({ query, mode: 'hybrid', limit: 50, workspace: state.currentWorkspace }).then(hits => renderConcepts(hits || []));
  } else {
    renderRawDocs(filterRawDocs(query));
  }
  updateLibraryBulkControls();
}

async function loadConcepts(ws = state.currentWorkspace) {
  state.concepts = (await window.llmwiki.listConcepts(ws)) || [];
  pruneLibrarySelection('concepts', new Set(state.concepts.map(concept => concept.concept_id || concept.id)));
  if (state.libraryMode === 'concepts') renderConcepts(state.concepts);
}

function renderConcepts(concepts) {
  const body = document.getElementById('db-concepts-tbody');
  state.libraryVisible.concepts = concepts;
  if (!concepts.length) {
    renderEmptyRow(body, 6, '저장된 개념이 없습니다.');
    updateLibraryBulkControls();
    return;
  }
  body.replaceChildren(...concepts.map(concept => {
    const row = document.createElement('tr');
    const id = concept.concept_id || concept.id;

    const selectCell = document.createElement('td');
    selectCell.className = 'select-cell';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'selection-checkbox';
    checkbox.checked = state.librarySelection.concepts.has(id);
    row.classList.toggle('selected-row', checkbox.checked);
    checkbox.addEventListener('click', event => event.stopPropagation());
    checkbox.addEventListener('change', event => {
      setLibraryItemSelected('concepts', id, event.target.checked);
      row.classList.toggle('selected-row', event.target.checked);
    });
    selectCell.addEventListener('click', event => event.stopPropagation());
    selectCell.appendChild(checkbox);

    const actionCell = document.createElement('td');
    actionCell.className = 'manage-cell';
    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn-row-action';
    copyBtn.textContent = 'ID 복사';
    copyBtn.title = id;
    copyBtn.addEventListener('click', async event => {
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(id);
        const before = copyBtn.textContent;
        copyBtn.textContent = '복사됨';
        setTimeout(() => { copyBtn.textContent = before; }, 1000);
      } catch (err) {
        alert('개념 ID를 복사하지 못했습니다.');
      }
    });
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn-delete-row';
    deleteBtn.textContent = '삭제';
    deleteBtn.title = '개념 삭제';
    deleteBtn.addEventListener('click', event => {
      event.stopPropagation();
      deleteConcept(id, concept.name || id);
    });
    actionCell.append(copyBtn, deleteBtn);

    row.append(
      selectCell,
      makeCell(concept.name || id, id),
      makeBadgeCell(concept.type || 'concept'),
      makeTextCell(concept.summary || '요약 없음'),
      makeTagsCell(concept.tags || []),
      actionCell,
    );
    row.addEventListener('click', () => openConceptFromLibrary({ ...concept, id }));
    return row;
  }));
  updateLibraryBulkControls();
}

function openConceptFromLibrary(concept) {
  const graphNode = state.graph.nodes.find(node => node.id === concept.id) || concept;
  activateTab('tab-graph', { fit: false });
  state.selectedNode = graphNode;
  requestAnimationFrame(() => {
    resizeCanvas();
    if (Number.isFinite(graphNode.x) && Number.isFinite(graphNode.y)) {
      state.transform.scale = Math.max(state.transform.scale, .82);
      state.transform.x = state.canvasWidth / 2 - graphNode.x * state.transform.scale;
      state.transform.y = state.canvasHeight / 2 - graphNode.y * state.transform.scale;
    }
  });
  showInspector(graphNode);
}

async function loadRawDocs(ws = state.currentWorkspace) {
  state.rawDocs = (await window.llmwiki.listRaw(ws)) || [];
  pruneLibrarySelection('raw', new Set(state.rawDocs.map(doc => doc.id)));
  if (state.libraryMode === 'raw') renderRawDocs(filterRawDocs(document.getElementById('db-search-input').value.trim()));
}

function filterRawDocs(query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return state.rawDocs;
  return state.rawDocs.filter(doc => `${doc.title || ''} ${doc.id || ''} ${doc.source_uri || ''}`.toLowerCase().includes(q));
}

function renderRawDocs(docs) {
  const body = document.getElementById('db-raw-tbody');
  state.libraryVisible.raw = docs;
  if (!docs.length) {
    renderEmptyRow(body, 6, '보관된 원문이 없습니다.');
    updateLibraryBulkControls();
    return;
  }
  body.replaceChildren(...docs.map(doc => {
    const row = document.createElement('tr');

    const selectCell = document.createElement('td');
    selectCell.className = 'select-cell';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'selection-checkbox';
    checkbox.checked = state.librarySelection.raw.has(doc.id);
    row.classList.toggle('selected-row', checkbox.checked);
    checkbox.addEventListener('click', event => event.stopPropagation());
    checkbox.addEventListener('change', event => {
      setLibraryItemSelected('raw', doc.id, event.target.checked);
      row.classList.toggle('selected-row', event.target.checked);
    });
    selectCell.addEventListener('click', event => event.stopPropagation());
    selectCell.appendChild(checkbox);

    const actionCell = document.createElement('td');
    actionCell.style.textAlign = 'center';
    const delBtn = document.createElement('button');
    delBtn.className = 'btn-delete-row';
    delBtn.textContent = '삭제';
    delBtn.title = '원문 삭제';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteRawDoc(doc.id, doc.title || doc.id);
    });
    actionCell.appendChild(delBtn);

    row.append(
      selectCell,
      makeCell(doc.title || doc.id, doc.id),
      makeTextCell(doc.source_uri || '로컬 파일'),
      makeTextCell(`${formatNumber(doc.char_count)}자`),
      makeTextCell(doc.created_at || '—'),
      actionCell
    );
    row.addEventListener('click', async () => {
      const raw = await window.llmwiki.getRaw(doc.id);
      if (raw && !raw.error) openRawModal(raw);
    });
    return row;
  }));
  updateLibraryBulkControls();
}

function libraryItemId(mode, item) {
  return mode === 'concepts' ? (item.concept_id || item.id) : item.id;
}

function pruneLibrarySelection(mode, validIds) {
  const selection = state.librarySelection[mode];
  for (const id of selection) {
    if (!validIds.has(id)) selection.delete(id);
  }
}

function setLibraryItemSelected(mode, id, selected) {
  const selection = state.librarySelection[mode];
  if (selected) selection.add(id);
  else selection.delete(id);
  updateLibraryBulkControls();
}

function setAllVisibleLibraryItems(mode, selected) {
  const selection = state.librarySelection[mode];
  for (const item of state.libraryVisible[mode]) {
    const id = libraryItemId(mode, item);
    if (selected) selection.add(id);
    else selection.delete(id);
  }
  if (mode === 'concepts') renderConcepts(state.libraryVisible.concepts);
  else renderRawDocs(state.libraryVisible.raw);
}

function selectAllVisibleLibraryItems() {
  setAllVisibleLibraryItems(state.libraryMode, true);
}

function clearLibrarySelection() {
  const mode = state.libraryMode;
  state.librarySelection[mode].clear();
  if (mode === 'concepts') renderConcepts(state.libraryVisible.concepts);
  else renderRawDocs(state.libraryVisible.raw);
}

function updateLibraryBulkControls() {
  const mode = state.libraryMode;
  const selection = state.librarySelection[mode];
  const visibleIds = state.libraryVisible[mode].map(item => libraryItemId(mode, item)).filter(Boolean);
  const selectedVisibleCount = visibleIds.filter(id => selection.has(id)).length;
  const allVisibleSelected = visibleIds.length > 0 && selectedVisibleCount === visibleIds.length;

  document.getElementById('library-selected-count').textContent = `${selection.size}개 선택`;
  document.getElementById('btn-clear-selection').disabled = selection.size === 0;
  document.getElementById('btn-delete-selected').disabled = selection.size === 0;
  document.getElementById('btn-select-visible').disabled = !visibleIds.length || allVisibleSelected;

  const master = document.getElementById(mode === 'concepts' ? 'concept-select-all' : 'raw-select-all');
  master.checked = allVisibleSelected;
  master.indeterminate = selectedVisibleCount > 0 && !allVisibleSelected;
}

async function deleteSelectedLibraryItems() {
  const mode = state.libraryMode;
  const selection = state.librarySelection[mode];
  const ids = Array.from(selection);
  if (!ids.length) return;

  const kind = mode === 'concepts' ? '개념' : '원문';
  const ok = confirm(`선택한 ${kind} ${ids.length}개를 모두 삭제하시겠습니까?\n\n이 작업은 되돌릴 수 없습니다.`);
  if (!ok) return;

  const button = document.getElementById('btn-delete-selected');
  const previous = button.textContent;
  button.disabled = true;
  const failed = [];

  for (let i = 0; i < ids.length; i++) {
    const id = ids[i];
    button.textContent = `삭제 중 ${i + 1}/${ids.length}`;
    try {
      const res = mode === 'concepts'
        ? await window.llmwiki.deleteConcept(id)
        : await window.llmwiki.deleteRaw(id);
      if (!res || res.error || res.success === false) failed.push(id);
      else selection.delete(id);
    } catch (err) {
      failed.push(id);
    }
  }

  if (mode === 'concepts' && ids.includes(state.selectedNode?.id)) closeInspector();
  if (mode === 'raw' && ids.includes(state.currentRawDoc?.id)) {
    document.getElementById('modal-raw').classList.add('hidden');
  }

  button.textContent = previous;
  await refreshAll();
  updateLibraryBulkControls();

  if (failed.length) {
    alert(`${ids.length - failed.length}개 삭제 완료, ${failed.length}개 삭제 실패\n\n실패 ID: ${failed.join(', ')}`);
  }
}

async function deleteRawDoc(docId, title) {
  const name = title || docId;
  const ok = confirm(`정말로 이 원문 문서를 삭제하시겠습니까?\n\n"${name}"\n\n(문서 파일과 DB 기록이 삭제되며, 연결된 개념의 출처 참조가 정리됩니다.)`);
  if (!ok) return;

  try {
    const res = await window.llmwiki.deleteRaw(docId);
    if (res && res.error) {
      alert(`삭제 실패: ${res.error}`);
      return;
    }
    if (state.currentRawDoc?.id === docId) {
      document.getElementById('modal-raw').classList.add('hidden');
    }
    await refreshAll();
  } catch (err) {
    alert(`삭제 중 오류가 발생했습니다: ${err.message || err}`);
  }
}

async function deleteConcept(conceptId, title) {
  const name = title || conceptId;
  const ok = confirm(
    `정말로 이 개념을 삭제하시겠습니까?\n\n"${name}"\n\n개념 카드, 검색 인덱스, 그래프 연결이 함께 정리됩니다.`
  );
  if (!ok) return;

  try {
    const res = await window.llmwiki.deleteConcept(conceptId);
    if (res && res.error) {
      alert(`삭제 실패: ${res.error}`);
      return;
    }
    if (state.selectedNode?.id === conceptId) closeInspector();
    await refreshAll();
  } catch (err) {
    alert(`삭제 중 오류가 발생했습니다: ${err.message || err}`);
  }
}

function makeCell(primary, secondary) {
  const cell = document.createElement('td');
  const main = document.createElement('span');
  main.className = 'primary-cell';
  main.textContent = primary;
  const sub = document.createElement('span');
  sub.className = 'secondary-cell';
  sub.textContent = secondary;
  cell.append(main, sub);
  return cell;
}

function makeTextCell(value) {
  const cell = document.createElement('td');
  cell.textContent = value;
  return cell;
}

function makeBadgeCell(value) {
  const cell = document.createElement('td');
  const badge = document.createElement('span');
  badge.className = 'badge badge-type';
  badge.textContent = value;
  cell.appendChild(badge);
  return cell;
}

function makeTagsCell(tags) {
  const cell = document.createElement('td');
  const wrap = document.createElement('span');
  wrap.className = 'tags-container';
  tags.forEach(tag => {
    const badge = document.createElement('span');
    badge.className = 'badge badge-tag';
    badge.textContent = tag;
    wrap.appendChild(badge);
  });
  cell.appendChild(wrap);
  return cell;
}

function renderEmptyRow(body, columns, copy) {
  const row = document.createElement('tr');
  const cell = document.createElement('td');
  cell.colSpan = columns;
  cell.className = 'empty-row';
  cell.textContent = copy;
  row.appendChild(cell);
  body.replaceChildren(row);
}

// Activity
function setupActivity() {
  document.getElementById('btn-refresh-logs').addEventListener('click', loadLogs);
}

async function loadLogs() {
  const logs = (await window.llmwiki.getLogs()) || [];
  const body = document.getElementById('logs-tbody');
  if (!logs.length) return renderEmptyRow(body, 5, '아직 기록된 활동이 없습니다.');
  body.replaceChildren(...logs.map(log => {
    const row = document.createElement('tr');
    row.append(
      makeTextCell(log.timestamp || '—'),
      makeBadgeCell((log.caller || 'cli').toUpperCase()),
      makeTextCell(log.action || '—'),
      makeTextCell(log.query || '—'),
      makeTextCell(`${formatNumber(log.result_count)}개`),
    );
    return row;
  }));
}

// Import
function setupIngest() {
  const dropZone = document.getElementById('file-drop-zone');
  const input = document.getElementById('ingest-url-input');
  const abortBtn = document.getElementById('btn-abort-ingest');

  if (abortBtn) {
    abortBtn.addEventListener('click', async () => {
      abortBtn.disabled = true;
      abortBtn.textContent = '중단 요청 중…';
      try {
        await window.llmwiki.abortIngest();
        setProgress({
          title: '탐색 중단됨',
          percent: 100,
          detail: '사용자 요청으로 탐색이 중단되었습니다. 현재까지 추출된 지식은 그래프에 안전하게 보존되었습니다.',
          state: 'error'
        });
      } catch (err) {
        console.error('Abort failed:', err);
      } finally {
        abortBtn.classList.add('hidden');
        abortBtn.disabled = false;
        abortBtn.textContent = '탐색 중단 (Stop)';
        await refreshAll();
      }
    });
  }

  // Live real-time ingestion stream listener
  if (window.llmwiki?.onIngestEvent) {
    window.llmwiki.onIngestEvent(async (evt) => {
      if (evt.event === 'page_crawled') {
        setProgress({
          title: '웹 문서 크롤링 중…',
          detail: `[p.${evt.page_index || '•'}] ${evt.title || evt.source}`,
          count: '문서 수집',
          indeterminate: true,
          state: 'running'
        });
      } else if (evt.event === 'concept_created') {
        setProgress({
          title: '새 개념 증류 및 그래프 연결…',
          detail: `✨ [[${evt.concept.name}]] (${evt.concept.type}) 추가됨`,
          count: '개념 증류',
          indeterminate: true,
          state: 'running'
        });
        // 즉각적인 그래프 및 통계 라이브 갱신
        loadGraph(state.currentWorkspace);
        loadStats(state.currentWorkspace);
      }
    });
  }

  const greedyToggle = document.getElementById('ingest-explore-greedy');
  if (greedyToggle) {
    greedyToggle.addEventListener('change', () => {
      const pagesInput = document.getElementById('ingest-explore-pages');
      const helpText = document.getElementById('web-explore-help-text');
      if (greedyToggle.checked) {
        pagesInput.disabled = true;
        pagesInput.value = '0';
        if (helpText) helpText.textContent = '🚀 그리디 모드: 최대 문서 수 제한 없이 연결된 모든 관련 링크를 무한 탐색합니다.';
      } else {
        pagesInput.disabled = false;
        pagesInput.value = '8';
        if (helpText) helpText.textContent = '같은 사이트의 관련 문서만 따라갑니다. 원하는 깊이와 최대 문서 수를 직접 숫자로 입력할 수 있습니다.';
      }
    });
  }

  document.getElementById('btn-ingest-url').addEventListener('click', async () => {
    const url = input.value.trim();
    if (!url) {
      return setProgress({
        title: '주소를 입력해 주세요',
        percent: 0,
        detail: '가져올 웹페이지 URL이 필요합니다.',
        state: 'error'
      });
    }
    const button = document.getElementById('btn-ingest-url');
    const explore = document.getElementById('ingest-explore-web').checked;
    const greedy = document.getElementById('ingest-explore-greedy')?.checked || false;
    const depth = Number(document.getElementById('ingest-explore-depth').value || 1);
    const maxPages = greedy ? 0 : Number(document.getElementById('ingest-explore-pages').value || 8);
    button.disabled = true;
    if (abortBtn) {
      abortBtn.classList.remove('hidden');
      abortBtn.disabled = false;
      abortBtn.textContent = '탐색 중단 (Stop)';
    }
    setProgress({
      title: explore ? (greedy ? '🚀 그리디 무한 탐색 중…' : '연결 문서 탐색 중…') : '웹페이지 분석 중…',
      percent: 30,
      detail: explore ? (greedy ? `깊이 ${depth} · 무한(그리디) 탐색 · ${url}` : `깊이 ${depth} · 최대 ${maxPages}개 문서 · ${url}`) : url,
      count: explore ? '탐색 중' : '파싱 중',
      indeterminate: true,
      state: 'running'
    });

    beginLiveGraphRefresh();
    try {
      const targetWs = document.getElementById('ingest-target-workspace')?.value || state.currentWorkspace;
      const result = await window.llmwiki.ingest([url], { explore, depth, maxPages, greedy, workspace: targetWs });
      if (result && !result.error) {
        const documents = result.total_pages || (Array.isArray(result) ? result.length : (result.results ? result.results.length : 1));
        const concepts = result.total_concepts !== undefined ? result.total_concepts : (Array.isArray(result)
          ? result.reduce((acc, r) => acc + (r.concepts?.length || 0), 0)
          : (result.concepts?.length || 0));
        const interruptedNotice = result.interrupted ? ' (중단 시점까지 보존됨)' : '';
        setProgress({
          title: result.interrupted ? '탐색 중단 완료' : '가져오기 완료',
          percent: 100,
          detail: `${documents}개 문서에서 ${concepts}개 개념을 정리해 지식 베이스에 추가했습니다.${interruptedNotice}`,
          count: '완료',
          state: 'success'
        });

        input.value = '';
        await refreshAll();
      } else {
        setProgress({
          title: '가져오기 실패',
          percent: 100,
          detail: result?.error || '웹페이지를 읽어오지 못했습니다.',
          state: 'error'
        });
      }
    } catch (err) {
      setProgress({
        title: '오류 발생',
        percent: 100,
        detail: err.message || '요청 처리 중 오류가 발생했습니다.',
        state: 'error'
      });
    } finally {
      await endLiveGraphRefresh();
      if (abortBtn) abortBtn.classList.add('hidden');
      button.disabled = false;
    }
  });

  document.getElementById('btn-select-file').addEventListener('click', async () => {
    const files = await window.llmwiki.selectFiles();
    addSelectedFiles(files || []);
  });

  document.getElementById('btn-clear-files').addEventListener('click', () => {
    state.selectedFiles = [];
    renderSelectedFiles();
    const box = document.getElementById('ingest-status-box');
    box.classList.add('hidden');
  });

  document.getElementById('btn-batch-ingest').addEventListener('click', async event => {
    if (!state.selectedFiles.length) return;
    const batchButton = event.currentTarget;
    const selectButton = document.getElementById('btn-select-file');
    const clearButton = document.getElementById('btn-clear-files');
    batchButton.disabled = true;
    selectButton.disabled = true;
    clearButton.disabled = true;

    const total = state.selectedFiles.length;
    let completed = 0;
    let totalConcepts = 0;
    let failed = 0;

    setProgress({
      title: `${total}개 파일 가져오기 시작…`,
      percent: 0,
      detail: '문서를 읽고 원문 보관 및 개념을 추출합니다.',
      count: `0 / ${total} (0%)`,
      state: 'running'
    });

    beginLiveGraphRefresh();
    for (let i = 0; i < total; i++) {
      const fileItem = state.selectedFiles[i];
      fileItem.status = 'processing';
      renderSelectedFiles();

      const currentPercent = Math.round((i / total) * 100);
      setProgress({
        title: `[${i + 1}/${total}] ${fileItem.name} 처리 중…`,
        percent: currentPercent,
        detail: `현재 파일: ${fileItem.name}`,
        count: `${i} / ${total} (${currentPercent}%)`,
        state: 'running'
      });

      try {
        const targetWs = document.getElementById('ingest-target-workspace')?.value || state.currentWorkspace;
        const result = await window.llmwiki.ingest([fileItem.path], { workspace: targetWs });
        if (result && !result.error) {
          fileItem.status = 'success';
          const concepts = Array.isArray(result)
            ? result.reduce((acc, r) => acc + (r.concepts?.length || 0), 0)
            : (result.concepts?.length || 0);
          fileItem.conceptsCount = concepts;
          totalConcepts += concepts;
          completed++;
        } else {
          fileItem.status = 'error';
          fileItem.error = result?.error || '처리 실패';
          failed++;
        }
      } catch (err) {
        fileItem.status = 'error';
        fileItem.error = err.message || '오류 발생';
        failed++;
      }

      renderSelectedFiles();
      const nextPercent = Math.round(((i + 1) / total) * 100);
      setProgress({
        title: `[${i + 1}/${total}] ${fileItem.name} 완료`,
        percent: nextPercent,
        detail: fileItem.status === 'success'
          ? `${fileItem.conceptsCount || 0}개 개념 추출 완료`
          : `오류: ${fileItem.error}`,
        count: `${i + 1} / ${total} (${nextPercent}%)`,
        state: 'running'
      });
    }

    if (failed === 0) {
      setProgress({
        title: `전체 ${total}개 파일 가져오기 완료`,
        percent: 100,
        detail: `총 ${totalConcepts}개 개념이 생성되어 저장되었습니다.`,
        count: `${total} / ${total} (100%)`,
        state: 'success'
      });
    } else {
      setProgress({
        title: `${total}개 중 ${completed}개 성공, ${failed}개 실패`,
        percent: 100,
        detail: `일부 파일 처리 중 문제가 발생했습니다. (${failed}개 실패)`,
        count: `${completed} / ${total}`,
        state: completed === 0 ? 'error' : 'warning'
      });
    }

    batchButton.disabled = false;
    selectButton.disabled = false;
    clearButton.disabled = false;

    await endLiveGraphRefresh();
    await refreshAll();
  });

  ['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => {
    event.preventDefault();
    dropZone.classList.add('dragging');
  }));
  ['dragleave', 'drop'].forEach(type => dropZone.addEventListener(type, event => {
    event.preventDefault();
    dropZone.classList.remove('dragging');
  }));
  dropZone.addEventListener('drop', event => {
    const paths = Array.from(event.dataTransfer?.files || [])
      .map(file => window.llmwiki.getPathForFile?.(file))
      .filter(Boolean);
    addSelectedFiles(paths);
  });
}

function addSelectedFiles(paths) {
  const existingPaths = new Set(state.selectedFiles.map(f => f.path));
  paths.forEach(p => {
    if (!existingPaths.has(p)) {
      state.selectedFiles.push({
        path: p,
        name: p.split('/').pop(),
        status: 'pending',
        conceptsCount: 0,
        error: ''
      });
      existingPaths.add(p);
    }
  });
  renderSelectedFiles();
}

function renderSelectedFiles() {
  const container = document.getElementById('multi-files-container');
  container.classList.toggle('hidden', !state.selectedFiles.length);
  document.getElementById('multi-files-count').textContent = `${state.selectedFiles.length}개 파일`;
  const list = document.getElementById('selected-files-list');
  list.replaceChildren(...state.selectedFiles.map(item => {
    const li = document.createElement('li');
    li.className = 'file-item-row';

    const nameSpan = document.createElement('span');
    nameSpan.className = 'file-name';
    nameSpan.textContent = item.name;
    nameSpan.title = item.path;

    const badge = document.createElement('span');
    badge.className = `badge file-status-${item.status}`;
    if (item.status === 'pending') {
      badge.textContent = '대기 중';
    } else if (item.status === 'processing') {
      badge.textContent = '처리 중…';
    } else if (item.status === 'success') {
      badge.textContent = `완료 (${item.conceptsCount || 0}개 개념)`;
    } else if (item.status === 'error') {
      badge.textContent = '실패';
      if (item.error) badge.title = item.error;
    }

    li.append(nameSpan, badge);
    return li;
  }));
}

function setProgress({ title, percent, detail, count, state: pState = 'running', indeterminate = false }) {
  const box = document.getElementById('ingest-status-box');
  box.classList.remove('hidden');
  box.classList.toggle('error', pState === 'error');
  box.classList.toggle('success', pState === 'success');

  const dot = document.getElementById('ingest-status-dot');
  dot.className = 'status-dot' + (pState === 'success' ? ' done' : (pState === 'error' ? ' error' : ''));

  document.getElementById('ingest-status-title').textContent = title || '';
  document.getElementById('ingest-progress-percent').textContent = indeterminate ? '…' : `${percent}%`;

  const bar = document.getElementById('ingest-progress-bar');
  bar.classList.toggle('indeterminate', Boolean(indeterminate));
  bar.style.width = indeterminate ? '100%' : `${percent}%`;

  document.getElementById('ingest-status-detail').textContent = detail || '';
  document.getElementById('ingest-status-count').textContent = count || '';
}

// Modals
function setupModals() {
  const rawModal = document.getElementById('modal-raw');
  document.getElementById('btn-close-modal').addEventListener('click', () => rawModal.classList.add('hidden'));
  rawModal.addEventListener('click', event => {
    if (event.target === rawModal) rawModal.classList.add('hidden');
  });

  const searchInput = document.getElementById('modal-raw-search');
  searchInput.addEventListener('input', (e) => {
    renderRawModalContent(e.target.value.trim());
  });

  const copyBtn = document.getElementById('btn-copy-raw');
  copyBtn.addEventListener('click', async () => {
    if (!state.currentRawDoc?.content) return;
    try {
      await navigator.clipboard.writeText(state.currentRawDoc.content);
      const prev = copyBtn.textContent;
      copyBtn.textContent = '복사됨 ✓';
      setTimeout(() => { copyBtn.textContent = prev; }, 1500);
    } catch (err) {
      alert('클립보드 복사에 실패했습니다.');
    }
  });

  const deleteBtn = document.getElementById('btn-delete-current-raw');
  deleteBtn.addEventListener('click', () => {
    if (state.currentRawDoc?.id) {
      deleteRawDoc(state.currentRawDoc.id, state.currentRawDoc.title);
    }
  });

  window.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      document.getElementById('modal-raw').classList.add('hidden');
      document.getElementById('modal-settings').classList.add('hidden');
      document.getElementById('modal-workspaces').classList.add('hidden');
      closeInspector();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
      const target = state.activeTab === 'tab-graph'
        ? document.getElementById('graph-search-input')
        : state.activeTab === 'tab-library'
          ? document.getElementById('db-search-input')
          : null;
      if (target) {
        event.preventDefault();
        target.focus();
        target.select();
      }
    }
  });
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function renderRawModalContent(query = '') {
  const contentEl = document.getElementById('modal-raw-content');
  const countEl = document.getElementById('modal-raw-match-count');
  const rawText = state.currentRawDoc?.content || '';

  if (!query) {
    countEl.textContent = '';
    contentEl.textContent = rawText;
    return;
  }

  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(`(${escapedQuery})`, 'gi');
  const matches = rawText.match(regex);
  const matchCount = matches ? matches.length : 0;

  countEl.textContent = matchCount > 0 ? `${matchCount}개 일치` : '일치 없음';

  if (!matchCount) {
    contentEl.textContent = rawText;
    return;
  }

  const parts = rawText.split(regex);
  contentEl.innerHTML = parts.map(part => {
    if (part.toLowerCase() === query.toLowerCase()) {
      return `<mark class="search-highlight">${escapeHtml(part)}</mark>`;
    }
    return escapeHtml(part);
  }).join('');
}

function openRawModal(rawDocOrTitle, content) {
  let doc;
  if (typeof rawDocOrTitle === 'object' && rawDocOrTitle !== null) {
    doc = rawDocOrTitle;
  } else {
    doc = {
      id: '',
      title: rawDocOrTitle || '원문',
      content: content || '',
      source_uri: '',
      char_count: content ? content.length : 0,
      created_at: ''
    };
  }
  state.currentRawDoc = doc;

  document.getElementById('modal-raw-title').textContent = doc.title || doc.id || '원문';
  document.getElementById('modal-raw-id').textContent = doc.id ? doc.id.slice(0, 16) : 'RAW';
  document.getElementById('modal-raw-chars').textContent = `${formatNumber(doc.char_count || doc.content?.length || 0)}자`;
  document.getElementById('modal-raw-date').textContent = doc.created_at || '';

  const sourceEl = document.getElementById('modal-raw-source');
  sourceEl.textContent = doc.source_uri ? `출처: ${doc.source_uri}` : '';
  sourceEl.style.display = doc.source_uri ? 'block' : 'none';

  const searchInput = document.getElementById('modal-raw-search');
  searchInput.value = '';
  renderRawModalContent('');

  const deleteBtn = document.getElementById('btn-delete-current-raw');
  deleteBtn.style.display = doc.id ? 'inline-flex' : 'none';

  document.getElementById('modal-raw').classList.remove('hidden');
}

// Settings
function setupSettings() {
  const modal = document.getElementById('modal-settings');
  document.getElementById('btn-open-settings').addEventListener('click', openSettings);
  document.getElementById('btn-close-settings').addEventListener('click', () => modal.classList.add('hidden'));
  modal.addEventListener('click', event => {
    if (event.target === modal) modal.classList.add('hidden');
  });
  document.querySelectorAll('.engine-card').forEach(card => {
    card.addEventListener('click', event => {
      if (card.disabled || event.target.matches('select')) return;
      selectProvider(card.dataset.provider);
    });
  });
  document.getElementById('select-ollama-model').addEventListener('click', event => event.stopPropagation());
  const btnConnectAntigravity = document.getElementById('btn-connect-antigravity');
  if (btnConnectAntigravity) {
    btnConnectAntigravity.addEventListener('click', () => connectAgent('antigravity'));
  }
  document.getElementById('btn-connect-codex').addEventListener('click', () => connectAgent('codex'));
  document.getElementById('btn-connect-claude').addEventListener('click', () => connectAgent('claude'));
  document.getElementById('btn-save-settings').addEventListener('click', saveSettings);
}

async function openSettings() {
  document.getElementById('modal-settings').classList.remove('hidden');
  setSettingsMessage('설치된 도구를 확인하고 있습니다.');
  await refreshEnvironment();
  setSettingsMessage('');
}

async function refreshEnvironment() {
  const response = await window.llmwiki.checkEnv();
  if (!response || response.error) {
    setSettingsMessage(response?.error || '환경을 확인하지 못했습니다.', 'error');
    return;
  }
  state.environment = response;
  const { env, config } = response;
  updateEngine('ollama', Boolean(env.ollama?.running && env.ollama?.models?.length));
  updateEngine('antigravity_cli', Boolean(env.antigravity?.cli_detected || env.antigravity?.detected));
  updateEngine('codex_cli', Boolean(env.codex?.detected));
  updateEngine('claude_cli', Boolean(env.claude?.detected));
  const modelSelect = document.getElementById('select-ollama-model');
  modelSelect.replaceChildren(...(env.ollama?.models || []).map(model => {
    const option = document.createElement('option');
    option.value = model;
    option.textContent = model;
    option.selected = model === config?.model;
    return option;
  }));
  modelSelect.classList.toggle('hidden', !env.ollama?.models?.length);
  const normalizedProvider = config?.provider === 'antigravity' ? 'antigravity_cli' : config?.provider;
  const validCurrent = ['ollama', 'antigravity_cli', 'codex_cli', 'claude_cli'].includes(normalizedProvider)
    && !document.querySelector(`.engine-card[data-provider="${normalizedProvider}"]`)?.disabled;
  selectProvider(validCurrent ? normalizedProvider : env.recommended_provider);
  updateConnectionRow('antigravity', env.antigravity);
  updateConnectionRow('codex', env.codex);
  updateConnectionRow('claude', env.claude);
  updateTopBarEngine(config, env);
}

function updateEngine(provider, available) {
  const card = document.querySelector(`.engine-card[data-provider="${provider}"]`);
  if (!card) return;
  card.disabled = !available;
  const statusId = provider === 'ollama' ? 'status-ollama' : `status-${provider.replace(/_/g, '-')}`;
  const statusEl = document.getElementById(statusId);
  if (statusEl) {
    statusEl.classList.toggle('online', available);
  }
}

function selectProvider(provider) {
  const card = document.querySelector(`.engine-card[data-provider="${provider}"]`);
  if (!card || card.disabled) return;
  state.selectedProvider = provider;
  document.querySelectorAll('.engine-card').forEach(item => item.classList.toggle('selected', item.dataset.provider === provider));
}

function updateConnectionRow(agent, info) {
  const copy = document.getElementById(`${agent}-connection-copy`);
  const button = document.getElementById(`btn-connect-${agent}`);
  if (!button || !copy) return;
  const isDetected = Boolean(info?.detected || info?.cli_detected);
  const isConnected = Boolean(info?.mcp_installed || info?.skill_installed);
  if (!isDetected) {
    copy.textContent = 'CLI/환경을 찾지 못했습니다';
    button.textContent = '사용 불가';
    button.disabled = true;
  } else if (isConnected) {
    copy.textContent = 'LLMWiki 연결됨';
    button.textContent = '연결됨';
    button.disabled = true;
  } else {
    copy.textContent = '설치됨 · 아직 연결되지 않음';
    button.textContent = '연결';
    button.disabled = false;
  }
}

async function connectAgent(agent) {
  const button = document.getElementById(`btn-connect-${agent}`);
  if (button) button.disabled = true;
  const agentName = agent === 'antigravity' ? 'Antigravity' : (agent === 'codex' ? 'Codex' : 'Claude Code');
  setSettingsMessage(`${agentName}에 연결하고 있습니다.`);
  const result = await window.llmwiki.installSkills({
    codex: agent === 'codex',
    claude: agent === 'claude',
    antigravity: agent === 'antigravity',
    symlink: agent === 'antigravity',
  });
  if (result?.error || result?.[agent] === false) {
    setSettingsMessage(result?.error || '연결하지 못했습니다.', 'error');
    if (button) button.disabled = false;
    return;
  }
  await refreshEnvironment();
  setSettingsMessage('연결했습니다. 에이전트를 다시 열면 사용할 수 있습니다.', 'success');
}

async function saveSettings() {
  const button = document.getElementById('btn-save-settings');
  button.disabled = true;
  setSettingsMessage('저장하고 있습니다.');
  const model = state.selectedProvider === 'ollama'
    ? document.getElementById('select-ollama-model').value
    : '';
  const result = await window.llmwiki.saveConfig({
    provider: state.selectedProvider,
    model,
    initialized: true,
  });
  button.disabled = false;
  if (!result || result.error) {
    setSettingsMessage(result?.error || '저장하지 못했습니다.', 'error');
    return;
  }
  setSettingsMessage('저장했습니다.', 'success');
  await refreshEnvironment();
  setTimeout(() => document.getElementById('modal-settings').classList.add('hidden'), 450);
}

function setSettingsMessage(message, type = '') {
  const element = document.getElementById('settings-message');
  element.textContent = message;
  element.className = `settings-message ${type}`.trim();
}

// Global Top Bar
function setupTopBar() {
  const engineBadge = document.getElementById('topbar-engine-badge');
  if (engineBadge) {
    engineBadge.addEventListener('click', openSettings);
  }
  const wsPill = document.getElementById('topbar-ws-pill');
  if (wsPill) {
    wsPill.addEventListener('click', () => {
      document.getElementById('modal-workspaces').classList.remove('hidden');
    });
  }
}

function updateTopBarEngine(config, env) {
  const dot = document.getElementById('topbar-engine-dot');
  const text = document.getElementById('topbar-engine-text');
  if (!text || !dot) return;

  const provider = config?.provider || env?.recommended_provider || 'ollama';
  let label = '';
  let online = false;

  if (provider === 'antigravity_cli' || provider === 'antigravity') {
    label = 'Antigravity CLI (agy)';
    online = Boolean(env?.antigravity?.cli_detected || env?.antigravity?.detected);
  } else if (provider === 'codex_cli') {
    label = 'Codex CLI';
    online = Boolean(env?.codex?.detected);
  } else if (provider === 'claude_cli' || provider === 'claude') {
    label = 'Claude Code';
    online = Boolean(env?.claude?.detected);
  } else if (provider === 'ollama') {
    const model = config?.model || (env?.ollama?.models?.[0] || '로컬');
    label = `Ollama (${model})`;
    online = Boolean(env?.ollama?.running);
  } else {
    label = provider;
    online = true;
  }

  text.innerHTML = `동작 모드: <strong>${escapeHtml(label)}</strong>`;
  dot.classList.toggle('online', online);
  dot.title = online ? '온라인 · 정상 동작' : '오프라인 / 감지 필요';
}

function updateTopBarWorkspace(wsId) {
  const el = document.getElementById('topbar-ws-name');
  if (!el) return;
  if (wsId === 'all') {
    el.textContent = '🌐 전체 (All Workspaces)';
  } else {
    const ws = state.workspaces.find(w => w.id === wsId);
    el.textContent = ws?.name ? `${ws.name} (${ws.id})` : wsId;
  }
}

// Workspaces Management
function setupWorkspaces() {
  const modal = document.getElementById('modal-workspaces');
  const openBtn = document.getElementById('btn-open-workspace-modal');
  const closeBtn = document.getElementById('btn-close-workspaces');
  if (openBtn) openBtn.addEventListener('click', () => modal.classList.remove('hidden'));
  if (closeBtn) closeBtn.addEventListener('click', () => modal.classList.add('hidden'));
  if (modal) {
    modal.addEventListener('click', e => {
      if (e.target === modal) modal.classList.add('hidden');
    });
  }

  const select = document.getElementById('workspace-select');
  if (select) {
    select.addEventListener('change', e => {
      switchWorkspace(e.target.value);
    });
  }

  const createBtn = document.getElementById('btn-create-workspace');
  if (createBtn) createBtn.addEventListener('click', createNewWorkspace);

  const routeBtn = document.getElementById('btn-test-route');
  if (routeBtn) routeBtn.addEventListener('click', testWorkspaceRouting);
}

async function loadWorkspaces() {
  const workspaces = (await window.llmwiki.listWorkspaces()) || [];
  state.workspaces = workspaces;
  const current = (await window.llmwiki.currentWorkspace())?.active_workspace || 'default';
  if (!state.currentWorkspace || state.currentWorkspace === 'default') {
    state.currentWorkspace = current;
  }

  const select = document.getElementById('workspace-select');
  if (select) {
    select.replaceChildren();
    const allOpt = document.createElement('option');
    allOpt.value = 'all';
    allOpt.textContent = '🌐 전체 (All Workspaces)';
    allOpt.selected = (state.currentWorkspace === 'all');
    select.appendChild(allOpt);

    workspaces.forEach(ws => {
      const opt = document.createElement('option');
      opt.value = ws.id;
      const count = ws.concepts || 0;
      opt.textContent = `${ws.name || ws.id} (${count})`;
      opt.selected = (ws.id === state.currentWorkspace);
      select.appendChild(opt);
    });
  }

  const ingestSelect = document.getElementById('ingest-target-workspace');
  if (ingestSelect) {
    ingestSelect.replaceChildren();
    workspaces.forEach(ws => {
      const opt = document.createElement('option');
      opt.value = ws.id;
      opt.textContent = `${ws.name || ws.id} (${ws.id})`;
      opt.selected = (ws.id === state.currentWorkspace || (state.currentWorkspace === 'all' && ws.id === 'default'));
      ingestSelect.appendChild(opt);
    });
  }

  renderWorkspacesTable(workspaces, state.currentWorkspace);
  updateTopBarWorkspace(state.currentWorkspace);
}

function renderWorkspacesTable(workspaces, activeId) {
  const tbody = document.getElementById('workspaces-tbody');
  if (!tbody) return;
  if (!workspaces.length) {
    return renderEmptyRow(tbody, 6, '등록된 워크스페이스가 없습니다.');
  }

  tbody.replaceChildren(...workspaces.map(ws => {
    const tr = document.createElement('tr');
    const isActive = ws.id === activeId;

    const idCell = document.createElement('td');
    const code = document.createElement('code');
    code.textContent = ws.id;
    idCell.appendChild(code);

    const nameCell = makeTextCell(ws.name || '—');
    const descCell = makeTextCell(ws.description || '—');
    const countCell = makeTextCell(`${formatNumber(ws.concepts || 0)}개`);

    const statusCell = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = isActive ? 'badge badge-type' : 'badge badge-tag';
    badge.textContent = isActive ? '현재 활성' : '대기';
    statusCell.appendChild(badge);

    const actionCell = document.createElement('td');
    actionCell.style.textAlign = 'center';
    if (ws.id !== 'default') {
      const delBtn = document.createElement('button');
      delBtn.className = 'btn-delete-row';
      delBtn.textContent = '삭제';
      delBtn.title = '워크스페이스 삭제';
      delBtn.addEventListener('click', () => deleteWorkspace(ws.id));
      actionCell.appendChild(delBtn);
    } else {
      actionCell.textContent = '기본값';
    }

    tr.append(idCell, nameCell, descCell, countCell, statusCell, actionCell);
    return tr;
  }));
}

async function switchWorkspace(wsId) {
  state.currentWorkspace = wsId;
  if (wsId !== 'all') {
    await window.llmwiki.switchWorkspace(wsId);
  }
  const select = document.getElementById('workspace-select');
  if (select) select.value = wsId;
  const ingestSelect = document.getElementById('ingest-target-workspace');
  if (ingestSelect && wsId !== 'all') ingestSelect.value = wsId;

  await refreshAll();
  await loadWorkspaces();
  updateTopBarWorkspace(state.currentWorkspace);
}

async function createNewWorkspace() {
  const idInput = document.getElementById('input-new-ws-id');
  const nameInput = document.getElementById('input-new-ws-name');
  const descInput = document.getElementById('input-new-ws-desc');
  const createBtn = document.getElementById('btn-create-workspace');

  let rawId = idInput.value.trim();
  const name = nameInput.value.trim();
  const desc = descInput.value.trim();

  if (!rawId && !name) {
    alert('워크스페이스 이름 또는 ID를 입력해주세요.');
    if (nameInput) nameInput.focus();
    return;
  }

  // Auto-generate clean slug ID from name if ID is omitted
  if (!rawId) {
    rawId = name;
  }

  // Sanitize: allow unicode letters/digits, convert whitespace and specials to underscore
  let finalId = rawId.toLowerCase()
    .replace(/[^\w가-힣0-9_-]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');

  if (!finalId) {
    finalId = 'ws_' + Date.now().toString(36);
  }

  const displayName = name || rawId || finalId;

  if (createBtn) {
    createBtn.disabled = true;
    createBtn.textContent = '생성 중…';
  }

  try {
    const res = await window.llmwiki.createWorkspace({ id: finalId, name: displayName, desc });
    if (res?.error) {
      alert(`생성 실패: ${res.error}`);
      return;
    }

    const createdId = res.id || finalId;
    idInput.value = '';
    nameInput.value = '';
    descInput.value = '';

    await loadWorkspaces();
    await switchWorkspace(createdId);
  } catch (err) {
    alert(`워크스페이스 생성 중 오류가 발생했습니다: ${err.message || err}`);
  } finally {
    if (createBtn) {
      createBtn.disabled = false;
      createBtn.textContent = '워크스페이스 생성';
    }
  }
}

async function deleteWorkspace(id) {
  if (id === 'default') {
    alert('기본(default) 워크스페이스는 삭제할 수 없습니다.');
    return;
  }
  if (!confirm(`'${id}' 워크스페이스를 삭제하시겠습니까? 해당 워크스페이스의 개념들은 기본 워크스페이스로 귀속되거나 정리됩니다.`)) {
    return;
  }
  const res = await window.llmwiki.deleteWorkspace(id);
  if (res?.error) {
    alert(`삭제 실패: ${res.error}`);
    return;
  }
  await switchWorkspace('default');
}

async function testWorkspaceRouting() {
  const queryInput = document.getElementById('input-route-query');
  const resultBox = document.getElementById('route-result-box');
  const query = queryInput.value.trim();
  if (!query) {
    resultBox.classList.remove('hidden');
    resultBox.textContent = '질의어를 입력해주세요.';
    return;
  }

  resultBox.classList.remove('hidden');
  resultBox.textContent = '질의어를 임베딩하고 워크스페이스 중심점과 비교 분석 중…';

  const res = await window.llmwiki.routeWorkspace(query);
  if (res && res.recommended_workspace) {
    const similarity = res.score ? (res.score * 100).toFixed(1) : 0;
    resultBox.innerHTML = `<strong>추천 워크스페이스:</strong> <code style="color:#58a6ff; font-weight:700;">${res.recommended_workspace}</code> (유사도: ${similarity}%)<br><small style="color:var(--muted)">검색 및 질의 시 이 워크스페이스가 최우선으로 매칭됩니다.</small>`;
  } else {
    resultBox.innerHTML = `<strong>결과:</strong> 특정 워크스페이스와의 유사도 임계치를 넘지 않아 <code style="color:#58a6ff;">전체(All)</code> 대상 전역 탐색이 권장됩니다.`;
  }
}
