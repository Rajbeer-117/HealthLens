/* HealthLens chatbot: floating widget, bottom-right on every signed-in page.
   Uses /api/patients/<id>/ask. The conversation survives page changes (kept per patient in sessionStorage). */
(function () {
  'use strict';
  const dataEl = document.getElementById('chatData');
  const panel = document.getElementById('chatPanel');
  if (!dataEl || !panel) return;
  const cfg = JSON.parse(dataEl.textContent);
  const launcher = document.getElementById('chatLauncher'), closeBtn = document.getElementById('chatClose');
  const log = document.getElementById('chatLog'), form = document.getElementById('chatForm'), input = document.getElementById('chatInput');
  const suggest = document.getElementById('chatSuggest');
  const KEY = `hl-chat:${cfg.source}:${cfg.patient}`, OPEN_KEY = 'hl-chat-open';
  const NOTES = { offline: 'Answered from templates (no AI connected)', fallback: 'Answered from templates (AI unavailable)' };
  const WELCOME = 'Hi! Ask me about this record: for example blood pressure, sleep, labs, medications or activity. I explain patterns in the data; I cannot diagnose or give treatment advice.';
  let history = [];

  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* storage unavailable: chat still works, just not remembered */ } },
  };
  function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
  // The model may mention evidence IDs like [E3]; they mean nothing here, so hide them. textContent keeps all text inert.
  const tidy = t => String(t)
    .replace(/\s*[\[(]\s*E\d{1,3}(?:\s*[,;&]\s*E\d{1,3})*\s*[\])]/g, '')   // "(E3, E4)" and "[E3]"
    .replace(/\s*\bE\d{1,3}\b/g, '')                                       // bare "E3"
    .replace(/\s{2,}/g, ' ').trim();

  function bubble(who, text, note) {
    const b = el('div', 'msg msg-' + who);
    b.append(el('p', '', text));
    if (note) b.append(el('span', 'msg-meta', note));
    log.append(b); log.scrollTop = log.scrollHeight;
    return b;
  }
  function remember(who, text, note) {
    history.push({ who, text, note }); store.set(KEY, JSON.stringify(history.slice(-40)));
  }

  // ---- open / close
  function setOpen(open) {
    panel.hidden = !open; launcher.setAttribute('aria-expanded', String(open)); store.set(OPEN_KEY, open ? '1' : '0');
    if (open) { log.scrollTop = log.scrollHeight; if (!input.disabled) input.focus(); } else launcher.focus();
  }
  launcher.addEventListener('click', () => setOpen(panel.hidden));
  closeBtn.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) setOpen(false); });

  // ---- restore earlier conversation
  try { history = JSON.parse(store.get(KEY) || '[]'); } catch (e) { history = []; }
  if (!Array.isArray(history)) history = [];
  bubble('bot', cfg.patient ? WELCOME : 'There is no patient record selected yet. Upload a workbook on the Documents page or switch to the demo patients, then ask me about it.');
  history.forEach(m => bubble(m.who, m.text, m.note));
  if (history.length) suggest.hidden = true;
  if (!cfg.patient) { input.disabled = true; input.placeholder = 'Select a patient first'; suggest.hidden = true; form.querySelector('button').disabled = true; }
  if (store.get(OPEN_KEY) === '1') setOpen(true);

  // ---- asking
  async function ask(q) {
    q = q.trim(); if (q.length < 3 || !cfg.patient) return;
    suggest.hidden = true;
    bubble('user', q); remember('user', q);
    input.value = ''; input.disabled = true;
    const wait = bubble('bot', 'Thinking…'); wait.classList.add('waiting');
    const slow = setTimeout(() => { wait.firstChild.textContent = 'Still thinking. The first answer can take up to a minute while the model loads.'; }, 8000);
    try {
      const res = await fetch(`/api/patients/${encodeURIComponent(cfg.patient)}/ask`, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': cfg.csrf }, body: JSON.stringify({ source: cfg.source, question: q }) });
      const r = await res.json().catch(() => ({ error: 'Unexpected server response' }));
      if (!res.ok) throw new Error(r.error || 'HTTP ' + res.status);
      const text = tidy(r.answer), note = NOTES[r.mode] || '';
      wait.remove(); bubble('bot', text, note); remember('bot', text, note);
    } catch (err) {
      wait.remove(); bubble('bot', 'Sorry, something went wrong: ' + err.message);
    } finally {
      clearTimeout(slow); input.disabled = false; if (!panel.hidden) input.focus();
    }
  }
  form.addEventListener('submit', e => { e.preventDefault(); ask(input.value); });
  suggest.querySelectorAll('.suggest').forEach(s => s.addEventListener('click', () => ask(s.textContent)));
})();
