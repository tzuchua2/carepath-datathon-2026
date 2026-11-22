"""Look up prepared facility candidates for a supported service in one city."""

from pyspark.sql import DataFrame, functions as F

from backend.geography import place_key, state_key
from backend.service_evidence import SERVICES


def find_candidates(candidates: DataFrame, service: str, state: str, city: str) -> DataFrame:
    """Filter a prepared candidate table by service and exact normalized city/state.

    Accepts the service key or its display label. Results retain all candidate
    evidence fields in a stable alphabetical order. An unmatched location
    produces an empty DataFrame with the same schema as the input.
    """
    for label, value in (("service", service), ("state", state), ("city", city)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")

    selection = service.strip().casefold()
    service_id = next(
        (key for key, label in SERVICES.items() if selection in (key.casefold(), label.casefold())),
        None,
    )
    if service_id is None:
        raise ValueError(f"Unknown service {service!r}. Supported service keys: {', '.join(SERVICES)}")

    return candidates.where(
        (F.col("service") == service_id)
        & (F.col("state_key") == state_key(F.lit(state)))
        & (F.col("city_key") == place_key(F.lit(city)))
    ).orderBy("name", "record_key")
