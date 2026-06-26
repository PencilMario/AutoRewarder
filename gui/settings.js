// =========================================================================
// Accounts management modal — list rendering + per-account actions.
// The generic toast / modal / avatar helpers live in script.js.
// =========================================================================

const ACCOUNT_ICONS = {
  proxy:  '<svg viewBox="0 0 24 24"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 12h10"/><path d="M10 9l-3 3 3 3"/><path d="M14 9l3 3-3 3"/></svg>',
  rename: '<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>',
  setup:  '<svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15A9 9 0 1 1 18 5.3L23 10"/></svg>',
  trash:  '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>',
};

function proxy_url_for_input(proxy) {
  if (!proxy || !proxy.enabled) return '';
  const scheme = proxy.scheme === 'https' ? 'https' : 'http';
  const host = String(proxy.host || '').trim();
  const port = proxy.port ? String(proxy.port) : '';
  if (!host || !port) return '';

  const username = String(proxy.username || '');
  const password = String(proxy.password || '');
  let auth = '';
  if (username || password) {
    auth = encodeURIComponent(username);
    if (password) auth += ':' + encodeURIComponent(password);
    auth += '@';
  }
  return `${scheme}://${auth}${host}:${port}`;
}

async function configure_account_proxy(acc) {
  let current = default_proxy_config();
  try {
    const saved = await pywebview.api.get_account_proxy(acc.id);
    current = Object.assign(current, saved || {});
  } catch (_) {
    show_toast('Could not load proxy settings.', 'error');
    return;
  }

  const proxyText = await prompt_modal(
    'Proxy settings',
    `Set proxy for "${acc.label}". Leave blank to disable it.`,
    proxy_url_for_input(current),
    { placeholder: 'http://user:pass@proxy.example.com:8080', confirmLabel: 'Save' }
  );
  if (proxyText === null) return;

  const proxyConfig = parse_proxy_url(proxyText);
  if (!proxyConfig) {
    show_toast('Proxy must be HTTP/HTTPS with a valid host and port.', 'warning');
    return;
  }

  pywebview.api.set_account_proxy(acc.id, proxyConfig).then(ok => {
    if (!ok) {
      show_toast('Proxy settings failed to save.', 'error');
      return;
    }
    show_toast(format_proxy_summary(proxyConfig), 'success');
    refresh_account_ui();
  });
}

function render_accounts_section(accounts) {
  const list = document.getElementById('accounts_list');
  if (!list) return;

  list.innerHTML = '';

  if (!accounts || accounts.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'accounts-empty';
    empty.textContent = 'No accounts yet. Click “Add account” to create your first one.';
    list.appendChild(empty);
    return;
  }

  for (const acc of accounts) {
    const item = document.createElement('li');
    item.className = 'account-item' + (acc.is_current ? ' current' : '');

    item.appendChild(make_avatar(acc));

    const info = document.createElement('div');
    info.className = 'account-item-info';
    const name = document.createElement('div');
    name.className = 'account-item-name';
    name.textContent = acc.label;
    const meta = document.createElement('div');
    meta.className = 'account-item-meta';
    meta.textContent =
      (acc.is_current ? 'Current · ' : '') +
      (acc.first_setup_done ? 'Ready' : 'Setup pending');
    info.appendChild(name);
    info.appendChild(meta);
    item.appendChild(info);

    const actions = document.createElement('div');
    actions.className = 'account-actions';

    const proxyBtn = document.createElement('button');
    proxyBtn.className = 'icon-btn';
    proxyBtn.title = 'Proxy settings';
    proxyBtn.setAttribute('aria-label', 'Proxy settings');
    proxyBtn.innerHTML = ACCOUNT_ICONS.proxy;
    proxyBtn.addEventListener('click', () => {
      configure_account_proxy(acc);
    });

    const renameBtn = document.createElement('button');
    renameBtn.className = 'icon-btn';
    renameBtn.title = 'Rename';
    renameBtn.setAttribute('aria-label', 'Rename');
    renameBtn.innerHTML = ACCOUNT_ICONS.rename;
    renameBtn.addEventListener('click', async () => {
      const newLabel = await prompt_modal(
        'Rename account',
        `Enter a new name for "${acc.label}".`,
        acc.label,
        { confirmLabel: 'Rename' }
      );
      if (newLabel === null) return;
      const trimmed = String(newLabel).trim();
      if (!trimmed) return;
      pywebview.api.rename_account(acc.id, trimmed).then(ok => {
        if (!ok) show_toast('Rename failed.', 'error');
        else show_toast(`Renamed to "${trimmed}".`, 'success');
      });
    });

    const resetupBtn = document.createElement('button');
    resetupBtn.className = 'icon-btn';
    resetupBtn.title = acc.first_setup_done ? 'Re-run setup' : 'Run setup';
    resetupBtn.setAttribute('aria-label', resetupBtn.title);
    resetupBtn.innerHTML = ACCOUNT_ICONS.setup;
    resetupBtn.addEventListener('click', () => {
      show_toast(`Opening browser to set up "${acc.label}"…`, 'info', { duration: 6000 });
      pywebview.api.rerun_setup(acc.id).then(ok => {
        if (!ok) show_toast('Setup could not be started.', 'error');
      });
    });

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'icon-btn danger';
    deleteBtn.title = 'Delete';
    deleteBtn.setAttribute('aria-label', 'Delete');
    deleteBtn.innerHTML = ACCOUNT_ICONS.trash;
    deleteBtn.addEventListener('click', async () => {
      const confirmed = await confirm_modal(
        `Delete "${acc.label}"?`,
        'This removes its browser profile, history, and daily-set status. This cannot be undone.',
        { confirmLabel: 'Delete', danger: true }
      );
      if (!confirmed) return;
      pywebview.api.delete_account(acc.id).then(success => {
        if (!success) show_toast('Delete failed.', 'error');
        else show_toast(`"${acc.label}" deleted.`, 'success');
      });
    });

    actions.appendChild(proxyBtn);
    actions.appendChild(resetupBtn);
    actions.appendChild(renameBtn);
    actions.appendChild(deleteBtn);
    item.appendChild(actions);

    list.appendChild(item);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const addBtn = document.getElementById('addAccountBtn');
  if (addBtn) addBtn.addEventListener('click', prompt_and_create_account);
});
