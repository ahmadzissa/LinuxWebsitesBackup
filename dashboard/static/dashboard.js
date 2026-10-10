'use strict';

function schedTypeChanged(select) {
  const builder = select.closest('.sched-builder');
  if (!builder) return;
  const type = select.value;
  const fields = {
    '.sched-time': ['daily', 'twice', 'weekly', 'monthly'].includes(type),
    '.sched-dow': type === 'weekly',
    '.sched-dom': type === 'monthly',
    '.sched-custom': type === 'custom'
  };
  Object.entries(fields).forEach(([selector, visible]) => {
    const field = builder.querySelector(selector);
    if (field) field.hidden = !visible;
  });
}

document.querySelectorAll('.sched-type').forEach(select => {
  const builder = select.closest('.sched-builder');
  const cron = builder.dataset.currentCron || '';
  const fields = cron.trim().split(/\s+/);
  if (fields.length === 5) {
    const [minute, hour, day, month, weekday] = fields;
    const timeIsSimple = /^\d+$/.test(minute) && Number(minute) < 60 && /^\d+$/.test(hour) && Number(hour) < 24;
    let type = 'custom';
    if (timeIsSimple && month === '*') {
      if (day === '*' && weekday === '*') type = 'daily';
      else if (day === '*' && /^[0-6]$/.test(weekday)) type = 'weekly';
      else if (/^\d+$/.test(day) && Number(day) >= 1 && Number(day) <= 28 && weekday === '*') type = 'monthly';
      builder.querySelector('[name="sched_time"]').value = hour.padStart(2, '0') + ':' + minute.padStart(2, '0');
      if (type === 'weekly') builder.querySelector('[name="sched_dow"]').value = weekday;
      if (type === 'monthly') builder.querySelector('[name="sched_dom"]').value = day;
    }
    if (cron === '15 */6 * * *') type = 'every6h';
    if (cron === '5 * * * *') type = 'hourly';
    select.value = type;
  }
  schedTypeChanged(select);
  select.addEventListener('change', () => schedTypeChanged(select));
});

const sidebar = document.getElementById('sidebar');
const menuToggle = document.getElementById('menu-toggle');
const overlay = document.getElementById('sidebar-overlay');
function setMenu(open) {
  if (!sidebar || !menuToggle || !overlay) return;
  sidebar.classList.toggle('is-open', open);
  const mobile = window.innerWidth <= 780;
  sidebar.inert = mobile && !open;
  sidebar.setAttribute('aria-hidden', String(mobile && !open));
  menuToggle.setAttribute('aria-expanded', String(open));
  menuToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
  overlay.hidden = !open;
  document.body.style.overflow = open ? 'hidden' : '';
  if (open) sidebar.querySelector('a').focus();
}
menuToggle?.addEventListener('click', () => setMenu(!sidebar.classList.contains('is-open')));
overlay?.addEventListener('click', () => { setMenu(false); menuToggle.focus(); });
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && sidebar?.classList.contains('is-open')) {
    setMenu(false);
    menuToggle.focus();
  }
  if (event.key === 'Tab' && sidebar?.classList.contains('is-open')) {
    const links = [...sidebar.querySelectorAll('a')];
    const first = links[0], last = links[links.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
sidebar?.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setMenu(false)));
window.matchMedia('(max-width: 780px)').addEventListener('change', () => setMenu(false));
setMenu(false);

const serverCheckboxes = [...document.querySelectorAll('input[name="server_ids"]')];
const selectAll = document.getElementById('select-all-servers');
function updateSelection() {
  const selected = serverCheckboxes.filter(box => box.checked).length;
  const visible = serverCheckboxes.filter(box => !box.closest('tr').hidden);
  if (selectAll) {
    selectAll.checked = visible.length > 0 && visible.every(box => box.checked);
    selectAll.indeterminate = visible.some(box => box.checked) && !selectAll.checked;
  }
  const label = document.getElementById('server-selection');
  if (label) label.textContent = selected ? selected + ' server' + (selected === 1 ? '' : 's') + ' selected' : 'Select servers to apply settings';
  const apply = document.getElementById('apply-server-settings');
  if (apply) apply.disabled = selected === 0;
  const note = document.getElementById('bulk-selection-note');
  if (note) note.textContent = selected ? 'Settings will apply to ' + selected + ' selected server' + (selected === 1 ? '.' : 's.') : 'Select at least one server above.';
}
serverCheckboxes.forEach(box => box.addEventListener('change', updateSelection));
selectAll?.addEventListener('change', () => {
  serverCheckboxes.filter(box => !box.closest('tr').hidden).forEach(box => { box.checked = selectAll.checked; });
  updateSelection();
});

document.querySelectorAll('[data-filter-table]').forEach(container => {
  const search = container.querySelector('[data-table-search]');
  const filter = container.querySelector('[data-table-filter]');
  const rows = [...container.querySelectorAll('[data-filter-row]')];
  function applyFilter() {
    const term = (search?.value || '').trim().toLowerCase();
    const state = filter?.value || '';
    let shown = 0;
    rows.forEach(row => {
      const matchesStatus = !state || row.dataset.status === state || (state === 'attention' && !['ok', 'linked'].includes(row.dataset.status));
      row.hidden = !matchesStatus || !(row.dataset.search || row.textContent).toLowerCase().includes(term);
      if (!row.hidden) shown++;
    });
    const empty = container.querySelector('[data-filter-empty]');
    if (empty) empty.hidden = shown !== 0 || rows.length === 0;
    const count = container.querySelector('[data-table-count]');
    if (count) count.textContent = 'Showing ' + shown + ' of ' + rows.length + ' ' + (container.dataset.tableNoun || 'servers');
    updateSelection();
  }
  search?.addEventListener('input', applyFilter);
  filter?.addEventListener('change', applyFilter);
  applyFilter();
});
updateSelection();

document.querySelectorAll('[data-tab-group]').forEach(group => {
  const links = [...group.querySelectorAll('[data-tab]')];
  const panels = links.map(link => document.getElementById(link.dataset.tab));
  group.setAttribute('role', 'tablist');
  function activate(link, updateHash) {
    links.forEach((item, index) => {
      const active = item === link;
      item.setAttribute('role', 'tab');
      item.setAttribute('aria-selected', String(active));
      item.setAttribute('aria-controls', item.dataset.tab);
      item.id = 'tab-' + item.dataset.tab;
      item.tabIndex = active ? 0 : -1;
      if (panels[index]) {
        panels[index].hidden = !active;
        panels[index].setAttribute('role', 'tabpanel');
        panels[index].setAttribute('aria-labelledby', item.id);
        panels[index].tabIndex = 0;
      }
    });
    if (updateHash) history.replaceState(null, '', '#' + link.dataset.tab);
  }
  links.forEach((link, index) => {
    link.addEventListener('click', event => { event.preventDefault(); activate(link, true); });
    link.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % links.length;
      if (event.key === 'ArrowLeft') next = (index + links.length - 1) % links.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = links.length - 1;
      if (next !== undefined) { event.preventDefault(); links[next].focus(); activate(links[next], true); }
    });
  });
  function fromHash() { activate(links.find(link => '#' + link.dataset.tab === location.hash) || links[0], false); }
  fromHash();
  window.addEventListener('hashchange', fromHash);
});

document.querySelectorAll('[data-copy-target]').forEach(button => {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      button.textContent = 'Copied!';
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target);
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Text selected — copy it';
    }
    setTimeout(() => { button.textContent = original; }, 2500);
  });
});

const confirmation = document.getElementById('action-confirmation');
function askConfirmation(message, expectedName = '') {
  return new Promise(resolve => {
    document.getElementById('confirmation-message').textContent = message;
    document.getElementById('confirmation-title').textContent = expectedName ? 'Permanently delete backups?' : 'Confirm this action';
    const nameField = document.getElementById('confirmation-name-field');
    const nameInput = document.getElementById('confirmation-name');
    const proceed = document.getElementById('confirmation-continue');
    nameField.hidden = !expectedName;
    nameInput.value = '';
    nameInput.required = Boolean(expectedName);
    nameInput.disabled = !expectedName;
    document.getElementById('confirmation-name-hint').textContent = expectedName;
    proceed.disabled = Boolean(expectedName);
    proceed.textContent = expectedName ? 'Delete backups & remove server' : 'Continue';
    proceed.classList.toggle('danger', Boolean(expectedName));
    proceed.classList.toggle('solid', Boolean(expectedName));
    const validateName = () => { proceed.disabled = nameInput.value !== expectedName; };
    nameInput.addEventListener('input', validateName);
    confirmation.returnValue = 'cancel';
    confirmation.addEventListener('close', () => {
      nameInput.removeEventListener('input', validateName);
      resolve(confirmation.returnValue === 'confirm');
    }, {once: true});
    confirmation.showModal();
    // Cancel is the initial focus for destructive operations.
    confirmation.querySelector('[value="cancel"]').focus();
  });
}

const confirmedForms = new WeakSet();
document.querySelectorAll('form:not([method="dialog"])').forEach(form => {
  form.addEventListener('submit', async event => {
    if (confirmedForms.has(form)) { confirmedForms.delete(form); return; }
    const submitter = event.submitter;
    const message = submitter?.dataset.confirm || form.dataset.confirm;
    const removing = form.hasAttribute('data-remove-server');
    if (!message && !removing) return;
    event.preventDefault();
    if (message && !await askConfirmation(message)) return;
    if (form.dataset.confirmSecond && !await askConfirmation(form.dataset.confirmSecond)) return;
    if (removing) {
      const removeBackups = form.querySelector('[name="delete_backups"]').checked;
      if (!await askConfirmation(removeBackups ? 'Remove this server AND permanently delete ALL its NAS backups?' : 'Remove this server from the workspace? Its agent and NAS backups will remain in place.')) return;
      if (removeBackups) {
        const name = form.dataset.removeServer;
        if (!await askConfirmation('Every snapshot of ' + name + ' will be deleted forever. This cannot be undone.', name)) return;
        form.querySelector('[name="confirm_name"]').value = name;
      }
    }
    confirmedForms.add(form);
    // Preserve the clicked button's action when submitting after confirmation.
    form.requestSubmit(submitter);
  });
});
