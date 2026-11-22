// Sorting changes presentation, never the overall score or its weights.
export const rankingModes = Object.freeze({
  recommended: 'Recommended',
  distance_first: 'Nearest first',
  service_first: 'Service evidence first',
  emergency_first: 'Emergency',
  coverage_first: 'Coverage',
});

export const defaultRankingMode = 'recommended';
export const isRankingMode = value => Object.hasOwn(rankingModes, value);

const metricForMode = { emergency_first: 'emergency', coverage_first: 'coverage' };
function supportedValue(facility, mode) {
  const metric = facility.metrics?.find(item => item.id === metricForMode[mode]);
  const hasEvidence = metric?.evidenceIds?.some(id => facility.evidence?.some(source => source.id === id && source.quote?.trim()));
  return hasEvidence && Number.isFinite(metric.value) ? metric.value : null;
}

// Keep backend Recommended order as the stable tie-breaker. Unknown values last.
export function orderedFacilities(data, mode) {
  // The API sends each view's order and how many to show; nothing is rescored here.
  const option = data.views?.options?.find(item => item.code === mode);
  if (option) {
    const byId = new Map(data.facilities.map(item => [item.id, item]));
    return option.order.map(id => byId.get(id)).filter(Boolean).slice(0, data.views.show || undefined);
  }
  if (data.views && !metricForMode[mode]) return data.facilities.slice(0, data.views.show || undefined);
  const facilities = data.views ? data.facilities.slice(0, data.views.show || undefined) : [...data.facilities];
  if (mode === 'service_first') facilities.sort((a, b) => (b.evidencePoints ?? -Infinity) - (a.evidencePoints ?? -Infinity));
  if (mode === 'distance_first') facilities.sort((a, b) =>
    (a.distanceKm ?? Infinity) - (b.distanceKm ?? Infinity));
  if (metricForMode[mode]) facilities.sort((a, b) =>
    (supportedValue(b, mode) ?? -Infinity) - (supportedValue(a, mode) ?? -Infinity));
  return facilities;
}

export function sortNotice(data, mode) {
  if (!metricForMode[mode] || !data.facilities.length) return '';
  const known = data.facilities.filter(facility => supportedValue(facility, mode) !== null).length;
  if (!known) return `${rankingModes[mode]} ranking data is unavailable. Recommended order retained.`;
  return known < data.facilities.length ? `${rankingModes[mode]} scores with linked evidence first; unknowns follow.` : '';
}
