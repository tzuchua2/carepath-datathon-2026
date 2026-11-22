// Model credentials live only on the backend. The local rules are explicit demo mode only.
export const agentConfig = { mode: new URLSearchParams(globalThis.location?.search).get('demo') === '1' ? 'demo' : 'api', endpoint: '/api/agent' };
import { serviceLabels, mentionedServices } from './services.js';
import { defaultRankingMode, isRankingMode } from './ranking.js';
import { referralPayload } from './referral.js';
export const services = serviceLabels;

export function emptyDraft() {
  return { service: '', serviceLabel: '', location: '', rankingMode: defaultRankingMode,
    patient: { reference: '', age: null, presentation: '', history: '', medicationsAndAllergies: '' } };
}

export function localAgentTurn(message, previous, result) {
  const draft = structuredClone(referralPayload(previous));
  const text = message.trim();
  // Explanation answers stay grounded in the displayed response, never an invented verification.
  if (/\b(why|evidence|reliab\w*)\b|为什么|依据|可靠/i.test(text)) {
    const facility = result?.facilities?.find(item => item.id === result.focusedFacilityId) || result?.facilities?.[0];
    return { draft, ready: false, reply: facility
      ? `${facility.name}: ${facility.reasons.join(' ')}\n${result.methodology}\nOpen Evidence on the facility card to inspect the underlying text. This explanation refers to ${result.focusedFacilityId ? 'the selected facility on the map' : 'the first facility in recommendation order'}.`
      : 'Once we have a shortlist, I can walk through its supplied reasons and source evidence. First, tell me the required service and starting location.' };
  }
  if (/availab|capacity|slots?|beds?|接诊|床位/i.test(text) && !/\b(find|need|search)\b/i.test(text)) {
    return { draft, ready: false, reply: result
      ? `${result.facilities.map(f => `${f.name}: ${f.availability.label}${f.availability.checkedAt ? ` (checked ${f.availability.checkedAt})` : ' (no verification timestamp)'}.`).join('\n')}\nReported doctor and bed counts describe facility size. They do not establish current availability.${result.demo ? ' These statuses belong to sample facilities.' : ''}`
      : 'Current availability must come from a verification source. The preview has no live staffing or bed feed. Tell me a service and location to build a shortlist.' };
  }
  // Ambiguous/negative language must not silently flip a service into a positive request.
  if (/\b(no|not|without|instead|rather)\b|不要|不是|而不是/i.test(text)) {
    return { draft, ready: false, reply: 'I want to capture that correction accurately. Please enter just the desired service and location, or use Edit in form to make the change directly.' };
  }
  let changed = false;
  const priorities = [
    ['recommended', /\brecommended(?:[\s_-]+order)?\b|推荐排序/i],
    ['distance_first', /\bdistance[\s_-]+(?:based|first)\b|距离优先/i],
    ['service_first', /\b(?:service|evidence)[\s_-]+(?:evidence[\s_-]+)?first\b/i],
    ['emergency_first', /\bemergency[\s_-]+(?:based|first)\b|急诊优先/i],
    ['coverage_first', /\bcoverage[\s_-]+first\b|覆盖优先/i],
  ].filter(([, pattern]) => pattern.test(text));
  if (priorities.length > 1) return { draft, ready: false, reply: 'Choose one result order: Recommended, Nearest first or Service evidence first.' };
  if (priorities.length) { draft.rankingMode = priorities[0][0]; changed = true; }
  // A ranking label is not a requested medical service or part of an address.
  const careText = priorities.length ? text.replace(priorities[0][1], '').replace(/[\s,;]+$/, '') : text;
  const matches = mentionedServices(careText);
  if (matches.length > 1) return { draft, ready: false, reply: 'I found more than one service. Which single service should this referral focus on first?' };
  if (matches.length === 1) {
    draft.service = matches[0].id; draft.serviceLabel = matches[0].label; changed = true;
  } else {
    const custom = careText.match(/\b(?:find|need|search for)\s+(?:an?\s+)?(.+?)\s+(?:near|in|from)\b/i);
    if (custom && !/^(?:care|a hospital|hospital|a facility|facility)$/i.test(custom[1].trim())) {
      draft.service = 'custom'; draft.serviceLabel = custom[1].trim().slice(0, 120); changed = true;
    }
  }
  const coordinateMatch = text.match(/(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)/);
  const locations = [
    ['Jaipur, Rajasthan', /\bjaipur\b|\b302001\b|斋浦尔/i],
    ['Patna, Bihar', /\bpatna\b|\b800001\b|巴特那/i],
    ['Bengaluru, Karnataka', /\bbengaluru\b|\bbangalore\b|\b560001\b/i],
    ['Mumbai, Maharashtra', /\bmumbai\b|\b400001\b|孟买/i],
  ].filter(([, regex]) => regex.test(text));
  if (locations.length > 1) return { draft: previous, ready: false, reply: 'I found multiple locations. Please choose one starting location for this referral.' };
  const place = careText.match(/\b(?:near|in|from|location[: ]+)\s+([\p{L}\p{N}][\p{L}\p{N}\s,.'’/#()-]*?)(?=\s+(?:for|within|with|and|who)\b|[!?;]|$)/iu);
  const address = place?.[1].trim().replace(/\.$/, '');
  const simpleCity = address && /^(?:jaipur(?:, rajasthan)?|patna(?:, bihar)?|bengaluru(?:, karnataka)?|bangalore|mumbai(?:, maharashtra)?|302001|800001|560001|400001)$/i.test(address);
  if (address && !simpleCity) { draft.location = address; changed = true; }
  else if (locations.length) { draft.location = locations[0][0]; changed = true; }
  else if (coordinateMatch) { draft.location = `${coordinateMatch[1]}, ${coordinateMatch[2]}`; changed = true; }
  const mentionsRange = /\b\d+(?:\.\d+)?\s*(?:km|kilomet(?:er|re)s?)\b|\d+\s*公里/i.test(text);
  const rangeNote = mentionsRange ? 'Search range is managed automatically; a custom radius has not been applied. ' : '';
  const age = text.match(/\b(\d{1,3})[- ]year[- ]old\b|\bage\s*:?\s*(\d{1,3})\b|(\d{1,3})\s*岁/i);
  if (age) {
    const value = Number(age[1] || age[2] || age[3]);
    if (value > 120) return { draft, ready: false, reply: 'Please check the age. This form accepts 0–120 years.' };
    draft.patient.age = value; changed = true;
  }
  if (!changed) return { draft, ready: false, reply: rangeNote || 'This local preview understands a service, location and age. Try “Dialysis near Jaipur”, or add detailed history using Edit in form. Live AI conversation will be connected through the backend.' };
  draft.patient.presentation = [draft.patient.presentation, text].filter(Boolean).join('\n').slice(-4000);
  if (!draft.service) return { draft, ready: false, reply: 'I have the starting location. Which service has the referring clinician requested? For example, dialysis, MRI or NICU.' };
  if (!draft.location) return { draft, ready: false, reply: `I’ve captured ${draft.serviceLabel.toLowerCase()}. Where is the patient starting from? Enter an address, city, PIN code or coordinates in the Starting address field.` };
  return { draft, ready: true, reply: `${rangeNote}I’ve organized your request below. Please review the service, location and patient context, then build the shortlist. You can adjust the brief in the form at any time.` };
}

export function validateAgentReply(data) {
  const d = data?.draft;
  if (typeof data?.reply !== 'string' || typeof data.ready !== 'boolean' || !d ||
      ![...Object.keys(services), '', 'custom'].includes(d.service) || typeof d.location !== 'string' ||
      (d.service === 'custom' && (typeof d.serviceLabel !== 'string' || !d.serviceLabel.trim() || d.serviceLabel.length > 120)) ||
      !isRankingMode(d.rankingMode) ||
      !d.patient || !['reference', 'presentation', 'history', 'medicationsAndAllergies'].every(k => typeof d.patient[k] === 'string') ||
      !(d.patient.age === null || Number.isInteger(d.patient.age) && d.patient.age >= 0 && d.patient.age <= 120) ||
      data.ready && (!d.service || !d.location.trim())) throw new Error('The agent returned an incomplete referral brief. Please try again or edit in form.');
  d.serviceLabel = d.service === 'custom' ? d.serviceLabel.trim() : services[d.service] || '';
  return { ...data, draft: referralPayload(d) };
}

export async function sendAgentMessage({ message, draft, messages, result }, signal) {
  if (agentConfig.mode === 'demo') {
    await new Promise(resolve => setTimeout(resolve, 650));
    if (signal.aborted) throw new DOMException('Cancelled', 'AbortError');
    return validateAgentReply(localAgentTurn(message, draft, result));
  }
  const response = await fetch(agentConfig.endpoint, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
    body: JSON.stringify({ message, draft: referralPayload(draft), messages,
      result: result ? { focusedFacilityId: result.focusedFacilityId || null } : null }),
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error('The agent response could not be read. Try again or continue in the form.'); }
  if (!response.ok) throw new Error(data.message || `The agent is unavailable (${response.status}). Try again or continue in the form.`);
  return validateAgentReply(data);
}
