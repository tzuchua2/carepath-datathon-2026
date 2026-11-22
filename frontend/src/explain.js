// Explanation pieces for results that come from the CareBricks API: public-source labels, the model check,
// leads that need checking, the call-ahead checklist and the "How we verify" panel.
// Every function returns text or escaped HTML built only from fields the API sent. Wording rules:
// the sort score is never a chance of treatment; the second model pass is a "re-check"; evaluation
// figures are "agreement", never accuracy; verifier counts are claims, not facilities.
import { escapeHtml as esc, safeSource } from './decision.js';

const words = code => { const t = String(code ?? '').replace(/_/g, ' ').toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };
const lower = label => /^[A-Z]{2,}\b|^C-/.test(label || '') ? label : (label || 'the service').charAt(0).toLowerCase() + (label || 'the service').slice(1);
const fieldLabel = (field, index) => `Field “${field ?? 'unknown'}”${index == null ? '' : `, item ${index}`}`;
const pct = value => value == null ? '–' : `${Math.round(value * 1000) / 10}%`;

export const SCORE_LABEL = 'Sort score';
export const SCORE_NOTE = 'The score orders this list using what the record says, distance and location consistency. It is not a chance of treatment and not a patient rating.';

// ---- public-source review: all four findings, and an explicit "not reviewed" ----
const FINDINGS = {
  recent_independent: { kind: 'check', text: s => `✓ ${s.charAt(0).toUpperCase() + s.slice(1)} independently documented`,
    meaning: 'A recent government, payer or accreditation record names this branch and this service.' },
  historical_independent: { kind: 'listed', text: s => `Hospital lists ${s} · older independent source`,
    meaning: 'An older independent record names this branch and service, and the hospital still lists it.' },
  provider: { kind: 'listed', text: s => `Hospital lists ${s}`,
    meaning: 'The hospital or its own clinician lists the service. No qualifying independent confirmation was found.' },
  unresolved: { kind: 'verify', text: s => `Call to verify ${s}`,
    meaning: 'The public sources found do not establish that this branch offers this exact service.' },
};

export function reviewLabel(review, serviceLabel) {
  const finding = FINDINGS[review?.finding];
  if (!finding) return { kind: 'none', text: 'Public sources not yet reviewed', meaning: 'Nobody has compared this record with public sources yet. Treat the service as the record’s own claim.' };
  return { kind: finding.kind, text: finding.text(lower(serviceLabel)), meaning: finding.meaning,
    basis: `desk review of public sources, ${review.reviewed_on || 'date not recorded'}` };
}

export function reviewMarkup(review, serviceLabel) {
  const label = reviewLabel(review, serviceLabel);
  if (label.kind === 'none') return `<section class="hospital-description"><h3>Public-source review</h3><p>${esc(label.meaning)}</p></section>`;
  const links = (review.sources || []).map(source => ({ role: source.role, url: safeSource(source.url) })).filter(source => source.url);
  return `<section class="hospital-description"><h3>Public-source review</h3><p><span class="explain-tag tag-${label.kind}">${esc(label.text)}</span></p>
    <p>${esc(label.meaning)}</p><p>${esc(review.finding_detail || '')}</p>
    ${links.length ? `<ul class="explain-links">${links.map(source => `<li><strong>${esc(words(source.role))}:</strong> <a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(new URL(source.url).hostname.replace(/^www\./, ''))} ↗</a></li>`).join('')}</ul>` : ''}
    <p class="source-attribution">Desk review of public sources, ${esc(review.reviewed_on || 'date not recorded')}. A one-time reading of public pages; no facility was contacted. Open today: not confirmed by any source.</p></section>`;
}

// ---- model check: first reading, re-check, code correction ----
const MODEL_LABELS = {
  EXPLICIT_POSITIVE: 'The model read the record and also finds that it states the service.',
  INDIRECT: 'The model finds only a related mention, not a statement of the service.',
  EXPLICIT_NEGATIVE: 'The model finds the record says the service is not available here.',
  PROVIDED_ELSEWHERE: 'The model finds the service is outsourced, referred or at another site.',
  PLANNED_OR_HISTORICAL: 'The model finds the service is planned or was offered in the past.',
  AMBIGUOUS: 'The model could not tell from the text whether this facility provides the service.',
  CONFLICTING: 'The model finds the record both states and denies the service.',
  NO_EVIDENCE: 'The model finds nothing in the passages about the service.',
};
const NO_ANSWER = 'The model check did not return a usable answer for this record, so the text rules stand.';
const RECHECK_DECISIONS = { STATES_SERVICE: 'the record states the service', DOES_NOT_STATE: 'the record does not state the service',
  UNCLEAR: 'it is unclear whether the record states the service' };
const RECHECK_APPLIED = {
  restored: 'Because the re-check supports the text rules, the claim is recommended again. It is marked so a person can still check it.',
  confirmed: 'The re-check supports the text rules, so the claim stays recommended.',
  kept_out: 'Neither reading supports the text rules, so the claim stays a lead for a person to check.',
};
const usable = check => check.status === 'classified' && check.outcome !== 'rules_only' && Boolean(check.label);

export function modelTag(check) {
  if (!check) return null;
  if (check.outcome === 'model_found_statement_rules_missed') return { kind: 'verify', text: 'Found by model reading, not by the text rules: verify before use' };
  const recheck = check.second_opinion;
  if (recheck?.applied === 'kept_out') return { kind: 'verify', text: 'Two model readings disagree with the rules' };
  if (recheck) return { kind: 'recheck', text: 'Supported on a re-check' };
  if (!usable(check)) return { kind: 'none', text: 'Model check: no usable answer' };
  return check.label === 'EXPLICIT_POSITIVE' ? { kind: 'agree', text: 'Model check agrees' }
    : { kind: 'verify', text: `Model check disagrees: ${words(check.label).toLowerCase()}` };
}

function findingMarkup(finding) {
  const positive = finding.effective_classification === 'EXPLICIT_POSITIVE';
  const changed = finding.code_override && finding.classification && finding.classification !== finding.effective_classification;
  return `<article class="source-card"><div class="source-heading"><strong>${esc(positive ? 'Read as a statement of the service' : `Read as: ${words(finding.effective_classification).toLowerCase()}`)}</strong></div>
    <blockquote>${esc(finding.exact_quote)}</blockquote><p class="source-attribution">${esc(fieldLabel(finding.field, finding.item_index))}</p>
    ${finding.model_explanation ? `<p class="published-reading"><strong>Model’s reason</strong> ${esc(finding.model_explanation)}</p>` : ''}
    ${finding.code_override ? `<p class="published-reading"><strong>Code check</strong> ${esc(`A code check adjusted the model’s reading (${words(finding.code_override).toLowerCase()})${changed ? `: the model first read this as ${words(finding.classification).toLowerCase()}.` : '.'}`)}</p>` : ''}</article>`;
}

export function modelCheckMarkup(check, findings = []) {
  if (!check) return '<section class="hospital-description"><h3>Model check</h3><p>No model check is attached to this record in this release. The decision comes from the text rules.</p></section>';
  const recheck = check.second_opinion;
  let body;
  if (recheck) {
    const first = check.first_outcome === 'rules_only' || check.status !== 'classified' ? 'It did not return a usable answer.'
      : `${MODEL_LABELS[check.label] || words(check.label)} That held the claim back.`;
    body = `<p><strong>First reading.</strong> ${esc(first)}</p>
      <p><strong>Re-check.</strong> ${esc(`The model re-read the record and found that ${RECHECK_DECISIONS[recheck.decision] || words(recheck.decision).toLowerCase()}.`)}</p>
      ${recheck.exact_quote ? `<article class="source-card"><blockquote>${esc(recheck.exact_quote)}</blockquote><p class="source-attribution">${esc(fieldLabel(recheck.field, recheck.item_index))}</p>${recheck.reason ? `<p class="published-reading"><strong>Reason given</strong> ${esc(recheck.reason)}</p>` : ''}</article>` : (recheck.reason ? `<p>${esc(recheck.reason)}</p>` : '')}
      <p>${esc(RECHECK_APPLIED[recheck.applied] || '')}</p>
      <p class="source-attribution">The re-check was shown the first reading and the rule quotes, so it is not a separate vote, and it has not been scored against any labelled set.</p>`;
  } else if (!usable(check)) body = `<p>${esc(NO_ANSWER)}</p>`;
  else body = `<p>${esc(MODEL_LABELS[check.label] || words(check.label))}</p><p>${esc(check.outcome === 'model_lowered'
    ? 'The text rules read this record as stating the service. Because the two disagree, it is shown as a lead to verify, not a recommendation.'
    : check.outcome === 'agree_supported' ? 'The text rules reached the same reading.' : `Compared with the text rules: ${words(check.outcome).toLowerCase()}.`)}</p>`;
  const ordered = [...findings].sort((a, b) => (a.effective_classification === 'INDIRECT') - (b.effective_classification === 'INDIRECT'));
  const leadQuotes = (check.lead_quotes || []).map(q => ({ field: q.field, item_index: q.item_index, exact_quote: q.quote,
    effective_classification: 'EXPLICIT_POSITIVE', model_explanation: q.model_explanation }));
  const shown = [...leadQuotes, ...ordered];
  return `<section class="hospital-description"><h3>Model check</h3>${body}
    <p class="source-attribution">${esc([check.model, check.prompt_version, recheck?.receipt?.prompt_version].filter(Boolean).join(' · '))}. The model reads only the record text and must quote it exactly. It can hold a claim back; it cannot add facts.</p>
    ${shown.length ? `<details class="original-passages"><summary>What the model made of each passage <span>${shown.length}</span></summary>${shown.map(findingMarkup).join('')}</details>` : ''}</section>`;
}

// ---- leads: records near the search that are not recommended, with the reason ----
const LEAD_GROUPS = [
  // Model-found leads come first so a record listed twice keeps the reading that explains it.
  ['model_found_leads', 'Found by model reading, not by the text rules'],
  ['closer_but_not_recommended', 'Closer, but not recommended'],
  ['unverified_leads', 'Lead to verify'],
  ['set_aside', 'Set aside'],
  ['location_unverified_leads', 'Position on the map is disputed'],
];

export function leadsFrom(data, limit = 8) {
  const seen = new Set(), leads = [];
  for (const [key, group] of LEAD_GROUPS) {
    for (const row of data[key] || []) {
      if (seen.has(row.facility_id) || leads.length >= limit) continue;
      seen.add(row.facility_id);
      const first = (row.evidence_preview || [])[0];
      leads.push({
        id: row.facility_id, name: row.display_name, group, groupKey: key,
        place: [row.location?.city, row.location?.state].filter(Boolean).join(', '),
        distanceKm: row.distance?.value_km ?? null,
        reasons: [...(row.reasons || []), ...(row.manual_review_reason ? [row.manual_review_reason] : [])],
        flags: row.flags || [], quote: first?.exact_quote || null, quoteField: first?.source_field || null,
        quoteReason: first?.model_explanation || null,
        phone: row.contact?.phone_status === 'listed_unconfirmed' ? row.contact.phone : null,
        modelCheck: key === 'model_found_leads' ? { ...(row.model_check || {}), outcome: 'model_found_statement_rules_missed' } : row.model_check || null,
        publicReview: row.public_source_review || null,
      });
    }
  }
  return leads;
}

export function leadsMarkup(leads, serviceLabel) {
  if (!leads.length) return '';
  return `<section class="lead-list" aria-label="Records that need checking"><h3>Needs checking before referral <span>${leads.length}</span></h3>
    <p class="source-attribution">These records are near the search but do not state ${esc(lower(serviceLabel))} clearly enough to recommend. Treat each as a phone call to make, not as a referral.</p>
    ${leads.map(lead => { const tag = modelTag(lead.modelCheck); return `<article class="lead-card"><header><strong>${esc(lead.name)}</strong><span>${esc(lead.distanceKm == null ? 'Distance unknown' : `${lead.distanceKm.toFixed(1)} km · straight-line`)}</span></header>
      <p class="lead-group">${esc(lead.group)}${lead.place ? ` · ${esc(lead.place)}` : ''}</p>
      ${lead.reasons.map(reason => `<p class="lead-reason"><strong>Why not recommended:</strong> ${esc(reason)}</p>`).join('')}
      ${tag ? `<p><span class="explain-tag tag-${tag.kind}">${esc(tag.text)}</span></p>` : ''}
      ${lead.quote ? `<blockquote>${esc(lead.quote)}</blockquote><p class="source-attribution">${esc(`Record field “${lead.quoteField || 'unknown'}”`)}${lead.quoteReason ? ` · Model’s reason: ${esc(lead.quoteReason)}` : ''}</p>` : ''}
      ${lead.phone ? `<p class="contact-line">Listed phone ${esc(lead.phone)} · branch not confirmed</p>` : ''}</article>`; }).join('')}</section>`;
}

// ---- call-ahead checklist ----
export function checklist(response, facilities) {
  const service = response.query?.serviceLabel || 'the service';
  return {
    title: `${service} near ${response.origin?.name || response.query?.location || 'the search point'}`,
    note: 'Built from published facility records. Nobody has confirmed the service is running today.',
    items: facilities.map((item, index) => ({
      head: `${index + 1}. ${item.name}`,
      lines: [`Phone: ${item.reportedPhone ? `${item.reportedPhone} (listed in the record; branch not confirmed)` : 'no usable phone number in the record'}`,
        `Address: ${item.address || 'not in the record'}`,
        `Distance: ${Number.isFinite(item.distanceKm) ? `about ${item.distanceKm.toFixed(1)} km in a straight line` : 'unknown'}`],
      quote: item.evidence?.[0] ? { text: item.evidence[0].quote, field: item.evidence[0].field } : null,
      asks: [`Is ${lower(service)} running today, and is there a slot or bed?`, 'What will it cost, and which schemes are accepted?',
        ...(item.uncertainties || []).map(text => `Not confirmed: ${text}`)],
    })),
  };
}

export function checklistText(sheet) {
  const out = ['Carepath call-ahead checklist', sheet.title, sheet.note, ''];
  for (const item of sheet.items) {
    out.push(item.head, ...item.lines.map(line => `   ${line}`));
    if (item.quote) out.push(`   Record says: "${item.quote.text}" (${item.quote.field || 'field unknown'})`);
    out.push(...item.asks.map(ask => `   [ ] ${ask}`), '   [ ] Spoke to: ________  Time: ________  Answer: ________', '');
  }
  return out.join('\n');
}

export function checklistMarkup(sheet) {
  return `<span class="eyebrow">BEFORE SENDING THE PATIENT</span><h2 id="dialog-title">Call-ahead checklist</h2>
    <p class="status-note">${esc(sheet.title)}. ${esc(sheet.note)}</p>
    <div class="checklist-actions"><button class="outline-button" type="button" data-copy-checklist>Copy as text</button><button class="outline-button" type="button" data-print-checklist>Print</button><span id="checklist-status" role="status" aria-live="polite"></span></div>
    ${sheet.items.map(item => `<section class="checklist-item"><h3>${esc(item.head)}</h3>${item.lines.map(line => `<p>${esc(line)}</p>`).join('')}
      ${item.quote ? `<blockquote>${esc(item.quote.text)}</blockquote>` : ''}
      <ul>${item.asks.map(ask => `<li>${esc(ask)}</li>`).join('')}<li>Spoke to: ____________ &nbsp; Time: ________ &nbsp; Answer: ____________</li></ul></section>`).join('')}`;
}

// ---- "How we verify" panel, from /api/v1/verification ----
export function verificationMarkup(info) {
  const run = info.full_run || {}, evaluation = info.evaluation || {}, subset = evaluation.human_checked_subset;
  const labels = evaluation.column_labels || { rules: 'Text rules alone', prompt_1: 'Model reading, first prompt', prompt_2: 'Model reading, second prompt' };
  const columns = Object.keys(labels), reader = evaluation.reader_vs_system || {};
  const list = items => `<ul class="explain-limits">${(items || []).map(text => `<li>${esc(text)}</li>`).join('')}</ul>`;
  const rows = (items, format) => (items || []).map(row => `<tr><th scope="row">${esc(row.measure)}</th>${columns.map(c => `<td>${esc(format(row[c]))}</td>`).join('')}</tr>`).join('');
  const counts = [[run.claims_supported ?? run.eligible_after, 'claims supported after all checks'], [run.facilities_with_a_supported_claim, 'facilities with a supported claim'],
    [run.claims_held_for_a_person ?? run.lowered, 'claims left for a person'], [run.facilities_with_a_held_claim, 'facilities with a held claim']].filter(([n]) => n != null);
  const brand = text => String(text ?? '').replace(/CareBricks/g, 'Carepath');  // the page has one product name
  return `<section class="verify-panel"><h3>${esc(brand(info.title || 'How we verify a claim'))}</h3><p>${esc(brand(info.summary || ''))}</p>
    <ol class="help-steps">${(info.steps || []).map(step => `<li><strong>${esc(step.title)}.</strong> ${esc(step.text)}</li>`).join('')}</ol>
    <h3>Limits</h3>${list(info.limits)}
    ${run.candidates ? `<h3>The full run on this release</h3>
      ${(run.cascade || []).length ? `<ol class="verify-cascade">${run.cascade.map(stage => `<li><b>${esc(stage.pairs)}</b><span><strong>${esc(stage.stage)}</strong>${stage.note ? `<small>${esc(stage.note)}</small>` : ''}</span></li>`).join('')}</ol>` : ''}
      <p class="verify-counts">${counts.map(([n, text]) => `<span><b>${esc(n)}</b> ${esc(text)}</span>`).join('')}</p>
      <p class="source-attribution">Counts are ${esc(run.units || 'facility-service claims')}: one facility offering two services is two claims.${run.second_opinion ? ` The re-check re-read ${esc(run.second_opinion.pairs)} claims. It was shown the first reading and has not been scored.` : ''} ${esc([run.model, run.prompt_version].filter(Boolean).join(' · '))}</p>` : ''}
    ${(evaluation.rows || []).length ? `<h3>${esc(evaluation.heading || `Agreement with the label set on ${evaluation.pairs} pairs`)}</h3>
      ${evaluation.not_accuracy ? `<p class="status-note">${esc(evaluation.not_accuracy)}</p>` : ''}${list(evaluation.caveats)}
      <div class="verify-table"><table><thead><tr><th scope="col">Agreement with the label set</th>${columns.map(c => `<th scope="col">${esc(labels[c])}</th>`).join('')}</tr></thead>
        <tbody>${rows(evaluation.rows, pct)}${rows(evaluation.count_rows, n => n == null ? '–' : String(n))}</tbody></table></div>
      ${['model_as_reader', 'shipped_system', 'error_that_matters_for_referrals'].map(key => reader[key] ? `<p>${esc(reader[key])}</p>` : '').join('')}
      ${subset ? `<div class="verify-checked"><h4>Checked by a person${subset.pairs ? ` (${esc(subset.pairs)} pairs)` : ''}</h4>
        ${['reviewer_vs_draft', 'shipped'].map(key => subset[key] ? `<p>${esc(subset[key])}</p>` : '').join('')}
        ${subset.how_chosen ? `<p class="status-note">${esc(subset.how_chosen)}</p>` : ''}
        ${['rules', 'not_independent'].map(key => subset[key] ? `<p>${esc(subset[key])}</p>` : '').join('')}</div>` : ''}` : ''}
    ${(info.cases || []).length ? `<details class="original-passages"><summary>Worked cases <span>${info.cases.length}</span></summary>${info.cases.map(c => `<article class="source-card"><div class="source-heading"><strong>${esc(c.name)}</strong></div>
      <p class="source-attribution">${esc([c.place, words(c.service), c.outcome].filter(Boolean).join(' · '))}${c.synthetic ? ' · made-up test record, not a real facility' : ''}</p>
      ${(c.quotes || []).map(q => `<blockquote>${esc(q.quote)}</blockquote>`).join('')}
      <p><strong>Rules:</strong> ${esc(c.rules_said || '')}</p><p><strong>Model:</strong> ${esc(c.model_said || '')}</p>
      ${c.second_opinion ? `<p><strong>Re-check:</strong> ${esc(c.second_opinion)}</p>` : ''}${c.code_said ? `<p><strong>Code check:</strong> ${esc(c.code_said)}</p>` : ''}
      ${c.note ? `<p class="source-attribution">${esc(c.note)}</p>` : ''}</article>`).join('')}</details>` : ''}</section>`;
}

// ---- evidence level: how many of five checks a claim passed. "Not checked" is not "not passed". ----
export function evidenceLevel(item) {
  if (!Number.isFinite(item?.checksPassed) || !Number.isFinite(item?.checksTotal)) return null;
  // A check that has not been run is not a failed check; say how many are still to run.
  const waiting = (item.evidenceChecks || []).filter(check => check.status === 'not_checked').length;
  return `${item.checksPassed} of ${item.checksTotal} evidence checks${waiting ? ` · ${waiting} not yet run` : ''}`;
}

export function evidenceChecksMarkup(item) {
  const checks = item?.evidenceChecks || [];
  if (!checks.length) return '';
  const mark = { passed: ['✓', 'Passed'], not_passed: ['✕', 'Not passed'], not_checked: ['○', 'Not checked'],
    not_found: ['–', 'Not found on the pages read'] };  // the pages were read and did not mention it: neutral, not a failure
  return `<section class="hospital-description"><h3>Evidence checks <span class="explain-tag tag-listed">${esc(evidenceLevel(item) || '')}</span></h3>
    <p class="source-attribution">Each check is about what is written and published. None says the service is running today.</p>
    <ul class="evidence-checks">${checks.map(check => { const [symbol, label] = mark[check.status] || mark.not_checked;
      return `<li class="check-${esc(check.status || 'not_checked')}"><span aria-hidden="true">${symbol}</span><span><strong>${label}.</strong> ${esc(check.text)}</span></li>`; }).join('')}</ul></section>`;
}

// ---- map check against OpenStreetMap ----
const MAP_STATUS = {
  name_match: 'A health facility with a matching name is mapped near this point.',
  health_facility_nearby_different_name: 'A health facility is mapped near this point under a different name.',
  nothing_mapped_nearby: 'No health facility is mapped near this point. The map may be incomplete.',
  not_checked: 'This facility has not been compared with the map.',
};
export function mapCheckMarkup(check) {
  if (!check) return '';
  const url = safeSource(check.url);
  return `<section class="hospital-description"><h3>Map check</h3><p>${esc(MAP_STATUS[check.status] || words(check.status))}</p>
    ${check.name ? `<p>${esc(check.name)}${Number.isFinite(check.distance_m) ? ` · about ${esc(Math.round(check.distance_m))} m from the record’s point` : ''}${url ? ` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">open on the map ↗</a>` : ''}</p>` : ''}
    <p class="source-attribution">${esc([check.source, check.checked_on ? `checked ${check.checked_on}` : null, check.attribution].filter(Boolean).join(' · '))}. A map entry shows a place exists. It says nothing about this service.</p></section>`;
}

// ---- the district around the search point, and the planner's table ----
const title = text => String(text ?? '').replace(/\b[a-z]/g, char => char.toUpperCase());

const SURVEY_NAMES = { 'Births delivered by C-section (%)': 'C-section births', 'Births in a health facility (%)': 'facility births' };
export function surveyText(survey) {
  return Object.entries(survey || {}).map(([label, value]) => {
    const percent = / \(%\)$/.test(label);
    return `${SURVEY_NAMES[label] || label.replace(/ \(%\)$/, '')} ${value}${percent ? '%' : ''}`;
  }).join(' · ');
}

export function areaContextMarkup(context, serviceLabel) {
  if (!context || !Number.isFinite(context.median_km)) return '';
  const service = lower(serviceLabel), survey = Object.entries(context.survey || {});
  return `<aside class="area-context"><h3>${esc(context.district_display || title(context.district))} district</h3>
    <p>The typical PIN area here is ${esc(Math.round(context.median_km))} km from a documented ${esc(service)} provider${Number.isFinite(context.national_median_km) ? ` (national median ${esc(Math.round(context.national_median_km))} km)` : ''}.</p>
    ${survey.length ? `<p>Survey: ${esc(surveyText(context.survey))}.</p>` : ''}
    ${context.dataset_blind_spot ? '<p><strong>Likely gap in the dataset.</strong> The survey shows this service is common here, yet no documented provider is within 100 km, so nearer providers are probably missing from the dataset.</p>' : ''}
    <p class="source-attribution">${esc(context.caveat || '')} ${esc(context.survey_source || '')}</p></aside>`;
}

export function plannerMarkup(data, order) {
  const national = data.national || {}, rows = data.districts || [];
  return `<p class="status-note"><strong>Read this first.</strong> ${esc(data.caveat || 'These distances describe the dataset, not proven absence of care.')}</p>
    <p>${esc(data.service_display_name)}: half of the measured PIN areas are over ${esc(Math.round(national.median_km))} km from a documented provider.
      ${esc(national.districts_with_median_over_100_km)} of ${esc(national.districts_measured)} districts have a median over 100 km.
      ${esc(national.dataset_blind_spots)} districts look like gaps in the dataset: the survey shows the service is common there, but no documented provider is within 100 km.</p>
    <p class="source-attribution">Island territories are far from every mainland facility in the dataset. ${esc(data.recommendable_facilities)} documented facilities. Unit: ${esc(data.unit || 'straight-line km')}. ${esc(data.survey_source || '')}</p>
    <div class="verify-table"><table><thead><tr><th scope="col">District${order === 'blind_spots' ? ' (likely dataset gaps)' : ''}</th><th scope="col">Median km</th><th scope="col">90th pct km</th><th scope="col">Over 100 km</th><th scope="col">Nearest documented facility</th></tr></thead>
      <tbody>${rows.map(row => `<tr><th scope="row">${esc(title(row.district))}, ${esc(title(row.state))}${row.dataset_blind_spot ? ' <span class="explain-tag tag-verify">likely dataset gap</span>' : ''}${Object.entries(row.survey || {}).length ? `<small>${esc(surveyText(row.survey))}</small>` : ''}</th>
        <td>${esc(Math.round(row.median_km))}</td><td>${esc(Math.round(row.p90_km))}</td><td>${esc(pct(row.share_over_100_km))}</td><td class="planner-nearest">${esc(row.nearest_facility || '–')}${Number.isFinite(row.nearest_facility_km) ? ` · ${esc(Math.round(row.nearest_facility_km))} km` : ''}</td></tr>`).join('')}</tbody></table></div>
    <p class="source-attribution">Showing ${esc(rows.length)} of ${esc(data.districts_total)} districts${data.state ? ` in ${esc(title(data.state))}` : ''}.</p>`;
}

// ---- how steady the order is, and a two-facility comparison from the engine ----
export function stabilityMarkup(stability) {
  if (!stability?.text) return '';
  return `<aside class="area-context"><h3>Would different weights change the first choice?</h3><p>${esc(stability.text)}</p>
    <p class="source-attribution">The weights are engineering judgments. We re-ranked this search under ${esc((stability.weightings_tested || []).length)} other weightings of evidence, distance and location.</p></aside>`;
}

export function rankRangeText(range) {
  return range && Number.isFinite(range.best) && Number.isFinite(range.worst) && range.worst - range.best >= 2
    ? `Position ${range.best}–${range.worst} depending on weights` : null;
}

const CHECK_WORDS = { passed: 'Passed', not_passed: 'Not passed', not_checked: 'Not checked', not_found: 'Not found on the pages read' };
const CHECK_NAMES = { record_states_service: 'Record states the service', statement_is_specific: 'Statement is specific',
  model_reading_agrees: 'Model reading agrees', facility_source_states_it: 'Facility’s own material states it',
  independent_source_lists_it: 'Independent source lists it' };

export function compareMarkup(result) {
  const first = result.first?.display_name || 'First', second = result.second?.display_name || 'Second';
  const favour = side => side === 'first' ? first : side === 'second' ? second : 'Neither';
  return `<p class="status-note">${esc(result.summary || '')} ${esc(SCORE_NOTE)}</p>
    <div class="verify-table"><table><thead><tr><th scope="col">Part of the sort score</th><th scope="col">${esc(first)}</th><th scope="col">${esc(second)}</th><th scope="col">Favours</th></tr></thead>
      <tbody>${(result.components || []).map(row => `<tr><th scope="row">${esc(row.label)}</th><td>${esc(row.first_points)}</td><td>${esc(row.second_points)}</td><td>${esc(favour(row.favours))}</td></tr>`).join('')}
      <tr><th scope="row">Sort score</th><td>${esc(result.first?.ranking?.score ?? '–')}</td><td>${esc(result.second?.ranking?.score ?? '–')}</td><td>${esc(result.higher == null ? 'Neither' : result.higher === result.first?.facility_id ? first : second)}</td></tr>
      <tr><th scope="row">Straight-line distance</th><td>${esc(result.first?.distance?.value_km ?? '–')} km</td><td>${esc(result.second?.distance?.value_km ?? '–')} km</td><td></td></tr></tbody></table></div>
    <div class="verify-table"><table><thead><tr><th scope="col">Evidence check</th><th scope="col">${esc(first)}</th><th scope="col">${esc(second)}</th></tr></thead>
      <tbody>${(result.evidence_checks || []).map(row => `<tr><th scope="row">${esc(CHECK_NAMES[row.key] || words(row.key))}</th><td title="${esc(row.first_text || '')}">${esc(CHECK_WORDS[row.first] || words(row.first))}</td><td title="${esc(row.second_text || '')}">${esc(CHECK_WORDS[row.second] || words(row.second))}</td></tr>`).join('')}</tbody></table></div>
    <p class="source-attribution">Both are options whose records state the service. Neither has been confirmed as open today. Call before sending a patient.</p>`;
}
