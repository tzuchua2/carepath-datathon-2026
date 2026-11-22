import { isRankingMode } from './ranking.js';
import { leadsFrom, SCORE_LABEL, SCORE_NOTE } from './explain.js';

async function post(path, body, signal) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message || 'Carepath could not complete the search.');
    // An ambiguous place is offered as choices to pick from; it is never resolved silently.
    // The API suggests near matches as plain names when nothing matched; those are offered as choices too.
    if (['ORIGIN_AMBIGUOUS', 'ORIGIN_NOT_FOUND'].includes(data.status)) error.options = originChoices(data.options);
    throw error;
  }
  return data;
}

// The engine compares two recommended facilities; nothing is computed in the browser.
export async function compareFacilities(service, originQuery, ids, signal) {
  return post('/api/v1/compare', { service: { code: service }, origin: { query: originQuery }, facility_ids: ids }, signal);
}

// A referral note written by the server from the same search; used for copying.
export async function referralNote(service, originQuery, facilityId, signal) {
  return post('/api/v1/referral-note', { service: { code: service }, origin: { query: originQuery }, facility_id: facilityId }, signal);
}

export function originChoices(options) {
  return (options || []).map(option => {
    if (typeof option === 'string') option = { query: option.replace(/\b[a-z]/g, char => char.toUpperCase()) };
    const label = option.label || (typeof option.query === 'string' ? option.query : option.display_name);
    return label && option.query != null ? { name: label, address: [option.district, option.state, option.pincode].filter(Boolean).join(', ') || 'Choose this place',
      query: option.query, precision: option.kind || 'area', unresolved: true } : null;
  }).filter(Boolean).slice(0, 25);
}

function point(origin) {
  return { lat: origin.latitude, lng: origin.longitude, precision: origin.precision,
    name: origin.display_name, address: origin.display_name, uncertaintyKm: origin.uncertainty_km ?? null };
}

export async function findOrigin(query, signal) {
  const data = await post('/api/v1/locations/resolve', typeof query === 'string' ? { query } : { origin: query }, signal);
  return { ...point(data.origin), query: typeof query === 'string' ? query : `${data.origin.latitude},${data.origin.longitude}` };
}

const metricLabels = {
  service_evidence: ['service', 'What the record says'],
  distance: ['distance', 'Distance'],
  location: ['reliability', 'Location consistency'],
};

function facility(row, policy) {
  const evidence = row.evidence_preview.map(item => ({
    id: item.evidence_id, field: item.source_field, quote: item.exact_quote,
    sourceLabel: 'Challenge facility record', sourceUrl: null, observedAt: null,
  }));
  const direct = row.evidence_preview.filter(item => item.classification === 'direct').map(item => item.evidence_id);
  const review = row.public_source_review;
  const publicReviewed = review?.blue_check === true && review.finding === 'recent_independent';
  const loc = row.location;
  return {
    id: row.facility_id, name: row.display_name, address: loc.address_label,
    location: { lat: loc.latitude, lng: loc.longitude, precision: 'facility' },
    distanceKm: row.distance.value_km, distanceType: row.distance.type,
    score: row.ranking.score, scoreLabel: SCORE_LABEL,
    scoreExplanation: `${SCORE_NOTE} Changing the order of the list does not change this score.`,
    evidencePoints: row.ranking.components.find(part => part.key === 'service_evidence')?.weighted_points ?? null,
    evidenceChecks: row.ranking.evidence_checks || [], checksPassed: row.service?.evidence_checks_passed ?? null,
    checksTotal: row.service?.evidence_checks_total ?? null, mapCheck: row.map_check || null,
    rankRange: row.ranking.rank_range || null,
    locationText: row.ranking.why_selected.find(reason => /mapped point|PIN|map/i.test(reason.text) && !/straight line/i.test(reason.text))?.text || null,
    reliability: 'Location consistency', capacity: row.context.beds_reported ?? null,
    doctors: row.context.doctors_reported ?? null,
    reportedPhone: row.contact?.phone_status === 'listed_unconfirmed' ? row.contact.phone : null,
    mappedSitePassedReview: loc.location_status === 'CONSISTENT' &&
      row.eligibility?.passed.includes('usable_location') === true,
    availability: { status: 'unknown', label: 'Availability unverified', checkedAt: null },
    claims: row.ranking.why_selected.map(item => ({ text: item.text, evidenceIds: item.evidence_ids })),
    reasons: row.ranking.why_selected.map(item => item.text),
    uncertainties: row.uncertainty.map(item => item.text),
    modelCheck: row.model_check, modelFindings: row.model_findings || [], publicReview: review,
    evidence, recordSourceUrls: row.source_urls,
    serviceEvidence: { status: 'direct', evidenceIds: direct },
    locationEvidence: { status: loc.location_status === 'CONSISTENT' ? 'consistent' : 'conflicting', evidenceIds: [] },
    reviewedService: publicReviewed ? { date: review.reviewed_on, detail: review.finding_detail, sources: review.sources } : null,
    metrics: row.ranking.components.map(item => {
      const [id, label] = metricLabels[item.key];
      return { id, label, value: Math.round(item.value * 1000) / 10, weight: Math.round(item.weight * 100),
        evidenceIds: id === 'service' ? direct : [], explanation: policy.score.find(part => part.key === item.key)?.text || '' };
    }),
  };
}

export async function searchCareBricks(path, request, signal) {
  if (!isRankingMode(request.rankingMode)) throw new Error('Choose a valid result sorting mode.');
  const query = request.origin?.query || request.location;
  const data = await post(path, {
    service: { code: request.service }, origin: { query },
    // One request serves every view: the pool is reordered in the browser from views.options[].order.
    constraints: { result_limit: 5, result_pool: 50 },
    preferences: { ranking_mode: 'evidence_first' }, include_unverified_leads: true,
  }, signal);
  return {
    demo: false, origin: point(data.query.origin), status: data.status, message: data.message,
    warnings: data.global_warnings, indexVersion: data.index_version, methodology: data.how_ranked.gates.join(' '),
    facilities: data.results.map(row => facility(row, data.how_ranked)),
    views: data.views || null, leads: leadsFrom(data), summary: data.summary || {},
    howRanked: data.how_ranked, areaContext: data.area_context || null, stability: data.ranking_stability || null,
  };
}
