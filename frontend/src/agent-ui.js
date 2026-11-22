import { emptyDraft, sendAgentMessage } from './agent.js';
import { serviceCoverage } from './services.js';

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

export function initAgent({ readForm, writeForm, search, clearResults, getResult, editForm, resetForm }) {
  let draft = emptyDraft();
  let messages = [];
  let pending = null;
  let generation = 0;
  let active = false;
  let briefReady = false;

  function scrollToEnd() {
    requestAnimationFrame(() => $('#agent-scroll').scrollTo({ top: $('#agent-scroll').scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }));
  }

  function addMessage(role, content) {
    $('#agent-welcome').hidden = true;
    const entry = document.createElement('article');
    entry.className = `chat-message ${role}`;
    entry.innerHTML = `<div class="message-avatar" aria-hidden="true">${role === 'user' ? 'You' : 'C'}</div><div class="message-body"><span class="message-author">${role === 'user' ? 'You' : 'Carepath'}</span><p>${escape(content)}</p></div>`;
    $(getResult() ? '#agent-followups' : '#conversation').append(entry);
    messages.push({ role, content });
    scrollToEnd();
  }

  function setBusy(busy) {
    $('#agent-send').disabled = busy;
    $('#agent-input').disabled = busy;
    $('#agent-thinking').hidden = !busy;
    $('#agent-form').setAttribute('aria-busy', String(busy));
    document.querySelectorAll('.suggestion, .followup-chips button, #build-shortlist, #edit-brief').forEach(el => { el.disabled = busy; });
    if ($('#build-shortlist')) $('#build-shortlist').disabled = busy || !briefReady;
  }

  function renderBrief(ready = Boolean(draft.service && draft.location)) {
    briefReady = ready;
    const brief = $('#referral-brief');
    if (!draft.service && !draft.location) { brief.hidden = true; return; }
    brief.hidden = false;
    brief.innerHTML = `<div class="brief-heading"><div><h3>${ready ? 'Referral details' : 'Complete your referral'}</h3></div></div>
      <dl class="brief-fields"><div><dt>Required service</dt><dd>${escape(draft.serviceLabel || 'Not specified')}</dd></div><div><dt>Starting location</dt><dd>${escape(draft.location || 'Not specified')}</dd></div></dl>
      <div class="brief-patient"><span>◎</span><span>${draft.patient.age === null ? 'Age not specified' : `${draft.patient.age} years old`}${draft.patient.history ? ' · History added' : ''}${draft.patient.presentation ? ' · Case notes added' : ''}</span></div>
      ${draft.service && serviceCoverage(draft.service).note ? `<p class="service-coverage">${escape(serviceCoverage(draft.service).note)}</p>` : ''}
      <div class="brief-actions"><button id="edit-brief" class="text-button">Edit in form ↗</button><button id="build-shortlist" class="primary" ${ready ? '' : 'disabled'}>Find hospitals <span>→</span></button></div>`;
    $('#edit-brief').onclick = () => { writeForm(draft); editForm(); };
    $('#build-shortlist').onclick = buildShortlist;
  }

  async function send(message) {
    const text = message.trim();
    if (!text || pending) return;
    draft = readForm();
    const version = ++generation;
    const current = new AbortController();
    pending = current;
    addMessage('user', text);
    $('#agent-input').value = '';
    setBusy(true);
    scrollToEnd();
    const timeout = setTimeout(() => current.abort(), 30000);
    try {
      const reply = await sendAgentMessage({ message: text, draft, messages: messages.slice(-20), result: getResult() }, current.signal);
      if (version !== generation) return;
      if (JSON.stringify(draft) !== JSON.stringify(reply.draft)) {
        $('#conversation').append(...$('#agent-followups').childNodes);
        clearResults();
        $('#followup-chips').hidden = true;
      }
      draft = reply.draft;
      writeForm(draft);
      addMessage('assistant', reply.reply);
      if (reply.ready || !getResult()) renderBrief(reply.ready);
    } catch (error) {
      if (version !== generation) return;
      addMessage('assistant', error.name === 'AbortError' ? 'The agent timed out. Your brief is still here. Try again or continue in the form.' : error.message);
    } finally {
      clearTimeout(timeout);
      if (version === generation) {
        pending = null;
        setBusy(false);
        if (active) $('#agent-input').focus({ preventScroll: true });
        scrollToEnd();
      }
    }
  }

  async function buildShortlist() {
    if (pending || !briefReady || !draft.service || !draft.location) return;
    const version = ++generation;
    pending = new AbortController();
    setBusy(true);
    $('#agent-thinking small').textContent = 'Finding facility options…';
    $('#followup-chips').hidden = true;
    draft = readForm();
    const outcome = await search(draft);
    if (version !== generation) return;
    pending = null;
    setBusy(false);
    $('#agent-thinking small').textContent = 'Organizing your referral…';
    if (outcome?.data) {
      addMessage('assistant', `${outcome.data.facilities.length} ${outcome.data.demo ? 'sample facilities' : 'facilities'} found. Open View details for scores and evidence.`);
      $('#followup-chips').hidden = !outcome.data.facilities.length;
    } else if (outcome?.error) addMessage('assistant', outcome.error);
    scrollToEnd();
  }

  $('#agent-form').onsubmit = event => { event.preventDefault(); send($('#agent-input').value); };
  $('#agent-input').onkeydown = event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(event.target.value); }
  };
  document.querySelectorAll('.suggestion').forEach(button => { button.onclick = () => send(button.dataset.prompt); });
  document.querySelectorAll('[data-question]').forEach(button => { button.onclick = () => send(button.dataset.question); });
  function reset() {
    generation++; pending?.abort(); pending = null; draft = emptyDraft(); messages = []; briefReady = false;
    setBusy(false); resetForm(); clearResults();
    $('#conversation').replaceChildren();
    $('#agent-followups').replaceChildren();
    $('#referral-brief').hidden = true; $('#followup-chips').hidden = true;
    $('#agent-welcome').hidden = false; $('#agent-input').value = ''; $('#referral-brief').replaceChildren();
    $('#agent-thinking small').textContent = 'Organizing your referral…';
    if (active) $('#agent-input').focus({ preventScroll: true });
  }
  return {
    reset,
    locationChanged() {
      generation++; pending?.abort(); pending = null; setBusy(false);
      draft = readForm();
      $('#followup-chips').hidden = true;
      if (!$('#referral-brief').hidden) renderBrief();
    },
    activate(isActive, fromForm = false) {
      active = isActive;
      if (!active && pending) {
        generation++; pending.abort(); pending = null; setBusy(false);
        addMessage('assistant', 'Paused while you edit in the form. Your conversation and brief are preserved.');
      }
      if (active) draft.location = readForm().location;
      if (active && fromForm && readForm().service) {
        draft = readForm();
        renderBrief();
        $('#agent-welcome').hidden = true;
        addMessage('assistant', 'Your referral details are here from the form. Review the brief or tell me what you’d like to change.');
      }
    },
  };
}
