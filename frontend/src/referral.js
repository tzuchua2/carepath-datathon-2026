// Search boundaries belong to the backend; never forward a legacy radius.
export function referralPayload({ service, serviceLabel, location, origin, rankingMode, patient }) {
  return { service, serviceLabel, location, ...(origin ? { origin } : {}), rankingMode, patient };
}
