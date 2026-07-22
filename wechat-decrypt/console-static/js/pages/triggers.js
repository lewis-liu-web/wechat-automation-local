import {
  list_targets,
  get_default_triggers,
  replace_default_triggers,
  get_triggers,
  add_triggers,
  replace_triggers,
  remove_triggers,
  clear_triggers,
} from '../api.js';

import {
  h,
  pageHeader,
  spinner,
  toast,
  confirmDanger,
  clear,
} from '../ui.js';

export async function render(root) {
  root.appendChild(pageHeader('触发词管理', '管理全局默认触发词与各目标触发词'));
  const loading = spinner('加载中…');
  root.appendChild(loading);

  try {
    const [targets, defaultTriggers] = await Promise.all([
      list_targets('all'),
      get_default_triggers(),
    ]);
    const enabledTargets = (targets || []).filter(t => t && t.enabled);
    loading.remove();
    renderPage(root, enabledTargets, defaultTriggers || []);
  } catch (err) {
    loading.remove();
    root.appendChild(errorBanner(err, root));
  }

  return () => {};
}

function errorBanner(err, root) {
  return h('div', { class: 'error-banner' },
    h('span', { text: `加载失败：${err.message}` }),
    h('button', {
      class: 'btn btn-sm',
      text: '重试',
      onclick: () => {
        clear(root);
        render(root);
      },
    })
  );
}

function renderPage(root, enabledTargets, defaultTriggers) {
  // app.py L944: 全局默认触发词
  root.appendChild(renderGlobalDefaultSection(defaultTriggers));
  root.appendChild(h('hr'));

  // app.py L972-973: 没有已启用目标提示
  if (!enabledTargets.length) {
    root.appendChild(
      h('div', { class: 'card' },
        h('p', { class: 'empty-state', text: '没有已启用的目标，请先启用一个目标。' })
      )
    );
    return;
  }

  // app.py L974-978: 选择目标
  const targetBody = h('div', { class: 'card target-body' });
  let selectedIndex = 0;

  const select = h('select', {
    class: 'select',
    onchange: () => {
      selectedIndex = Number(select.value);
      loadTargetBody(targetBody, enabledTargets[selectedIndex]);
    },
  },
    ...enabledTargets.map((t, i) => h('option', {
      value: String(i),
      text: `${t.name || '?'}（${t.username || ''}）`,
      selected: i === 0,
    }))
  );

  root.appendChild(
    h('div', { class: 'card' },
      h('h2', { class: 'card-title', text: '目标触发词' }),
      h('div', { class: 'form-row' },
        h('label', { text: '选择目标' }),
        select
      ),
      // app.py L980: 响应模式提示
      h('p', { class: 'caption', text: '响应模式请在「监听目标」页统一配置；本页只管理触发词。' }),
      targetBody
    )
  );

  loadTargetBody(targetBody, enabledTargets[0]);
}

function renderGlobalDefaultSection(defaultTriggers) {
  // app.py L946-947: 当前默认触发词提示
  const caption = h('div', { class: 'caption', text: `当前：${defaultTriggers.length ? defaultTriggers.join('，') : '（空）'}` });
  const textarea = h('textarea', {
    class: 'textarea',
    rows: 4,
    placeholder: '每行一个默认触发词',
    value: defaultTriggers.join('\n'),
  });

  async function save(words) {
    try {
      await replace_default_triggers(words);
      const fresh = await get_default_triggers();
      caption.textContent = `当前：${fresh.length ? fresh.join('，') : '（空）'}`;
      textarea.value = fresh.join('\n');
      toast(words.length ? `已保存 ${words.length} 个默认触发词` : '已清空默认触发词', 'ok');
    } catch (err) {
      toast(words.length ? `保存失败：${err.message}` : `清空失败：${err.message}`, 'err');
    }
  }

  // app.py L950-961: 保存默认触发词
  // app.py L961-969: 清空默认触发词（通过 replace 传空数组）
  return h('div', { class: 'card' },
    h('h2', { class: 'card-title', text: '全局默认触发词' }),
    caption,
    textarea,
    h('div', { class: 'form-row' },
      h('button', {
        class: 'btn btn-primary',
        text: '保存默认触发词',
        onclick: () => {
          const words = textarea.value.split('\n').map(s => s.trim()).filter(Boolean);
          save(words);
        },
      }),
      h('button', {
        class: 'btn btn-danger',
        text: '清空默认触发词',
        onclick: () => save([]),
      })
    )
  );
}

async function loadTargetBody(container, target) {
  clear(container);
  container.appendChild(spinner('加载触发词…'));

  const key = target.name || target.username || '';
  try {
    const info = await get_triggers(key);
    const current = (info && info.triggers) || [];
    const defaultTriggers = (info && info.default_triggers) || [];

    clear(container);
    renderTargetBody(container, target, key, current, defaultTriggers);
  } catch (err) {
    clear(container);
    container.appendChild(
      h('div', { class: 'error-banner' },
        h('span', { text: `读取触发词失败：${err.message}` }),
        h('button', {
          class: 'btn btn-sm',
          text: '重试',
          onclick: () => loadTargetBody(container, target),
        })
      )
    );
  }
}

function renderTargetBody(container, target, key, current, defaultTriggers) {
  // app.py L990-991: 当前目标触发词与全局默认触发词
  container.appendChild(
    h('div', { class: 'kv' },
      h('span', { text: '当前目标触发词：' }),
      h('span', { text: current.length ? current.join('，') : '（空，使用全局 default）' })
    )
  );
  container.appendChild(
    h('div', { class: 'kv' },
      h('span', { text: '全局默认触发词：' }),
      h('span', { text: defaultTriggers.length ? defaultTriggers.join('，') : '（空）' })
    )
  );
  container.appendChild(h('hr'));

  // app.py L994-1003: 新增单个触发词
  const addInput = h('input', {
    class: 'input',
    type: 'text',
    placeholder: '例如：@测试助手',
  });
  container.appendChild(
    h('div', { class: 'form-row' },
      h('h3', { text: '新增单个触发词' }),
      addInput,
      h('button', {
        class: 'btn btn-primary',
        text: '添加',
        onclick: async () => {
          const word = addInput.value.trim();
          if (!word) {
            toast('触发词不能为空', 'warn');
            return;
          }
          try {
            await add_triggers(key, [word]);
            toast('已添加', 'ok');
            addInput.value = '';
            await loadTargetBody(container, target);
          } catch (err) {
            toast(`添加失败：${err.message}`, 'err');
          }
        },
      })
    )
  );

  // app.py L1004-1014: 批量替换
  const bulkTextarea = h('textarea', {
    class: 'textarea',
    rows: 5,
    placeholder: '每行一个触发词',
    value: current.join('\n'),
  });
  container.appendChild(
    h('div', { class: 'form-row' },
      h('h3', { text: '批量替换' }),
      bulkTextarea,
      h('button', {
        class: 'btn btn-primary',
        text: '替换为以上内容',
        onclick: async () => {
          const words = bulkTextarea.value.split('\n').map(s => s.trim()).filter(Boolean);
          try {
            // 目标 replace 服务端拒绝空数组，前端为空时改调 clear 端点
            if (words.length === 0) {
              await clear_triggers(key);
            } else {
              await replace_triggers(key, words);
            }
            toast(`已替换为 ${words.length} 个词`, 'ok');
            await loadTargetBody(container, target);
          } catch (err) {
            toast(`替换失败：${err.message}`, 'err');
          }
        },
      })
    )
  );

  // app.py L1015-1027: 清空本目标触发词（confirmDanger）
  // app.py L1028-1044: 删除全部当前触发词
  container.appendChild(
    h('div', { class: 'form-row' },
      h('button', {
        class: 'btn btn-danger',
        text: '清空本目标触发词',
        onclick: async () => {
          if (!confirmDanger('确定清空本目标触发词？将回退到全局默认触发词。')) return;
          try {
            await clear_triggers(key);
            toast('已清空', 'ok');
            await loadTargetBody(container, target);
          } catch (err) {
            toast(`清空失败：${err.message}`, 'err');
          }
        },
      }),
      h('button', {
        class: 'btn btn-danger',
        text: '删除全部当前触发词',
        onclick: async () => {
          if (!current.length) {
            toast('当前为空', 'warn');
            return;
          }
          try {
            await remove_triggers(key, current);
            toast('已删除', 'ok');
            await loadTargetBody(container, target);
          } catch (err) {
            toast(`删除失败：${err.message}`, 'err');
          }
        },
      })
    )
  );
}
