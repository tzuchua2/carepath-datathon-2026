import { validCoordinates } from './map-data.js';
import { knownLocation } from './locations.js';
import { isRankingMode } from './ranking.js';
import { referralPayload } from './referral.js';
import { searchCareBricks } from './carebricks.js';

export const config = { mode: new URLSearchParams(globalThis.location?.search).get('demo') === '1' ? 'demo' : 'api',
  endpoint: '/api/v1/referrals/search' };

export function distanceKm(a, b) {
  const radians = value => value * Math.PI / 180;
  const dLat = radians(b.lat - a.lat);
  const dLng = radians(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(radians(a.lat)) * Math.cos(radians(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
}

export function resolveDemoLocation(query) {
  const point = knownLocation(query);
  if (point) return point;
  throw new Error('Locate and select a starting address before searching.');
}

export function demoRecommendations(request) {
  const origin = request.origin === undefined ? resolveDemoLocation(request.location) : request.origin;
  if (!validCoordinates(origin)) throw new Error('The starting address has invalid coordinates.');
  const names = ['Northside Medical Centre', 'Riverbend Specialty Hospital', 'Community Care Hospital'];
  const locations = [{ lat: origin.lat - .048, lng: origin.lng + .061 }, { lat: origin.lat + .026, lng: origin.lng + .032 }, { lat: origin.lat + .13, lng: origin.lng - .08 }];
  const service = request.serviceLabel;
  const facilities = names.map((name, index) => ({
    id: `demo-${index + 1}`, name, address: '',
    location: { ...locations[index], precision: 'demo' }, distanceKm: distanceKm(origin, locations[index]), distanceType: 'straight_line',
    score: null, scoreLabel: 'Overall match', reliability: ['Strong evidence', 'Some evidence', 'Limited evidence'][index],
    availability: { status: 'unknown', label: 'Availability unverified', checkedAt: null },
    doctors: [24, null, 12][index], capacity: [120, 80, null][index],
    reasons: [[`Example record explicitly lists ${service.toLowerCase()}.`, `The example lists related enquiries, without confirming on-site ${service.toLowerCase()}.`, 'The example service and location records conflict.'][index], ['Facility identity and address agree in the example.', 'On-site delivery needs confirmation.', 'The service may belong to a different campus.'][index]],
    metrics: [
      { id: 'distance', label: 'Distance', value: [100, 100, 60][index], weight: 25, evidenceIds: ['address'], explanation: 'Demo rule: up to 10 km = 100/100; 10–25 km = 60/100. Uses straight-line distance from the search origin, not driving time.' },
      { id: 'capacity', label: 'Capacity', value: [100, null, null][index], weight: 20, evidenceIds: ['resources'], explanation: index === 0 ? 'The demo assigns 100/100 to this resource profile. This is an illustrative size rating, not a clinical capacity model or a count of available beds.' : 'Not scored because the reported resource profile is incomplete. A missing value is not zero.' },
      { id: 'service', label: 'Service match', value: [80, 50, 20][index], weight: 35, evidenceIds: index === 2 ? ['service', 'address'] : ['service'], explanation: ['Demo rule: an explicit on-site service statement = 80/100. Current delivery has not been confirmed.', 'Demo rule: related enquiries without confirmed on-site delivery = 50/100.', 'Demo rule: service attribution across conflicting campuses = 20/100.'][index] },
      { id: 'reliability', label: 'Reliability', value: [80, 60, 30][index], weight: 20, evidenceIds: ['service', 'address'], explanation: ['Demo rule: a consistent facility and explicit service statement = 80/100.', 'Demo rule: a consistent facility with indirect service evidence = 60/100.', 'Demo rule: conflicting facility and service attribution = 30/100.'][index] },
    ],
    serviceEvidence: { status: ['direct', 'related', 'unclear'][index], evidenceIds: index === 2 ? ['service', 'address'] : ['service'] },
    locationEvidence: { status: index === 2 ? 'conflicting' : 'consistent', evidenceIds: ['address'] },
    claims: [
      { id: 'service-claim', text: [`The example explicitly mentions ${service.toLowerCase()}.`, `Related enquiries are listed; on-site ${service.toLowerCase()} is unconfirmed.`, 'Service attribution is unclear across the two campuses.'][index], evidenceIds: index === 2 ? ['service', 'address'] : ['service'] },
      { id: 'address-claim', text: index === 2 ? 'The address note describes two different campuses.' : 'The example identifies a single facility location.', evidenceIds: ['address'] },
    ],
    evidence: [
      { id: 'service', field: 'capability', quote: [
        `This fictional centre provides ${service.toLowerCase()} services at its main hospital.`,
        `This fictional hospital handles ${service.toLowerCase()} enquiries and refers patients to partner providers. On-site delivery is not confirmed.`,
        `The combined example record lists ${service.toLowerCase()} services for the Community Care network without specifying which campus delivers them.`,
      ][index], sourceLabel: 'Synthetic service description', sourceUrl: null, observedAt: null },
      { id: 'address', field: 'description', quote: index === 2
        ? `The fictional directory combines one campus with a separate campus in another district. The service location is ambiguous.`
        : `The fictional listing identifies ${name} at a single hospital site.`, sourceLabel: 'Synthetic directory description', sourceUrl: null, observedAt: null },
      { id: 'resources', field: 'capacity / doctors', quote: [`This fictional centre reports 120 total beds and 24 doctors. Current bed occupancy, on-duty staff and ${service.toLowerCase()} slots are not provided.`, 'This fictional hospital reports 80 total beds. Doctor count and current service slots are not provided.', 'This fictional hospital reports 12 doctors. Bed count and current service slots are not provided.'][index], sourceLabel: 'Demo resource record', sourceUrl: null, observedAt: null },
    ],
    uncertainties: ['Current staffing and service availability need confirmation.', 'All figures and evidence in this card are synthetic demo data.'],
  })).map(facility => {
    const scored = facility.metrics.filter(metric => metric.value !== null);
    const totalWeight = scored.reduce((sum, metric) => sum + metric.weight, 0);
    facility.score = Math.round(scored.reduce((sum, metric) => sum + metric.value * metric.weight, 0) / totalWeight);
    facility.scoreExplanation = `${totalWeight}% of metric weight has data. Missing metrics are excluded and remaining weights are normalized.`;
    return facility;
  });
  return { demo: true, origin, methodology: 'Scores and order are illustrative demo results. Overall match = weighted average of distance (25%), capacity (20%), service match (35%) and reliability (20%), excluding missing metrics. Subscores use the demo rules shown under each metric. Patient context does not change demo scores. Distance sorting uses straight-line distance; emergency and coverage ranking data are not provided in this sample. Dynamic weighting requires the recommendation backend. These rules are not clinically validated; the backend will supply production scoring and verification.', facilities };
}

export function validateResponse(data) {
  if (!data || !validCoordinates(data.origin) || !Array.isArray(data.facilities)) throw new Error('The recommendation response is incomplete.');
  const ids = new Set();
  for (const item of data.facilities) {
    if (!item.id || ids.has(item.id) || typeof item.name !== 'string' || (item.location !== null && !validCoordinates(item.location)) || !Array.isArray(item.evidence) || !Array.isArray(item.reasons) || !Array.isArray(item.uncertainties) || !Array.isArray(item.metrics) || !item.availability) throw new Error('A facility response is incomplete or contains duplicate IDs.');
    ids.add(item.id);
    const evidenceIds = new Set();
    for (const source of item.evidence) {
      if (!source || typeof source.quote !== 'string') throw new Error('Source evidence must contain text.');
      if (source.id !== undefined) {
        if (typeof source.id !== 'string' || !source.id || evidenceIds.has(source.id)) throw new Error('Source IDs must be unique within a facility.');
        evidenceIds.add(source.id);
      }
    }
    const referencesValid = refs => Array.isArray(refs) && refs.every(id => typeof id === 'string');
    if (item.contact != null && (typeof item.contact !== 'object' ||
      !(item.contact.phone == null || typeof item.contact.phone === 'string') ||
      !referencesValid(item.contact.evidenceIds))) throw new Error('Contact evidence references are invalid.');
    if (item.claims !== undefined && (!Array.isArray(item.claims) || item.claims.some(claim => !claim || typeof claim.text !== 'string' || !referencesValid(claim.evidenceIds)))) throw new Error('Claim evidence references are invalid.');
    for (const entry of [item.serviceEvidence, item.locationEvidence]) {
      if (entry !== undefined && (!entry || typeof entry.status !== 'string' || !referencesValid(entry.evidenceIds))) throw new Error('Evidence assessments are invalid.');
    }
    if (item.score !== null && (!Number.isFinite(item.score) || item.score < 0 || item.score > 100)) throw new Error('Facility scores must be null or between 0 and 100.');
    if (item.distanceKm !== null && (!Number.isFinite(item.distanceKm) || item.distanceKm < 0)) throw new Error('A facility distance is invalid.');
    if (item.metrics.some(metric => !metric || typeof metric.label !== 'string' || (metric.value !== null && (!Number.isFinite(metric.value) || metric.value < 0 || metric.value > 100)))) throw new Error('Metric values must be null or between 0 and 100.');
    const metricIds = new Set();
    for (const metric of item.metrics) {
      if (metric.id !== undefined) {
        if (typeof metric.id !== 'string' || !metric.id || metricIds.has(metric.id)) throw new Error('Metric IDs must be unique within a facility.');
        metricIds.add(metric.id);
      }
      if (metric.evidenceIds !== undefined && !referencesValid(metric.evidenceIds)) throw new Error('Metric evidence references are invalid.');
      if (metric.weight !== undefined && metric.weight !== null && (!Number.isFinite(metric.weight) || metric.weight < 0 || metric.weight > 100)) throw new Error('Metric weights must be between 0 and 100.');
      for (const key of ['detail', 'explanation']) if (metric[key] !== undefined && typeof metric[key] !== 'string') throw new Error('Metric descriptions must be text.');
    }
  }
  return data;
}

export async function searchFacilities(request, signal) {
  if (!isRankingMode(request.rankingMode)) throw new Error('Choose a valid result sorting mode.');
  const payload = referralPayload(request);
  if (config.mode === 'demo') {
    await new Promise(resolve => setTimeout(resolve, 450));
    if (signal.aborted) throw new DOMException('Search cancelled', 'AbortError');
    return validateResponse(demoRecommendations(payload));
  }
  return validateResponse(await searchCareBricks(config.endpoint, request, signal));
}
