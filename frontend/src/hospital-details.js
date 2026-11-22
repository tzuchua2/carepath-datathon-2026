import { locationPrecision } from './map-data.js';
import { actionsMarkup, facilityActions } from './facility-actions.js';
import { escapeHtml as esc, safeSource, linkedEvidence, assessment, highlightText, ratingMetrics, starsMarkup } from './decision.js';
import { fetchEvidenceSummary, summarySources, summaryMarkup, canReuseSummary } from './evidence-summary.js';
import { reviewMarkup, modelCheckMarkup, evidenceChecksMarkup, mapCheckMarkup, SCORE_LABEL, SCORE_NOTE } from './explain.js';

const $ = selector => document.querySelector(selector);

export function initHospitalDetails({ isSaved = () => false } = {}) {
  let data = null, lensId = null, lensView = 'summary', focusIds = [], focusRequested = false;
  let reading = null, readingController = null, readingVersion = 0;
  const lensDialog = $('#evidence-dialog');
  const find = id => data?.facilities.find(item => item.id === id);

  function cancelReading() {
    readingController?.abort(); readingController = null; readingVersion++;
    if (reading?.status === 'loading') reading = null;
  }

  async function loadReading(item) {
    if (data.demo) return;
    const key = `${item.id}:${data.query.service}:${data.indexVersion}`;
    if (canReuseSummary(reading, key)) return;
    cancelReading();
    const version = readingVersion;
    readingController = new AbortController();
    reading = { key, status: 'loading' };
    try {
      const result = await fetchEvidenceSummary(item.id, data.query.service, readingController.signal);
      if (version !== readingVersion) return;
      reading = { key, status: 'ready', result };
    } catch (error) {
      if (version !== readingVersion || error.name === 'AbortError') return;
      reading = { key, status: 'error' };
    }
    readingController = null;
    if (lensDialog.open && lensId === item.id && lensView === 'source') {
      // Update only the reading area; preserve scroll, keyboard focus and expanded originals.
      const panel = $('#evidence-reading-area');
      if (panel) panel.innerHTML = summaryMarkup(reading, data.query?.serviceLabel);
      const originals = $('#original-source-list');
      if (reading.status === 'error') $('#original-passages').open = true;
      if (originals && reading.status === 'ready') {
        const enriched = { ...item, evidence: summarySources(reading.result) };
        originals.innerHTML = `${reading.result.source_published_at ? `<p class="source-attribution">Record source date: ${esc(reading.result.source_published_at)}</p>` : ''}${sourceCards(enriched, !focusRequested)}`;
        $('#original-count').textContent = String(enriched.evidence.length);
        $('#lens-evidence-count').textContent = String(enriched.evidence.length);
        $('#record-source-links').innerHTML = sourceLinks(reading.result.record_source_urls, 'Record sources');
        if (reading.result.mode !== 'summary') $('#original-passages').open = true;
      }
    }
  }

  function sourceCards(item, all = false) {
    const sources = all ? item.evidence : linkedEvidence(item, focusIds);
    if (!sources.length) return '<div class="source-missing">No source text is linked to this claim. It remains unverified.</div>';
    return sources.map((source, index) => {
      const url = safeSource(source.sourceUrl);
      return `<article tabindex="-1" data-source-id="${esc(source.id)}" class="source-card ${focusIds.includes(source.id) ? 'source-focused' : ''}"><div class="source-heading"><span class="source-number">${item.evidence.indexOf(source) + 1}</span><strong>${esc(source.field || source.sourceLabel || `Source ${index + 1}`)}</strong></div><blockquote>${highlightText(source.quote, data.query?.serviceLabel)}</blockquote>${(source.interpretations || []).map(f => `<p class="published-reading"><strong>Published review</strong> ${esc(f.model_explanation || f.effective_classification)}</p>`).join('')}${url ? `<div class="source-link-row"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open original source ↗</a></div>` : ''}</article>`;
    }).join('');
  }

  function sourceLinks(sources, label) {
    const urls = [...new Set((sources || []).map(safeSource).filter(Boolean))];
    return urls.length ? `<details class="record-websites"><summary>${esc(label)} <span>${urls.length}</span></summary><p class="source-attribution">Links belong to the full record; individual passages are not linked to a specific page.</p><ul>${urls.map(url => {
      const parsed = new URL(url), path = parsed.pathname === '/' ? '' : parsed.pathname;
      return `<li><a href="${esc(url)}" title="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(parsed.hostname.replace(/^www\./, ''))}<span>${esc(path.length > 65 ? path.slice(0, 62) + '…' : path)}</span> ↗</a></li>`;
    }).join('')}</ul></details>` : '<p class="source-attribution">No original website URL is available for these passages.</p>';
  }

  // Points each part adds to the sort score: its value times its weight. Shown as numbers, not stars.
  function pointsMarkup(metric) {
    if (metric.value === null || metric.weight == null) return '<span class="rating-unknown">Not scored</span>';
    return `<span class="metric-points">${esc(Math.round(metric.value * metric.weight / 10) / 10)} of ${esc(metric.weight)}</span>`;
  }

  function scoreMethod(how) {
    if (!how) return '';
    return `<p><strong>Checks every recommended record passed</strong></p><ol>${(how.gates || []).map(gate => `<li>${esc(gate)}</li>`).join('')}</ol>
      <p><strong>Deliberately not used</strong></p><ul>${(how.not_used || []).map(text => `<li>${esc(text)}</li>`).join('')}</ul><p>${esc(how.limits || '')}</p>`;
  }

  function renderLens() {
    let item = find(lensId);
    if (!item) return;
    const readingKey = `${item.id}:${data.query?.service}:${data.indexVersion}`;
    if (!canReuseSummary(reading, readingKey)) { cancelReading(); reading = null; }
    if (lensView !== 'source') cancelReading();
    if (reading?.status === 'ready') item = { ...item, evidence: summarySources(reading.result), recordSourceUrls: reading.result.record_source_urls };
    const claims = Array.isArray(item.claims) ? item.claims : [];
    // For real results: only the three parts that make up the sort score, each tied to what explains it.
    const metrics = data.demo ? ratingMetrics(item) : ratingMetrics(item).filter(metric => metric.id !== 'capacity').map(metric =>
      metric.id === 'service' ? { ...metric, label: 'Service evidence', detail: Number.isFinite(item.checksPassed) ? `${item.checksPassed} of ${item.checksTotal} checks passed` : metric.detail }
        : metric.id === 'reliability' ? { ...metric, label: 'Location consistency', detail: item.locationText || metric.detail } : metric);
    const reportedSize = data.demo ? '' : `<p class="source-attribution">Reported size: ${esc(item.capacity ?? 'unknown')} beds · ${esc(item.doctors ?? 'unknown')} doctors. Not used in ranking, and not a count of free beds or staff on duty.</p>`;
    const serviceSource = linkedEvidence(item, item.serviceEvidence?.evidenceIds)[0];
    const actions = facilityActions(item, data);
    $('#evidence-content').innerHTML = `<header class="lens-header"><button class="sheet-close" data-close-lens aria-label="Close hospital details">×</button><h2 id="lens-title">${esc(item.name)}</h2>${item.address ? `<p>${esc(item.address)}</p>` : ''}</header>
      ${actionsMarkup(item, data, isSaved(item.id))}
      <div class="lens-tabs" role="tablist" aria-label="Hospital details"><button role="tab" id="lens-summary-tab" aria-controls="lens-body" aria-selected="${lensView === 'summary'}" tabindex="${lensView === 'summary' ? 0 : -1}" data-lens-view="summary">Overview</button><button role="tab" id="lens-source-tab" aria-controls="lens-body" aria-selected="${lensView === 'source'}" tabindex="${lensView === 'source' ? 0 : -1}" data-lens-view="source">Evidence <span id="lens-evidence-count">${item.evidence.length}</span></button></div>
      <div class="lens-body" id="lens-body" role="tabpanel" aria-labelledby="${lensView === 'summary' ? 'lens-summary-tab' : 'lens-source-tab'}">${lensView === 'summary' ? `
        ${locationPrecision(item) ? `<p class="location-precision">${esc(locationPrecision(item))}</p>` : ''}
        ${!actions.callHref || !actions.directionsHref ? `<p class="action-availability">${esc([!actions.callHref && actions.callReason, !actions.directionsHref && actions.directionsReason].filter(Boolean).join(' '))}</p>` : ''}
        ${actions.phone ? `<p class="contact-line">Listed phone: ${esc(actions.phone)} <button class="text-button" data-contact-source>Source ${linkedEvidence(item, item.contact.evidenceIds).map(source => `[${item.evidence.indexOf(source) + 1}]`).join(' ')}</button></p>` : ''}
        ${item.reportedPhone ? `<p class="contact-line">Supplier-listed phone ${esc(item.reportedPhone)} · Branch ownership unconfirmed</p>` : ''}
        <section class="overall-rating" aria-label="${data.demo ? 'Overall match score' : 'Sort score'}"><div><h3>${data.demo ? 'Overall match' : SCORE_LABEL}</h3><p>${esc(data.demo ? (data.query?.serviceLabel || 'Requested service') : 'Orders this list. Not a chance of treatment.')}</p></div><div class="overall-number"><strong>${esc(item.score ?? '—')}</strong><span>/100</span></div></section>
        <div class="rating-breakdown" aria-label="Score breakdown">${metrics.map(metric => `<details class="rating-row"><summary><span class="rating-description"><strong>${esc(metric.label)}</strong><span>${esc(metric.detail)}</span></span>${data.demo ? starsMarkup(metric.value) : pointsMarkup(metric)}<span class="rating-chevron" aria-hidden="true">⌄</span></summary><div class="rating-basis"><p>${esc(metric.explanation)}</p>${metric.id === 'capacity' ? '<p>Reported size only. Available beds and on-duty staff are unknown.</p>' : ''}<p>${metric.value === null ? 'No subscore supplied.' : `${esc(metric.value)}/100`}${metric.weight === null ? '' : ` · ${esc(metric.weight)}% base weight`}</p><button class="text-button" data-metric-source="${metric.id}">View supporting evidence</button></div></details>`).join('')}</div>
        ${reportedSize}<section class="current-capacity"><div><h3>Current availability</h3><strong>${esc(item.availability.label)}</strong></div><p>${item.availability.checkedAt ? `Checked ${esc(item.availability.checkedAt)}` : 'Open slots not verified'}</p></section>
        ${data.demo ? '' : evidenceChecksMarkup(item)}
        ${data.demo ? '' : reviewMarkup(item.publicReview, data.query?.serviceLabel)}
        ${data.demo ? '' : modelCheckMarkup(item.modelCheck, item.modelFindings)}
        ${data.demo ? '' : mapCheckMarkup(item.mapCheck)}
        ${serviceSource ? `<section class="hospital-description"><h3>Service details</h3><p>${esc(serviceSource.quote)}</p><button class="text-button" data-assessment="service">View source</button></section>` : ''}
        <details class="score-method"><summary>How the score is calculated</summary><p>${esc(data.methodology || 'No scoring method supplied.')}</p>${item.scoreExplanation ? `<p>${esc(item.scoreExplanation)}</p>` : ''}<p>${data.demo ? 'Stars show each subscore out of 5, rounded to the nearest half star. Overall match uses the underlying subscores, not the rounded stars. This is a referral match score, not a patient review rating.' : esc(SCORE_NOTE)}</p>${data.demo ? '' : scoreMethod(data.howRanked)}</details>
        <details class="verification-details"><summary>Evidence and model reading notes</summary><div class="claim-list">${claims.map((claim, index) => `<button data-claim-index="${index}"><span>${esc(claim.text)}</span><small>${linkedEvidence(item, claim.evidenceIds).length ? linkedEvidence(item, claim.evidenceIds).map(e => `[${item.evidence.indexOf(e) + 1}]`).join(' ') : 'No source'} ↗</small></button>`).join('')}</div><ul>${item.uncertainties.map(text => `<li>${esc(text)}</li>`).join('')}</ul></details>
      ` : `${focusRequested ? '<div class="source-view-heading"><h3>Supporting evidence</h3><button class="text-button" data-all-sources>Read service summary</button></div>' : ''}
        ${!focusRequested && !data.demo ? `<div id="evidence-reading-area" aria-live="polite">${summaryMarkup(reading, data.query?.serviceLabel)}</div>` : ''}
        <details class="original-passages" id="original-passages" ${focusRequested || data.demo || reading?.status === 'error' || reading?.result?.mode === 'originals' ? 'open' : ''}><summary>Original passages <span id="original-count">${item.evidence.length}</span></summary><div id="original-source-list">${reading?.result?.source_published_at ? `<p class="source-attribution">Record source date: ${esc(reading.result.source_published_at)}</p>` : ''}${sourceCards(item, !focusRequested)}</div></details>
        <div id="record-source-links">${sourceLinks(item.recordSourceUrls, 'Record sources')}</div>`}</div>
      <footer class="lens-footer"><button class="outline-button" data-close-lens>Done</button></footer>`;
    if (lensView === 'source') loadReading(item);
  }

  function openLens(id, kind) {
    if (!find(id)) return;
    lensId = id; focusIds = kind ? assessment(find(id), kind).evidenceIds : [];
    focusRequested = Boolean(kind); lensView = kind ? 'source' : 'summary';
    renderLens();
    if (!lensDialog.open) lensDialog.showModal();
  }

  lensDialog.onclick = event => {
    if (event.target.closest('[data-close-lens]')) lensDialog.close();
    const tab = event.target.closest('[data-lens-view]');
    const claim = event.target.closest('[data-claim-index]');
    const item = find(lensId);
    const status = event.target.closest('[data-assessment]');
    const metricSource = event.target.closest('[data-metric-source]');
    if (tab) { lensView = tab.dataset.lensView; focusIds = []; focusRequested = false; renderLens(); $(`[data-lens-view="${lensView}"]`).focus(); }
    if (claim) { focusRequested = true; focusIds = item.claims[Number(claim.dataset.claimIndex)].evidenceIds || []; lensView = 'source'; renderLens(); $('#lens-source-tab').focus(); }
    if (metricSource) { focusRequested = true; focusIds = ratingMetrics(item).find(metric => metric.id === metricSource.dataset.metricSource)?.evidenceIds || []; lensView = 'source'; renderLens(); $('#lens-source-tab').focus(); }
    if (status) { focusRequested = true; focusIds = assessment(item, status.dataset.assessment).evidenceIds; lensView = 'source'; renderLens(); $('#lens-source-tab').focus(); }
    if (event.target.closest('[data-all-sources]')) { focusIds = []; focusRequested = false; renderLens(); $('#lens-source-tab').focus(); }
    if (event.target.closest('[data-contact-source]')) { focusIds = item.contact.evidenceIds; focusRequested = true; lensView = 'source'; renderLens(); $('#lens-source-tab').focus(); }
    const reference = event.target.closest('[data-evidence-reference]');
    if (reference) {
      $('#original-passages').open = true;
      const target = [...lensDialog.querySelectorAll('[data-source-id]')].find(node => node.dataset.sourceId === reference.dataset.evidenceReference);
      target?.scrollIntoView({ block: 'nearest', behavior: 'auto' }); target?.focus({ preventScroll: true });
    }
    if (event.target.closest('[data-retry-summary]')) { cancelReading(); reading = null; renderLens(); }
  };
  lensDialog.addEventListener('close', cancelReading);
  lensDialog.onkeydown = event => {
    if (event.target.matches('[data-lens-view]') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); lensView = event.key === 'Home' ? 'summary' : event.key === 'End' ? 'source' : lensView === 'summary' ? 'source' : 'summary';
      focusIds = []; focusRequested = false; renderLens(); $(`[data-lens-view="${lensView}"]`).focus();
    }
  };
  return {
    update(next) { if (next !== data) { cancelReading(); reading = null; } data = next; },
    reset() { cancelReading(); reading = null; data = null; lensId = null; lensDialog.close(); },
    openLens,
    openSources(id) {
      if (!find(id)) return;
      lensId = id; focusIds = []; focusRequested = false; lensView = 'source';
      renderLens();
      if (!lensDialog.open) lensDialog.showModal();
      $('#lens-source-tab').focus();
    },
  };
}
