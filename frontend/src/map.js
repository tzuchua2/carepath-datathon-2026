import { TEST_ORIGIN } from './locations.js';
import { mapFacilities, validCoordinates, locationPrecision } from './map-data.js';

const container = document.querySelector('#map');
const error = document.querySelector('#map-error');
const initialCenter = { lat: TEST_ORIGIN.lat, lng: TEST_ORIGIN.lng };
let adapter, results, onSelect, selectedId, generation = 0;
let fallbackNotice = '';

function status(message = '') {
  error.textContent = message || fallbackNotice;
  error.hidden = !error.textContent;
}

function points(data) {
  return [data?.origin, ...mapFacilities(data).map(({ facility }) => facility.location)].filter(validCoordinates);
}

function activate(next, provider) {
  adapter = next;
  container.dataset.provider = provider;
  container.classList.remove('map-offline');
  if (results) { adapter.render(results); adapter.select(selectedId, false); }
  else adapter.fit();
}

function useOpenStreetMap(notice = '') {
  generation++;
  fallbackNotice = notice;
  adapter?.destroy(); adapter = null;
  container.replaceChildren();
  if (!window.L) {
    container.classList.add('map-offline');
    container.dataset.provider = 'unavailable';
    status('Map unavailable. You can still review the facility list and evidence.');
    return;
  }
  activate(createLeafletMap(), 'osm');
  status();
}

export async function initMap() {
  const current = ++generation;
  let config;
  try {
    const response = await fetch('/api/map-config');
    if (!response.ok) throw new Error('No map configuration');
    config = await response.json();
  } catch { config = { provider: 'osm' }; }
  if (current !== generation) return;
  if (config.provider !== 'google') { useOpenStreetMap(); return; }
  if (!config.googleMapsApiKey) { useOpenStreetMap('Google Maps needs a key. Showing OpenStreetMap.'); return; }
  try {
    await loadGoogleMaps(config.googleMapsApiKey);
    if (current !== generation) return;
    activate(createGoogleMap(config.googleMapsMapId || 'DEMO_MAP_ID'), 'google');
    status();
  } catch {
    if (current === generation) useOpenStreetMap('Google Maps unavailable. Showing OpenStreetMap.');
  }
}

function loadGoogleMaps(key) {
  return new Promise((resolve, reject) => {
    let finished = false;
    const finish = failure => {
      if (finished) return;
      finished = true; clearTimeout(timeout);
      failure ? reject(failure) : resolve();
    };
    const timeout = setTimeout(() => finish(new Error('Map load timed out')), 15000);
    window.carepathMapsReady = () => finish();
    window.gm_authFailure = () => {
      if (!finished) finish(new Error('Map authentication failed'));
      else if (container.dataset.provider === 'google') useOpenStreetMap('Google Maps unavailable. Showing OpenStreetMap.');
    };
    const script = document.createElement('script');
    const url = new URL('https://maps.googleapis.com/maps/api/js');
    url.search = new URLSearchParams({ key, callback: 'carepathMapsReady', loading: 'async', libraries: 'marker', v: 'weekly' });
    script.src = url.href; script.async = true;
    script.onerror = () => finish(new Error('Map could not load'));
    document.head.append(script);
  });
}

function createLeafletMap() {
  const map = window.L.map(container, { zoomControl: false }).setView(initialCenter, 14);
  window.L.control.zoom({ position: 'bottomright' }).addTo(map);
  const tiles = window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  tiles.on('tileerror', () => status('Map tiles unavailable. Facility results are still available.'));
  tiles.on('tileload', () => status());
  const layer = window.L.layerGroup().addTo(map), markers = new Map();
  const resize = new ResizeObserver(() => map.invalidateSize({ pan: false }));
  resize.observe(container);
  function fit() {
    const locations = points(results);
    if (locations.length === 1) map.setView(locations[0], 14);
    else if (locations.length) map.fitBounds(locations, { paddingTopLeft: [60, 80], paddingBottomRight: [60, 80], maxZoom: 13 });
    else map.setView(initialCenter, 14);
  }
  return {
    render(data) {
      layer.clearLayers(); markers.clear();
      if (validCoordinates(data.origin)) window.L.marker(data.origin, {
        title: data.origin.name || 'Starting point', icon: window.L.divIcon({ className: 'origin-marker', html: '', iconSize: [20, 20] }),
      }).addTo(layer);
      for (const { facility, rank, approximate } of mapFacilities(data)) {
        const marker = window.L.marker(facility.location, {
          title: `${rank}. ${facility.name}${locationPrecision(facility) ? ` · ${locationPrecision(facility)}` : ''}`, keyboard: true,
          icon: window.L.divIcon({ className: `facility-marker${approximate ? ' approximate' : ''}`, html: `<span>${rank}</span>`, iconSize: [38, 38] }),
        }).addTo(layer).on('click', () => onSelect?.(facility.id));
        markers.set(facility.id, marker);
      }
      fit();
    },
    select(id, pan = true) {
      for (const [markerId, marker] of markers) marker.getElement()?.classList.toggle('active', markerId === id);
      if (pan && markers.has(id)) map.panTo(markers.get(id).getLatLng());
    },
    fit,
    destroy() { resize.disconnect(); map.remove(); },
  };
}

function createGoogleMap(mapId) {
  const { Map: GoogleMap, LatLngBounds, event, marker: { AdvancedMarkerElement } } = window.google.maps;
  const map = new GoogleMap(container, {
    center: initialCenter, zoom: 14, mapId, disableDefaultUI: true,
    zoomControl: true, gestureHandling: 'cooperative', clickableIcons: false,
  });
  const markers = new Map();
  let origin;
  const resize = new ResizeObserver(() => {
    const center = map.getCenter();
    event.trigger(map, 'resize');
    if (center) map.setCenter(center);
  });
  resize.observe(container);
  const makeContent = (className, label = '') => {
    const element = document.createElement('div'); element.className = className;
    element.textContent = label; return element;
  };
  function fit() {
    const locations = points(results);
    if (locations.length <= 1) { const point = locations[0] || initialCenter; map.setCenter({ lat: point.lat, lng: point.lng }); map.setZoom(14); return; }
    const bounds = new LatLngBounds(); locations.forEach(point => bounds.extend({ lat: point.lat, lng: point.lng }));
    map.fitBounds(bounds, { top: 80, right: 60, bottom: 80, left: 60 });
    event.addListenerOnce(map, 'idle', () => { if (map.getZoom() > 13) map.setZoom(13); });
  }
  return {
    render(data) {
      if (origin) origin.map = null;
      for (const { marker } of markers.values()) { event.clearInstanceListeners(marker); marker.map = null; }
      markers.clear();
      if (validCoordinates(data.origin)) origin = new AdvancedMarkerElement({ map, position: { lat: data.origin.lat, lng: data.origin.lng }, title: data.origin.name || 'Starting point', content: makeContent('origin-marker') });
      for (const { facility, rank, approximate } of mapFacilities(data)) {
        const content = makeContent(`facility-marker${approximate ? ' approximate' : ''}`, String(rank));
        const marker = new AdvancedMarkerElement({ map, position: { lat: facility.location.lat, lng: facility.location.lng },
          title: `${rank}. ${facility.name}${locationPrecision(facility) ? ` · ${locationPrecision(facility)}` : ''}`, content });
        marker.addListener('click', () => onSelect?.(facility.id));
        markers.set(facility.id, { marker, content });
      }
      fit();
    },
    select(id, pan = true) {
      for (const [markerId, { marker, content }] of markers) { content.classList.toggle('active', markerId === id); marker.zIndex = markerId === id ? 10 : 1; }
      if (pan && markers.has(id)) map.panTo(markers.get(id).marker.position);
    },
    fit,
    destroy() {
      resize.disconnect();
      if (origin) origin.map = null;
      for (const { marker } of markers.values()) { event.clearInstanceListeners(marker); marker.map = null; }
      event.clearInstanceListeners(map);
    },
  };
}

export function renderMap(data, select) {
  results = data; onSelect = select; selectedId = null;
  adapter?.render(data);
}

export function selectMarker(id) { selectedId = id; adapter?.select(id); }
export function fitMap() { adapter?.fit(); }
