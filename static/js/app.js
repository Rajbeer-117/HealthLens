/* HealthLens dashboard (Deliverable 2): tabs, charts, evidence popovers, AI summary and Ask HealthLens. */
(function () {
  'use strict';

  // ------------------------------------------------------------------ tabs
  function openTab(name) {
    const btn = document.querySelector(`.tab[data-tab="${name}"]`);
    if (!btn) return;
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === btn));
    document.querySelectorAll('.panel').forEach(x => x.classList.toggle('active', x.id === name));
    if (name === 'charts') drawCharts();
  }
  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => openTab(b.dataset.tab)));
  document.querySelectorAll('[data-open-tab]').forEach(a => a.addEventListener('click', e => {
    e.preventDefault(); openTab(a.dataset.openTab);
    document.querySelector('.tabs').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));

  const raw = document.getElementById('dashboardData');
  if (!raw) return;
  const data = JSON.parse(raw.textContent);
  const evidence = Object.fromEntries((data.evidence || []).map(e => [e.id, e]));

  // ------------------------------------------------------------------ helpers
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text; // textContent only: LLM text is never parsed as HTML
    return n;
  }
  function chip(id) {
    const b = el('button', 'ev-chip', id);
    b.type = 'button'; b.dataset.ev = id;
    if (!evidence[id]) b.classList.add('ev-missing');
    return b;
  }

  // ------------------------------------------------------------------ evidence popover
  const pop = document.getElementById('evPop');
  document.addEventListener('click', e => {
    const c = e.target.closest('.ev-chip');
    if (!c) { if (pop && !e.target.closest('#evPop')) pop.hidden = true; return; }
    const item = evidence[c.dataset.ev];
    pop.replaceChildren();
    if (!item) { pop.append(el('p', '', 'Unknown evidence ID')); }
    else {
      const head = el('div', 'ev-pop-head');
      head.append(el('strong', '', item.id), el('span', 'pill', item.kind.replace('_', ' ')));
      const close = el('button', 'ev-close', '×'); close.type = 'button'; close.setAttribute('aria-label', 'Close');
      close.addEventListener('click', () => { pop.hidden = true; });
      head.append(close);
      pop.append(head, el('p', '', item.text), el('p', 'small muted', 'Method: ' + item.method), el('p', 'small muted', 'Source: ' + item.source));
      const link = el('a', 'small', 'Open in Evidence Register →'); link.href = '#row-' + item.id;
      link.addEventListener('click', ev => { ev.preventDefault(); pop.hidden = true; openTab('evidence');
        const row = document.getElementById('row-' + item.id); if (row) { row.scrollIntoView({ block: 'center' }); row.classList.add('flash'); setTimeout(() => row.classList.remove('flash'), 1600); } });
      pop.append(link);
    }
    const r = c.getBoundingClientRect();
    pop.hidden = false;
    const w = Math.min(380, window.innerWidth - 24);
    pop.style.width = w + 'px';
    pop.style.left = Math.max(12, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - w - 12)) + 'px';
    pop.style.top = (window.scrollY + r.bottom + 8) + 'px';
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && pop) pop.hidden = true; });

  // ------------------------------------------------------------------ timeline filters
  document.querySelectorAll('#timelineFilters .filter').forEach(f => f.addEventListener('click', () => {
    document.querySelectorAll('#timelineFilters .filter').forEach(x => x.classList.toggle('active', x === f));
    const cat = f.dataset.cat;
    document.querySelectorAll('.tl-item').forEach(i => { i.hidden = !(cat === 'all' || i.dataset.cat === cat); });
    // hide month headers with no visible items
    document.querySelectorAll('.tl-month').forEach(m => {
      let n = m.nextElementSibling, any = false;
      while (n && !n.classList.contains('tl-month')) { if (!n.hidden) any = true; n = n.nextElementSibling; }
      m.hidden = !any;
    });
  }));

  // ------------------------------------------------------------------ charts
  let chartsDrawn = false, mainChart = null;
  const metric = document.getElementById('metricSelect');
  const order = data.chart_order || Object.keys(data.charts);  // tojson sorts keys, so keep the engine's order
  order.forEach(k => { const c = data.charts[k];
    const o = el('option', '', `${c.label} (${c.unit})${c.direction === 'rising' ? ' ▲' : c.direction === 'falling' ? ' ▼' : ''}`);
    o.value = k; metric.append(o);
  });
  function datasetsFor(c, mini) {
    const ds = [{ type: 'line', label: c.label, data: c.points.map(p => ({ x: p.date, y: p.value })), borderColor: '#9445c7',
      backgroundColor: 'rgba(148,69,199,.10)', tension: .25, pointRadius: c.points.length > 40 ? 0 : 3, borderWidth: mini ? 1.5 : 2, fill: false, order: 3 }];
    if (c.trendline.length) ds.push({ type: 'line', label: 'Trend (Theil-Sen)', data: c.trendline.map(p => ({ x: p.date, y: p.value })),
      borderColor: '#138a83', borderDash: [6, 5], borderWidth: 2, pointRadius: 0, fill: false, order: 1 });
    if (c.anomalies.length) ds.push({ type: 'scatter', label: 'Unusual reading', data: c.anomalies.map(p => ({ x: p.date, y: p.value })),
      backgroundColor: '#d1453b', pointRadius: mini ? 3 : 5, order: 0 });
    return ds;
  }
  const refBand = {
    id: 'refBand',
    beforeDatasetsDraw(chart, _a, opts) {
      const ref = opts && opts.ref; if (!ref || (ref.low == null && ref.high == null)) return;
      const { ctx, chartArea: a, scales: { y } } = chart;
      const top = ref.high != null ? y.getPixelForValue(ref.high) : a.top;
      const bot = ref.low != null ? y.getPixelForValue(ref.low) : a.bottom;
      ctx.save(); ctx.fillStyle = 'rgba(19,138,131,.08)';
      ctx.fillRect(a.left, Math.max(a.top, top), a.right - a.left, Math.min(a.bottom, bot) - Math.max(a.top, top)); ctx.restore();
    }
  };
  function chartOptions(c, mini) {
    return { responsive: true, maintainAspectRatio: false, animation: false, parsing: true,
      plugins: { legend: { display: !mini }, refBand: { ref: c.ref } },
      scales: { x: { type: 'category', labels: c.points.map(p => p.date), ticks: { maxTicksLimit: mini ? 4 : 10 } }, y: { beginAtZero: false } } };
  }
  function drawMain() {
    const c = data.charts[metric.value]; if (!c) return;
    if (mainChart) mainChart.destroy();
    mainChart = new Chart(document.getElementById('healthChart'), { type: 'line', data: { datasets: datasetsFor(c, false) }, options: chartOptions(c, false), plugins: [refBand] });
  }
  function drawCharts() {
    if (chartsDrawn || !metric.options.length) return;
    if (typeof Chart === 'undefined') { document.getElementById('healthChart').insertAdjacentHTML('beforebegin', '<p>Chart.js could not load. Check internet access.</p>'); return; }
    chartsDrawn = true;
    metric.addEventListener('change', drawMain); drawMain();
    const holder = document.getElementById('miniCharts');
    order.map(k => data.charts[k]).forEach(c => {
      const panel = el('article', 'mini-chart');
      const h = el('h4', '', `${c.label} `); h.append(el('small', 'muted', c.unit));
      if (c.direction === 'rising' || c.direction === 'falling') h.append(el('span', 'dir dir-' + c.direction, c.direction === 'rising' ? ' ▲' : ' ▼'));
      const wrap = el('div', 'mini-canvas'); const cv = el('canvas'); wrap.append(cv); panel.append(h, wrap); holder.append(panel);
      new Chart(cv, { type: 'line', data: { datasets: datasetsFor(c, true) }, options: chartOptions(c, true), plugins: [refBand] });
    });
  }

  // ------------------------------------------------------------------ sheet preview
  const select = document.getElementById('sheetSelect');
  Object.keys(data.sheets).forEach(k => { const o = el('option', '', k); o.value = k; select.append(o); });
  function showSheet() {
    const sheet = data.sheets[select.value]; if (!sheet) return;
    const table = el('table'), thead = el('thead'), tr = el('tr'), tbody = el('tbody');
    sheet.columns.forEach(c => tr.append(el('th', '', c))); thead.append(tr);
    sheet.rows.forEach(r => { const row = el('tr'); r.forEach(v => row.append(el('td', '', v))); tbody.append(row); });
    table.append(thead, tbody);
    document.getElementById('sheetTable').replaceChildren(table);
  }
  select.addEventListener('change', showSheet); showSheet();

  // ------------------------------------------------------------------ AI summary
  async function api(path, body) {
    const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': data.csrf },
      body: JSON.stringify({ source: data.source, ...body }) });
    const json = await res.json().catch(() => ({ error: 'Unexpected server response' }));
    if (!res.ok) throw new Error(json.error || ('HTTP ' + res.status));
    return json;
  }
  const base = `/api/patients/${encodeURIComponent(data.patient)}`;
  const modeText = { llm: 'LLM-generated · passed guardrails', offline: 'Template explanation (offline mode)',
    fallback: 'Fallback template', screened: 'Safety response' };

  function metaBadges(r) {
    const meta = document.getElementById('aiMeta'); meta.replaceChildren();
    meta.append(el('span', 'pill mode-' + r.mode, modeText[r.mode] || r.mode));
    meta.append(el('span', 'pill', `${r.provider} · ${r.model}`));
    if (r.latency_ms != null) meta.append(el('span', 'pill', r.cached ? 'cached' : `${(r.latency_ms / 1000).toFixed(1)} s`));
    if (r.note && r.mode === 'fallback') meta.append(el('p', 'note', r.note));
    if (r.guardrails && r.guardrails.issues && r.guardrails.issues.length) {
      const d = el('details', 'guard-issues'); d.append(el('summary', '', `Guardrails rejected the LLM reply (${r.guardrails.issues.length} issue(s))`));
      const ul = el('ul'); r.guardrails.issues.forEach(i => ul.append(el('li', '', i))); d.append(ul); meta.append(d);
    }
  }
  function renderTrace(trace, title) {
    const box = document.getElementById('aiTrace');
    if (!trace) { box.hidden = true; return; }
    box.hidden = false;
    document.getElementById('traceTitle').textContent = '(' + title + ')';
    document.getElementById('traceSystem').textContent = trace.system_prompt;
    document.getElementById('traceUser').textContent = trace.user_prompt;
    const holder = document.getElementById('traceAttempts'); holder.replaceChildren();
    if (trace.offline) {
      holder.append(el('p', 'note', 'Offline mode: this prompt was NOT sent, because no LLM is connected. Start Ollama (or add an API key) and refresh to send it.'));
      return;
    }
    (trace.attempts || []).forEach(a => {
      const card = el('div', 'attempt ' + (a.error ? 'bad' : a.problems.length ? 'bad' : 'good'));
      const head = el('div', 'attempt-head');
      head.append(el('strong', '', 'Attempt ' + a.n), el('span', 'pill', (a.ms / 1000).toFixed(1) + ' s'));
      head.append(el('span', 'pill ' + (a.error || a.problems.length ? 'mode-fallback' : 'mode-llm'),
        a.error ? 'LLM error' : a.problems.length ? 'Rejected by guardrails' : 'Passed all guardrails ✓'));
      card.append(head);
      if (a.error) card.append(el('p', 'small', a.error));
      if (a.problems.length) { const ul = el('ul', 'small'); a.problems.forEach(p => ul.append(el('li', '', p))); card.append(ul); }
      if (a.raw_reply) { card.append(el('p', 'small muted', 'Raw LLM output:')); card.append(el('pre', '', a.raw_reply)); }
      holder.append(card);
    });
  }

  async function loadSummary() {
    const L = document.getElementById('aiLoading'), E = document.getElementById('aiError'), R = document.getElementById('aiResult');
    L.hidden = false; E.hidden = true;
    try {
      const r = await api(base + '/ai-summary', {});
      metaBadges(r);
      document.getElementById('aiOverview').textContent = r.overview;
      const pts = document.getElementById('aiPoints'); pts.replaceChildren();
      (r.points || []).forEach((p, i) => {
        const card = el('article', 'ai-point');
        card.append(el('span', 'ai-num', String(i + 1)));
        const body = el('div'); body.append(el('h4', '', p.title), el('p', '', p.explanation));
        const ev = el('div', 'ev-row'); ev.append(el('span', 'small muted', 'Evidence: ')); (p.evidence || []).forEach(id => ev.append(chip(id)));
        body.append(ev); card.append(body); pts.append(card);
      });
      const q = document.getElementById('aiQuestions'); q.replaceChildren(); (r.questions || []).forEach(x => q.append(el('li', '', x)));
      document.getElementById('aiLimits').textContent = r.limitations || '';
      renderTrace(r.trace, 'AI summary');
      R.hidden = false;
    } catch (err) { E.textContent = 'Could not generate the summary: ' + err.message; E.hidden = false; }
    finally { L.hidden = true; }
  }
  document.getElementById('aiRegenerate').addEventListener('click', loadSummary);
  loadSummary();

  // ------------------------------------------------------------------ Ask HealthLens
  const log = document.getElementById('chatLog'), form = document.getElementById('askForm'), input = document.getElementById('askInput');
  function bubble(who, text, r) {
    const b = el('div', 'msg msg-' + who);
    b.append(el('p', '', text));
    if (r) {
      const foot = el('div', 'ev-row');
      (r.evidence || []).forEach(id => foot.append(chip(id)));
      foot.append(el('span', 'pill mode-' + r.mode, modeText[r.mode] || r.mode));
      if (r.note) foot.append(el('span', 'small muted', r.note));
      if (r.mode === 'llm') foot.append(el('span', 'small muted', `${r.model} · ${(r.latency_ms / 1000).toFixed(1)} s`));
      b.append(foot);
    }
    log.append(b); log.scrollTop = log.scrollHeight; return b;
  }
  async function ask(q) {
    q = q.trim(); if (q.length < 3) return;
    bubble('user', q); input.value = ''; input.disabled = true;
    const wait = bubble('bot', 'Thinking…'); wait.classList.add('waiting');
    try { const r = await api(base + '/ask', { question: q }); wait.remove(); bubble('bot', r.answer, r); if (r.trace) renderTrace(r.trace, 'last question: ' + q); }
    catch (err) { wait.remove(); bubble('bot', 'Sorry, something went wrong: ' + err.message); }
    finally { input.disabled = false; input.focus(); }
  }
  form.addEventListener('submit', e => { e.preventDefault(); ask(input.value); });
  document.querySelectorAll('.suggest').forEach(s => s.addEventListener('click', () => ask(s.textContent)));
})();
