(() => {
  const root = document.documentElement;
  const theme = document.querySelector('#theme');
  const applyTheme = value => {
    if (value === 'light' || value === 'dark') root.dataset.theme = value;
    else delete root.dataset.theme;
  };
  let savedTheme = 'system';
  try { savedTheme = localStorage.getItem('telesubmit-theme') || 'system'; } catch {}
  applyTheme(savedTheme);
  if (theme) {
    theme.value = ['system', 'light', 'dark'].includes(savedTheme) ? savedTheme : 'system';
    theme.addEventListener('change', () => {
      applyTheme(theme.value);
      try { localStorage.setItem('telesubmit-theme', theme.value); } catch {}
    });
  }

  const path = location.pathname.replace(/\/$/, '');
  const links = [...document.querySelectorAll('[data-nav]')];
  const active = links.filter(link => path === link.pathname || path.startsWith(link.pathname + '/'))
    .sort((a, b) => b.pathname.length - a.pathname.length)[0];
  if (active) active.setAttribute('aria-current', 'page');

  const menu = document.querySelector('.menu-toggle');
  const shade = document.querySelector('.menu-shade');
  const sidebar = document.querySelector('#sidebar');
  const main = document.querySelector('main');
  function setMenu(open) {
    document.body.classList.toggle('menu-open', open);
    menu.setAttribute('aria-expanded', String(open));
    shade.hidden = !open;
    main.inert = open;
    if (open) (sidebar.querySelector('[aria-current]') || sidebar.querySelector('a')).focus();
    else menu.focus();
  }
  if (menu) {
    menu.addEventListener('click', () => setMenu(menu.getAttribute('aria-expanded') !== 'true'));
    shade.addEventListener('click', () => setMenu(false));
    document.addEventListener('keydown', event => {
      if (!document.body.classList.contains('menu-open')) return;
      if (event.key === 'Escape') setMenu(false);
      if (event.key === 'Tab') {
        const items = [...sidebar.querySelectorAll('a, select, button')];
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    matchMedia('(min-width: 801px)').addEventListener('change', event => {
      if (event.matches && document.body.classList.contains('menu-open')) setMenu(false);
    });
  }

  // Keep native selects as the submitted values and as the no-JavaScript fallback.
  document.querySelectorAll('main select').forEach((select, number) => {
    if (!select.id) select.id = 'setting-' + number;
    const previous = select.previousElementSibling;
    if (previous?.tagName === 'LABEL' && !previous.htmlFor) previous.htmlFor = select.id;
    const options = [...select.options];
    const values = options.map(option => option.value);
    if (options.length !== 2 || select.disabled ||
        !(values.includes('true') && values.includes('false') || values.includes('1') && values.includes('0'))) return;
    const onValue = values.includes('true') ? 'true' : '1';
    const offValue = values.includes('false') ? 'false' : '0';
    const wrapper = document.createElement('div');
    wrapper.className = 'toggle-field';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle';
    toggle.setAttribute('role', 'switch');
    const label = select.labels[0];
    toggle.setAttribute('aria-label', label?.textContent.trim() || select.name);
    if (select.hasAttribute('aria-describedby')) toggle.setAttribute('aria-describedby', select.getAttribute('aria-describedby'));
    const state = document.createElement('span');
    state.className = 'toggle-label';
    state.setAttribute('aria-hidden', 'true');
    function sync() {
      const on = select.value === onValue;
      toggle.setAttribute('aria-checked', String(on));
      state.textContent = on ? '已开启' : '已关闭';
    }
    toggle.addEventListener('click', () => {
      select.value = select.value === onValue ? offValue : onValue;
      select.dispatchEvent(new Event('change', {bubbles: true}));
    });
    select.addEventListener('change', sync);
    select.form?.addEventListener('reset', () => setTimeout(sync, 0));
    sync();
    wrapper.append(toggle, state);
    select.after(wrapper);
    select.hidden = true;
  });

  document.querySelectorAll('main input, main textarea').forEach((input, number) => {
    if (!input.id) input.id = 'field-' + number;
    const previous = input.previousElementSibling;
    if (previous?.tagName === 'LABEL' && !previous.htmlFor) previous.htmlFor = input.id;
  });
  document.querySelectorAll('table').forEach(table => {
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll';
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', '表格，可横向滚动');
    table.before(wrapper);
    wrapper.append(table);
  });
  document.querySelectorAll('main form').forEach(form => {
    const buttons = [...form.querySelectorAll('button[type=submit]')];
    const primary = buttons.find(button => !button.classList.contains('danger'));
    if (primary) primary.classList.add('primary');
    if (form.closest('.login-page')) return;
    const fields = [...form.querySelectorAll('input:not([type=hidden]), select, textarea')];
    if (!fields.length) return;
    const initial = new Map(fields.map(field => [field, field.type === 'checkbox' ? field.checked : field.value]));
    const status = document.createElement('span');
    status.className = 'save-status';
    status.setAttribute('role', 'status');
    const directButtons = buttons.filter(button => button.parentElement === form);
    if (directButtons.length) {
      const actions = document.createElement('div');
      actions.className = 'form-actions' + (fields.length >= 6 && !form.closest('td') ? ' sticky' : '');
      for (const button of directButtons) actions.append(button);
      actions.append(status);
      if (actions.classList.contains('sticky')) form.prepend(actions);
      else form.append(actions);
    } else {
      status.hidden = true;
      form.append(status);
    }
    function checkDirty() {
      const dirty = fields.some(field => (field.type === 'checkbox' ? field.checked : field.value) !== initial.get(field));
      status.textContent = dirty ? '有未保存的更改' : '';
      status.classList.toggle('dirty', dirty);
    }
    form.addEventListener('input', checkDirty);
    form.addEventListener('change', checkDirty);
    form.addEventListener('submit', event => {
      if (event.defaultPrevented) return;
      if (form.dataset.sending) { event.preventDefault(); return; }
      form.dataset.sending = 'true';
      status.textContent = '正在保存…';
      status.classList.remove('dirty');
      // Keep the submitter enabled so its name/value and formaction still submit.
      form.setAttribute('aria-busy', 'true');
    });
  });
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form[data-sending]').forEach(form => {
      delete form.dataset.sending;
      form.removeAttribute('aria-busy');
      const status = form.querySelector('.save-status');
      if (status) status.textContent = '';
    });
  });
})();
