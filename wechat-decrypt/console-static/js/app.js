/* 微信助手控制台 —— SPA 路由与布局 */

import { h, clear, toast } from './ui.js';
import {
  health, status, start_monitor, stop_monitor, restart_monitor, list_targets,
  reliable_pipeline_status, reliable_scheduler_status
} from './api.js';

const ROUTES = {
  overview: '#/overview',
  targets: '#/targets',
  pipeline: '#/pipeline',
  triggers: '#/triggers',
  knowledge: '#/knowledge',
  agent: '#/agent',
};

const routeMap = {
  'overview': () => import('./pages/overview.js'),
  'targets': () => import('./pages/targets.js'),
  'pipeline': () => import('./pages/pipeline.js'),
  'triggers': () => import('./pages/triggers.js'),
  'knowledge': () => import('./pages/knowledge.js'),
  'agent': () => import('./pages/agent.js'),
};

let currentCleanup = null;
let pollingTimer = null;

function getPageName() {
  const hash = window.location.hash || '';
  const name = hash.replace(/^#\//, '').split('/')[0];
  return routeMap[name] ? name : 'overview';
}

function setActiveNav(name) {
  for (const link of document.querySelectorAll('.side-nav a')) {
    link.classList.toggle('active', link.dataset.page === name);
  }
}

function setChip(name, text, kind) {
  const chip = document.querySelector(`.side-status [data-chip="${name}"]`);
  if (!chip) return;
  chip.textContent = text;
  chip.className = 'chip' + (kind ? ' ' + kind : '');
}

async function loadChips() {
  // health
  try {
    await health();
    setChip('health', '控制面: 就绪', 'ok');
  } catch (e) {
    setChip('health', '控制面: 离线', 'err');
  }

  // monitor
  try {
    const s = await status();
    if (s.monitor_running) {
      setChip('monitor', `监听: 运行中 PID ${s.monitor_pid || '?'}`, 'ok');
    } else {
      setChip('monitor', '监听: 已停止', 'muted');
    }
  } catch (e) {
    setChip('monitor', '监听: 未知', 'err');
  }

  // enabled targets
  try {
    const targets = await list_targets('all');
    const enabled = targets.filter(t => t.enabled === true).length;
    setChip('targets', `已启用目标: ${enabled}`, 'ok');
  } catch (e) {
    setChip('targets', '已启用目标: 未知', 'err');
  }

  // scheduler
  try {
    const pipe = await reliable_pipeline_status();
    const sched = await reliable_scheduler_status();
    const running = sched.scheduler_running === true || sched.running === true || pipe.scheduler_running === true;
    setChip('scheduler', running ? '自动回复: 运行中' : '自动回复: 已停止', running ? 'ok' : 'muted');
  } catch (e) {
    setChip('scheduler', '自动回复: 未知', 'err');
  }
}

function showGlobalError(root, msg, onRetry) {
  clear(root);
  root.appendChild(h('div', { class: 'error-banner' },
    h('span', { text: msg }),
    h('button', { class: 'btn btn-primary btn-sm', text: '重试', onclick: onRetry })
  ));
}

async function renderPage(name) {
  const root = document.getElementById('page');
  if (!root) return;
  clear(root);
  setActiveNav(name);

  try {
    await health();
  } catch (e) {
    showGlobalError(root, '控制面离线', () => renderPage(name));
    return;
  }

  clear(root);
  root.appendChild(h('div', { class: 'spinner', text: '加载中…' }));

  try {
    const mod = await routeMap[name]();
    if (typeof mod.render !== 'function') {
      throw new Error('页面未导出 render 函数');
    }
    clear(root);
    const cleanup = await mod.render(root);
    if (typeof cleanup === 'function') {
      currentCleanup = cleanup;
    }
  } catch (e) {
    clear(root);
    showGlobalError(root, '页面渲染失败: ' + (e.message || e), () => renderPage(name));
  }
}

function onHashChange() {
  if (typeof currentCleanup === 'function') {
    try { currentCleanup(); } catch (e) { console.error(e); }
    currentCleanup = null;
  }
  const name = getPageName();
  window.location.hash = '#/' + name;
  renderPage(name);
}

function ensureHash() {
  const name = getPageName();
  if (!window.location.hash || !routeMap[name]) {
    window.location.hash = '#/overview';
  }
}

async function pollStatus(targetRunning, onDone) {
  const start = Date.now();
  const maxMs = 12000;
  const interval = 500;

  function stop() {
    if (pollingTimer) {
      clearTimeout(pollingTimer);
      pollingTimer = null;
    }
  }

  async function tick() {
    try {
      const s = await status();
      if (targetRunning ? s.monitor_running : !s.monitor_running) {
        stop();
        await loadChips();
        if (typeof onDone === 'function') onDone();
        return;
      }
    } catch (e) {
      // keep polling
    }
    if (Date.now() - start < maxMs) {
      pollingTimer = setTimeout(tick, interval);
    } else {
      stop();
      await loadChips();
      toast('状态同步超时，请手动刷新', 'info');
    }
  }

  tick();
}

function bindMonitorButtons() {
  document.getElementById('btn-start').addEventListener('click', async () => {
    try {
      await start_monitor();
      await pollStatus(true, () => toast('监听已启动', 'ok'));
    } catch (e) {
      toast(e.message || '启动失败', 'err');
    }
  });

  document.getElementById('btn-stop').addEventListener('click', async () => {
    try {
      await stop_monitor();
      await pollStatus(false, () => toast('监听已停止', 'ok'));
    } catch (e) {
      toast(e.message || '停止失败', 'err');
    }
  });

  document.getElementById('btn-restart').addEventListener('click', async () => {
    try {
      await restart_monitor();
      await pollStatus(true, () => toast('监听已重启', 'ok'));
    } catch (e) {
      toast(e.message || '重启失败', 'err');
    }
  });

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    await loadChips();
    const name = getPageName();
    await renderPage(name);
    toast('数据已刷新', 'ok');
  });
}

function init() {
  ensureHash();
  bindMonitorButtons();
  loadChips();
  window.addEventListener('hashchange', onHashChange);
  onHashChange();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
