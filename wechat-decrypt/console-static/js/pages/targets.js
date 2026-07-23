import {
  list_targets,
  list_kbs,
  scan_targets,
  set_target_mode,
  set_target_field,
  set_target_category,
  set_target_dedicated_agent,
  list_agent_instances,
  enable_target,
  disable_target,
  delete_target,
} from '../api.js';

import {
  h,
  clear,
  badge,
  toast,
  spinner,
  confirmDanger,
  emptyState,
  metricCard,
  detailsEl,
  pageHeader,
} from '../ui.js';

const CATEGORY_LABELS = { user: '普通', admin: '管理员' };
const KB_SOURCE_LABELS = {
  obsidian: 'Obsidian 本地知识库',
  local_folder: '本地文件夹知识库',
  getnote: 'Get笔记 在线知识库',
  ima: 'IMA 在线知识库',
  hook: '外部适配器知识库',
  leann: 'LEANN 向量索引',
};

const KIND_OPTIONS = [
  { label: '已启用', value: 'enabled' },
  { label: '待启用', value: 'pending' },
  { label: '已停用', value: 'disabled' },
  { label: '全部', value: 'all' },
];

const MODE_OPTIONS = [
  { label: '平衡', value: 'group_assistant' },
  { label: '客服', value: 'customer_service' },
];

const RESPONSE_MODE_OPTIONS = [
  { label: '触发词回复', value: 'trigger' },
  { label: '自由回复（所有消息都回）', value: 'free' },
];

function _label(mapping, value) {
  const v = String(value || '').trim();
  if (!v) return '—';
  return mapping[v] || v;
}

function _statusLabel(t) {
  if (t.enabled) return '已启用';
  if (t.is_candidate || t.status === 'pending') return '待启用';
  return '已停用';
}

function _kbSource(kbId, kbMap) {
  const kb = kbMap[kbId];
  if (!kb) return null;
  return String(
    kb.source ||
    ((kb.type || 'local') === 'local' ? 'local_folder' : kb.type) ||
    'local_folder'
  ).toLowerCase();
}

function _errorBanner(msg, onRetry) {
  return h('div', { class: 'error-banner' },
    h('span', { text: msg }),
    h('button', { class: 'btn btn-sm', onclick: onRetry }, '重试')
  );
}

export async function render(root) {
  clear(root);

  const state = {
    kind: 'enabled',
    search: '',
    privateOnly: false,
    includeContacts: false,
  };
  let targets = [];
  let kbs = [];
  let kbMap = {};
  let agentInstances = [];
  let scanResult = null;
  let isLoading = true;
  let isScanning = false;

  const loadData = async () => {
    const [targetsRes, kbsRes, instancesRes] = await Promise.all([
      list_targets('all'),
      list_kbs(),
      list_agent_instances().catch(() => ({ instances: [] })),
    ]);
    targets = Array.isArray(targetsRes) ? targetsRes : (targetsRes.targets || []);
    kbs = kbsRes || [];
    kbMap = {};
    for (const kb of kbs) {
      if (kb.id) kbMap[kb.id] = kb;
    }
    agentInstances = Array.isArray(instancesRes) ? instancesRes : (instancesRes.instances || []);
  };

  const saveAndReload = async (label, fn) => {
    await fn();
    toast(label, 'ok');
    await loadData();
    renderBody();
  };

  const filterTargets = () => {
    let rows = targets;
    if (state.kind !== 'all') {
      rows = rows.filter((t) => {
        const enabled = !!t.enabled;
        const isCandidate = t.is_candidate || t.status === 'pending';
        if (state.kind === 'enabled') return enabled;
        if (state.kind === 'pending') return isCandidate;
        if (state.kind === 'disabled') return !enabled && !isCandidate;
        return true;
      });
    }
    if (state.privateOnly) {
      rows = rows.filter((t) => (t.type || '') !== 'group');
    }
    const q = state.search.trim().toLowerCase();
    if (q) {
      rows = rows.filter((t) =>
        ((t.name || '').toLowerCase().includes(q)) ||
        ((t.username || '').toLowerCase().includes(q))
      );
    }
    return rows;
  };

  function renderToolbar() {
    const kindSelect = h('select', {
      class: 'select',
      value: state.kind,
      onchange: (e) => { state.kind = e.target.value; renderBody(); },
    }, KIND_OPTIONS.map((opt) =>
      h('option', { value: opt.value, selected: opt.value === state.kind }, opt.label)
    ));

    const searchInput = h('input', {
      class: 'input',
      type: 'text',
      placeholder: '输入关键字过滤',
      value: state.search,
      oninput: (e) => { state.search = e.target.value; renderBody(); },
    });

    const includeContactsCheckbox = h('label', { class: 'checkbox-row' },
      h('input', {
        type: 'checkbox',
        checked: state.includeContacts,
        onchange: (e) => { state.includeContacts = e.target.checked; },
      }),
      h('span', { text: '包含私聊' }),
      h('small', { text: '（仅影响扫描新目标）' })
    );

    const privateOnlyCheckbox = h('label', { class: 'checkbox-row' },
      h('input', {
        type: 'checkbox',
        checked: state.privateOnly,
        onchange: (e) => { state.privateOnly = e.target.checked; renderBody(); },
      }),
      h('span', { text: '仅看私聊' })
    );

    const refreshBtn = h('button', {
      class: 'btn btn-ghost btn-sm',
      onclick: async () => {
        await loadData();
        scanResult = null;
        renderBody();
        toast('列表已刷新', 'ok');
      },
    }, '刷新列表');

    const scanBtn = h('button', {
      class: 'btn btn-primary btn-sm',
      disabled: isScanning,
      onclick: async () => {
        isScanning = true;
        renderBody();
        try {
          const res = await scan_targets(state.includeContacts);
          scanResult = res;
          toast('扫描完成', 'ok');
          await loadData();
        } catch (err) {
          toast('扫描失败：' + err.message, 'err');
        } finally {
          isScanning = false;
          renderBody();
        }
      },
    }, isScanning ? '扫描中…' : '扫描新目标');

    return h('div', { class: 'card filter-card' },
      h('div', { class: 'card-body' },
        h('div', { class: 'form-row' },
          h('div', null, h('label', { text: '筛选' }), kindSelect),
          h('div', null, h('label', { text: '搜索' }), searchInput),
          h('div', { class: 'flex gap-2' }, refreshBtn, scanBtn),
          h('div', { class: 'flex gap-3' }, includeContactsCheckbox, privateOnlyCheckbox)
        )
      )
    );
  }

  function renderScanResult() {
    if (!scanResult) return null;
    const lines = [
      `会话 ${scanResult.discovered || 0}（群 ${scanResult.discovered_groups || 0}）`,
      `新增待启用 ${scanResult.added || 0}`,
      `更新 ${scanResult.updated || 0}`,
      `已配置跳过 ${scanResult.skipped_configured || 0}`,
      `待启用池 ${scanResult.pending || 0}`,
    ];
    const caption = (scanResult.added || 0) === 0 && (scanResult.discovered || 0) > 0
      ? '没有新增：常见原因是群已在监听列表/待启用池，或名称未从 contact 库解析到。可切换「待启用」筛选查看。'
      : '';
    return h('div', { class: 'scan-banner' },
      h('div', { class: 'badge badge-ok', text: '扫描完成' }),
      h('div', { class: 'caption follow', text: lines.join(' · ') }),
      caption ? h('div', { class: 'field-hint', text: caption }) : null
    );
  }

  function renderCard(t) {
    const actionKey = t.username || t.name || '';
    const statusLabel = _statusLabel(t);
    const cat = String(t.category || 'user').trim().toLowerCase();
    const isAdmin = cat === 'admin';
    const durableOn = t.reliable_pipeline_target === true;
    const shadowOn = !!t.reliable_pipeline_shadow;

    let pipeState;
    if (durableOn && shadowOn) pipeState = '自动回复开（只演练不真发）';
    else if (durableOn) pipeState = '自动回复开';
    else pipeState = '自动回复关（消息跳过）';

    const boundKbIds = (t.knowledge_bases || []).filter((id) => kbMap[id]);
    const boundSources = new Set(boundKbIds.map((id) => _kbSource(id, kbMap)));
    let kbSourceText;
    if (boundKbIds.length === 0) {
      kbSourceText = '未绑定';
    } else if (boundSources.size === 1) {
      const src = Array.from(boundSources)[0];
      kbSourceText = `${_label(KB_SOURCE_LABELS, src)}（${boundKbIds.length} 个）`;
    } else {
      kbSourceText = '混合（' + Array.from(boundSources).sort().map((s) => _label(KB_SOURCE_LABELS, s)).join(', ') + '）';
    }

    const actions = [];
    if (!t.enabled && t.is_candidate) {
      const catSelect = h('select', { class: 'select select-inline' },
        h('option', { value: 'user', selected: cat === 'user' }, '普通'),
        h('option', { value: 'admin', selected: cat === 'admin' }, '管理员')
      );
      actions.push(
        catSelect,
        h('button', {
          class: 'btn btn-primary btn-sm',
          type: 'button',
          onclick: async () => {
            try {
              await saveAndReload(`已启用 ${actionKey}`, async () => {
                await enable_target(actionKey, [], catSelect.value);
              });
            } catch (err) {
              toast('启用失败：' + err.message, 'err');
            }
          },
        }, '启用')
      );
    } else if (!t.enabled && !t.is_candidate) {
      actions.push(h('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        onclick: async () => {
          try {
            await saveAndReload(`已恢复监听 ${actionKey}`, async () => {
              await enable_target(actionKey);
            });
          } catch (err) {
            toast('恢复失败：' + err.message, 'err');
          }
        },
      }, '恢复监听'));
    } else {
      actions.push(h('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        onclick: async () => {
          try {
            await saveAndReload(`已停用 ${actionKey}`, async () => {
              await disable_target(actionKey);
            });
          } catch (err) {
            toast('停用失败：' + err.message, 'err');
          }
        },
      }, '停用'));
    }

    actions.push(h('button', {
      class: 'btn btn-danger btn-sm',
      type: 'button',
      onclick: async () => {
        if (!confirmDanger(`确定删除目标「${t.name || actionKey}」？此操作不可恢复。`)) return;
        try {
          await delete_target(actionKey);
          toast('已删除 ' + actionKey, 'ok');
          await loadData();
          renderBody();
        } catch (err) {
          toast('删除失败：' + err.message, 'err');
        }
      },
    }, '删除'));

    const head = h('div', { class: 'card-head' },
      h('div', { class: 'card-head-main' },
        h('span', { class: 'card-head-title', text: t.name || '(no name)' }),
        badge(statusLabel, t.enabled ? 'ok' : ((t.is_candidate || t.status === 'pending') ? 'warn' : 'muted')),
        isAdmin ? badge('管理员', 'info') : null
      ),
      h('div', { class: 'card-actions' }, ...actions)
    );

    const metrics = h('div', { class: 'metric-grid' },
      metricCard('已读到ID', t.last_local_id || 0),
      metricCard('触发词', (t.triggers || []).length),
      metricCard('知识库', (t.knowledge_bases || []).length),
      metricCard('自动回复', durableOn ? '开' : '关')
    );

    const body = h('div', { class: 'card-body target-card' },
      h('div', { class: 'kv-stack' },
        h('div', { class: 'kv' },
          h('span', { class: 'k', text: '标识' }),
          h('span', { class: 'v', text: t.username || '—' })
        ),
        h('div', { class: 'kv' },
          h('span', { class: 'k', text: '模式 / 策略 / 类别' }),
          h('span', {
            class: 'v',
            text: `${t.mode || '?'} · ${t.reply_policy || '?'} · ${_label(CATEGORY_LABELS, cat)}`
          })
        ),
        h('div', { class: 'kv' },
          h('span', { class: 'k', text: '自动回复 / Agent' }),
          h('span', { class: 'v', text: `${pipeState} · ${t.dedicated_agent_instance_id || '通用池'}` })
        ),
        h('div', { class: 'kv' },
          h('span', { class: 'k', text: '知识库来源' }),
          h('span', { class: 'v', text: kbSourceText })
        )
      ),
      metrics,
      renderDetails(t)
    );

    return h('div', { class: 'card target-card' }, head, body);
  }

  function renderDetails(t) {
    const actionKey = t.username || t.name || '';
    const cat = String(t.category || 'user').trim().toLowerCase();

    const modeSelect = h('select', { class: 'select' },
      MODE_OPTIONS.map((opt) =>
        h('option', { value: opt.value, selected: opt.value === (t.mode || 'group_assistant') }, opt.label)
      )
    );
    const responseModeSelect = h('select', { class: 'select' },
      RESPONSE_MODE_OPTIONS.map((opt) =>
        h('option', { value: opt.value, selected: opt.value === (t.response_mode || 'trigger') }, opt.label)
      )
    );

    const responseBody = h('div', { class: 'form-grid' },
      h('div', null,
        h('label', { text: '响应模式' }),
        modeSelect,
        h('button', {
          class: 'btn btn-sm btn-primary',
          type: 'button',
          onclick: async () => {
            try {
              await saveAndReload('已切换响应模式', async () => {
                await set_target_mode(actionKey, modeSelect.value);
              });
            } catch (err) {
              toast('保存失败：' + err.message, 'err');
            }
          },
        }, '保存响应模式')
      ),
      h('div', null,
        h('label', { text: '回复策略' }),
        responseModeSelect,
        h('button', {
          class: 'btn btn-sm btn-primary',
          type: 'button',
          onclick: async () => {
            try {
              await saveAndReload('已切换回复策略', async () => {
                await set_target_field(actionKey, 'response_mode', responseModeSelect.value);
              });
            } catch (err) {
              toast('保存失败：' + err.message, 'err');
            }
          },
        }, '保存回复策略')
      )
    );

    const catSelect = h('select', { class: 'select' },
      h('option', { value: 'user', selected: cat === 'user' }, '普通'),
      h('option', { value: 'admin', selected: cat === 'admin' }, '管理员')
    );
    const adminText = h('textarea', {
      class: 'textarea',
      rows: 4,
      placeholder: '每行一个 wxid 或昵称，留空=该目标下所有发送者均为管理员',
    }, (t.admin_senders || []).join('\n'));
    const categoryBody = h('div', null,
      h('div', { class: 'form-row' },
        h('div', null, h('label', { text: '类别' }), catSelect),
        h('button', {
          class: 'btn btn-sm btn-primary',
          onclick: async () => {
            try {
              await saveAndReload('已保存类别', async () => {
                await set_target_category(actionKey, catSelect.value);
              });
            } catch (err) {
              toast('保存失败：' + err.message, 'err');
            }
          },
        }, '保存类别')
      ),
      h('p', { class: 'field-hint', text: '管理员白名单：留空表示该目标下所有发送者都被视为管理员。可填写 wxid 或昵称/备注，每行一个。' }),
      adminText,
      h('button', {
        class: 'btn btn-sm btn-primary',
        type: 'button',
        onclick: async () => {
          try {
            await saveAndReload('已保存管理员白名单', async () => {
              const cleaned = adminText.value.split('\n').map((s) => s.trim()).filter(Boolean);
              await set_target_field(actionKey, 'admin_senders', cleaned);
            });
          } catch (err) {
            toast('保存失败：' + err.message, 'err');
          }
        },
      }, '保存管理员白名单')
    );

    const currentDedicated = String(t.dedicated_agent_instance_id || '');
    const instanceOptions = ['', ...agentInstances.map((i) => String(i.id || '')).filter(Boolean)];
    const agentSelect = h('select', { class: 'select' },
      instanceOptions.map((id) =>
        h('option', { value: id, selected: id === currentDedicated }, id || '通用池（不绑定）')
      )
    );
    const agentBody = h('div', null,
      h('label', { text: '专用 Agent 实例' }),
      h('p', { class: 'field-hint', text: '留空=不绑定（任务进入通用池）；选择具体实例后只由该实例处理该目标的任务。' }),
      agentSelect,
      h('button', {
        class: 'btn btn-sm btn-primary',
        type: 'button',
        onclick: async () => {
          try {
            await saveAndReload('已保存 Agent 绑定', async () => {
              await set_target_dedicated_agent(actionKey, agentSelect.value);
            });
          } catch (err) {
            toast('保存失败：' + err.message, 'err');
          }
        },
      }, '保存 Agent 绑定')
    );

    const cuaInput = h('input', {
      class: 'input',
      type: 'text',
      value: String(t.cua_window_title || ''),
      placeholder: t.name || '',
    });
    const cuaBody = h('div', null,
      h('p', { class: 'field-hint', text: '如果该目标的独立聊天窗口标题与目标名不同，可在此覆盖。' }),
      h('label', { text: '窗口标题' }),
      cuaInput,
      h('button', {
        class: 'btn btn-sm btn-primary',
        type: 'button',
        onclick: async () => {
          try {
            await saveAndReload('已保存窗口标题', async () => {
              await set_target_field(actionKey, 'cua_window_title', cuaInput.value.trim());
            });
          } catch (err) {
            toast('保存失败：' + err.message, 'err');
          }
        },
      }, '保存标题覆盖')
    );

    const durableInput = h('input', { type: 'checkbox', checked: t.reliable_pipeline_target === true });
    const shadowInput = h('input', { type: 'checkbox', checked: !!t.reliable_pipeline_shadow });
    const durableToggle = h('label', { class: 'checkbox-row' }, durableInput, h('span', { text: '开启本群自动回复' }));
    const shadowToggle = h('label', { class: 'checkbox-row' }, shadowInput, h('span', { text: '只演练，不真发到微信' }));
    const autoReplyBody = h('div', null,
      h('p', { class: 'field-hint', text: '打开后：群消息会进入自动回复后台（写库 → 想回复 → 发微信）。关闭后：仍继续监听并推进已读位置，但不会自动回。多个群可以同时打开。' }),
      durableToggle,
      shadowToggle,
      h('button', {
        class: 'btn btn-sm btn-primary',
        type: 'button',
        onclick: async () => {
          try {
            await set_target_field(actionKey, 'reliable_pipeline_target', durableInput.checked);
          } catch (err) {
            toast('第一个开关未保存：' + err.message, 'err');
            return;
          }
          try {
            await set_target_field(actionKey, 'reliable_pipeline_shadow', shadowInput.checked);
          } catch (err) {
            toast('第二个开关未保存，请重试', 'err');
            return;
          }
          toast('已保存自动回复开关', 'ok');
          await loadData();
          renderBody();
        },
      }, '保存自动回复开关')
    );

    const storedMode = String(t.agent_mode || '').trim() || '（未设置，历史默认 standard）';
    const deprecatedBody = h('div', null,
      h('p', { class: 'field-hint', text: '现在自动回复走：监听收消息 → 后台写库 → Agent 想回复 → 发微信。下面字段不会改变群聊是否自动回。' }),
      h('p', { text: '真正要改的：上面「这个群要不要自动回复」、响应模式、回复策略、触发词、知识库、专用 Agent、CUA 窗口标题、管理员。' }),
      h('p', { text: 'agent_mode / thin_monitor：旧离线脚本用，monitor 已不读。' }),
      h('div', { class: 'kv' },
        h('span', { text: '当前配置中的 agent_mode 只读值' }),
        h('span', { text: storedMode })
      ),
      t.thin_monitor ? h('div', { class: 'warn-banner' },
        h('div', { text: '仍残留 thin_monitor=true。对 live monitor 无效果，建议清除。' }),
        h('button', {
          class: 'btn btn-sm btn-primary',
          onclick: async () => {
            try {
              await saveAndReload('已清除 thin_monitor', async () => {
                await set_target_field(actionKey, 'thin_monitor', false);
              });
            } catch (err) {
              toast('清除失败：' + err.message, 'err');
            }
          },
        }, '清除 thin_monitor 遗留字段')
      ) : null,
      storedMode !== '（未设置，历史默认 standard）' ? h('button', {
        class: 'btn btn-sm btn-primary',
        onclick: async () => {
          try {
            await saveAndReload('已清除 agent_mode', async () => {
              await set_target_field(actionKey, 'agent_mode', '');
            });
          } catch (err) {
            toast('清除失败：' + err.message, 'err');
          }
        },
      }, '清除 agent_mode 遗留字段') : null
    );

    return h('div', { class: 'details-group' },
      t.enabled ? detailsEl('响应模式 / 会话策略', responseBody) : null,
      t.enabled ? detailsEl('类别 / 管理员设置', categoryBody) : null,
      t.enabled ? detailsEl('Agent 绑定', agentBody) : null,
      t.enabled ? detailsEl('CUA 独立窗口标题覆盖', cuaBody) : null,
      t.enabled ? detailsEl('这个群要不要自动回复', autoReplyBody) : null,
      detailsEl('已废弃字段（勿用于生产）', deprecatedBody)
    );
  }

  function renderBody() {
    clear(root);
    root.appendChild(pageHeader('监听目标', '管理监听目标、响应模式与自动回复开关'));
    root.appendChild(renderToolbar());
    if (scanResult) root.appendChild(renderScanResult());
    if (isLoading) {
      root.appendChild(spinner('加载中…'));
      return;
    }
    const filtered = filterTargets();
    if (filtered.length === 0) {
      root.appendChild(emptyState('没有匹配的目标。'));
      return;
    }
    for (const t of filtered) {
      root.appendChild(renderCard(t));
    }
  }

  try {
    renderBody();
    await loadData();
    isLoading = false;
    renderBody();
  } catch (err) {
    clear(root);
    root.appendChild(_errorBanner(err.message || '加载失败', () => render(root)));
  }

  return () => {};
}
