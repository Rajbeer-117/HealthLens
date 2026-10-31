/* HealthLens Documents page: drag & drop, selected-file list, delete confirmation. */
(function () {
  'use strict';
  const input = document.getElementById('fileInput');
  const zone = document.getElementById('dropzone');
  const list = document.getElementById('fileList');
  const btn = document.getElementById('uploadBtn');

  function size(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' KB' : (n / 1048576).toFixed(1) + ' MB'; }
  function show() {
    list.replaceChildren();
    Array.from(input.files).forEach(f => {
      const li = document.createElement('li');
      const name = document.createElement('span'), meta = document.createElement('span');
      name.textContent = f.name; meta.textContent = size(f.size); li.append(name, meta); list.append(li);
    });
    btn.disabled = !input.files.length;
    btn.textContent = input.files.length > 1 ? `Upload ${input.files.length} files` : 'Upload';
  }
  if (input) {
    input.addEventListener('change', show);
    ['dragenter', 'dragover'].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.remove('drag'); }));
    zone.addEventListener('drop', e => { if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); } });
    document.getElementById('uploadForm').addEventListener('submit', () => { btn.disabled = true; btn.textContent = 'Uploading…'; });
  }
  document.querySelectorAll('form[data-confirm]').forEach(f => f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));
})();
