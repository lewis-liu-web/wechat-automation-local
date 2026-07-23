import {
  list_kbs,
  list_targets,
  list_leann_indexes,
  save_kb,
  import_kb,
  upload_kb_files,
  enable_kb,
  disable_kb,
  delete_kb,
  search_kb,
  diagnose_kb,
  open_kb,
  open_kb_obsidian,
  build_leann_kb,
  leann_build_status,
  leann_build_log,
  replace_target_kbs,
} from "../api.js";

import {
  h,
  clear,
  badge,
  toast,
  spinner,
  confirmDanger,
  emptyState,
  detailsEl,
  pageHeader,
  fmtTs,
} from "../ui.js";

const KB_SOURCE_LABELS = {
  obsidian: "Obsidian 本地知识库",
  local_folder: "本地文件夹知识库",
  getnote: "Get笔记 在线知识库",
  ima: "IMA 在线知识库",
  hook: "外部适配器知识库",
  leann: "LEANN 向量索引",
};

const KB_TYPE_LABELS = {
  local: "本地文件夹",
  getnote: "Get笔记在线知识库",
  ima: "IMA 在线知识库",
  hook: "外部适配器",
  leann: "LEANN 向量索引",
};

const lastImports = new Map();

function labelSource(value) {
  const v = String(value || "").toLowerCase();
  return KB_SOURCE_LABELS[v] || value || "—";
}

function kbSourceOf(kb) {
  return (
    kb.source ||
    ((kb.type || "local") === "local" ? "local_folder" : kb.type) ||
    "local_folder"
  ).toLowerCase();
}

function kbTypeOf(kb) {
  return (kb.type || "local").toLowerCase();
}

function kbOptionLabel(kbs, kbId) {
  const kb = kbs.find((k) => k.id === kbId);
  if (!kb) return kbId;
  const src = labelSource(kbSourceOf(kb));
  const type = kbTypeOf(kb);
  let count = "";
  if (type === "local") {
    const fc = kb.file_count;
    if (Number.isInteger(fc) && fc) count = `文档 ${fc}`;
  } else if (type === "getnote" || type === "ima") {
    const bits = [];
    if (Number.isInteger(kb.online_note_count) && kb.online_note_count) {
      bits.push(`笔记 ${kb.online_note_count}`);
    }
    if (Number.isInteger(kb.online_file_count) && kb.online_file_count) {
      bits.push(`文件 ${kb.online_file_count}`);
    }
    if (bits.length) count = bits.join("，");
  } else if (type === "leann") {
    const idx = kb.index_name || kb.knowledge_base_id || "";
    const exists = Boolean(kb.exists);
    count = idx ? `索引 ${idx}${exists ? " · 存在" : " · 缺失"}` : "未配置索引";
  }
  const enabled = kb.enabled ? "已启用" : "已停用";
  return `${kbId} · ${src} · ${count || "在线"} · ${enabled}`;
}

function failureBucket(err) {
  const e = (err || "").toLowerCase();
  if (
    e.includes("cli_exit=1") &&
    (e.includes("markitdown._except") ||
      e.includes("unsupportedformatexception") ||
      e.includes("no converter") ||
      e.includes("unknown format") ||
      e.includes("not supported"))
  ) {
    return {
      title: "格式不支持",
      advice:
        "MarkItDown 处理 .pdf/.docx/.pptx/.xlsx/.md/.txt/图片/HTML 等。换个文档或把内容另存为通用格式再导。",
    };
  }
  if (
    e.includes("modulenotfounderror") &&
    e.includes("markitdown") &&
    e.includes("cli_exit=1")
  ) {
    return {
      title: "MarkItDown 没装上",
      advice: "重启 control_api 让它加载 D:\\programs\\wechat-kb-tools。",
    };
  }
  if (e.includes("cli_exit=0") && (e.includes("stdout_head=''") || e.includes('stdout_head=""'))) {
    return {
      title: "文件没正文",
      advice: "MarkItDown 跑了但提不到字。可能是扫描件 / 加密 / 损坏，OCR 转一次或换文件。",
    };
  }
  if (e.includes("pdf") && (e.includes("encrypted") || e.includes("password"))) {
    return { title: "PDF 加密", advice: "用浏览器或 Adobe 解密后重导。" };
  }
  if (
    e.includes("no text") ||
    e.includes("empty") ||
    e.includes("no text extracted") ||
    e.includes("tesseract") ||
    e.includes("ocr")
  ) {
    return {
      title: "PDF 没文字（扫描件/图片型）",
      advice: "OCR 转一次再导，或换 .docx/.txt/.md。",
    };
  }
  if (e.includes("notimplementederror") || e.includes("unsupported") || e.includes("missingdependency")) {
    return {
      title: "缺少解析器",
      advice: "在 D:\\programs\\wechat-kb-tools 重装 `pip install markitdown[all]`。",
    };
  }
  if (e.includes("filenotfounderror") || e.includes("no such file")) {
    return { title: "源文件已找不到", advice: "重新选择文件再导。" };
  }
  if (e.includes("permission") || e.includes("access")) {
    return { title: "权限不足", advice: "换个 control_api 可读的目录。" };
  }
  return {
    title: "其它原因",
    advice: "可先在 D:\\programs\\wechat-kb-tools 用 markitdown 单独跑一次确认。",
  };
}

function groupFailures(failed) {
  const grouped = new Map();
  for (const item of failed || []) {
    const bucket = failureBucket(item.error || "");
    const key = bucket.title;
    if (!grouped.has(key)) grouped.set(key, { bucket, items: [] });
    grouped.get(key).items.push(item);
  }
  return Array.from(grouped.values());
}

function failureBasename(err) {
  const m = String(err || "").match(/但转换失败:\s*([^\[]+?)\s*\[/);
  if (m) {
    let path = m[1].trim();
    if (path.includes("\\")) return path.split("\\").pop();
    if (path.includes("/")) return path.split("/").pop();
    return path;
  }
  return "";
}

function failureShortReason(err) {
  const s = String(err || "");
  let m = s.match(/stderr_tail='([^']*)'/);
  if (m && m[1].trim()) {
    let line = m[1].trim().split(/\r?\n/)[0];
    if (line.includes(":") && line.split(":", 1)[0].includes("Exception")) {
      line = line.split(":", 1)[1].trim();
    }
    return line.slice(0, 160);
  }
  m = s.match(/python_markitdown_error:\s*([^|]+?)(?:\s*\||$)/);
  if (m) return m[1].trim().slice(0, 160);
  return "";
}

function showError(root, err, retry) {
  const banner = h("div", { class: "error-banner" },
    h("span", { text: String(err.message || err) }),
    h("button", { class: "btn btn-sm", text: "重试", onclick: retry })
  );
  clear(root);
  root.appendChild(banner);
}

export async function render(root) {
  const buildPollTimers = {};

  function clearTimers() {
    Object.values(buildPollTimers).forEach((id) => {
      clearInterval(id);
      clearTimeout(id);
    });
    for (const key of Object.keys(buildPollTimers)) delete buildPollTimers[key];
  }

  try {
    await renderPage(root, clearTimers, buildPollTimers);
  } catch (err) {
    showError(root, err, () => render(root));
  }

  return clearTimers;
}

async function renderPage(root, clearTimers, buildPollTimers) {
  clear(root);

  const page = h("div", { class: "page" },
    pageHeader("知识库管理"),
    h("div", { class: "card" },
      h("div", { class: "card-title", text: "加载中" }),
      spinner()
    )
  );
  root.appendChild(page);

  const [kbsRes, targetsRes, leannRes] = await Promise.all([
    list_kbs(),
    list_targets("all"),
    list_leann_indexes().catch(() => ({ indexes: [], error: "" })),
  ]);

  const kbs = Array.isArray(kbsRes) ? kbsRes : (kbsRes.knowledge_bases || []);
  const targets = (Array.isArray(targetsRes) ? targetsRes : (targetsRes.targets || [])).filter((t) => !t.is_candidate);
  const leannIndexes = (leannRes.indexes || []).map((i) => i.name).filter(Boolean);
  const leannError = leannRes.error || "";

  const localKbPaths = {};
  for (const kb of kbs) {
    if (kbTypeOf(kb) === "local" && kb.path) {
      localKbPaths[kb.id] = kb.path;
    }
  }

  function refresh() {
    clearTimers();
    return renderPage(root, clearTimers, buildPollTimers);
  }

  clear(root);
  const container = h("div", { class: "page" });
  root.appendChild(container);

  // Header
  container.appendChild(pageHeader("知识库管理"));

  // Refresh
  container.appendChild(
    h("button", { class: "btn btn-ghost btn-sm", text: "刷新", onclick: refresh })
  );

  // 新增本地知识库
  container.appendChild(renderLocalKbForm(kbs, refresh));

  // 新增 LEANN 索引
  container.appendChild(
    renderLeannForm(kbs, localKbPaths, leannIndexes, leannError, refresh)
  );

  // 绑定知识库到目标
  container.appendChild(renderBindingSection(kbs, targets, refresh));

  // 已配置知识库
  container.appendChild(
    h("h3", { class: "section-heading", text: "已配置知识库" })
  );
  if (!kbs.length) {
    container.appendChild(emptyState("未配置知识库。"));
  } else {
    const list = h("div", {});
    for (const kb of kbs) {
      list.appendChild(
        renderKbCard(kb, kbs, refresh, buildPollTimers)
      );
    }
    container.appendChild(list);
  }
}

function renderLocalKbForm(kbs, refresh) {
  const newId = h("input", { class: "input", placeholder: "例如：workdocs" });
  const newDesc = h("input", { class: "input", placeholder: "例如：工作资料本地知识库" });
  const sourceObsidian = h("input", { type: "radio", name: "local_source", value: "obsidian" });
  const sourceLocal = h("input", { type: "radio", name: "local_source", value: "local_folder", checked: true });

  const sourceRow = h("div", { class: "form-row" },
    h("label", { class: "checkbox-row" }, sourceLocal, h("span", { text: "普通文件夹" })),
    h("label", { class: "checkbox-row" }, sourceObsidian, h("span", { text: "Obsidian vault" }))
  );

  const createBtn = h("button", { class: "btn btn-primary", text: "创建" });
  createBtn.onclick = async () => {
    const id = newId.value.trim();
    if (!id) {
      toast("别名不能为空", "err");
      return;
    }
    const source = sourceObsidian.checked ? "obsidian" : "local_folder";
    try {
      await save_kb({
        id,
        type: "local",
        source,
        description: newDesc.value.trim(),
        replace: false,
      });
      toast("已创建本地知识库", "ok");
      refresh();
    } catch (err) {
      toast(`创建失败：${err.message}`, "err");
    }
  };

  return h("div", { class: "card" },
    h("div", { class: "card-title", text: "新增本地知识库" }),
    h("div", { class: "form-grid" },
      h("div", null, h("label", { text: "别名" }), newId),
      h("div", null, h("label", { text: "说明" }), newDesc),
      h("div", null, h("label", { text: "来源" }), sourceRow),
      h("div", { class: "form-row" }, createBtn)
    )
  );
}

function renderLeannForm(kbs, localKbPaths, leannIndexes, leannError, refresh) {
  const options = ["(新建)", ...leannIndexes];
  const modeSelect = h("select", { class: "select" },
    ...options.map((opt) => h("option", { value: opt, text: opt }))
  );

  const newId = h("input", { class: "input", placeholder: "例如：work_kb" });
  const newDesc = h("input", { class: "input", placeholder: "例如：工作资料向量索引" });
  const indexName = h("input", { class: "input", placeholder: "例如：work_kb" });

  const docsDir = h("input", { class: "input", placeholder: "例如 wiki/workdocs 或 C:\\docs\\work" });

  const copyFrom = h("select", { class: "select" },
    h("option", { value: "", text: "" }),
    ...Object.keys(localKbPaths).map((id) => h("option", { value: id, text: id }))
  );
  copyFrom.onchange = () => {
    const v = copyFrom.value;
    if (v && localKbPaths[v]) docsDir.value = localKbPaths[v];
  };

  const buildNow = h("input", { type: "checkbox" });

  // Update indexName visibility based on mode
  const indexNameWrap = h("div", null, h("label", { text: "LEANN 索引名" }), indexName);
  const indexCaption = h("div", { class: "caption flush" });

  function updateMode() {
    const isNew = modeSelect.value === "(新建)";
    indexNameWrap.classList.toggle("hidden", !isNew);
    indexCaption.textContent = isNew ? "" : `将绑定到已有索引：${modeSelect.value}`;
    if (isNew) indexName.value = newId.value.trim();
  }
  modeSelect.onchange = updateMode;
  newId.oninput = () => {
    if (modeSelect.value === "(新建)") indexName.value = newId.value.trim();
  };
  updateMode();

  const createBtn = h("button", { class: "btn btn-primary", text: "创建" });
  createBtn.onclick = async () => {
    const id = newId.value.trim();
    const name = modeSelect.value === "(新建)" ? indexName.value.trim() : modeSelect.value;
    if (!id || !name) {
      toast("别名和索引名不能为空", "err");
      return;
    }
    const payload = {
      id,
      type: "leann",
      knowledge_base_id: name,
      description: newDesc.value.trim(),
      replace: false,
    };
    if (docsDir.value.trim()) payload.docs_dir = docsDir.value.trim();
    try {
      await save_kb(payload);
      const messages = ["已创建 LEANN 索引绑定"];
      if (buildNow.checked) {
        try {
          const res = await build_leann_kb(id, null, true);
          messages.push(`已启动后台构建：${res.build_id || ""}`);
        } catch (e) {
          messages.push(`索引绑定已创建，但后台构建启动失败：${e.message}`);
        }
      }
      messages.forEach((m) => toast(m, "info"));
      refresh();
    } catch (err) {
      toast(`创建失败：${err.message}`, "err");
    }
  };

  return h("div", { class: "card" },
    h("div", { class: "card-title", text: "新增 LEANN 索引" }),
    h("div", { class: "card-body stack" },
      leannError
        ? h("div", { class: "error-banner", text: `读取现有 LEANN 索引列表失败：${leannError}` })
        : null,
      h("div", { class: "form-row" },
        h("label", { text: "选择已有索引或新建" }),
        modeSelect
      ),
      h("div", { class: "form-grid" },
        h("div", null, h("label", { text: "别名" }), newId),
        h("div", null, h("label", { text: "说明" }), newDesc),
        indexNameWrap
      ),
      indexCaption,
      h("div", { class: "form-grid" },
        h("div", null, h("label", { text: "来源目录" }), docsDir),
        h("div", null, h("label", { text: "从本地 KB 复制路径" }), copyFrom)
      ),
      h("label", { class: "checkbox-row" }, buildNow, h("span", { text: "创建后立即后台构建索引" })),
      h("div", { class: "btn-row" }, createBtn)
    )
  );
}

function renderBindingSection(kbs, targets, refresh) {
  const card = h("div", { class: "card" },
    h("div", { class: "card-title", text: "绑定知识库到目标" })
  );
  const body = h("div", { class: "card-body stack" });

  if (!targets.length) {
    body.appendChild(h("div", { class: "muted", text: "暂无可绑定目标。" }));
    card.appendChild(body);
    return card;
  }

  const targetOptions = targets.map((t) => ({
    value: t.name || t.username || "",
    label: `${t.name || "?"}（${t.username || ""}）`,
    target: t,
  }));

  const targetSelect = h("select", { class: "select" },
    ...targetOptions.map((opt) => h("option", { value: opt.value, text: opt.label }))
  );

  const kbSelect = h("select", { class: "select", multiple: true, size: Math.min(6, kbs.length || 1) });
  for (const kb of kbs) {
    const opt = h("option", { value: kb.id, text: kbOptionLabel(kbs, kb.id) });
    kbSelect.appendChild(opt);
  }

  const warningEl = h("div", { class: "error-banner hidden" });
  const sourceCaption = h("div", { class: "caption flush" });
  const leannInfo = h("div", { class: "leann-hint hidden" });

  function currentTarget() {
    return targetOptions.find((o) => o.value === targetSelect.value)?.target || targets[0];
  }

  function selectedIds() {
    return Array.from(kbSelect.selectedOptions).map((o) => o.value);
  }

  function updateSelected() {
    const sel = selectedIds();
    const sources = new Set(sel.map((id) => kbSourceOf(kbs.find((k) => k.id === id) || {})));
    warningEl.classList.toggle("hidden", sources.size <= 1);
    warningEl.textContent = sources.size > 1
      ? "一个监听目标只能绑定同源知识库。请只选同一个来源，例如只选 Obsidian、只选普通本地文件夹、只选 Get笔记，或只选 LEANN 索引。"
      : "";
    if (sources.size === 1) {
      const only = Array.from(sources)[0];
      sourceCaption.textContent = `当前绑定来源：${labelSource(only)}`;
      if (only === "leann") {
        const allowed = kbs
          .filter((k) => sel.includes(k.id) && kbTypeOf(k) === "leann")
          .map((k) => k.index_name || k.id || "?")
          .filter(Boolean);
        if (allowed.length) {
          leannInfo.textContent = `该目标在工具 Agent 模式下只能检索以下 LEANN 索引：${allowed.join(", ")}`;
          leannInfo.classList.remove("hidden");
        } else {
          leannInfo.classList.add("hidden");
        }
      } else {
        leannInfo.classList.add("hidden");
      }
    } else {
      sourceCaption.textContent = "";
      leannInfo.classList.add("hidden");
    }
  }

  kbSelect.onchange = updateSelected;

  // Set initial selection from current target bindings
  function applyCurrentBindings() {
    const t = currentTarget();
    const current = (t.knowledge_bases || []).filter((id) => kbs.some((k) => k.id === id));
    Array.from(kbSelect.options).forEach((o) => {
      o.selected = current.includes(o.value);
    });
    updateSelected();
  }
  targetSelect.onchange = applyCurrentBindings;
  applyCurrentBindings();

  const saveBtn = h("button", { class: "btn btn-primary", text: "保存绑定" });
  saveBtn.onclick = async () => {
    const sel = selectedIds();
    const sources = new Set(sel.map((id) => kbSourceOf(kbs.find((k) => k.id === id) || {})));
    if (sources.size > 1) {
      toast("保存失败：不能混用不同来源的知识库。", "err");
      return;
    }
    const key = currentTarget().name || currentTarget().username || "";
    try {
      await replace_target_kbs(key, sel);
      toast("已保存绑定", "ok");
      refresh();
    } catch (err) {
      toast(`保存失败：${err.message}`, "err");
    }
  };

  // Target search/diagnose
  const queryInput = h("input", { class: "input", placeholder: "例如：押金怎么退" });
  const resultEl = h("div");
  const searchBtn = h("button", { class: "btn btn-primary", text: "模拟检索" });
  const diagBtn = h("button", { class: "btn btn-ghost", text: "诊断索引" });
  const boundSelect = h("select", { class: "select" });
  const boundSelectWrap = h("div", { class: "form-row hidden" },
    h("label", { text: "选择要测试的知识库" }),
    boundSelect
  );

  const diagSection = detailsEl("按目标模拟检索", h("div", null,
    h("div", { class: "form-row" },
      h("label", { text: "查询词（测试当前目标绑定的知识库）" }),
      queryInput
    ),
    boundSelectWrap,
    h("div", { class: "form-row" }, searchBtn, diagBtn),
    resultEl
  ));

  function updateBoundSelect() {
    const t = currentTarget();
    const bound = (t.knowledge_bases || []).filter((id) => kbs.some((k) => k.id === id));
    clear(boundSelect);
    if (bound.length <= 1) {
      boundSelectWrap.classList.add("hidden");
      return;
    }
    boundSelectWrap.classList.remove("hidden");
    for (const id of bound) {
      boundSelect.appendChild(h("option", { value: id, text: kbOptionLabel(kbs, id) }));
    }
  }
  targetSelect.onchange = () => {
    applyCurrentBindings();
    updateBoundSelect();
  };
  updateBoundSelect();

  async function doTargetSearch() {
    const t = currentTarget();
    const bound = (t.knowledge_bases || []).filter((id) => kbs.some((k) => k.id === id));
    if (!bound.length) {
      toast("当前目标未绑定知识库", "err");
      return;
    }
    let kbId = bound[0];
    if (bound.length > 1 && boundSelect.value) kbId = boundSelect.value;
    const kb = kbs.find((k) => k.id === kbId) || {};
    const type = kbTypeOf(kb);
    if (!["local", "leann"].includes(type)) {
      toast(`${labelSource(kbSourceOf(kb))} 暂不支持模拟检索/诊断`, "err");
      return;
    }
    const q = queryInput.value.trim();
    if (!q) {
      toast("请先输入查询词", "err");
      return;
    }
    try {
      const res = await search_kb(kbId, q, 5);
      renderSearchResult(resultEl, res);
    } catch (err) {
      toast(`查询失败：${err.message}`, "err");
    }
  }

  async function doTargetDiagnose() {
    const t = currentTarget();
    const bound = (t.knowledge_bases || []).filter((id) => kbs.some((k) => k.id === id));
    if (!bound.length) {
      toast("当前目标未绑定知识库", "err");
      return;
    }
    let kbId = bound[0];
    if (bound.length > 1 && boundSelect.value) kbId = boundSelect.value;
    const kb = kbs.find((k) => k.id === kbId) || {};
    const type = kbTypeOf(kb);
    if (!["local", "leann"].includes(type)) {
      toast(`${labelSource(kbSourceOf(kb))} 暂不支持模拟检索/诊断`, "err");
      return;
    }
    const q = queryInput.value.trim();
    if (!q && type !== "leann") {
      toast("请先输入查询词", "err");
      return;
    }
    try {
      const res = await diagnose_kb(kbId, q);
      renderDiagnoseResult(resultEl, res);
    } catch (err) {
      toast(`诊断失败：${err.message}`, "err");
    }
  }

  searchBtn.onclick = doTargetSearch;
  diagBtn.onclick = doTargetDiagnose;

  body.appendChild(h("div", { class: "form-row" },
    h("label", { text: "选择目标" }),
    targetSelect
  ));
  body.appendChild(h("div", { class: "form-row" },
    h("label", { text: "绑定知识库" }),
    kbSelect
  ));
  body.appendChild(warningEl);
  body.appendChild(sourceCaption);
  body.appendChild(leannInfo);
  body.appendChild(saveBtn);
  body.appendChild(diagSection);

  card.appendChild(body);
  return card;
}

function renderKbCard(kb, kbs, refresh, buildPollTimers) {
  const kbId = kb.id || "";
  const enabled = Boolean(kb.enabled);
  const type = kbTypeOf(kb);
  const managed = kb.managed !== false;
  const sourceLabel = labelSource(kbSourceOf(kb));

  // --- title / badges ---
  let typeBadge = null;
  if (type === "local") {
    const fc = kb.file_count;
    typeBadge = Number.isInteger(fc) && fc
      ? badge(`${fc} 个文档`, "info")
      : badge("本地", "info");
  } else if (type === "getnote" || type === "ima") {
    typeBadge = badge(type === "getnote" ? "Get笔记" : "IMA", "info");
  } else if (type === "leann") {
    const exists = Boolean(kb.exists);
    typeBadge = badge(exists ? "LEANN · 存在" : "LEANN · 缺失", exists ? "ok" : "warn");
  } else if (type) {
    typeBadge = badge(type, "muted");
  }

  // --- meta rows (proper k/v so long paths use full width) ---
  const meta = h("div", { class: "kv-stack" });
  function addRow(label, value, valueClass) {
    if (value === null || value === undefined || value === "") return;
    const vCls = valueClass ? `v ${valueClass}` : "v";
    meta.appendChild(
      h("div", { class: "kv" },
        h("span", { class: "k", text: label }),
        h("span", { class: vCls, text: String(value) })
      )
    );
  }

  addRow("来源", sourceLabel);
  addRow("说明", (kb.description || "").trim() || "—");
  if (!managed) addRow("注册状态", "未注册（只读）");

  if (type === "local") {
    addRow("路径", kb.path || "—", "path-value");
  }
  if (type === "getnote" || type === "ima") {
    const name = (kb.online_name || "").trim();
    if (name) {
      const bits = [];
      if (Number.isInteger(kb.online_note_count) && kb.online_note_count) bits.push(`笔记 ${kb.online_note_count}`);
      if (Number.isInteger(kb.online_file_count) && kb.online_file_count) bits.push(`文件 ${kb.online_file_count}`);
      addRow("在线库", bits.length ? `${name}（${bits.join("，")}）` : name);
    } else if (kb.online_error) {
      addRow("在线库", `名称未获取：${kb.online_error}`, "text-warn");
    }
    if (kb.knowledge_base_id) addRow("外部ID", kb.knowledge_base_id, "path-value");
  }
  if (type === "leann") {
    addRow("索引名", kb.index_name || kb.knowledge_base_id || "—", "path-value");
    const docsDir = kb.docs_dir || "";
    const docsDirResolved = kb.docs_dir_resolved || "";
    if (docsDir) {
      addRow(
        "来源目录",
        docsDirResolved && docsDirResolved !== docsDir ? `${docsDir}  →  ${docsDirResolved}` : docsDir,
        "path-value"
      );
    }
    if (kb.index_path) addRow("物理位置", kb.index_path, "path-value");
  } else if (kb.path && type !== "local") {
    addRow("路径", kb.path, "path-value");
  }

  // --- LEANN: missing docs_dir warning + edit + build ---
  const leannBlocks = [];
  if (type === "leann") {
    const docsDir = kb.docs_dir || "";
    if (!docsDir) {
      leannBlocks.push(
        h("div", { class: "error-banner" },
          h("span", { text: "未配置来源目录（docs_dir），无法构建索引。请在下方补充。" })
        )
      );
    }

    const editDocsDir = h("input", {
      class: "input",
      value: docsDir,
      placeholder: "例如 wiki/workdocs 或 C:\\docs\\work",
    });
    const saveDocsBtn = h("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: "保存来源目录",
    });
    saveDocsBtn.onclick = async () => {
      const v = editDocsDir.value.trim();
      if (!v) {
        toast("来源目录不能为空", "err");
        return;
      }
      const payload = {
        id: kbId,
        type: "leann",
        knowledge_base_id: kb.index_name || kb.knowledge_base_id || "",
        description: kb.description || "",
        enabled,
        replace: true,
        docs_dir: v,
      };
      if (kb.executable) payload.executable = kb.executable;
      if (kb.limit !== null && kb.limit !== undefined) payload.limit = kb.limit;
      if (kb.timeout !== null && kb.timeout !== undefined) payload.timeout = kb.timeout;
      try {
        await save_kb(payload);
        toast("已保存来源目录", "ok");
        refresh();
      } catch (err) {
        toast(`保存失败：${err.message}`, "err");
      }
    };
    leannBlocks.push(
      detailsEl(
        "编辑 LEANN 来源目录",
        h("div", { class: "stack-sm" },
          h("div", { class: "form-row" },
            h("label", { text: "来源目录" }),
            editDocsDir
          ),
          saveDocsBtn
        ),
        !docsDir
      )
    );

    const buildBtn = h("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: "重建索引",
    });
    const buildStatusEl = h("span", { class: "badge badge-muted", text: "—" });

    function applyBuildStatus(info) {
      if (!info) {
        buildStatusEl.className = "badge badge-muted";
        buildStatusEl.textContent = "—";
        return;
      }
      const st = String(info.status || "");
      if (st === "running" || st === "started" || st === "pending") {
        buildStatusEl.className = "badge badge-info";
        buildStatusEl.textContent = `构建中 · ${st}`;
      } else if (st === "done" || st === "success" || st === "completed") {
        buildStatusEl.className = "badge badge-ok";
        const raw = info.finished_at || info.updated_at || info.started_at || "";
        let tsLabel = "";
        if (raw !== "" && raw !== null && raw !== undefined) {
          const n = Number(raw);
          if (Number.isFinite(n) && n > 1e9) tsLabel = fmtTs(n > 1e12 ? n / 1000 : n);
          else tsLabel = String(raw).slice(0, 16).replace("T", " ");
        }
        buildStatusEl.textContent = tsLabel
          ? `构建完成 · ${tsLabel}`
          : "构建完成";
      } else if (st === "error" || st === "failed") {
        buildStatusEl.className = "badge badge-err";
        buildStatusEl.textContent = `构建失败 · ${st}`;
      } else {
        buildStatusEl.className = "badge badge-muted";
        buildStatusEl.textContent = st || "—";
      }
    }

    // seed status from kb if present
    if (kb.last_build_status || kb.build_status) {
      applyBuildStatus({
        status: kb.last_build_status || kb.build_status,
        finished_at: kb.last_build_finished_at || kb.last_build_at,
      });
    } else if (kb.last_build_id) {
      buildStatusEl.textContent = "有历史构建";
    }

    function startBuildPoll(buildId, { autoRefreshOnDone = true } = {}) {
      if (!buildId || !buildPollTimers) return;
      if (buildPollTimers[kbId]) {
        clearInterval(buildPollTimers[kbId]);
        delete buildPollTimers[kbId];
      }
      const TERMINAL = ["done", "success", "completed", "error", "failed"];
      const tick = async () => {
        try {
          const info = await leann_build_status(kbId, buildId || "");
          applyBuildStatus(info);
          const st = String((info && info.status) || "");
          if (TERMINAL.includes(st) || st === "unknown" || !st) {
            if (buildPollTimers[kbId]) {
              clearInterval(buildPollTimers[kbId]);
              delete buildPollTimers[kbId];
            }
            // Full-page refresh only after a user-started build finishes.
            // History last_build_id must NOT re-trigger refresh (infinite loop).
            if (
              autoRefreshOnDone &&
              (st === "done" || st === "success" || st === "completed")
            ) {
              setTimeout(() => refresh(), 400);
            }
          }
        } catch (err) {
          buildStatusEl.className = "badge badge-err";
          buildStatusEl.textContent = `读取构建状态失败：${err.message}`;
        }
      };
      tick();
      buildPollTimers[kbId] = setInterval(tick, 2000);
    }

    buildBtn.onclick = async () => {
      try {
        const res = await build_leann_kb(kbId, null, true);
        toast(`已启动后台构建：${res.build_id || ""}`, "ok");
        applyBuildStatus({ status: "started" });
        if (res.build_id) startBuildPoll(res.build_id, { autoRefreshOnDone: true });
      } catch (err) {
        toast(`启动失败：${err.message}`, "err");
      }
    };

    // History build: one-shot status only. Poll only if still in-flight.
    if (kb.last_build_id) {
      (async () => {
        try {
          const info = await leann_build_status(kbId, kb.last_build_id);
          applyBuildStatus(info);
          const st = String((info && info.status) || "");
          if (["running", "started", "pending"].includes(st)) {
            startBuildPoll(kb.last_build_id, { autoRefreshOnDone: false });
          }
        } catch (err) {
          buildStatusEl.className = "badge badge-muted";
          buildStatusEl.textContent = "有历史构建";
        }
      })();
    }

    leannBlocks.push(
      h("div", { class: "kb-toolbar" }, buildBtn, buildStatusEl)
    );

    const logBody = h("div");
    const logDetails = detailsEl("查看构建日志", logBody);
    logDetails.addEventListener("toggle", async () => {
      if (!logDetails.open) return;
      try {
        const info = await leann_build_log(kbId, kb.last_build_id || null);
        clear(logBody);
        logBody.appendChild(h("pre", null, h("code", { text: info.log_tail || "（空）" })));
      } catch (err) {
        clear(logBody);
        logBody.appendChild(
          h("div", {
            class: "muted",
            text: err.status === 404 ? "暂无日志" : `读取日志失败：${err.message}`,
          })
        );
      }
    }, { once: false });
    leannBlocks.push(logDetails);
  }

  // --- local KB: upload / import ---
  const localBlocks = [];
  if (type === "local") {
    const fileInput = h("input", { class: "input", type: "file", multiple: true });
    const dirInput = h("input", {
      class: "input",
      type: "text",
      placeholder: "或填写目录路径，例如 C:\\\\docs\\\\project-x",
    });
    const importBtn = h("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: "导入文件",
    });
    importBtn.onclick = async () => {
      const files = fileInput.files;
      const source = (dirInput.value || "").trim();
      if ((!files || !files.length) && !source) {
        toast("请先选择文件或输入目录路径", "err");
        return;
      }
      importBtn.disabled = true;
      const prev = importBtn.textContent;
      importBtn.textContent = "导入/转换中…";
      try {
        if (files && files.length) {
          const fileList = await readFilesAsBase64(files);
          await upload_kb_files(kbId, fileList);
          toast(`已上传 ${fileList.length} 个文件`, "ok");
          fileInput.value = "";
        }
        if (source) {
          await import_kb(kbId, source, true);
          toast("已从目录导入", "ok");
        }
        refresh();
      } catch (err) {
        toast(`导入失败：${err.message}`, "err");
      } finally {
        importBtn.disabled = false;
        importBtn.textContent = prev;
      }
    };
    localBlocks.push(
      detailsEl(
        "导入文档",
        h("div", { class: "stack" },
          h("div", { class: "form-row" }, h("label", { text: "选择文件" }), fileInput),
          h("div", { class: "form-row" }, h("label", { text: "目录路径" }), dirInput),
          importBtn
        )
      )
    );
  }

  // --- actions ---
  const toggleBtn = h("button", {
    class: "btn " + (enabled ? "btn-ghost" : "btn-primary") + " btn-sm",
    type: "button",
    text: enabled ? "停用" : "启用",
    disabled: !managed,
    title: managed ? "" : "未注册的 KB 无法启用/停用",
  });
  toggleBtn.onclick = async () => {
    try {
      if (enabled) await disable_kb(kbId);
      else await enable_kb(kbId);
      toast(enabled ? "已停用" : "已启用", "ok");
      refresh();
    } catch (err) {
      toast(`${enabled ? "停用" : "启用"}失败：${err.message}`, "err");
    }
  };

  const deleteBtn = h("button", {
    class: "btn btn-danger btn-sm",
    type: "button",
    text: "删除",
    disabled: !managed,
  });
  deleteBtn.onclick = async () => {
    if (!confirmDanger(`确定删除知识库「${kbId}」？此操作不可恢复。`)) return;
    try {
      await delete_kb(kbId, false);
      toast("已删除", "ok");
      refresh();
    } catch (err) {
      toast(`删除失败：${err.message}`, "err");
    }
  };

  // --- test / open ---
  const testBlocks = [];
  if (type === "local" || type === "leann") {
    const queryInput = h("input", {
      class: "input",
      type: "text",
      placeholder: "例如：押金怎么退",
    });
    const testResultEl = h("div", { class: "kb-test-result" });
    const searchBtn = h("button", {
      class: "btn btn-primary btn-sm",
      type: "button",
      text: "查询",
    });
    const diagBtn = h("button", {
      class: "btn btn-ghost btn-sm",
      type: "button",
      text: "诊断索引",
    });
    searchBtn.onclick = async () => {
      const q = queryInput.value.trim();
      if (!q) {
        toast("请先输入查询词", "err");
        return;
      }
      try {
        const res = await search_kb(kbId, q, 5);
        renderSearchResult(testResultEl, res);
      } catch (err) {
        toast(`查询失败：${err.message}`, "err");
      }
    };
    diagBtn.onclick = async () => {
      const q = queryInput.value.trim() || "测试";
      try {
        const res = await diagnose_kb(kbId, q);
        clear(testResultEl);
        testResultEl.appendChild(
          h("pre", null, h("code", { text: JSON.stringify(res, null, 2) }))
        );
      } catch (err) {
        toast(`诊断失败：${err.message}`, "err");
      }
    };

    const openBtns = [];
    if (type === "local" || type === "leann") {
      const openBtn = h("button", {
        class: "btn btn-ghost btn-sm",
        type: "button",
        text: "打开目录",
      });
      openBtn.onclick = async () => {
        try {
          await open_kb(kbId);
          toast("已打开目录", "ok");
        } catch (err) {
          toast(`打开失败：${err.message}`, "err");
        }
      };
      openBtns.push(openBtn);
    }
    // obsidian open if applicable
    if (String(kbSourceOf(kb) || "").includes("obsidian") || kb.source === "obsidian") {
      const obsBtn = h("button", {
        class: "btn btn-ghost btn-sm",
        type: "button",
        text: "打开 Obsidian",
      });
      obsBtn.onclick = async () => {
        try {
          await open_kb_obsidian(kbId);
          toast("已打开 Obsidian", "ok");
        } catch (err) {
          toast(`打开失败：${err.message}`, "err");
        }
      };
      openBtns.push(obsBtn);
    }

    testBlocks.push(
      detailsEl(
        "测试检索 / 打开目录",
        h("div", { class: "stack" },
          h("div", { class: "form-row" },
            h("label", { text: "查询词" }),
            queryInput
          ),
          h("div", { class: "btn-row" }, searchBtn, diagBtn, ...openBtns),
          testResultEl
        )
      )
    );
  }

  const head = h("div", { class: "card-head" },
    h("div", { class: "card-head-main" },
      h("span", { class: "card-head-title", text: kbId || "(no id)" }),
      badge(enabled ? "已启用" : "已停用", enabled ? "ok" : "muted"),
      typeBadge
    ),
    h("div", { class: "card-actions" }, toggleBtn, deleteBtn)
  );

  const bodyChildren = [meta, ...leannBlocks, ...localBlocks, ...testBlocks];
  const body = h("div", { class: "card-body kb-card-body" }, ...bodyChildren);

  return h("div", { class: "card kb-card" }, head, body);
}

function renderSearchResult(el, res) {
  clear(el);
  const hits = res.hits || [];
  const matched = res.matched_files || 0;
  const total = res.total_files || 0;
  el.appendChild(h("div", { class: "muted", text: `命中 ${matched} / ${total} 个文档` }));
  if (!hits.length) {
    el.appendChild(h("div", { class: "muted", text: "没有命中。" }));
    return;
  }
  const ul = h("ul", { class: "hit-list" });
  for (const hItem of hits) {
    const score = hItem.score;
    const scoreTxt = (typeof score === "number") ? ` · 分数 ${Number.isInteger(score) ? score : score.toFixed(3)}` : "";
    const li = h("li", { class: "hit-item" },
      h("div", { class: "hit-title", text: `${hItem.rel_path || "（无路径）"}${scoreTxt}` })
    );
    const snippet = (hItem.snippet || "").trim();
    if (snippet) {
      li.appendChild(h("div", { class: "muted", text: snippet.slice(0, 200).replace(/\n/g, " ") }));
    }
    ul.appendChild(li);
  }
  el.appendChild(ul);
}

function renderDiagnoseResult(el, res) {
  clear(el);
  const details = detailsEl("诊断结果", h("pre", {}, h("code", { text: JSON.stringify(res, null, 2) })), true);
  el.appendChild(details);
}

function renderBuildStatus(el, info, kbId, refresh, buildPollTimers) {
  clear(el);
  const status = info.status || "unknown";
  const error = info.error || "";
  const updated = info.updated_at;
  let statusText = status;
  if (updated) statusText += ` · ${fmtTs(updated)}`;

  let badgeKind = "muted";
  let text = `状态：${statusText}`;
  if (status === "done") {
    badgeKind = "ok";
    text = `构建完成 · ${statusText}`;
  } else if (status === "failed") {
    badgeKind = "err";
    let extra = "";
    if (error) extra += `：${error}`;
    if (info.returncode !== null && info.returncode !== undefined) {
      extra += `（返回码 ${info.returncode}）`;
    }
    text = `构建失败${extra}`;
  } else if (status === "running" || status === "started") {
    badgeKind = "info";
    text = status === "running" ? `构建中… · ${statusText}` : `已启动 · ${statusText}`;
  }
  el.appendChild(badge(text, badgeKind));
}

function renderImportResult(el, lastImport, kbId) {
  clear(el);
  if (!lastImport || !lastImport.failed || !lastImport.failed.length) return;
  const ts = Math.floor((lastImport.ts || 0) / 1000);
  const grouped = groupFailures(lastImport.failed);
  const wrapper = h("div", {},
    h("div", { class: "error-banner" }, h("span", { text: `失败明细 · ${lastImport.failed.length} 个 · ${fmtTs(ts)}` }))
  );
  for (const { bucket, items } of grouped) {
    wrapper.appendChild(h("strong", { text: `${bucket.title}（${items.length} 个）` }));
    wrapper.appendChild(h("div", { class: "muted" }, h("span", { text: bucket.advice })));
    const ul = h("ul");
    for (const item of items) {
      const err = item.error || "";
      const name = failureBasename(err) || item.path || "?";
      const short = failureShortReason(err);
      const li = h("li", null, h("code", { text: name }));
      if (short) li.appendChild(h("div", { class: "muted" }, h("span", { text: short })));
      const fullDetails = detailsEl("完整错误", h("pre", null, h("code", { text: err })));
      li.appendChild(fullDetails);
      ul.appendChild(li);
    }
    wrapper.appendChild(ul);
  }
  el.appendChild(wrapper);
}

function readFilesAsBase64(files) {
  return Promise.all(
    Array.from(files).map((file) => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = reader.result;
        const commaIdx = dataUrl.indexOf(",");
        const contentB64 = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl;
        resolve({ filename: file.name, content_b64: contentB64 });
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    }))
  );
}
