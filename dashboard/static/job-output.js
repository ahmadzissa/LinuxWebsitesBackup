'use strict';

(() => {
  const output = document.getElementById('job-output');
  if (!output?.dataset.outputUrl) return;
  const live = document.getElementById('job-live');
  const status = document.getElementById('job-status');
  const statusLabel = document.getElementById('job-status-label');
  const finishedAt = document.getElementById('job-finished');
  const failure = document.getElementById('job-failure');
  let offset = Number(output.dataset.outputOffset);
  let timer, controller;
  let failures = 0, finished = false, paused = false;
  output.scrollTop = output.scrollHeight;

  function setText(element, value) {
    // Avoid repeating unchanged status announcements for screen readers.
    if (element.textContent !== value) element.textContent = value;
  }

  function stop(message) {
    finished = true;
    live.classList.remove('live-label');
    setText(live, message);
  }

  async function poll() {
    if (finished || paused || controller) return;
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 10000);
    try {
      const url = new URL(output.dataset.outputUrl, window.location.href);
      url.searchParams.set('offset', offset);
      const response = await fetch(url, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal
      });
      if (response.redirected || response.status === 401) {
        stop('Session expired · sign in to resume');
        document.getElementById('job-sign-in').hidden = false;
        return;
      }
      if (response.status === 404) {
        stop('This job is no longer available');
        return;
      }
      if (!response.ok) throw new Error('Job output unavailable');
      const data = await response.json();
      if (!['running', 'ok', 'fail'].includes(data.status) ||
          typeof data.output !== 'string' || !Number.isSafeInteger(data.offset) || data.offset < 0) {
        throw new Error('Invalid job output');
      }
      const selection = window.getSelection();
      const reading = selection && !selection.isCollapsed &&
        (output.contains(selection.anchorNode) || output.contains(selection.focusNode));
      const follow = !reading && output.scrollHeight - output.scrollTop - output.clientHeight < 48;
      if (data.reset || (offset === 0 && data.output)) output.textContent = '';
      if (data.output) output.appendChild(document.createTextNode(data.output));
      offset = data.offset;
      if (follow) output.scrollTop = output.scrollHeight;
      failures = 0;
      status.className = 'pill ' + data.status;
      setText(statusLabel, {running: 'Running', ok: 'Completed', fail: 'Failed'}[data.status]);
      setText(finishedAt, data.finished || (data.status === 'running' ? 'In progress' : '—'));
      failure.hidden = data.status !== 'fail';
      if (data.status !== 'running') {
        if (offset === 0) output.textContent = 'No output recorded.';
        stop(data.status === 'ok' ? 'Job completed' : 'Job failed');
      } else {
        live.classList.add('live-label');
        setText(live, 'Live · updates automatically');
      }
    } catch {
      if (!paused) {
        failures++;
        live.classList.remove('live-label');
        setText(live, 'Connection interrupted · retrying…');
      }
    } finally {
      clearTimeout(timeout);
      controller = null;
      if (!finished && !paused) timer = setTimeout(poll, Math.min(15000, 2000 * 2 ** Math.min(failures, 3)));
    }
  }

  window.addEventListener('pagehide', () => {
    paused = true;
    clearTimeout(timer);
    controller?.abort();
  });
  window.addEventListener('pageshow', () => {
    if (!paused) return;
    paused = false;
    poll();
  });
  poll();
})();
