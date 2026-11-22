import { validCoordinates } from './map-data.js';

// Public test starting point. Postal address: museum's official visitor page.
// Point: OSM relation 18882436 (Photon, 2026-11-21), not a hospital fixture.
export const TEST_ORIGIN = Object.freeze({
  name: 'Bihar Museum', address: 'Bihar Museum, Nehru Path, Patna, Bihar 800001, India',
  lat: 25.6077763, lng: 85.1203199, precision: 'place',
  sourceUrl: 'https://www.biharmuseum.org/visit/',
});

const cities = [
  { name: 'Jaipur, Rajasthan', matches: ['jaipur', '302001'], lat: 26.9124, lng: 75.7873 },
  { name: 'Patna, Bihar', matches: ['patna', '800001'], lat: 25.5941, lng: 85.1376 },
  { name: 'Bengaluru, Karnataka', matches: ['bengaluru', 'bangalore', '560001'], lat: 12.9716, lng: 77.5946 },
  { name: 'Mumbai, Maharashtra', matches: ['mumbai', '400001'], lat: 19.076, lng: 72.8777 },
];
export const normalizeLocation = value => String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
const testAliases = [TEST_ORIGIN.name, TEST_ORIGIN.address, 'Bihar Museum, Patna', 'Bihar Museum Patna'];

// Small, explicitly curated suggestions remain usable without an address server.
// A suggestion is never selected just because part of the query matches a city.
export function locationSuggestions(query) {
  const normalized = normalizeLocation(query);
  if (normalized.length < 2) return [];
  const terms = normalized.replace(/[,]/g, ' ').split(/\s+/).filter(Boolean);
  return [
    { ...TEST_ORIGIN, matches: testAliases },
    ...cities.map(city => ({ ...city, address: city.name, precision: 'city' })),
  ].filter(place => {
    const text = normalizeLocation([place.name, place.address, ...place.matches].join(' '));
    return terms.every(term => text.includes(term));
  }).map(({ matches, ...place }) => place).slice(0, 5);
}

export function knownLocation(query) {
  const normalized = normalizeLocation(query);
  if (!normalized) return null;
  if (testAliases.some(value => normalizeLocation(value) === normalized)) return { ...TEST_ORIGIN };
  // Exact matches only: a street address containing "Patna" is not the city centroid.
  const city = cities.find(item => [...item.matches, item.name].some(value => normalizeLocation(value) === normalized));
  if (city) return { name: city.name, address: city.name, lat: city.lat, lng: city.lng, precision: /^\d+$/.test(normalized) ? 'pin' : 'city' };
  if (/^-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?$/.test(normalized)) {
    const [lat, lng] = normalized.split(',').map(Number);
    if (validCoordinates({ lat, lng })) return { name: query.trim(), address: query.trim(), lat, lng, precision: 'coordinates' };
  }
  return null;
}

export function photonLocations(data) {
  if (!Array.isArray(data?.features)) throw new Error('Invalid address response.');
  const seen = new Set();
  return data.features.flatMap(feature => {
    const p = feature.properties || {};
    const [lng, lat] = feature.geometry?.coordinates || [];
    if (p.countrycode?.toUpperCase() !== 'IN' || feature.geometry?.type !== 'Point' || !validCoordinates({ lat, lng })) return [];
    const address = [...new Set([p.name, [p.housenumber, p.street].filter(Boolean).join(' '), p.city || p.district, p.state, p.postcode, 'India'].filter(Boolean))].join(', ');
    const key = `${address}:${lat}:${lng}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ name: p.name || p.street || p.city || address, address, lat, lng,
      precision: p.type === 'house' ? 'place' : p.type || 'unknown' }];
  }).slice(0, 5);
}
