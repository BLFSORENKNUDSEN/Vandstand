#!/usr/bin/env python3
import argparse
import json
import math
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_find_nearest,
    codes_grib_new_from_file,
    codes_release,
)

STAC_ITEMS_URL = "https://opendataapi.dmi.dk/v1/forecastdata/collections/{collection}/items"
PARAMETER_ID = 82
PARAMETER_CODE = "DSLM"
USER_AGENT = "strandvejr.dk-waterlevel/1.0"
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180
NEAREST_CANDIDATES = 4
MAX_MODEL_DISTANCE_KM = 3.0

LOCATIONS = [
    {
        "id": "vordingborg",
        "name": "Vordingborg",
        "lat": 55.00376,
        "lon": 11.91587,
        "collections": ["dkss_idw", "dkss_nsbs"],
    },
    {
        "id": "stubbekoebing",
        "name": "Stubbekøbing",
        "lat": 54.89167,
        "lon": 12.04667,
        "collections": ["dkss_idw"],
    },
    {
        "id": "hesnaes",
        "name": "Hesnæs",
        "lat": 54.82313,
        "lon": 12.13815,
        "collections": ["dkss_idw"],
    },
]


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_json(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                print(f"DMI svarede 429. Nyt forsøg om {delay} sekunder.", file=sys.stderr)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Kunne ikke hente JSON fra DMI: {last_error}")


def fetch_stac_items(session: requests.Session, collection: str) -> List[Dict[str, Any]]:
    payload = get_json(
        session,
        STAC_ITEMS_URL.format(collection=collection),
        params={"limit": 1000},
    )
    features = payload.get("features") or []
    if not features:
        raise RuntimeError(f"DMI STAC returnerede ingen items for {collection}")
    return features


def group_runs(features: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for feature in features:
        props = feature.get("properties") or {}
        model_run = props.get("modelRun")
        valid_time = props.get("datetime")
        if model_run and valid_time:
            grouped[model_run].append(feature)
    return grouped


def choose_run(features: List[Dict[str, Any]], now: datetime, horizon_hours: int) -> Tuple[str, List[Dict[str, Any]]]:
    grouped = group_runs(features)
    if not grouped:
        raise RuntimeError("Ingen modelRun felter fundet i STAC svaret")

    candidates = []
    horizon = now + timedelta(hours=horizon_hours)
    for run, items in grouped.items():
        valid_times = sorted(parse_dt(item["properties"]["datetime"]) for item in items)
        if not valid_times:
            continue
        candidates.append((
            parse_dt(run),
            run,
            items,
            valid_times[0] <= now <= valid_times[-1],
            valid_times[-1] >= horizon,
            valid_times[-1],
        ))

    fully_usable = [c for c in candidates if c[3] and c[4]]
    if fully_usable:
        chosen = max(fully_usable, key=lambda c: c[0])
        return chosen[1], chosen[2]

    usable_now = [c for c in candidates if c[3]]
    if usable_now:
        chosen = max(usable_now, key=lambda c: (c[5], c[0]))
        print(
            f"Advarsel: intet run dækker hele {horizon_hours} timers horisonten. "
            f"Bruger {chosen[1]} med data til {iso_z(chosen[5])}.",
            file=sys.stderr,
        )
        return chosen[1], chosen[2]

    chosen = max(candidates, key=lambda c: c[0])
    return chosen[1], chosen[2]


def asset_href(feature: Dict[str, Any]) -> str:
    assets = feature.get("asset") or feature.get("assets") or {}
    data_asset = assets.get("data") or {}
    href = data_asset.get("href")
    if not href:
        raise RuntimeError(f"STAC item mangler data href: {feature.get('id')}")
    return href


def select_steps(items: List[Dict[str, Any]], now: datetime, horizon_hours: int, step_hours: int) -> List[Dict[str, Any]]:
    sorted_items = sorted(items, key=lambda item: parse_dt(item["properties"]["datetime"]))
    eligible = [
        item for item in sorted_items
        if now - timedelta(minutes=90)
        <= parse_dt(item["properties"]["datetime"])
        <= now + timedelta(hours=horizon_hours, minutes=90)
    ]
    if not eligible:
        raise RuntimeError("Det valgte modelrun har ingen forecasttrin i det ønskede tidsrum")

    selected: List[Dict[str, Any]] = []
    target = now
    while target <= now + timedelta(hours=horizon_hours):
        closest = min(
            eligible,
            key=lambda item: abs((parse_dt(item["properties"]["datetime"]) - target).total_seconds()),
        )
        if not selected or closest.get("id") != selected[-1].get("id"):
            selected.append(closest)
        target += timedelta(hours=step_hours)
    return selected


def download_file(session: requests.Session, url: str, destination: Path) -> None:
    last_error = None
    for attempt in range(4):
        try:
            with session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
                if response.status_code == 429:
                    delay = 2 ** attempt
                    print(f"GRIB download fik 429. Nyt forsøg om {delay} sekunder.", file=sys.stderr)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            if destination.stat().st_size == 0:
                raise RuntimeError("Downloadet GRIB fil er tom")
            return
        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Kunne ikke downloade GRIB fil: {last_error}")


def is_waterlevel_message(gid: int) -> bool:
    for key, expected in (("indicatorOfParameter", PARAMETER_ID), ("paramId", PARAMETER_ID)):
        try:
            if int(codes_get(gid, key)) == expected:
                return True
        except Exception:
            pass
    try:
        return str(codes_get(gid, "shortName")).lower() == PARAMETER_CODE.lower()
    except Exception:
        return False


def normalize_nearest_result(nearest: Any) -> Tuple[float, float, float, float, int]:
    if all(hasattr(nearest, key) for key in ("lat", "lon", "value", "distance", "index")):
        return (
            float(nearest.lat),
            float(nearest.lon),
            float(nearest.value),
            float(nearest.distance),
            int(nearest.index),
        )
    if isinstance(nearest, dict):
        return (
            float(nearest["lat"]),
            float(nearest["lon"]),
            float(nearest["value"]),
            float(nearest.get("distance", 0.0)),
            int(nearest.get("index", -1)),
        )
    if isinstance(nearest, (list, tuple)):
        if len(nearest) == 1:
            return normalize_nearest_result(nearest[0])
        if len(nearest) >= 5 and not isinstance(nearest[0], (list, tuple, dict)):
            try:
                return (
                    float(nearest[0]),
                    float(nearest[1]),
                    float(nearest[2]),
                    float(nearest[3]),
                    int(nearest[4]),
                )
            except (TypeError, ValueError):
                pass
        if nearest:
            return normalize_nearest_result(nearest[0])
    raise RuntimeError(
        f"Ukendt returformat fra codes_grib_find_nearest: {type(nearest).__name__}: {nearest!r}"
    )


def get_missing_value(gid: int) -> Optional[float]:
    try:
        value = float(codes_get(gid, "missingValue"))
        return value if math.isfinite(value) else None
    except Exception:
        return None


def is_valid_waterlevel_value(value: float, missing_value: Optional[float] = None) -> bool:
    if not math.isfinite(value):
        return False
    if missing_value is not None and math.isclose(value, missing_value, rel_tol=0.0, abs_tol=1e-9):
        return False
    return abs(value) < 100.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_valid_from_full_grid(gid: int, lat: float, lon: float) -> Tuple[float, float, float, float, int]:
    lat_lon_values = codes_get_array(gid, "latLonValues")
    missing_value = get_missing_value(gid)
    best = None
    best_distance = float("inf")
    point_index = 0

    for offset in range(0, len(lat_lon_values), 3):
        point_lat = float(lat_lon_values[offset])
        point_lon = float(lat_lon_values[offset + 1])
        value = float(lat_lon_values[offset + 2])
        if not is_valid_waterlevel_value(value, missing_value):
            point_index += 1
            continue
        distance_km = haversine_km(lat, lon, point_lat, point_lon)
        if distance_km < best_distance:
            best_distance = distance_km
            best = (point_lat, point_lon, value, distance_km, point_index)
        point_index += 1

    if best is None:
        raise RuntimeError(f"DKSS gridet indeholder ingen gyldige vandstandspunkter nær {lat:.5f},{lon:.5f}")
    return best


def nearest_valid_point(gid: int, lat: float, lon: float) -> Tuple[float, float, float, float, int]:
    nearest = codes_grib_find_nearest(gid, lat, lon, npoints=NEAREST_CANDIDATES)
    candidates = nearest if isinstance(nearest, (list, tuple)) else [nearest]
    missing_value = get_missing_value(gid)
    valid = []

    for candidate in candidates:
        point = normalize_nearest_result(candidate)
        if is_valid_waterlevel_value(point[2], missing_value):
            valid.append(point)

    if valid:
        return min(valid, key=lambda point: point[3])

    return nearest_valid_from_full_grid(gid, lat, lon)


def extract_locations(grib_path: Path, locations: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not is_waterlevel_message(gid):
                    continue
                result: Dict[str, Dict[str, float]] = {}
                for location in locations:
                    model_lat, model_lon, value, distance_km, _index = nearest_valid_point(
                        gid, location["lat"], location["lon"]
                    )
                    result[location["id"]] = {
                        "modelLat": model_lat,
                        "modelLon": model_lon,
                        "distanceKm": round(distance_km, 3),
                        "levelCm": int(round(value * 100)),
                    }
                return result
            finally:
                codes_release(gid)
    raise RuntimeError(f"Fandt ikke DSLM/parameter 82 i {grib_path.name}")


def process_collection(
    session: requests.Session,
    collection: str,
    locations: List[Dict[str, Any]],
    now: datetime,
    horizon_hours: int,
    step_hours: int,
) -> Dict[str, Any]:
    features = fetch_stac_items(session, collection)
    model_run, run_items = choose_run(features, now, horizon_hours)
    steps = select_steps(run_items, now, horizon_hours, step_hours)

    forecasts: Dict[str, List[Dict[str, Any]]] = {loc["id"]: [] for loc in locations}
    model_points: Dict[str, Dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix=f"strandvejr-{collection}-") as tmpdir:
        tmp_path = Path(tmpdir)
        for index, item in enumerate(steps, start=1):
            valid_time = parse_dt(item["properties"]["datetime"])
            href = asset_href(item)
            destination = tmp_path / f"step-{index:02d}.grib"
            print(f"[{collection} {index}/{len(steps)}] {iso_z(valid_time)}: downloader {href}")
            download_file(session, href, destination)
            extracted = extract_locations(destination, locations)

            for location in locations:
                loc_id = location["id"]
                point = extracted[loc_id]
                forecasts[loc_id].append({
                    "time": iso_z(valid_time),
                    "levelCm": point["levelCm"],
                })
                model_points[loc_id] = {
                    "lat": point["modelLat"],
                    "lon": point["modelLon"],
                    "distanceKm": point["distanceKm"],
                }

    return {
        "collection": collection,
        "modelRun": model_run,
        "forecasts": forecasts,
        "modelPoints": model_points,
    }


def build_output(session: requests.Session, horizon_hours: int, step_hours: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    needed_collections = sorted({collection for loc in LOCATIONS for collection in loc["collections"]})
    collection_results: Dict[str, Dict[str, Any]] = {}

    for collection in needed_collections:
        locations = [loc for loc in LOCATIONS if collection in loc["collections"]]
        collection_results[collection] = process_collection(
            session,
            collection,
            locations,
            now,
            horizon_hours,
            step_hours,
        )

    output_locations = []
    for location in LOCATIONS:
        candidates = []
        for collection in location["collections"]:
            result = collection_results[collection]
            model_point = result["modelPoints"].get(location["id"])
            forecast = result["forecasts"].get(location["id"], [])
            if model_point and forecast:
                candidates.append({
                    "collection": collection,
                    "modelRun": result["modelRun"],
                    "modelPoint": model_point,
                    "forecast": forecast,
                })

        if not candidates:
            output_locations.append({
                "id": location["id"],
                "name": location["name"],
                "lat": location["lat"],
                "lon": location["lon"],
                "available": False,
                "quality": "unavailable",
                "reason": "Ingen gyldig DKSS modelprognose fundet",
                "forecast": [],
            })
            continue

        best = min(candidates, key=lambda item: item["modelPoint"]["distanceKm"])
        distance_km = best["modelPoint"]["distanceKm"]
        available = distance_km <= MAX_MODEL_DISTANCE_KM

        output_locations.append({
            "id": location["id"],
            "name": location["name"],
            "lat": location["lat"],
            "lon": location["lon"],
            "available": available,
            "quality": "good" if available else "insufficient",
            "collection": best["collection"],
            "modelRun": best["modelRun"],
            "modelPoint": best["modelPoint"],
            "candidateModels": [
                {
                    "collection": candidate["collection"],
                    "modelRun": candidate["modelRun"],
                    "distanceKm": candidate["modelPoint"]["distanceKm"],
                    "modelPoint": {
                        "lat": candidate["modelPoint"]["lat"],
                        "lon": candidate["modelPoint"]["lon"],
                    },
                }
                for candidate in sorted(candidates, key=lambda item: item["modelPoint"]["distanceKm"])
            ],
            "forecast": best["forecast"] if available else [],
            "reason": None if available else f"Nærmeste gyldige modelpunkt er {distance_km:.3f} km væk",
        })

    return {
        "generated": iso_z(datetime.now(timezone.utc)),
        "source": "DMI DKSS via Forecast Data STAC API",
        "parameter": {
            "id": PARAMETER_ID,
            "code": PARAMETER_CODE,
            "description": "Deviation of sea level from mean",
            "sourceUnit": "m",
            "outputUnit": "cm",
        },
        "stepHours": step_hours,
        "horizonHours": horizon_hours,
        "maxModelDistanceKm": MAX_MODEL_DISTANCE_KM,
        "locations": output_locations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hent DMI DKSS vandstandsprognose til Strandvejr")
    parser.add_argument("--output", default="data/waterlevel.json")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--step", type=int, default=3)
    args = parser.parse_args()

    if args.hours < 3 or args.hours > 120:
        parser.error("--hours skal være mellem 3 og 120")
    if args.step < 1 or args.step > 24:
        parser.error("--step skal være mellem 1 og 24")

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json, application/json",
    })

    output = build_output(session, args.hours, args.step)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Skrev {output_path} med {len(output['locations'])} lokaliteter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
