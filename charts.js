/* HealthLens Summary page: trend charts for every measure, with a group filter. */
(function () {
  'use strict';
  const raw = document.getElementById('chartData');
  if (!raw) return;
  const data = JSON.parse(raw.textContent);
  const order = data.order;
  const metric = document.getElementById('metricSelect');
  const filters = document.getElementById('groupFilters');
  const note = document.getElementById('trendNote');
  if (typeof Chart === 'undefined') {
    document.getElementById('healthChart').insertAdjacentHTML('beforebegin', '<p>Chart.js could not load, so the charts are unavailable.</p>');
    return;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // ---- chart drawing
  function datasetsFor(c, mini) {
    const ds = [{ type: 'line', label: c.label, data: c.points.map(p => ({ x: p.date, y: p.value })), borderColor: '#9445c7',
      tension: .25, pointRadius: c.points.length > 40 ? 0 : 3, borderWidth: mini ? 1.5 : 2, fill: false, order: 2 }];
    if (c.trendline.length) ds.push({ type: 'line', label: 'Trend', data: c.trendline.map(p => ({ x: p.date, y: p.value })),
      borderColor: '#138a83', borderDash: [6, 5], borderWidth: 2, pointRadius: 0, fill: false, order: 1 });
    return ds;
  }
  const refBand = {
    id: 'refBand',
    beforeDatasetsDraw(chart, _args, opts) {
      const ref = opts && opts.ref; if (!ref || (ref.low == null && ref.high == null)) return;
      const { ctx, chartArea: a, scales: { y } } = chart;
      const top = ref.high != null ? y.getPixelForValue(ref.high) : a.top;
      const bot = ref.low != null ? y.getPixelForValue(ref.low) : a.bottom;
      ctx.save(); ctx.fillStyle = 'rgba(19,138,131,.08)';
      ctx.fillRect(a.left, Math.max(a.top, top), a.right - a.left, Math.min(a.bottom, bot) - Math.max(a.top, top)); ctx.restore();
    }
  };
  function options(c, mini) {
    return { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: !mini }, refBand: { ref: c.ref } },
      scales: { x: { type: 'category', labels: c.points.map(p => p.date), ticks: { maxTicksLimit: mini ? 4 : 10 } }, y: { beginAtZero: false } } };
  }

  // ---- main chart + the sentence under it
  let mainChart = null;
  function describe(c) {
    const t = c.trend, parts = [];
    if (t && t.sustained) {
      const sign = t.slope_per_month > 0 ? '+' : '';
      parts.push(`${c.label} has been ${t.direction} consistently: ${t.baseline} → ${t.recent} ${c.unit} (${t.change_pct > 0 ? '+' : ''}${t.change_pct}%), about ${sign}${t.slope_per_month} ${c.unit} per month.`);
    }
    if (c.ref && c.ref.text) parts.push(`Reference range: ${c.ref.text} ${c.unit}.`);
    note.textContent = parts.join(' ');
  }
  function drawMain() {
    const c = data.charts[metric.value]; if (!c) return;
    if (mainChart) mainChart.destroy();
    mainChart = new Chart(document.getElementById('healthChart'), { type: 'line', data: { datasets: datasetsFor(c, false) }, options: options(c, false), plugins: [refBand] });
    describe(c);
  }
  metric.addEventListener('change', drawMain);

  // ---- selector options: one full-sized chart only
  const arrow = c => c.trend && c.trend.sustained ? (c.trend.direction === 'rising' ? ' ▲' : c.trend.direction === 'falling' ? ' ▼' : '') : '';
  function fillSelect(keys) {
    metric.replaceChildren();
    keys.forEach(k => { const c = data.charts[k]; const o = el('option', '', `${c.label} (${c.unit})${arrow(c)}`); o.value = k; metric.append(o); });
  }
  fillSelect(order);

  // ---- group filter (Labs, Vitals, Activity & sleep, ...)
  const groups = ['All', ...new Set(order.map(k => data.charts[k].group))];
  groups.forEach((g, i) => {
    const n = g === 'All' ? order.length : order.filter(k => data.charts[k].group === g).length;
    const b = el('button', 'filter' + (i === 0 ? ' active' : ''), `${g} (${n})`); b.type = 'button'; b.dataset.group = g;
    b.addEventListener('click', () => {
      filters.querySelectorAll('.filter').forEach(x => x.classList.toggle('active', x === b));
      const keys = order.filter(k => g === 'All' || data.charts[k].group === g);
      const current = metric.value;
      fillSelect(keys);
      if (keys.includes(current)) metric.value = current;
      if (keys.length) drawMain();
    });
    filters.append(b);
  });
  if (groups.length < 3) filters.hidden = true;

  if (order.length) { metric.value = order[0]; drawMain(); }
  else note.textContent = 'This workbook has no measurements that can be charted.';
})();
