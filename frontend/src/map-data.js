export function validCoordinates(point) {
  return Boolean(point && Number.isFinite(point.lat) && Number.isFinite(point.lng)
    && Math.abs(point.lat) <= 90 && Math.abs(point.lng) <= 180);
}

export function mapFacilities(data) {
  // Preserve recommendation numbers when some records have no usable point.
  return (data?.facilities || []).flatMap((facility, index) => validCoordinates(facility.location)
    ? [{ facility, rank: index + 1, approximate: ['pin', 'city'].includes(facility.location.precision) }]
    : []);
}

export function locationPrecision(item) {
  if (!validCoordinates(item.location)) return 'Location not mapped';
  if (item.location.precision === 'demo') return '';
  return ({ facility: 'Reported facility point', pin: 'Approximate PIN area', city: 'Approximate city location' })[item.location.precision]
    || 'Location precision unknown';
}
