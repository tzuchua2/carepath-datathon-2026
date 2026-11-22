import { knownLocation, locationSuggestions, normalizeLocation, photonLocations } from './locations.js';
import { validCoordinates } from './map-data.js';

// Same public address provider as the default server adapter. Set false when
// deploying with a private geocoder that must never use the public service.
export const addressSearchConfig = { publicFallback: true };
const fallbackCache = new Map();
let fallbackQueue = Promise.resolve(), lastFallbackAt = 0;
async function browserLookup(query, fetcher, signal) {
  const key = normalizeLocation(query);
  if (fallbackCache.has(key)) return fallbackCache.get(key);
  const task = fallbackQueue.catch(() => {}).then(async () => {
    await new Promise(resolve => setTimeout(resolve, Math.max(0, 1100 - (Date.now() - lastFallbackAt))));
    signal?.throwIfAborted();
    if (fallbackCache.has(key)) return fallbackCache.get(key);
    lastFallbackAt = Date.now();
    const url = new URL('https://photon.komoot.io/api/');
    url.search = new URLSearchParams({ q: query.trim(), countrycode: 'IN', limit: '5', lang: 'en' });
    const response = await fetcher(url.href, { signal });
    if (!response.ok) throw new Error('Address search is unavailable. Try again or enter coordinates.');
    const points = photonLocations(await response.json());
    fallbackCache.set(key, points);
    if (fallbackCache.size > 50) fallbackCache.delete(fallbackCache.keys().next().value);
    return points;
  });
  fallbackQueue = task.catch(() => {});
  return task;
}

export async function searchStartingLocations(query, { fetcher = fetch, signal } = {}) {
  const local = knownLocation(query);
  if (local) return [local];
  if (query.trim().length < 3) throw new Error('Enter an address, city, PIN or coordinates.');
  const suggestions = locationSuggestions(query);
  if (suggestions.length) return suggestions;
  try {
    let response;
    try {
      response = await fetcher('/api/geocode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query }), signal: AbortSignal.any([signal, AbortSignal.timeout(6000)].filter(Boolean)) });
    } catch (error) {
      if (signal?.aborted || !addressSearchConfig.publicFallback) throw error;
      return await browserLookup(query, fetcher, signal);
    }
    if ((response.status === 404 || response.status >= 500) && addressSearchConfig.publicFallback) return await browserLookup(query, fetcher, signal);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Address search is unavailable. Try again or enter coordinates.');
    if (!Array.isArray(data.locations) || data.locations.some(point => !validCoordinates(point) || typeof point.name !== 'string' || typeof point.address !== 'string')) {
      throw new Error('Address search returned an incomplete result. Please try again.');
    }
    return data.locations;
  } catch (error) {
    if (error.name === 'TimeoutError') throw new Error('Address search timed out. Try again or enter coordinates.');
    if (signal?.aborted || error.name === 'AbortError') throw error;
    if (error instanceof TypeError || error instanceof SyntaxError) {
      throw new Error('Address search could not connect. Try again, choose a suggested place or enter coordinates.');
    }
    throw error;
  }
}
