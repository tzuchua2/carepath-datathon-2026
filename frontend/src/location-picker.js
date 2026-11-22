import { knownLocation, locationSuggestions, normalizeLocation, TEST_ORIGIN } from './locations.js';
import { searchStartingLocations } from './location-search.js';
import { findOrigin } from './carebricks.js';

export function initLocationPicker({ onEdit, onPreview, useBackend = false }) {
  const input = document.querySelector('#location');
  const button = document.querySelector('#locate-address');
  const options = document.querySelector('#address-options');
  const status = document.querySelector('#address-status');
  let selected = useBackend ? null : { ...TEST_ORIGIN }, pending, version = 0;
  let selectedQuery = normalizeLocation(selected?.address);
  let candidates = [], activeIndex = -1;
  input.value = selected?.address || ''; input.title = input.value;

  function cancel() {
    version++; pending?.abort(); pending = null; button.disabled = false; button.textContent = 'Locate';
    candidates = []; activeIndex = -1; options.hidden = true; options.replaceChildren(); status.textContent = '';
    input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant');
  }
  function getOrigin(query = input.value) {
    return normalizeLocation(query) === selectedQuery ? selected : null;
  }
  function set(value) {
    if (normalizeLocation(value) === normalizeLocation(input.value)) return;
    cancel(); input.value = value; input.title = value;
    selected = useBackend ? null : knownLocation(value); selectedQuery = normalizeLocation(value);
    onPreview(selected);
  }
  function choose(point) {
    if (point.unresolved) {  // a place the backend offered as one of several: resolve the chosen one, then use it
      cancel(); status.textContent = 'Looking up this place…';
      findOrigin(point.query).then(choose, error => { status.textContent = error.message; });
      return;
    }
    cancel(); selected = point; input.value = point.address || point.name;
    selectedQuery = normalizeLocation(input.value); input.title = input.value;
    onEdit(); onPreview(point);
  }
  function showCandidates(points) {
    candidates = points; activeIndex = -1; options.replaceChildren(); input.removeAttribute('aria-activedescendant');
    for (const [index, point] of points.entries()) {
      const option = document.createElement('button'); option.type = 'button'; option.className = 'address-option';
      option.id = `address-option-${index}`; option.setAttribute('role', 'option'); option.setAttribute('aria-selected', 'false'); option.tabIndex = -1;
      const title = document.createElement('strong'); title.textContent = point.name;
      const detail = document.createElement('span'); detail.textContent = point.address;
      option.append(title, detail);
      if (['city', 'pin', 'district', 'county', 'state', 'locality', 'street'].includes(point.precision)) {
        const precision = document.createElement('small'); precision.textContent = point.precision === 'street' ? 'Street location' : 'Approximate area centre'; option.append(precision);
      }
      option.onclick = () => { choose(point); input.focus(); cancel(); }; options.append(option);
    }
    options.hidden = !points.length; input.setAttribute('aria-expanded', String(Boolean(points.length)));
  }
  async function locate(query = input.value, signal) {
    cancel();
    const current = version;
    const local = getOrigin(query) || (!useBackend && knownLocation(query));
    if (local) {
      selected = local; selectedQuery = normalizeLocation(query); onPreview(local); return local;
    }
    pending = new AbortController();
    const requestSignal = AbortSignal.any([signal, pending.signal, AbortSignal.timeout(16000)].filter(Boolean));
    button.disabled = true; button.textContent = 'Locating…';
    status.textContent = 'Looking up this address…';
    try {
      if (useBackend) {
        const point = await findOrigin(query, requestSignal);
        if (current !== version) return null;
        selected = point; selectedQuery = normalizeLocation(query); onPreview(point);
        // Say how the place was read only when that adds something to what was typed.
        const precision = { locality: 'city centre', pincode_approximate: 'centre of the PIN area', district: 'district centre', post_office: 'post office', coordinates: '' }[point.precision] ?? '';
        const detail = [precision, Number.isFinite(point.uncertaintyKm) && point.uncertaintyKm > 0 ? `about ${Math.round(point.uncertaintyKm)} km uncertainty` : ''].filter(Boolean).join(', ');
        status.textContent = [normalizeLocation(point.name) === normalizeLocation(query) ? '' : point.name, detail].filter(Boolean).join(' · ');
        return point;
      }
      const points = await searchStartingLocations(query, { signal: requestSignal });
      if (current !== version) return null;
      if (!points.length) throw new Error('No matching address in India. Try a nearby landmark, PIN or coordinates.');
      showCandidates(points); status.textContent = 'Choose your starting point.';
      return null;
    } catch (error) {
      if (current === version && error.options?.length) {
        // More than one place has this name. Offer the choices; never pick one silently.
        showCandidates(error.options); status.textContent = `${error.message} ${error.options.length > 1 ? 'Choose your starting point.' : 'Did you mean this place?'}`;
        return null;
      }
      if (current === version && error.name !== 'AbortError') status.textContent = error.message;
      throw error;
    } finally {
      if (current === version) { pending = null; button.disabled = false; button.textContent = 'Locate'; }
    }
  }
  input.addEventListener('input', () => {
    input.title = input.value; cancel(); selected = null; selectedQuery = ''; onEdit(); onPreview(null);
    if (!useBackend) showCandidates(locationSuggestions(input.value));
  });
  input.addEventListener('focus', () => { if (!useBackend && !pending) showCandidates(locationSuggestions(input.value)); });
  const locateManually = () => locate().catch(error => { if (error.name !== 'AbortError') status.textContent = error.message; });
  button.onclick = locateManually;
  input.addEventListener('keydown', event => {
    if (['ArrowDown', 'ArrowUp'].includes(event.key) && candidates.length) {
      event.preventDefault();
      activeIndex = activeIndex < 0 ? (event.key === 'ArrowDown' ? 0 : candidates.length - 1) : (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + candidates.length) % candidates.length;
      [...options.children].forEach((element, index) => element.setAttribute('aria-selected', String(index === activeIndex)));
      input.setAttribute('aria-activedescendant', `address-option-${activeIndex}`);
      options.children[activeIndex].scrollIntoView({ block: 'nearest' });
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (activeIndex >= 0 && !options.hidden) choose(candidates[activeIndex]); else locateManually();
    }
    if (event.key === 'Escape') cancel();
  });
  document.addEventListener('pointerdown', event => { if (!event.target.closest('.starting-address')) cancel(); });
  document.addEventListener('focusin', event => { if (!event.target.closest('.starting-address')) cancel(); });
  return { getOrigin, set, locate, cancel, reset() { cancel(); selected = null; selectedQuery = ''; input.value = ''; input.title = ''; onPreview(null); } };
}
