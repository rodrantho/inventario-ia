(() => {
  const q = (s, root = document) => root.querySelector(s);
  const qa = (s, root = document) => [...root.querySelectorAll(s)];

  qa('[data-open-dialog]').forEach(button => button.addEventListener('click', () => {
    const dialog = document.getElementById(button.dataset.openDialog);
    if (dialog) dialog.showModal();
  }));
  qa('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog')?.close()));
  qa('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  }));

  qa('[data-toggle-menu]').forEach(button => button.addEventListener('click', event => {
    event.stopPropagation();
    button.closest('.export-menu')?.classList.toggle('open');
  }));
  document.addEventListener('click', () => qa('.export-menu.open').forEach(menu => menu.classList.remove('open')));

  const upload = q('.upload-form');
  if (upload) {
    const input = q('input[type=file]', upload);
    const selected = q('.selected-file', upload);
    input?.addEventListener('change', () => {
      selected.textContent = input.files?.[0]?.name || '';
    });
    upload.addEventListener('submit', async event => {
      event.preventDefault();
      const file = input.files?.[0];
      if (!file) return;
      const csrf = q('input[name=csrf_token]', upload).value;
      const roomName = q('input[name=room_name]', upload).value;
      const chunkSize = 8 * 1024 * 1024;
      const totalChunks = Math.ceil(file.size / chunkSize);
      const progressBox = q('.upload-progress', upload);
      const bar = q('.progress span', progressBox);
      const label = q('small b', progressBox);
      const submit = q('button[type=submit]', upload);
      progressBox.hidden = false;
      submit.disabled = true;
      submit.textContent = 'Subiendo…';
      const showProgress = uploaded => {
        const percent = Math.min(100, Math.round((uploaded / file.size) * 100));
        bar.style.width = `${percent}%`;
        label.textContent = `${percent}%`;
      };
      const request = async (url, options, attempts = 3) => {
        let lastError;
        for (let attempt = 0; attempt < attempts; attempt++) {
          try {
            const response = await fetch(url, {...options, credentials: 'same-origin'});
            if (!response.ok) throw new Error(await response.text());
            return response;
          } catch (error) {
            lastError = error;
            if (attempt + 1 < attempts) await new Promise(resolve => setTimeout(resolve, 700 * (attempt + 1)));
          }
        }
        throw lastError;
      };
      try {
        const startData = new FormData();
        startData.set('room_name', roomName);
        startData.set('original_name', file.name);
        startData.set('content_type', file.type || 'application/octet-stream');
        startData.set('expected_size', String(file.size));
        startData.set('total_chunks', String(totalChunks));
        startData.set('csrf_token', csrf);
        const started = await request(`${upload.action}/start`, {method: 'POST', body: startData});
        const {upload_id: uploadId} = await started.json();
        let uploaded = 0;
        for (let index = 0; index < totalChunks; index++) {
          const chunk = file.slice(index * chunkSize, Math.min(file.size, (index + 1) * chunkSize));
          await request(`/api/uploads/${uploadId}/chunks/${index}`, {
            method: 'PUT', body: chunk, headers: {'X-CSRF-Token': csrf, 'Content-Type': 'application/octet-stream'}
          });
          uploaded += chunk.size;
          showProgress(uploaded);
        }
        submit.textContent = 'Preparando análisis…';
        const completed = await request(`/api/uploads/${uploadId}/complete`, {
          method: 'POST', headers: {'X-CSRF-Token': csrf}
        });
        const result = await completed.json();
        window.location.href = result.location;
      } catch (error) {
        submit.disabled = false;
        submit.textContent = 'Reintentar';
        alert('No pudimos completar la carga. Revisá la conexión e intentá nuevamente.');
      }
    });
  }

  const processing = q('[data-status-url]');
  if (processing) {
    const poll = async () => {
      try {
        const response = await fetch(processing.dataset.statusUrl, {credentials: 'same-origin'});
        if (!response.ok) return;
        const state = await response.json();
        q('#progress-bar').style.width = `${state.progress}%`;
        q('#progress-label').textContent = `${state.progress}%`;
        q('#status-message').textContent = state.message;
        if (!['queued', 'processing'].includes(state.status)) {
          window.setTimeout(() => window.location.reload(), 700);
          return;
        }
      } catch (_) {}
      window.setTimeout(poll, 2500);
    };
    window.setTimeout(poll, 1500);
  }
})();
