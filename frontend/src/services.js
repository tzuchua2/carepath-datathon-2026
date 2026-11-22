// Search vocabulary, not a claim that a matching facility is available.
const entries = [
  ['dialysis', 'Dialysis', 'Renal care', ['hemodialysis', 'haemodialysis', 'kidney dialysis', '透析'], ['renal', 'kidney']],
  ['mri', 'MRI', 'Imaging', ['magnetic resonance imaging', '磁共振']],
  ['nicu', 'Neonatal intensive care', 'Critical care', ['nicu', 'newborn intensive care', '新生儿重症']],
  ['cardiac_surgery', 'Cardiac surgery', 'Heart care', ['heart surgery', '心脏手术']],
  ['burn_care', 'Burn care', 'Emergency care', ['burn treatment', 'burn unit', '烧伤']],
  ['emergency_c_section', 'Emergency C-section', 'Maternity', ['emergency cesarean', 'emergency caesarean', '紧急剖宫产']],
  ['ct_scan', 'CT scan', 'Imaging', ['computed tomography', 'cat scan']],
  ['ultrasound', 'Ultrasound', 'Imaging', ['sonography', 'sonogram']],
  ['x_ray', 'X-ray', 'Imaging', ['xray', 'radiography']],
  ['mammography', 'Mammography', 'Imaging', ['mammogram', 'breast screening']],
  ['pet_scan', 'PET scan', 'Imaging', ['positron emission tomography']],
  ['icu', 'Intensive care', 'Critical care', ['icu', 'adult intensive care']],
  ['picu', 'Pediatric intensive care', 'Critical care', ['picu', 'paediatric intensive care']],
  ['trauma_care', 'Trauma care', 'Emergency care', ['trauma centre', 'trauma center', 'accident care']],
  ['emergency_care', 'Emergency care', 'Emergency care', ['emergency department', 'emergency medicine']],
  ['blood_transfusion', 'Blood transfusion', 'Blood services', ['transfusion', 'blood bank']],
  ['high_risk_delivery', 'High-risk delivery', 'Maternity', ['high risk pregnancy', 'high risk maternity']],
  ['c_section', 'C-section', 'Maternity', ['cesarean', 'caesarean', 'cesarean delivery']],
  ['maternity', 'Maternity care', 'Maternity', ['obstetric care', 'obstetrics', 'antenatal care']],
  ['pediatrics', 'Pediatric care', 'Child health', ['pediatrics', 'paediatrics', 'paediatric care']],
  ['angioplasty', 'Angioplasty', 'Heart care', ['coronary angioplasty', 'pci']],
  ['echocardiography', 'Echocardiography', 'Heart care', ['echocardiogram', 'echo test']],
  ['cardiology', 'Cardiology', 'Heart care', ['cardiac consultation', 'heart specialist']],
  ['chemotherapy', 'Chemotherapy', 'Cancer care', ['chemo']],
  ['radiotherapy', 'Radiotherapy', 'Cancer care', ['radiation therapy']],
  ['oncology', 'Oncology', 'Cancer care', ['cancer care', 'cancer treatment']],
  ['neurosurgery', 'Neurosurgery', 'Surgery', ['brain surgery', 'spine surgery']],
  ['orthopedic_surgery', 'Orthopedic surgery', 'Surgery', ['orthopaedic surgery', 'joint replacement']],
  ['general_surgery', 'General surgery', 'Surgery', ['laparoscopic surgery']],
  ['kidney_transplant', 'Kidney transplant', 'Renal care', ['renal transplant']],
  ['nephrology', 'Nephrology', 'Renal care', ['renal consultation', 'kidney specialist']],
  ['endoscopy', 'Endoscopy', 'Digestive care', ['gastroscopy', 'upper gi endoscopy']],
  ['colonoscopy', 'Colonoscopy', 'Digestive care', []],
  ['physiotherapy', 'Physiotherapy', 'Rehabilitation', ['physical therapy', 'physio']],
  ['rehabilitation', 'Rehabilitation', 'Rehabilitation', ['rehab', 'rehabilitation medicine']],
  ['mental_health', 'Mental health care', 'Mental health', ['psychiatry', 'mental health', 'counselling']],
  ['ophthalmology', 'Eye care', 'Eye care', ['ophthalmology', 'cataract surgery']],
  ['ent', 'ENT care', 'ENT', ['ent', 'ear nose throat', 'otolaryngology']],
  ['dental', 'Dental care', 'Dental', ['dentistry', 'dental']],
  ['lab_tests', 'Laboratory tests', 'Diagnostics', ['lab tests', 'blood tests', 'pathology']],
];
export const serviceCatalog = entries.map(([id, label, category, mentions, searchOnly = []]) => ({ id, label, category, mentions: [label, ...mentions], aliases: [...mentions, ...searchOnly] }));
export const serviceLabels = Object.fromEntries(serviceCatalog.map(item => [item.id, item.label]));
// Coverage from the supplied v3 frontend brief; these are national eligible
// facility–service pairs, not nearby results or current appointment slots.
const pilotPairs = { dialysis: 455, mri: 275, nicu: 276, c_section: 230, emergency_c_section: 1 };
export function serviceCoverage(id) {
  if (id === 'emergency_c_section') return { status: 'limited', label: 'Limited coverage', pairs: 1, note: 'Only one eligible record in the current dataset. Emergency readiness is unverified.' };
  if (pilotPairs[id]) return { status: 'ready', label: 'Pilot service', pairs: pilotPairs[id], note: '' };
  return { status: 'browse', label: 'Browse only', pairs: null, note: 'Search term supported; evidence-backed recommendations are not yet included in the current data release.' };
}
const normalize = text => String(text).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

function editDistance(a, b) {
  let row = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const next = [i];
    for (let j = 1; j <= b.length; j++) next[j] = Math.min(next[j - 1] + 1, row[j] + 1, row[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    row = next;
  }
  return row[b.length];
}

export function searchServices(query, limit = 8) {
  const q = normalize(query).slice(0, 120);
  if (!q) {
    const priority = item => pilotPairs[item.id] > 1 ? 2 : pilotPairs[item.id] ? 1 : 0;
    return [...serviceCatalog].sort((a, b) => priority(b) - priority(a)).slice(0, limit);
  }
  return serviceCatalog.map(item => {
    let score = 0;
    for (const [text, categoryOnly] of [...[item.label, ...item.aliases].map(text => [text, false]), [item.category, true]]) {
      const term = normalize(text);
      let match = 0;
      if (term === q) match = 100;
      else if (term.startsWith(q)) match = 85;
      else if (term.includes(q)) match = 70;
      else if (q.split(' ').every(token => term.includes(token))) match = 60;
      else if (q.length >= 4 && term.split(' ').some(word => Math.abs(word.length - q.length) <= 2 && editDistance(q, word) <= (q.length > 6 ? 2 : 1))) match = 40;
      score = Math.max(score, categoryOnly ? Math.min(match, 45) : match);
    }
    return { ...item, score };
  }).filter(item => item.score > 0).sort((a, b) => b.score - a.score || a.label.localeCompare(b.label)).slice(0, limit);
}

export function mentionedServices(text) {
  const normalized = ` ${normalize(text)} `;
  const hits = serviceCatalog.flatMap(item => item.mentions.flatMap(mention => {
    const phrase = normalize(mention);
    const search = /[\u3400-\u9fff]/.test(phrase) ? phrase : ` ${phrase} `;
    const start = normalized.indexOf(search);
    return start < 0 ? [] : [{ item, start, end: start + search.length }];
  }));
  // A specific phrase such as "neonatal intensive care" subsumes "intensive care".
  return [...new Map(hits.filter(hit => !hits.some(other => other.item.id !== hit.item.id && other.start <= hit.start && other.end >= hit.end && other.end - other.start > hit.end - hit.start)).map(hit => [hit.item.id, hit.item])).values()];
}
