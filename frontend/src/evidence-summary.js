import { escapeHtml as esc, safeSource } from './decision.js';

const RETRYABLE = new Set(['provider_error', 'connection_error', 'invalid_summary', 'busy']);

export function canReuseSummary(state, key) {
  return state?.key === key && (state.status === 'loading' ||
    (state.status === 'ready' && !RETRYABLE.has(state.result?.reason)));
}

export async function fetchEvidenceSummary(facilityId, serviceCode, signal) {
  const response = await fetch('/api/v1/evidence/summary', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    // Patient details, search address, scores, and client-provided quotes never enter this request.
    body: JSON.stringify({ facility_id: facilityId, service_code: serviceCode }),
  });
  if (!response.ok) throw new Error('Evidence summary unavailable. Original passages are below.');
  return validateSummary(await response.json(), facilityId, serviceCode);
}

export function validateSummary(data, facilityId, serviceCode) {
  if (!data || data.facility_id !== facilityId || data.service_code !== serviceCode ||
      !['summary', 'originals'].includes(data.mode) || !Array.isArray(data.sources) ||
      !Array.isArray(data.findings) || !Array.isArray(data.notes)) throw new Error('Incomplete evidence summary.');
  const sources = new Map();
  for (const source of data.sources) {
    if (typeof source.evidence_id !== 'string' || sources.has(source.evidence_id) ||
        source.record_id !== facilityId || typeof source.source_field !== 'string' ||
        typeof source.exact_quote !== 'string') throw new Error('Invalid source attribution.');
    sources.set(source.evidence_id, source);
  }
  for (const finding of data.findings) {
    if (typeof finding.text !== 'string' || !Array.isArray(finding.references) || !finding.references.length ||
        finding.references.some(ref => typeof ref.quote !== 'string' || !ref.quote.trim() ||
          !sources.get(ref.evidence_id)?.exact_quote.includes(ref.quote))) throw new Error('Untraceable summary.');
  }
  for (const note of data.notes) {
    if (typeof note.text !== 'string' || !Array.isArray(note.evidence_ids) ||
        note.evidence_ids.some(id => !sources.has(id))) throw new Error('Untraceable evidence note.');
  }
  return data;
}

export function summarySources(result) {
  return result.sources.map(source => ({ id: source.evidence_id, field: source.source_field,
    quote: source.exact_quote, sourceLabel: 'Facility record', sourceUrl: null,
    observedAt: result.source_published_at, interpretations: source.interpretations || [] }));
}

export function summaryMarkup(state, serviceLabel) {
  if (!state || state.status === 'loading') return `<div class="evidence-reading loading" role="status"><span class="reading-indicator" aria-hidden="true"></span>Reading the service evidence…</div>`;
  if (state.status === 'error') return '<div class="evidence-reading summary-fallback" role="status">Could not load the AI summary. Read the original passages below.<button class="text-button" data-retry-summary>Retry summary</button></div>';
  const result = state.result;
  const reference = (id, quote) => {
    const number = result.sources.findIndex(source => source.evidence_id === id) + 1;
    return `<button class="evidence-ref" data-evidence-reference="${esc(id)}" aria-label="Read source ${number}" title="${esc(quote || `Read original passage ${number}`)}">${number}</button>`;
  };
  const originalReason = ({
    review_required: 'This evidence needs review. Read the passages and the model’s readings below.',
    no_evidence: 'No service-specific evidence is available for this record.',
    busy: 'The AI summary service is busy. Original passages are available below.',
    connection_error: 'The AI summary request could not complete. Original passages are available below.',
    invalid_summary: 'The AI summary did not pass the evidence checks. Original passages are available below.',
    provider_error: 'The AI summary service is temporarily unavailable. Original passages are available below.',
    not_configured: 'AI summaries are not configured for this app. Original passages are available below.',
  })[result.reason] || 'AI summary unavailable. Original passages are available below.';
  const retry = RETRYABLE.has(result.reason);
  const review = result.public_source_review;
  const reviewTitle = ({ recent_independent: 'Independent source review', historical_independent: 'Historical source review',
    provider: 'Provider source review', unresolved: 'Source review: unresolved' })[review?.finding];
  const reviewLinks = (review?.sources || []).map(source => safeSource(source.url)).filter(Boolean);
  return `<section class="evidence-reading" aria-label="Service evidence summary">
    <div class="reading-heading"><h3>${esc(serviceLabel || 'Service')} evidence</h3>${result.mode === 'summary' ? '<span>AI reading aid</span>' : ''}</div>${result.mode === 'summary' ? '<p class="source-attribution">A summary written by an AI model from the passages below. The quotes are checked against the record; the wording in between is not.</p>' : ''}
    ${result.mode === 'summary' ? `<ul class="reading-findings">${result.findings.map(finding => `<li><p>${esc(finding.text)} <span class="evidence-refs">${[...finding.references].sort((a, b) => result.sources.findIndex(s => s.evidence_id === a.evidence_id) - result.sources.findIndex(s => s.evidence_id === b.evidence_id)).map(ref => reference(ref.evidence_id, ref.quote)).join(' ')}</span></p></li>`).join('')}</ul>`
      : `<p class="summary-fallback">${esc(originalReason)}${retry ? ' <button class="text-button" data-retry-summary>Retry summary</button>' : ''}</p>`}
    <div class="reading-checks"><h3>Before referring</h3><ul>${result.notes.map(note => `<li>${esc(note.text)} ${note.evidence_ids.length ? `<span class="evidence-refs">${note.evidence_ids.slice(0, 3).map(id => reference(id)).join(' ')}</span>` : ''}</li>`).join('')}</ul></div>
    ${reviewTitle ? `<details class="public-source-review"><summary>${esc(reviewTitle)}</summary><p>${esc(review.finding_detail || '')}</p><p class="source-attribution">Reviewed ${esc(review.reviewed_on || 'date unknown')} · Current availability unknown</p>${reviewLinks.map(url => `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(new URL(url).hostname.replace(/^www\./, ''))} ↗</a>`).join(' ')}</details>` : ''}
  </section>`;
}
