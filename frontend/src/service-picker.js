import { serviceCatalog, searchServices, serviceCoverage } from './services.js';
import { escapeHtml as esc } from './decision.js';

export function initServicePicker() {
  const input = document.querySelector('#service-search');
  const select = document.querySelector('#service');
  const panel = document.querySelector('#service-options');
  const status = document.querySelector('#service-search-status');
  const coverage = document.querySelector('#service-coverage');
  let options = [], active = -1;
  select.innerHTML = '<option value=""></option>' + serviceCatalog.map(item => `<option value="${item.id}">${esc(item.label)}</option>`).join('');

  function close() { panel.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); active = -1; }
  function set(id, label) {
    select.querySelector('[data-custom]')?.remove();
    if (id === 'custom' && label) {
      const option = new Option(label, 'custom'); option.dataset.custom = 'true'; select.add(option);
    }
    select.value = id || '';
    document.querySelectorAll('[data-service]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.service === id)));
    input.value = select.selectedOptions[0]?.textContent || '';
    coverage.textContent = id ? serviceCoverage(id).note : '';
    coverage.hidden = !coverage.textContent;
    input.setCustomValidity(''); close();
  }
  function show() {
    options = searchServices(input.value);
    const query = input.value.trim();
    if (query && !options.some(item => item.label.toLowerCase() === query.toLowerCase())) options.push({ id: 'custom', label: query, category: 'Custom service' });
    panel.innerHTML = options.map((item, index) => {
      const info = serviceCoverage(item.id);
      return `<button type="button" role="option" id="service-option-${index}" aria-selected="false" tabindex="-1" data-service-index="${index}"><span>${item.id === 'custom' ? `Search for “${esc(item.label)}”` : esc(item.label)}</span><small class="coverage-${info.status}">${item.id === 'custom' ? 'Custom service' : esc(info.label)}</small></button>`;
    }).join('');
    panel.hidden = !options.length; active = -1;
    input.setAttribute('aria-expanded', String(options.length > 0)); input.removeAttribute('aria-activedescendant');
    status.textContent = `${options.length} service options.`;
  }
  function choose(index) {
    const option = options[index]; if (!option) return;
    set(option.id, option.label);
    select.dispatchEvent(new Event('input', { bubbles: true }));
    close(); input.focus(); close();
  }
  function move(next) {
    active = (next + options.length) % options.length;
    panel.querySelectorAll('[role=option]').forEach((item, index) => item.setAttribute('aria-selected', String(index === active)));
    input.setAttribute('aria-activedescendant', `service-option-${active}`);
    panel.children[active]?.scrollIntoView({ block: 'nearest' });
  }
  input.addEventListener('focus', show);
  input.addEventListener('click', show);
  input.addEventListener('input', () => {
    coverage.hidden = true;
    document.querySelectorAll('[data-service]').forEach(button => button.setAttribute('aria-pressed', 'false'));
    select.value = ''; input.setCustomValidity(input.value.trim() ? 'Choose a service from the suggestions, or select the custom service option.' : ''); show();
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); if (panel.hidden) show(); if (options.length) move(active < 0 ? (event.key === 'ArrowDown' ? 0 : options.length - 1) : active + (event.key === 'ArrowDown' ? 1 : -1));
    } else if (event.key === 'Enter' && !panel.hidden) {
      event.preventDefault(); if (active >= 0) choose(active); else if (options.length) move(0);
    } else if (event.key === 'Tab') close();
  });
  panel.addEventListener('mousedown', event => event.preventDefault());
  panel.addEventListener('click', event => { const option = event.target.closest('[data-service-index]'); if (option) choose(Number(option.dataset.serviceIndex)); });
  document.addEventListener('pointerdown', event => { if (!event.target.closest('.service-picker')) close(); });
  input.addEventListener('blur', close);
  document.querySelectorAll('[data-service]').forEach(button => {
    button.onclick = () => {
      set(button.dataset.service);
      select.dispatchEvent(new Event('input', { bubbles: true }));
    };
  });
  set('', '');
  return { set, close };
}
