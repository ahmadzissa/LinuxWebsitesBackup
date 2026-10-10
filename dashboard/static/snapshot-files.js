/* File/folder selection persists across navigation and the restore review. */
(async function () {
  'use strict';
  const browser = document.getElementById('file-browser');
  const status = document.getElementById('load-status');
  const rows = document.getElementById('file-rows');
  const filter = document.getElementById('file-filter');
  const selectVisible = document.getElementById('select-visible');
  const storageKey = 'webbackup-selection:' + location.pathname;
  let nodes, children, current, restorable, selected = new Set();
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function button(text, action, className) {
    const node = element('button', text, className);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }
  function ancestors(path) {
    const parts = path.split('/');
    return parts.map((_, i) => parts.slice(0, i + 1).join('/'));
  }
  function covered(path) { return ancestors(path).some(p => selected.has(p)); }
  function selectable(entry) { return ['file', 'directory'].includes(entry.type); }
  function choose(path, checked) {
    if (checked) {
      if (!covered(path)) {
        for (const p of selected) if (p.startsWith(path + '/')) selected.delete(p);
        selected.add(path);
      }
    } else {
      // Split a selected ancestor into sibling folders/files when a child is
      // unchecked. Large folders stay represented by folders, not every file.
      const covering = ancestors(path).find(p => selected.has(p));
      if (covering && covering !== path) {
        selected.delete(covering);
        let branch = covering;
        while (branch !== path) {
          const next = ancestors(path).find(p => p.startsWith(branch + '/') && !p.slice(branch.length + 1).includes('/'));
          for (const child of children.get(branch) || []) {
            if (child.path !== next && selectable(child)) selected.add(child.path);
          }
          branch = next;
        }
      }
      selected.delete(path);
      for (const p of selected) if (p.startsWith(path + '/')) selected.delete(p);
    }
  }
  function size(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return (i ? bytes.toFixed(1) : bytes) + ' ' + units[i];
  }
  function visibleEntries() {
    const term = filter.value.trim().toLocaleLowerCase();
    return (children.get(current) || []).filter(e => e.path.split('/').pop().toLocaleLowerCase().includes(term));
  }
  function updateSelection() {
    try { sessionStorage.setItem(storageKey, JSON.stringify([...selected])); } catch (_) { /* Private browsing may disable storage. */ }
    const files = [...nodes.values()].filter(e => e.type === 'file' && covered(e.path));
    document.getElementById('selection-summary').textContent = selected.size
      ? selected.size + ' selected · ' + files.length + ' file(s) · ' + size(files.reduce((n, e) => n + e.size, 0))
      : 'No files selected';
    const list = document.getElementById('selected-paths');
    list.replaceChildren();
    const inputs = document.getElementById('selection-inputs');
    inputs.replaceChildren();
    for (const path of [...selected].sort()) {
      const row = element('li');
      row.append(element('span', path, 'mono'));
      const remove = button('Remove', () => { choose(path, false); render(); }, 'small secondary');
      remove.setAttribute('aria-label', 'Remove ' + path + ' from selection');
      row.append(remove);
      list.append(row);
      const input = element('input');
      input.type = 'hidden'; input.name = 'paths'; input.value = path;
      inputs.append(input);
    }
    document.getElementById('review-restore').disabled = !restorable || !selected.size || selected.size > 5000;
    const available = visibleEntries().filter(selectable);
    selectVisible.checked = available.length > 0 && available.every(e => covered(e.path));
    selectVisible.indeterminate = !selectVisible.checked && available.some(e => covered(e.path));
    selectVisible.disabled = !restorable || !available.length;
  }
  function navigate(path) { current = path; filter.value = ''; render(); }
  function render() {
    const crumbs = document.getElementById('breadcrumbs');
    crumbs.replaceChildren();
    for (const path of ancestors(current)) {
      if (crumbs.childNodes.length) crumbs.append(element('span', '/'));
      const crumb = button(path.split('/').pop(), () => navigate(path));
      if (path === current) crumb.setAttribute('aria-current', 'page');
      crumbs.append(crumb);
    }
    rows.replaceChildren();
    const visible = visibleEntries();
    for (const entry of visible) {
      const row = element('tr');
      if (covered(entry.path)) row.className = 'selected';
      const checkCell = element('td');
      const check = element('input');
      check.type = 'checkbox';
      check.checked = covered(entry.path);
      check.indeterminate = !check.checked && [...selected].some(p => p.startsWith(entry.path + '/'));
      check.disabled = !restorable || !selectable(entry);
      const name = entry.path.split('/').pop();
      check.setAttribute('aria-label', 'Select ' + name);
      check.addEventListener('change', () => { choose(entry.path, check.checked); render(); });
      checkCell.append(check);
      const nameCell = element('td');
      if (entry.type === 'directory') {
        const folder = button('📁 ' + name, () => navigate(entry.path), 'file-name-button');
        folder.setAttribute('aria-label', 'Open folder ' + name);
        nameCell.append(folder);
      } else {
        nameCell.append(element('span', (entry.type === 'file' ? '📄 ' : '🔗 ') + name));
        if (!selectable(entry)) nameCell.append(element('span', ' · skipped', 'hint'));
      }
      row.append(checkCell, nameCell, element('td', entry.type === 'file' ? size(entry.size) : '—', 'mono'),
        element('td', entry.modified ? new Date(entry.modified * 1000).toLocaleString() : '—', 'hint'));
      rows.append(row);
    }
    if (!visible.length) {
      const row = element('tr'), cell = element('td', filter.value ? 'No matching files or folders.' : 'This folder is empty.');
      cell.colSpan = 4; row.append(cell); rows.append(row);
    }
    document.getElementById('folder-status').textContent = visible.length + ' item(s) in this folder';
    updateSelection();
  }
  filter.addEventListener('input', render);
  selectVisible.addEventListener('change', () => {
    const checked = selectVisible.checked;
    for (const entry of visibleEntries().filter(selectable)) choose(entry.path, checked);
    render();
  });
  document.getElementById('clear-selection').addEventListener('click', () => { selected.clear(); render(); });
  document.getElementById('restore-form').addEventListener('submit', event => {
    if (!restorable || !selected.size || selected.size > 5000) {
      event.preventDefault();
      status.hidden = false;
      status.textContent = selected.size > 5000 ? 'Select up to 5000 files or folders in one restore. Select their parent folder to include more files.' : 'Choose at least one file or folder.';
    }
  });
  try {
    const response = await fetch(browser.dataset.indexUrl, {headers: {Accept: 'application/json'}});
    if (response.redirected) throw new Error('Your session expired. Sign in and reopen the snapshot.');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not load the backup.');
    nodes = new Map(data.index.entries.map(e => [e.path, e]));
    children = new Map();
    for (const entry of nodes.values()) {
      const parent = entry.path.includes('/') ? entry.path.slice(0, entry.path.lastIndexOf('/')) : '';
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(entry);
    }
    for (const entries of children.values()) entries.sort((a, b) =>
      Number(b.type === 'directory') - Number(a.type === 'directory') || a.path.localeCompare(b.path));
    restorable = data.restorable;
    current = data.index.root;
    document.getElementById('website-title').textContent = data.site.label;
    document.getElementById('restore-destination').textContent = data.site.path || 'Original path unavailable';
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
      selected = new Set(saved.filter(p => nodes.has(p) && selectable(nodes.get(p))));
    } catch (_) { selected = new Set(); }
    status.hidden = restorable;
    status.textContent = restorable ? '' : 'This older snapshot has no original path map. You can browse it, but restoring requires a manual download.';
    document.getElementById('browser-content').hidden = false;
    document.getElementById('selection-card').hidden = false;
    render();
  } catch (error) {
    status.textContent = error.message;
    status.className = 'flash';
  }
})();
