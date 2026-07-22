/* 微信助手控制台 —— 通用 UI 工具 */

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs && typeof attrs === 'object' && !Array.isArray(attrs) && !(attrs instanceof Node)) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val == null) continue;
      if (key === 'text') {
        el.textContent = String(val);
      } else if (key === 'html') {
        el.innerHTML = String(val);
      } else if (key === 'class') {
        el.className = String(val);
      } else if (key.startsWith('on') && typeof val === 'function') {
        const evt = key.slice(2).toLowerCase();
        el.addEventListener(evt, val);
      } else if (key === 'value' || key === 'checked' || key === 'disabled' || key === 'selected' || key === 'multiple') {
        el[key] = val;
      } else if (key === 'colspan') {
        el.colSpan = val;
      } else {
        el.setAttribute(key === 'href' || key === 'title' || key === 'placeholder' || key === 'type' || key === 'id' || key === 'rows' || key === 'max' || key === 'min' || key === 'accept' || key === 'size' ? key : key, val);
      }
    }
  } else if (attrs !== undefined && attrs !== null) {
    children.unshift(attrs);
  }

  function append(c) {
    if (c == null) return;
    if (Array.isArray(c)) {
      c.forEach(append);
    } else if (c instanceof Node) {
      el.appendChild(c);
    } else {
      el.appendChild(document.createTextNode(String(c)));
    }
  }
  children.forEach(append);
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

export function tableEl(columns, rows) {
  if (!rows || rows.length === 0) {
    return h('div', { class: 'empty-state', text: '暂无数据' });
  }
  const table = h('table', { class: 'table' });
  const thead = h('thead');
  const trHead = h('tr');
  for (const col of columns) {
    trHead.appendChild(h('th', { text: col.label }));
  }
  thead.appendChild(trHead);
  table.appendChild(thead);

  const tbody = h('tbody');
  for (const row of rows) {
    const tr = h('tr');
    for (const col of columns) {
      let content = '';
      if (col.render) {
        content = col.render(row);
      } else if (col.key) {
        const v = row[col.key];
        content = v === undefined || v === null ? '' : String(v);
      }
      const td = h('td');
      if (content instanceof Node) td.appendChild(content);
      else td.textContent = String(content);
      if (col.class) td.className = col.class;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

export function badge(text, kind = 'muted') {
  return h('span', { class: `badge badge-${kind}`, text: String(text) });
}

let toastContainer = null;
export function toast(msg, kind = 'info') {
  if (!toastContainer) {
    toastContainer = document.getElementById('toast-container');
  }
  if (!toastContainer) return;
  const el = h('div', { class: `toast toast-${kind}`, text: String(msg) });
  toastContainer.appendChild(el);
  setTimeout(() => {
    if (el.parentNode) el.parentNode.removeChild(el);
  }, 3500);
}

function pad2(n) { return n < 10 ? '0' + n : n; }

export function fmtTs(epochSeconds) {
  const d = new Date((epochSeconds || 0) * 1000);
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function fmtDate(epochSeconds) {
  const d = new Date((epochSeconds || 0) * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

export function spinner(text = '加载中…') {
  return h('span', { class: 'spinner', text });
}

export function confirmDanger(msg) {
  return window.confirm(msg);
}

export function emptyState(text) {
  return h('div', { class: 'empty-state', text });
}

export function metricCard(label, value, sub) {
  const card = h('div', { class: 'metric' });
  card.appendChild(h('div', { class: 'metric-value', text: String(value) }));
  card.appendChild(h('div', { class: 'metric-label', text: label }));
  if (sub !== undefined && sub !== null) {
    card.appendChild(h('div', { class: 'metric-sub', text: String(sub) }));
  }
  return card;
}

export function detailsEl(summaryText, body, open = false) {
  const details = h('details', { class: 'details', ...(open ? { open: true } : {}) });
  details.appendChild(h('summary', { text: summaryText }));
  const bodyWrap = h('div', { class: 'details-body' });
  if (body instanceof Node) bodyWrap.appendChild(body);
  else bodyWrap.textContent = String(body);
  details.appendChild(bodyWrap);
  return details;
}

export function svgGroupedBars({ labels, series, height = 220 }) {
  const width = 640;
  const margin = { top: 16, right: 16, bottom: 40, left: 44 };
  const innerW = width - margin.left - margin.right;
  const innerH = height - margin.top - margin.bottom;

  const allValues = [];
  for (const s of series) {
    for (const v of s.values) {
      if (typeof v === 'number') allValues.push(v);
    }
  }
  const maxVal = allValues.length ? Math.max(...allValues) : 0;
  const niceMax = maxVal === 0 ? 1 : Math.ceil(maxVal * 1.1) || 1;

  const svgNs = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNs, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  svg.style.width = '100%';
  svg.style.height = 'auto';

  const g = document.createElementNS(svgNs, 'g');
  g.setAttribute('transform', `translate(${margin.left},${margin.top})`);
  svg.appendChild(g);

  // grid + y-axis labels
  const yTicks = 5;
  for (let i = 0; i <= yTicks; i++) {
    const y = innerH - (i * innerH / yTicks);
    const val = Math.round(i * niceMax / yTicks);

    const line = document.createElementNS(svgNs, 'line');
    line.setAttribute('x1', 0);
    line.setAttribute('x2', innerW);
    line.setAttribute('y1', y);
    line.setAttribute('y2', y);
    line.setAttribute('stroke', '#e5e7eb');
    line.setAttribute('stroke-width', '1');
    g.appendChild(line);

    const text = document.createElementNS(svgNs, 'text');
    text.setAttribute('x', -8);
    text.setAttribute('y', y + 4);
    text.setAttribute('text-anchor', 'end');
    text.setAttribute('font-size', '10');
    text.setAttribute('fill', '#6b7280');
    text.textContent = String(val);
    g.appendChild(text);
  }

  if (!labels.length || !series.length) return svg;

  const n = labels.length;
  const groupW = innerW / n;
  const barPad = 4;
  const barW = series.length > 0 ? (groupW - barPad * 2) / series.length : 0;

  for (let gi = 0; gi < n; gi++) {
    const x0 = gi * groupW + barPad;
    for (let si = 0; si < series.length; si++) {
      const s = series[si];
      const val = typeof s.values[gi] === 'number' ? s.values[gi] : 0;
      const bh = maxVal === 0 ? 0 : (val / niceMax) * innerH;
      const x = x0 + si * barW;
      const y = innerH - bh;

      const rect = document.createElementNS(svgNs, 'rect');
      rect.setAttribute('x', x);
      rect.setAttribute('y', y);
      rect.setAttribute('width', Math.max(barW - 1, 1));
      rect.setAttribute('height', Math.max(bh, 0));
      rect.setAttribute('fill', s.color || '#9ca3af');
      rect.setAttribute('rx', 2);
      g.appendChild(rect);

      const title = document.createElementNS(svgNs, 'title');
      title.textContent = `${labels[gi]} ${s.name}: ${val}`;
      rect.appendChild(title);
    }

    // x-axis label
    const label = document.createElementNS(svgNs, 'text');
    label.setAttribute('x', gi * groupW + groupW / 2);
    label.setAttribute('y', innerH + 18);
    label.setAttribute('text-anchor', 'middle');
    label.setAttribute('font-size', '10');
    label.setAttribute('fill', '#64748b');
    label.textContent = String(labels[gi]);
    g.appendChild(label);
  }

  return svg;
}

export function pageHeader(title, subtitle, actions) {
  const textCol = h('div', null);
  textCol.appendChild(h('h1', { text: title }));
  if (subtitle !== undefined && subtitle !== null) {
    textCol.appendChild(h('p', { class: 'subtitle', text: String(subtitle) }));
  }
  const el = h('div', { class: 'pageHeader' }, textCol);
  if (actions) {
    const bar = h('div', { class: 'page-toolbar', style: 'margin:0;' });
    if (Array.isArray(actions)) actions.forEach((n) => n && bar.appendChild(n));
    else if (actions instanceof Node) bar.appendChild(actions);
    el.appendChild(bar);
  }
  return el;
}
