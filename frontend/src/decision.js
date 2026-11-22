// Display helpers never infer service readiness from scores or facility size.
import { locationPrecision } from './map-data.js';
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

export function safeSource(url) {
  try { const parsed = new URL(url); return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : null; }
  catch { return null; }
}

export function linkedEvidence(item, ids) {
  if (!Array.isArray(ids)) return [];
  return (item.evidence || []).filter(e => typeof e.id === 'string' && ids.includes(e.id) && typeof e.quote === 'string' && e.quote.trim());
}

export function assessment(item, type = 'service') {
  const entry = type === 'service' ? item.serviceEvidence : item.locationEvidence;
  const labels = type === 'service'
    ? { direct: 'Explicit service evidence', related: 'Related description only', unclear: 'Conflicting evidence' }
    : { consistent: 'Location consistent', conflicting: 'Location needs review' };
  if (!entry || !labels[entry.status] || !linkedEvidence(item, entry.evidenceIds).length) {
    return { status: 'unknown', label: type === 'service' ? 'Service evidence unassessed' : 'Location unassessed', evidenceIds: [] };
  }
  return { ...entry, label: labels[entry.status] };
}

export function distanceLabel(item) {
  if (!Number.isFinite(item.distanceKm)) return 'Distance unknown';
  const unit = item.distanceType === 'straight_line' ? ' · straight-line' : item.distanceType === 'road' ? ' · road' : ' · method unspecified';
  return `${item.distanceKm.toFixed(1)} km${unit}`;
}

export function serviceSummary(item, serviceLabel = 'Requested service') {
  const evidence = assessment(item);
  const summary = {
    direct: 'Listed service', related: 'On-site care unconfirmed',
    unclear: 'Service location unclear', unknown: 'Evidence unavailable',
  }[evidence.status];
  const sources = linkedEvidence(item, evidence.evidenceIds);
  return { text: `${serviceLabel || 'Requested service'} · ${summary}`,
    references: sources.map(source => `[${item.evidence.indexOf(source) + 1}]`).join(' ') };
}

export function distanceBand(item) {
  return !Number.isFinite(item.distanceKm) ? 'unknown' : item.distanceKm <= 10 ? 'near' : 'far';
}

// Presentation only: a supplied 0–100 subscore becomes a half-star rating.
// Missing scores stay missing; counts, distances and the overall score are not substitutes.
export function starRating(value) {
  return Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value / 10) / 2 : null;
}

export function ratingMetrics(item) {
  const definitions = [
    { id: 'distance', label: 'Distance', aliases: ['distance', 'proximity'], detail: distanceLabel(item) },
    { id: 'capacity', label: 'Capacity', aliases: ['capacity', 'resources'], detail: `Reported: ${item.capacity ?? 'unknown'} beds · ${item.doctors ?? 'unknown'} doctors` },
    { id: 'service', label: 'Service match', aliases: ['service', 'service match'], detail: assessment(item).label },
    { id: 'reliability', label: 'Reliability', aliases: ['reliability', 'evidence confidence'], detail: item.reliability || 'Not assessed' },
  ];
  return definitions.map(definition => {
    const supplied = (item.metrics || []).find(metric => metric.id === definition.id)
      || (item.metrics || []).find(metric => definition.aliases.includes(String(metric.label).toLowerCase()));
    return { ...definition, label: supplied?.label || definition.label, value: supplied?.value ?? null, stars: starRating(supplied?.value),
      detail: supplied?.detail || definition.detail, explanation: supplied?.explanation || 'No scoring explanation supplied.',
      weight: supplied?.weight ?? null, evidenceIds: supplied?.evidenceIds || [] };
  });
}

export function starsMarkup(value) {
  const rating = starRating(value);
  if (rating === null) return '<span class="rating-unknown">Not scored</span>';
  return `<span class="star-rating" role="img" aria-label="${rating} out of 5 stars">${Array.from({ length: 5 }, (_, index) => `<span class="rating-star" aria-hidden="true" style="--fill:${Math.max(0, Math.min(1, rating - index)) * 100}%">★</span>`).join('')}<span class="rating-number" aria-hidden="true">${rating.toFixed(1)}</span></span>`;
}

export function comparisonRows(a, b) {
  const resource = value => Number.isFinite(value) ? String(value) : 'Unknown';
  const availability = item => `${item.availability.label} · ${item.availability.checkedAt ? `Checked ${item.availability.checkedAt}` : 'No verification timestamp'}`;
  return [
    { label: 'Distance from origin', values: [a, b].map(distanceLabel) },
    { label: 'Service evidence', values: [a, b].map(i => assessment(i).label), evidence: 'service' },
    { label: 'Location evidence', values: [a, b].map(i => assessment(i, 'location').label), evidence: 'location' },
    { label: 'Map precision', values: [a, b].map(locationPrecision) },
    { label: 'Current availability', values: [a, b].map(availability) },
    { label: 'Reported doctors', values: [a, b].map(i => resource(i.doctors)) },
    { label: 'Reported total beds', values: [a, b].map(i => resource(i.capacity)) },
    { label: 'Sort score · not clinical quality', values: [a, b].map(i => resource(i.score)) },
    { label: 'Uncertainties', values: [a, b].map(i => i.uncertainties.join(' ') || 'None supplied; this does not establish certainty.') },
  ].filter(row => row.values.some(Boolean)).map(row => ({ ...row, different: row.values[0] !== row.values[1] }));
}

export function distanceTradeoff(a, b) {
  if (![a.distanceKm, b.distanceKm].every(Number.isFinite) || !['straight_line', 'road'].includes(a.distanceType) || a.distanceType !== b.distanceType) return 'Distances cannot be compared on the same basis.';
  const delta = Math.abs(a.distanceKm - b.distanceKm);
  if (delta < .05) return 'Both options have approximately the same reported distance.';
  const nearer = a.distanceKm < b.distanceKm ? a : b;
  return `${nearer.name} is ${delta.toFixed(1)} km closer by ${a.distanceType === 'road' ? 'road' : 'straight-line distance'}.`;
}

export function highlightText(text, term) {
  // Literal highlighting only; a keyword occurrence is not verification.
  if (!term?.trim()) return escapeHtml(text);
  const value = String(text ?? '');
  const lower = value.toLowerCase();
  const needle = term.toLowerCase();
  let output = '', start = 0, index;
  while ((index = lower.indexOf(needle, start)) !== -1) {
    output += escapeHtml(value.slice(start, index)) + '<mark>' + escapeHtml(value.slice(index, index + term.length)) + '</mark>';
    start = index + term.length;
  }
  return output + escapeHtml(value.slice(start));
}

export function togglePair(ids, id) {
  if (ids.includes(id)) return ids.filter(value => value !== id);
  return ids.length < 2 ? [...ids, id] : ids;
}
