/* 微信助手控制台 —— API 客户端 */

const DEFAULT_BASE_URL = '';
const DEFAULT_TIMEOUT = 5;

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export const ControlAPIError = ApiError;
export { DEFAULT_BASE_URL, DEFAULT_TIMEOUT };

function encodeKey(key) {
  return encodeURIComponent(key).replace(/%20/g, '+');
}

function toQuery(params) {
  if (!params) return '';
  const parts = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) parts.push(`${encodeKey(k)}=${encodeKey(String(item))}`);
    } else {
      parts.push(`${encodeKey(k)}=${encodeKey(String(v))}`);
    }
  }
  return parts.length ? '?' + parts.join('&') : '';
}

async function _fetchWithTimeout(path, opts = {}, timeoutSec = DEFAULT_TIMEOUT) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutSec * 1000);
  let res;
  try {
    res = await fetch(path, { ...opts, signal: controller.signal });
  } catch (e) {
    clearTimeout(id);
    if (e.name === 'AbortError') {
      throw new ApiError('请求超时', -1);
    }
    throw new ApiError(e.message || '网络错误', 0);
  }
  clearTimeout(id);
  return res;
}

export async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined && body !== null) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await _fetchWithTimeout(path, opts, DEFAULT_TIMEOUT);
  let json;
  try {
    json = await res.json();
  } catch (e) {
    throw new ApiError('响应解析失败: HTTP ' + res.status, res.status);
  }
  if (json.ok !== true) {
    throw new ApiError(json.error || ('HTTP ' + res.status), res.status);
  }
  return json;
}

async function _request(method, path, { body, timeout = DEFAULT_TIMEOUT } = {}) {
  const opts = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined && body !== null) {
    opts.body = JSON.stringify(body);
    opts.headers['Content-Type'] = 'application/json';
  }
  const res = await _fetchWithTimeout(path, opts, timeout);
  let json;
  try {
    json = await res.json();
  } catch (e) {
    throw new ApiError('响应解析失败: HTTP ' + res.status, res.status);
  }
  if (json.ok !== true) {
    throw new ApiError(json.error || ('HTTP ' + res.status), res.status);
  }
  return json;
}

// ---- high-level methods used by the UI ----

export function health() { return _request('GET', '/health'); }
export function status() { return _request('GET', '/status'); }
export function start_monitor() { return _request('POST', '/monitor/start', { body: {}, timeout: 10 }); }
export function stop_monitor() { return _request('POST', '/monitor/stop', { body: {}, timeout: 10 }); }
export function restart_monitor() { return _request('POST', '/monitor/restart', { body: {}, timeout: 20 }); }

export async function list_targets(kind = 'all') {
  const res = await _request('GET', '/targets' + toQuery({ kind }));
  const targets = res.targets;
  return Array.isArray(targets) ? targets : [];
}

export function target_info(key) { return _request('GET', '/targets/' + encodeKey(key), { timeout: 10 }); }

export function enable_target(key, knowledge_bases = null, category = null) {
  const body = {};
  if (knowledge_bases) body.knowledge_bases = Array.from(knowledge_bases);
  if (category) body.category = String(category);
  return _request('POST', '/targets/' + encodeKey(key) + '/on', { body });
}

export function disable_target(key) { return _request('POST', '/targets/' + encodeKey(key) + '/off', { body: {} }); }

export function set_target_category(key, category) {
  return _request('POST', '/targets/' + encodeKey(key) + '/category', { body: { category: String(category) } });
}

export function set_target_field(key, field, value) {
  return _request('POST', '/targets/' + encodeKey(key) + '/field', { body: { field: String(field), value } });
}

export function delete_target(key) { return _request('POST', '/targets/' + encodeKey(key) + '/delete', { body: {} }); }

export function scan_targets(include_contacts = false) {
  return _request('POST', '/targets/scan' + toQuery({ include_contacts: include_contacts ? 'true' : 'false' }));
}

export async function list_kbs() {
  const res = await _request('GET', '/kbs');
  return res.knowledge_bases || [];
}

export async function kb_info(kb_id) {
  const res = await _request('GET', '/kbs/' + encodeKey(kb_id) + '/info', { timeout: 15 });
  return res.knowledge_base || {};
}

export function save_kb(payload) { return _request('POST', '/kbs', { body: payload }); }

export function enable_kb(kb_id) { return _request('POST', '/kbs/' + encodeKey(kb_id) + '/on', { body: {} }); }
export function disable_kb(kb_id) { return _request('POST', '/kbs/' + encodeKey(kb_id) + '/off', { body: {} }); }

export function delete_kb(kb_id, remove_files = false) {
  return _request('POST', '/kbs/' + encodeKey(kb_id) + '/delete', { body: { remove_files } });
}

export function import_kb(kb_id, source) {
  return _request('POST', '/kbs/' + encodeKey(kb_id) + '/import', { body: { source }, timeout: 60 });
}

export async function search_kb(kb_id, query, limit = 5) {
  return _request('GET', '/kbs/' + encodeKey(kb_id) + '/search' + toQuery({ q: query, limit }), { timeout: 30 });
}

export function open_kb(kb_id) { return _request('POST', '/kbs/' + encodeKey(kb_id) + '/open', { body: {} }); }
export function open_kb_obsidian(kb_id) { return _request('POST', '/kbs/' + encodeKey(kb_id) + '/obsidian', { body: {} }); }

export async function diagnose_kb(kb_id, query = '') {
  return _request('GET', '/kbs/' + encodeKey(kb_id) + '/diagnose' + toQuery({ q: query }), { timeout: 30 });
}

export function replace_target_kbs(key, knowledge_bases) {
  return _request('POST', '/targets/' + encodeKey(key) + '/kbs/replace', { body: { knowledge_bases } });
}

export async function list_leann_indexes() { return _request('GET', '/kbs/leann/indexes', { timeout: 15 }); }

export async function leann_index_info(index_name) {
  return _request('GET', '/kbs/leann/indexes/' + encodeKey(index_name) + '/info', { timeout: 15 });
}

export function build_leann_kb(kb_id, docs_dir = null, force = false) {
  const body = { force };
  if (docs_dir !== undefined && docs_dir !== null) body.docs_dir = docs_dir;
  return _request('POST', '/kbs/' + encodeKey(kb_id) + '/leann/build', { body, timeout: 10 });
}

export function leann_build_status(kb_id, build_id) {
  return _request('GET', '/kbs/' + encodeKey(kb_id) + '/leann/build/status' + toQuery({ build_id }), { timeout: 10 });
}

export async function get_triggers(key) {
  const res = await _request('GET', '/targets/' + encodeKey(key) + '/triggers');
  return res.target || {};
}

export async function get_default_triggers() {
  const res = await _request('GET', '/triggers/default');
  return res.default_triggers || [];
}

export function replace_default_triggers(words) {
  return _request('POST', '/triggers/default/replace', { body: { words } });
}

export function clear_default_triggers() { return _request('POST', '/triggers/default/clear', { body: {} }); }

export function add_triggers(key, words) {
  return _request('POST', '/targets/' + encodeKey(key) + '/triggers/add', { body: { words } });
}

export function remove_triggers(key, words) {
  return _request('POST', '/targets/' + encodeKey(key) + '/triggers/remove', { body: { words } });
}

export function replace_triggers(key, words) {
  return _request('POST', '/targets/' + encodeKey(key) + '/triggers/replace', { body: { words } });
}

export function clear_triggers(key) {
  return _request('POST', '/targets/' + encodeKey(key) + '/triggers/clear');
}

export async function events_recent(limit = 100, kind = null, target = null) {
  const res = await _request('GET', '/events/recent' + toQuery({ limit, kind, target }));
  return res.events || [];
}

export async function events_stats(since = null) {
  const res = await _request('GET', '/events/stats' + toQuery({ since }));
  return res.stats || {};
}

export function run_dispatcher_once(instance_id = null, provider = null, max_global_dispatching = 2, per_group_concurrency = 1) {
  return _request('POST', '/agent/dispatcher/run-once', {
    body: { instance_id, provider, max_global_dispatching, per_group_concurrency },
    timeout: 30,
  });
}

export function run_reconciler_once(limit = 10) {
  return _request('POST', '/agent/reconciler/run-once', { body: { limit }, timeout: 30 });
}

export function run_sender_once(limit = 5) {
  return _request('POST', '/agent/sender/run-once', { body: { limit }, timeout: 30 });
}

export async function agent_jobs(limit = 50, status = null, group_key = null) {
  const res = await _request('GET', '/agent/jobs' + toQuery({ limit, status, group_key }));
  return res.jobs || [];
}

export async function agent_jobs_stats() {
  const res = await _request('GET', '/agent/jobs/stats');
  return res.stats || {};
}

export function create_agent_test_job(prompt, provider = 'echo', group_key = 'manual-test', sender = 'tester', target = null, mention_name = '', priority = 0, task_type = 'deep_free') {
  const body = { prompt, provider, group_key, sender, mention_name, priority, task_type };
  if (target && typeof target === 'object') {
    body.target_username = target.username;
    body.target_name = target.name;
    body.target_table = target.table;
  } else if (typeof target === 'string' && target) {
    body.group_key = target;
  }
  return _request('POST', '/agent/jobs/test', { body });
}

export function run_agent_worker_once(provider = 'echo', timeout = 240) {
  return _request('POST', '/agent/worker/run-once', { body: { provider, timeout }, timeout: Math.max(DEFAULT_TIMEOUT, timeout + 5) });
}

export function agent_worker_status() { return _request('GET', '/agent/worker/status'); }

export function start_agent_worker(provider = 'echo', timeout = 240, worker_count = 1) {
  return _request('POST', '/agent/worker/start', { body: { provider, timeout, worker_count }, timeout: 10 });
}

export function stop_agent_worker() { return _request('POST', '/agent/worker/stop', { body: {}, timeout: 15 }); }

export function retry_job_send(job_id) {
  return _request('POST', '/agent/jobs/' + job_id + '/retry-send', { body: {}, timeout: 30 });
}

export function dismiss_job(job_id) {
  return _request('POST', '/agent/jobs/' + job_id + '/dismiss', { body: {}, timeout: 15 });
}

export async function dismiss_jobs_batch(job_ids) {
  let ok_count = 0;
  const failed = [];
  for (const job_id of job_ids) {
    try {
      const res = await dismiss_job(job_id);
      if (res.action === 'dismissed') ok_count += 1;
      else failed.push(job_id);
    } catch (e) {
      failed.push(job_id);
    }
  }
  return { ok_count, failed_ids: failed, total: job_ids.length };
}

export function recover_job_result(job_id, send = false) {
  return _request('POST', '/agent/jobs/' + job_id + '/recover-result', { body: { send }, timeout: 30 });
}

export async function agent_provider_health(provider = 'hermes', instance_id = null) {
  const params = { provider };
  if (instance_id) params.instance_id = instance_id;
  const res = await _request('GET', '/agent/provider/health' + toQuery(params), { timeout: 10 });
  return res.health || {};
}

export async function get_agent_job(job_id) {
  const res = await _request('GET', '/agent/jobs/' + job_id, { timeout: 10 });
  return res.job || {};
}

export function set_target_mode(key, mode) {
  return _request('POST', '/targets/' + encodeKey(key) + '/mode', { body: { mode } });
}

export function set_target_dedicated_agent(key, instance_id = null) {
  return _request('POST', '/targets/' + encodeKey(key) + '/dedicated-agent', { body: { instance_id: instance_id || '' } });
}

export async function list_agent_instances() {
  const res = await _request('GET', '/agent/instances', { timeout: 10 });
  return res.instances || [];
}

export async function discover_hermes_profiles() {
  const res = await _request('GET', '/agent-providers/hermes/profiles', { timeout: 10 });
  return res.profiles || [];
}

export function register_agent_instance(instance) {
  return _request('POST', '/agent/instances', { body: { instance }, timeout: 10 });
}

export function start_agent_instance(instance_id, timeout = 240, worker_count = 1) {
  return _request('POST', '/agent/instance/' + encodeKey(String(instance_id)) + '/start', {
    body: { timeout, worker_count },
    timeout: 15,
  });
}

export function agent_instance_on_duty(instance_id) {
  return _request('POST', '/agent/instance/' + encodeKey(String(instance_id)) + '/on-duty', { body: {}, timeout: 10 });
}

export function agent_instance_off_duty(instance_id) {
  return _request('POST', '/agent/instance/' + encodeKey(String(instance_id)) + '/off-duty', { body: {}, timeout: 10 });
}

export async function async_loop_status() { return _request('GET', '/agent/async-loop/status', { timeout: 10 }); }
export async function agent_pool_status() { return _request('GET', '/agent/pool/status', { timeout: 15 }); }

export function start_async_loop(instance_id = null, max_global_dispatching = 1, per_group_concurrency = 1) {
  return _request('POST', '/agent/async-loop/start', {
    body: { instance_id, max_global_dispatching, per_group_concurrency },
    timeout: 10,
  });
}

export function stop_async_loop() { return _request('POST', '/agent/async-loop/stop', { body: {}, timeout: 10 }); }

export async function reliable_pipeline_status() { return _request('GET', '/reliable-pipeline/status'); }
export async function reliable_scheduler_status() { return _request('GET', '/reliable-pipeline/scheduler/status'); }
export function reliable_scheduler_start() { return _request('POST', '/reliable-pipeline/scheduler/start', { body: {}, timeout: 10 }); }
export function reliable_scheduler_stop() { return _request('POST', '/reliable-pipeline/scheduler/stop', { body: {}, timeout: 10 }); }

export function requeue_reliable_outbox(outbox_id, reason) {
  return _request('POST', '/reliable-pipeline/outbox/' + parseInt(outbox_id, 10) + '/requeue', {
    body: { reason: String(reason || '').trim() },
    timeout: 15,
  });
}

export async function list_pipeline_escalations(limit = 50) {
  const res = await _request('GET', '/reliable-pipeline/escalations' + toQuery({ limit }));
  return res.items || [];
}

export async function list_pipeline_terminal_failures(limit = 50) {
  const res = await _request('GET', '/reliable-pipeline/terminal-failures' + toQuery({ limit }));
  return res.items || [];
}

// ---- Step 1f new endpoints ----

export function overview_today() { return _request('GET', '/overview/today'); }

export function overview_history(days = 7) {
  return _request('GET', '/overview/history' + toQuery({ days }));
}

export function overview_topics() { return _request('GET', '/overview/topics'); }

export function refresh_topics(target = null) {
  const body = target === undefined || target === null ? {} : { target };
  return _request('POST', '/overview/topics/refresh', { body });
}

export function upload_kb_files(kbId, files) {
  return _request('POST', '/kbs/' + encodeKey(kbId) + '/upload', { body: { files } });
}

export function leann_build_log(kbId, buildId = null) {
  return _request('GET', '/kbs/' + encodeKey(kbId) + '/leann/build/log' + toQuery({ build_id: buildId }));
}
