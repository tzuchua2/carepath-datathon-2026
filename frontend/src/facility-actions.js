import { iconMarkup } from './icons.js';
import { validCoordinates } from './map-data.js';
import { assessment, linkedEvidence, escapeHtml as esc } from './decision.js';

export function normalizePhone(value) {
  if (typeof value !== 'string' || !/^\+?[\d\s().-]+$/.test(value.trim())) return null;
  const raw = value.trim();
  const digits = raw.replace(/\D/g, '');
  if (raw.startsWith('+')) return /^[1-9]\d{7,14}$/.test(digits) ? `+${digits}` : null;
  if (/^0\d{10}$/.test(digits)) return `+91${digits.slice(1)}`;
  if (/^\d{10}$/.test(digits)) return `+91${digits}`;
  if (/^91\d{10}$/.test(digits)) return `+${digits}`;
  return null;
}

export function facilityActions(item, data) {
  const real = data?.demo === false && item.location?.precision !== 'demo';
  const phone = normalizePhone(item.contact?.phone);
  const phoneSources = linkedEvidence(item, item.contact?.evidenceIds);
  const phoneSupported = phone && phoneSources.some(source =>
    (source.quote.match(/\+?\d[\d\s().-]*\d/g) || []).some(candidate => normalizePhone(candidate) === phone));
  const callHref = real && phoneSupported ? `tel:${phone}` : null;
  const exactLocation = item.location?.precision === 'facility' && validCoordinates(item.location)
    && (item.mappedSitePassedReview === true || assessment(item, 'location').status === 'consistent');
  let directionsHref = null;
  if (real && exactLocation && validCoordinates(data.origin)) {
    const url = new URL('https://www.google.com/maps/dir/');
    url.search = new URLSearchParams({ api: '1', origin: `${data.origin.lat},${data.origin.lng}`,
      destination: `${item.location.lat},${item.location.lng}` });
    directionsHref = url.href;
  }
  return {
    phone: callHref ? phone : null, callHref, directionsHref,
    callReason: !real ? 'Calls are unavailable for sample facilities.'
      : !phone ? (item.reportedPhone ? 'See the listed phone in Details and confirm the branch.' : 'Phone number not provided.')
        : !phoneSupported ? 'Phone number needs source evidence.' : `Call the listed number ${phone}`,
    directionsReason: !real ? 'Directions are unavailable for sample locations.'
      : !exactLocation ? 'An exact, consistent facility location is needed.'
      : !validCoordinates(data.origin) ? 'Choose a starting address first.' : 'Open Google Maps to the reported facility point',
    hasSources: Array.isArray(item.evidence) && item.evidence.some(source => source.quote?.trim()),
  };
}

export function actionsMarkup(item, data, saved = false, compact = false) {
  const actions = facilityActions(item, data);

  const link = (kind, label, href, reason) => href
    ? `<a class="facility-action action-${kind}" href="${esc(href)}" ${kind === 'directions' ? 'target="_blank" rel="noopener noreferrer"' : ''} title="${esc(reason)}" aria-label="${esc(label)} ${esc(item.name)}">${iconMarkup(kind)}<span>${label}</span></a>`
    : `<button type="button" class="facility-action" disabled title="${esc(reason)}" aria-label="${label} unavailable: ${esc(reason)}">${iconMarkup(kind)}<span>${label}</span></button>`;
  return `<div class="facility-actions${compact ? ' compact' : ''}" role="group" aria-label="Actions for ${esc(item.name)}">
    ${link('call', 'Call', actions.callHref, actions.callReason)}
    ${link('directions', 'Directions', actions.directionsHref, actions.directionsReason)}
    <button type="button" class="facility-action action-save" data-facility-action="save" data-facility-id="${esc(item.id)}" aria-pressed="${saved}" aria-label="${saved ? 'Unsave' : 'Save'} ${esc(item.name)}" title="${saved ? 'Remove from' : 'Save for'} this case">${iconMarkup('save')}<span data-action-label>${saved ? 'Saved' : 'Save'}</span></button>
    <button type="button" class="facility-action" data-facility-action="source" data-facility-id="${esc(item.id)}" aria-label="View sources for ${esc(item.name)}" ${actions.hasSources ? '' : 'disabled title="Source text not provided."'}>${iconMarkup('source')}<span>Source</span></button>
  </div>`;
}
