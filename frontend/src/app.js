import { config, searchFacilities } from './api.js';
import { initAgent } from './agent-ui.js';
import { initHospitalDetails } from './hospital-details.js';
import { initLocationPicker } from './location-picker.js';
import { TEST_ORIGIN } from './locations.js';
import { initServicePicker } from './service-picker.js';
import { serviceLabels } from './services.js';
import { serviceSummary, distanceLabel } from './decision.js';
import { initMap, renderMap, selectMarker, fitMap } from './map.js';
import { actionsMarkup } from './facility-actions.js';
import { defaultRankingMode, isRankingMode, orderedFacilities, sortNotice } from './ranking.js';
import { initWorkspaceLayout } from './layout.js';
import { reviewLabel, modelTag, leadsMarkup, checklist, checklistText, checklistMarkup, verificationMarkup, evidenceLevel, areaContextMarkup, plannerMarkup, stabilityMarkup, rankRangeText, compareMarkup } from './explain.js';
import { compareFacilities, referralNote } from './carebricks.js';

const $ = selector => document.querySelector(selector);
initWorkspaceLayout();
const servicePicker = initServicePicker();
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
let response = null;
let selectedId = null;
let controller;
let displayed = [];
let inputMode = 'manual';
let formChanged = false;
let rankingMode = defaultRankingMode;
let lastRequest = null;
const savedFacilities = new Set();
const dialog = $('#detail-dialog');
const distance = distanceLabel;
const statusClass = item => item.availability.status === 'unavailable' ? 'unavailable' : 'pending';

function selectFacility(id, scroll = false) {
  selectedId = id;
  const item = displayed.find(facility => facility.id === id);
  if (!item) return;
  document.querySelectorAll('.facility-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.id === id);
    if (scroll && card.dataset.id === id) card.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
  });
  selectMarker(id);
}

function renderResults() {
  displayed = orderedFacilities(response, response.query.rankingMode);
  setSortMode(response.query.rankingMode);
  $('#result-sort').hidden = !displayed.length;
  $('#search-status').className = 'status-note';
  const view = response.views?.options?.find(option => option.code === rankingMode);
  $('#search-status').textContent = [sortNotice(response, rankingMode), view?.description, ...(response.warnings || [])].filter(Boolean).join(' ');
  const inRange = response.summary?.documented_in_range;
  $('#result-title').textContent = response.demo ? `${displayed.length} sample facilities`
    : Number.isFinite(inRange) && inRange > displayed.length ? `${displayed.length} of ${inRange} documented options` : `${displayed.length} documented options`;
  $('#open-checklist').hidden = response.demo || !displayed.length;
  $('#results').innerHTML = displayed.map((item, index) => {
    const summary = serviceSummary(item, response.query?.serviceLabel);
    return `<article class="facility-card" data-id="${escape(item.id)}">
    <button class="facility-select" type="button" aria-label="View details for ${escape(item.name)}" aria-haspopup="dialog" aria-controls="evidence-dialog"><span class="rank">${index + 1}</span><span class="facility-title"><strong>${escape(item.name)}</strong></span><span class="score">${item.score ?? '—'}<small>${escape(item.scoreLabel || 'Sort score')}</small></span></button>
    <p class="card-distance">${escape(distance(item))}</p>
    <button data-evidence-kind="service" class="service-context" aria-label="View service evidence for ${escape(item.name)}">${escape(summary.text)}${summary.references ? `<sup>${escape(summary.references)}</sup>` : ''}</button>
    ${response.demo ? '' : explainTags(item)}
    <div class="card-footer"><span class="availability ${statusClass(item)}">${escape(item.availability.label)}</span><div class="card-footer-actions"><button class="text-button evidence-button">View details</button></div></div>
    ${actionsMarkup(item, response, savedFacilities.has(item.id), true)}
  </article>`; }).join('');
  for (const card of document.querySelectorAll('.facility-card')) {
    card.querySelector('.facility-select').onclick = () => openDetails(card.dataset.id);
    card.querySelector('.evidence-button').onclick = () => openDetails(card.dataset.id);
    card.querySelectorAll('[data-evidence-kind]').forEach(button => { button.onclick = () => hospitalDetails.openLens(card.dataset.id, button.dataset.evidenceKind); });
  }
  $('.map-panel').classList.remove('is-idle');
  renderMap({ ...response, facilities: displayed }, id => openDetails(id, true));
  hospitalDetails.update(response);
  if (displayed.length) selectFacility(displayed.some(item => item.id === selectedId) ? selectedId : displayed[0].id);
  else $('#results').innerHTML = `<div class="empty-results"><h3>${response.demo ? 'No facilities in this search' : 'No record documents this clearly enough to recommend'}</h3><p>${escape(response.message || 'Try a different service or starting location.')}</p>${response.demo ? '' : `<p>Carepath does not fill the gap with weaker matches.${response.leads?.length ? ' The records below mention the service without stating it: treat them as phone calls to make.' : ' Try a wider area or the nearest large city.'}</p>`}</div>`;
  if (!response.demo) $('#results').insertAdjacentHTML('beforeend',
    stabilityMarkup(displayed.length ? response.stability : null) + leadsMarkup(response.leads || [], response.query?.serviceLabel) +
    areaContextMarkup(response.areaContext, response.query?.serviceLabel));
  $('#open-compare').hidden = response.demo || displayed.length < 2;
}

// Public-source label and model check for a card. Both come from the API; nothing is inferred here.
function explainTags(item) {
  const review = reviewLabel(item.publicReview, response.query?.serviceLabel), model = modelTag(item.modelCheck);
  const level = evidenceLevel(item), range = rankRangeText(item.rankRange);
  return `<p class="explain-tags">${level ? `<span class="explain-tag tag-level" title="Open View details to see each check">${escape(level)}</span>` : ''}<span class="explain-tag tag-${review.kind}" title="${escape(review.meaning)}">${escape(review.text)}</span>${review.basis ? `<span class="explain-basis">${escape(review.basis)}</span>` : ''}${model ? `<span class="explain-tag tag-${model.kind}">${escape(model.text)}</span>` : ''}${range ? `<span class="explain-tag tag-none" title="Its position under the other weightings we tried">${escape(range)}</span>` : ''}</p>`;
}

function openChecklist() {
  if (!response || !displayed.length) return;
  const sheet = checklist(response, displayed);
  $('#dialog-content').innerHTML = checklistMarkup(sheet);
  $('#dialog-content [data-copy-checklist]').onclick = async () => {
    let done = false, text = checklistText(sheet);
    try {  // prefer the server's referral notes, one per option; fall back to the local checklist text
      const notes = await Promise.all(displayed.map(item => referralNote(response.query.service, lastRequest.origin?.query || response.query.location, item.id)));
      text = notes.map(note => note.text).join('\n\n' + '-'.repeat(60) + '\n\n');
    } catch { /* keep the local text */ }
    try { await navigator.clipboard.writeText(text); done = true; } catch { done = false; }
    $('#checklist-status').textContent = done ? 'Copied.' : 'Copying is blocked here. Use Print instead.';
  };
  $('#dialog-content [data-print-checklist]').onclick = () => {
    document.body.classList.add('printing-checklist');
    window.print();
    setTimeout(() => document.body.classList.remove('printing-checklist'), 500);
  };
  dialog.showModal();
}

function openDetails(id, scroll = false) {
  selectFacility(id, scroll);
  hospitalDetails.openLens(id);
}

function readForm() {
  const values = new FormData($('#search-form'));
  return {
    service: values.get('service'), serviceLabel: $('#service').selectedOptions[0]?.textContent || '',
    location: values.get('location').trim(), rankingMode,
    patient: { reference: values.get('reference'), age: values.get('age') === '' ? null : Number(values.get('age')), presentation: values.get('presentation'), history: values.get('history'), medicationsAndAllergies: values.get('medications') },
  };
}

function writeForm(draft) {
  servicePicker.set(draft.service, draft.serviceLabel);
  locationPicker.set(draft.location);
  setSortMode(draft.rankingMode || defaultRankingMode);
  const fields = { reference: 'reference', age: 'age', presentation: 'presentation', history: 'history', medications: 'medicationsAndAllergies' };
  for (const [id, key] of Object.entries(fields)) $(`#${id}`).value = draft.patient[key] ?? '';
}

function clearResults() {
  hospitalDetails.reset();
  controller?.abort();
  controller = null;
  response = null;
  lastRequest = null;
  setSortMode(defaultRankingMode);
  $('#result-sort').hidden = true;
  $('#shortlist').removeAttribute('aria-busy');
  displayed = [];
  selectedId = null;
  $('#results').replaceChildren();
  $('#search-status').textContent = '';
  $('#result-title').textContent = 'Hospitals';
  $('#shortlist').hidden = true;
  $('.map-panel').classList.add('is-idle');
  $('#search-button').disabled = false;
  $('#search-button').innerHTML = 'Find facilities';
  previewOrigin(locationPicker.getOrigin());
}

async function submitSearch(event) {
  event?.preventDefault();
  if (!$('#search-form').reportValidity()) return;
  formChanged = true;
  return runSearch(readForm());
}

async function runSearch(request) {
  hospitalDetails.reset();
  controller?.abort();
  const current = new AbortController();
  controller = current;
  $('#shortlist').hidden = false;
  $('#result-sort').hidden = true;
  $('#shortlist').setAttribute('aria-busy', 'true');
  lastRequest = null;
  $('#search-button').disabled = true;
  $('#search-button').textContent = 'Finding facilities…';
  $('#search-status').className = 'status-note';
  $('#search-status').textContent = 'Searching for facility options…';
  $('#results').innerHTML = '';
  $('.map-panel').classList.add('is-idle');
  response = null;
  displayed = [];
  previewOrigin(locationPicker.getOrigin());
  $('#result-title').textContent = 'Searching…';
  const timeout = setTimeout(() => current.abort(), 30000);
  try {
    locationPicker.set(request.location);
    const origin = await locationPicker.locate(request.location, current.signal);
    if (controller !== current) return;
    if (!origin) {
      $('#search-status').textContent = ''; $('#shortlist').hidden = true;
      return { error: 'Choose a matching starting address above, then search again.' };
    }
    const data = await searchFacilities({ ...request, origin }, current.signal);
    if (controller !== current) return;
    lastRequest = { ...request, origin };
    response = { ...data, query: { service: request.service, serviceLabel: request.serviceLabel, location: request.location, rankingMode: request.rankingMode } };
    selectedId = null;
    $('#search-status').textContent = '';
    $('.patient-section').open = false;
    renderResults();
    if (inputMode === 'manual') $('.result-header').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    return { data: response };
  } catch (error) {
    if (controller !== current) return;
    $('#search-status').className = 'status-note error';
    $('#search-status').textContent = error.name === 'AbortError' ? 'The search timed out. Please try again.' : error.message;
    $('#result-title').textContent = 'Search unavailable';
    return { error: $('#search-status').textContent };
  } finally {
    clearTimeout(timeout);
    if (controller === current) {
      $('#shortlist').removeAttribute('aria-busy');
      $('#search-button').disabled = false;
      $('#search-button').innerHTML = 'Find facilities';
    }
  }
}

function setSortMode(mode) {
  rankingMode = isRankingMode(mode) ? mode : defaultRankingMode;
  for (const input of document.querySelectorAll('[name="resultSort"]')) input.checked = input.value === rankingMode;
}

function changeSort(mode) {
  if (!response || !lastRequest || !isRankingMode(mode)) return;
  // Reorder the current result set. Scores, evidence and metric weights stay fixed.
  hospitalDetails.reset();
  response = { ...response, query: { ...response.query, rankingMode: mode } };
  lastRequest = { ...lastRequest, rankingMode: mode };
  renderResults();
}

$('#search-form').onsubmit = submitSearch;
$('#result-sort').onchange = event => changeSort(event.target.value);
$('#fit-map').onclick = fitMap;
$('#open-checklist').onclick = openChecklist;

// Planner view: districts by distance to the nearest documented provider. It describes the dataset only.
const planner = { service: 'c_section', state: '', order: 'blind_spots' };
async function openPlanner() {
  const services = [['dialysis', 'Dialysis'], ['mri', 'MRI'], ['nicu', 'Neonatal intensive care'], ['c_section', 'C-section']];
  if (response?.query?.service && services.some(([code]) => code === response.query.service)) planner.service = response.query.service;
  $('#dialog-content').innerHTML = `<span class="eyebrow">FOR PLANNERS</span><h2 id="dialog-title">Where documented care is far away</h2>
    <div class="planner-controls"><label>Service <select id="planner-service">${services.map(([code, label]) => `<option value="${code}">${label}</option>`).join('')}</select></label>
      <label>State <input id="planner-state" placeholder="All states" maxlength="60"></label>
      <label>Show <select id="planner-order"><option value="blind_spots">Likely gaps in the dataset</option><option value="farthest">Farthest districts</option><option value="nearest">Nearest districts</option></select></label></div>
    <div id="planner-body" aria-live="polite"><p class="status-note">Loading…</p></div>`;
  $('#planner-service').value = planner.service; $('#planner-state').value = planner.state; $('#planner-order').value = planner.order;
  const load = async () => {
    Object.assign(planner, { service: $('#planner-service').value, state: $('#planner-state').value.trim(), order: $('#planner-order').value });
    const query = new URLSearchParams({ service: planner.service, order: planner.order, limit: '25' });
    if (planner.state) query.set('state', planner.state);
    try {
      const reply = await fetch(`/api/v1/access-gaps?${query}`), data = await reply.json();
      if (!$('#planner-body')) return;
      $('#planner-body').innerHTML = reply.ok ? plannerMarkup(data, planner.order) : `<p class="status-note error">${escape(data.message || 'The district summary is not available.')}</p>`;
    } catch { if ($('#planner-body')) $('#planner-body').innerHTML = '<p class="status-note error">The district summary could not be loaded.</p>'; }
  };
  for (const id of ['planner-service', 'planner-order', 'planner-state']) $(`#${id}`).onchange = load;
  dialog.showModal();
  await load();
}
$('#open-planner').onclick = openPlanner;

// Compare two recommended facilities. The numbers come from POST /api/v1/compare.
async function openCompare() {
  if (!response || displayed.length < 2) return;
  const options = displayed.map(item => `<option value="${escape(item.id)}">${escape(item.name)}</option>`).join('');
  $('#dialog-content').innerHTML = `<span class="eyebrow">COMPARE</span><h2 id="dialog-title">Why one ranks above the other</h2>
    <div class="planner-controls"><label>First <select id="compare-first">${options}</select></label><label>Second <select id="compare-second">${options}</select></label></div>
    <div id="compare-body" aria-live="polite"><p class="status-note">Loading…</p></div>`;
  $('#compare-first').value = displayed[0].id; $('#compare-second').value = displayed[1].id;
  const load = async () => {
    const ids = [$('#compare-first').value, $('#compare-second').value];
    if (ids[0] === ids[1]) { $('#compare-body').innerHTML = '<p class="status-note">Choose two different facilities.</p>'; return; }
    try {
      const result = await compareFacilities(response.query.service, lastRequest.origin?.query || response.query.location, ids);
      if ($('#compare-body')) $('#compare-body').innerHTML = compareMarkup(result);
    } catch (error) { if ($('#compare-body')) $('#compare-body').innerHTML = `<p class="status-note error">${escape(error.message)}</p>`; }
  };
  $('#compare-first').onchange = load; $('#compare-second').onchange = load;
  dialog.showModal();
  await load();
}
$('#open-compare').onclick = openCompare;
$('#clear-case').onclick = () => {
  for (const name of ['reference', 'age', 'presentation', 'history', 'medications']) $(`#${name}`).value = '';
  formChanged = true; clearResults();
};
$('.dialog-close').onclick = () => dialog.close();
dialog.onclick = event => { if (event.target === dialog) dialog.close(); };
$('#help').onclick = () => {
  $('#dialog-content').innerHTML = '<span class="eyebrow">CAREPATH</span><h2 id="dialog-title">From referral need to shortlist.</h2><ol class="help-steps"><li>Choose the service identified by the referring clinician and the patient’s starting location.</li><li>Patient context is optional and does not change the results.</li><li>Review what each record says and the distance, then open hospital details for the evidence and the checks.</li><li>Contact the receiving facility to confirm the required service and current capacity.</li></ol><p class="status-note">Facilities, scores and source snippets are sample data while the recommendation backend is being connected. The starting address is a real mapped location. Patient details stay in page memory and clear on reload. Ask Carepath currently uses a local rule-based parser.</p>';
  if (config.mode === 'api') {
    const verification = document.createElement('section');
    $('#dialog-content').append(verification);
    fetch('/api/v1/verification').then(r => r.ok ? r.json() : null).then(info => {
      if (!info || !verification.isConnected) return;
      verification.innerHTML = verificationMarkup(info);
    }).catch(() => { verification.textContent = 'Verification summary unavailable.'; });
    $('#dialog-content li:nth-child(2)').textContent = 'Use a city, district, PIN or coordinates as your search origin.';
    $('#dialog-content .status-note').textContent = 'Carepath searches the cleaned facility release. Every option quotes the record text it relies on. The sort score orders the list and is not a chance of treatment. Confirm the service and current capacity by phone before referral.';
  } else {
    $('#dialog-content').insertAdjacentHTML('beforeend', '<p>Address searches use <a href="https://github.com/komoot/photon" target="_blank" rel="noopener noreferrer">Photon</a> and <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>.</p>');
  }
  dialog.showModal();
};
$('#search-form').addEventListener('input', () => {
  formChanged = true;
  clearResults();
});
if (config.mode === 'api') {
  $('.patient-section .field-hint').textContent = 'Case notes stay in this browser and on the Carepath server. They are not used for ranking and are never sent to an outside service.';
  $('#location').placeholder = 'City, district, PIN or coordinates';
}
window.addEventListener('load', initMap);

const hospitalDetails = initHospitalDetails({ isSaved: id => savedFacilities.has(id) });

document.addEventListener('click', event => {
  const button = event.target.closest('[data-facility-action]');
  if (!button || button.disabled) return;
  const item = response?.facilities.find(facility => facility.id === button.dataset.facilityId);
  if (!item) return;
  if (button.dataset.facilityAction === 'source') { hospitalDetails.openSources(item.id); return; }
  if (button.dataset.facilityAction !== 'save') return;
  if (savedFacilities.has(item.id)) savedFacilities.delete(item.id);
  else savedFacilities.add(item.id);
  const saved = savedFacilities.has(item.id);
  for (const control of document.querySelectorAll('[data-facility-action="save"]')) {
    if (control.dataset.facilityId !== item.id) continue;
    control.setAttribute('aria-pressed', String(saved));
    control.setAttribute('aria-label', `${saved ? 'Unsave' : 'Save'} ${item.name}`);
    control.title = `${saved ? 'Remove from' : 'Save for'} this case`;
    control.querySelector('[data-action-label]').textContent = saved ? 'Saved' : 'Save';
  }
  $('#facility-action-status').textContent = `${item.name} ${saved ? 'saved for this case' : 'removed from saved facilities'}.`;
});

function previewOrigin(origin) {
  renderMap({ origin, facilities: [] }, () => {});
}
let agent;
const locationPicker = initLocationPicker({
  useBackend: config.mode === 'api',
  onEdit: () => { formChanged = true; clearResults(); agent?.locationChanged(); },
  onPreview: previewOrigin,
});
previewOrigin(config.mode === 'api' ? null : TEST_ORIGIN);

agent = initAgent({
  readForm, writeForm, search: runSearch, clearResults, getResult: () => response ? { ...response, focusedFacilityId: selectedId } : null,
  editForm: () => { setMode('manual'); $('.patient-section').open = true; $('#service-search').focus(); },
  resetForm: () => { savedFacilities.clear(); $('#facility-action-status').textContent = ''; $('#search-form').reset(); servicePicker.set('', ''); locationPicker.reset(); setSortMode(defaultRankingMode); $('.patient-section').open = false; formChanged = false; },
});
$('#manual-new-case').onclick = () => { agent.reset(); $(inputMode === 'agent' ? '#agent-input' : '#service-search').focus(); };

function setMode(mode) {
  if (inputMode === mode) return;
  if (controller && !response) clearResults();
  inputMode = mode;
  servicePicker.close(); locationPicker.cancel();
  const isAgent = mode === 'agent';
  $('.workspace').classList.toggle('agent-mode', isAgent);
  $('#manual-panel').hidden = isAgent;
  $('#agent-panel').hidden = !isAgent;
  for (const name of ['manual', 'agent']) {
    $(`#${name}-tab`).setAttribute('aria-selected', String(name === mode));
    $(`#${name}-tab`).tabIndex = name === mode ? 0 : -1;
  }
  $(isAgent ? '#agent-results-slot' : '#manual-results-slot').append($('#shortlist'));
  $('#shortlist').hidden = !response;
  agent.activate(isAgent, isAgent && formChanged);
  if (isAgent) formChanged = false;
}
$('#manual-results-slot').append($('#shortlist'));
for (const mode of ['manual', 'agent']) {
  const tab = $(`#${mode}-tab`);
  tab.onclick = () => setMode(mode);
  tab.onkeydown = event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const target = event.key === 'Home' ? 'manual' : event.key === 'End' ? 'agent' : mode === 'manual' ? 'agent' : 'manual';
      setMode(target); $(`#${target}-tab`).focus();
    }
  };
}

// A link can reopen a search: #s=dialysis&o=Patna%2C%20Bihar, optionally &v=distance_first and
// &show=details | checklist | compare | leads | help | planner. Only the service code, place and view are read from the link.
async function openSharedSearch() {
  if (config.mode !== 'api') return;
  const params = new URLSearchParams(location.hash.slice(1));
  const service = params.get('s'), place = params.get('o');
  if (!service || !place || !serviceLabels[service]) return;
  servicePicker.set(service, serviceLabels[service]);
  $('#service-search').value = serviceLabels[service];
  locationPicker.set(place.slice(0, 300));
  setSortMode(params.get('v'));
  const outcome = await runSearch(readForm());
  const show = params.get('show');
  if (show === 'help') $('#help').click();
  if (show === 'leads') document.querySelector('.lead-list')?.scrollIntoView({ block: 'start' });
  if (show === 'planner') openPlanner();
  if (!outcome?.data || !displayed.length) return;
  if (show === 'details') openDetails(displayed[0].id);
  if (show === 'checklist') openChecklist();
  if (show === 'compare') openCompare();
}
window.addEventListener('load', openSharedSearch);
