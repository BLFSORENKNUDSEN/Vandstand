#!/usr/bin/env python3
import argparse
import json
import math
import os
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
    codes_grib_find_nearest,
    codes_grib_new_from_file,
    codes_release,
)

STAC_ITEMS_URL = "https://opendataapi.dmi.dk/v1/forecastdata/collections/dkss_idw/items"
COLLECTION = "dkss_idw"
PARAMETER_ID = 82
PARAMETER_CODE = "DSLM"
USER_AGENT = "strandvejr.dk-waterlevel/1.0"
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180

LOCATIONS = [
    {"id": "vordingborg", "name": "Vordingborg", "lat": 55.00376, "lon": 11.91587},
    {"id": "stubbekoebing", "name": "Stubbekøbing", "lat": 54.89167, "lon": 12.04667},
    {"id": "hesnaes", "name": "Hesnæs", "lat": 54.82313, "lon": 12.13815},
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


def fetch_stac_items(session: requests.Session) -> List[Dict[str, Any]]:
    params = {"limit": 1000}
    payload = get_json(session, STAC_ITEMS_URL, params=params)
    features = payload.get("features") or []
    if not features:
        raise RuntimeError("DMI STAC returnerede ingen dkss_idw items")
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
        covers_now = valid_times[0] <= now <= valid_times[-1]
        covers_horizon = valid_times[-1] >= horizon
        candidates.append((parse_dt(run), run, items, covers_now, covers_horizon, valid_times[-1]))

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
        if now - timedelta(minutes=90) <= parse_dt(item["properties"]["datetime"]) <= now + timedelta(hours=horizon_hours, minutes=90)
    ]
    if not eligible:
        raise RuntimeError("Det valgte modelrun har ingen forecasttrin i det ønskede tidsrum")

    selected: List[Dict[str, Any]] = []
    target = now
    while target <= now + timedelta(hours=horizon_hours):
        closest = min(eligible, key=lambda item: abs((parse_dt(item["properties"]["datetime"]) - target).total_seconds()))
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
    try:
        indicator = int(codes_get(gid, "indicatorOfParameter"))
        if indicator == PARAMETER_ID:
            return True
    except Exception:
        pass

    try:
        param_id = int(codes_get(gid, "paramId"))
        if param_id == PARAMETER_ID:
            return True
    except Exception:
        pass

    try:
        short_name = str(codes_get(gid, "shortName")).lower()
        if short_name == PARAMETER_CODE.lower():
            return True
    except Exception:
        pass

    return False


def extract_locations(grib_path: Path) -> Dict[str, Dict[str, float]]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not is_waterlevel_message(gid):
                    continue

                result: Dict[str, Dict[str, float]] = {}
                for location in LOCATIONS:
                    nearest = codes_grib_find_nearest(gid, location["lat"], location["lon"], npoints=1)
                    if isinstance(nearest, list):
                        point = nearest[0]
                    else:
                        point = nearest

                    if isinstance(point, dict):
                        model_lat = float(point["lat"])
                        model_lon = float(point["lon"])
                        value = float(point["value"])
                        distance_km = float(point.get("distance", 0.0))
                    else:
                        model_lat, model_lon, value, distance_km, _index = point
                        model_lat = float(model_lat)
                        model_lon = float(model_lon)
                        value = float(value)
                        distance_km = float(distance_km)

                    if not math.isfinite(value):
                        raise RuntimeError(f"Ikke numerisk vandstand for {location['name']}")

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


def build_output(session: requests.Session, horizon_hours: int, step_hours: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    features = fetch_stac_items(session)
    model_run, run_items = choose_run(features, now, horizon_hours)
    steps = select_steps(run_items, now, horizon_hours, step_hours)

    forecasts: Dict[str, List[Dict[str, Any]]] = {loc["id"]: [] for loc in LOCATIONS}
    model_points: Dict[str, Dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix="strandvejr-grib-") as tmpdir:
        tmp_path = Path(tmpdir)
        for index, item in enumerate(steps, start=1):
            valid_time = parse_dt(item["properties"]["datetime"])
            href = asset_href(item)
            destination = tmp_path / f"step-{index:02d}.grib"
            print(f"[{index}/{len(steps)}] {iso_z(valid_time)}: downloader {href}")
            download_file(session, href, destination)
            extracted = extract_locations(destination)

            for location in LOCATIONS:
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

    output_locations = []
    for location in LOCATIONS:
        loc_id = location["id"]
        output_locations.append({
            **location,
            "modelPoint": model_points.get(loc_id),
            "forecast": forecasts[loc_id],
        })

    return {
        "generated": iso_z(datetime.now(timezone.utc)),
        "source": "DMI DKSS via Forecast Data STAC API",
        "collection": COLLECTION,
        "parameter": {
            "id": PARAMETER_ID,
            "code": PARAMETER_CODE,
            "description": "Deviation of sea level from mean",
            "sourceUnit": "m",
            "outputUnit": "cm",
        },
        "modelRun": model_run,
        "stepHours": step_hours,
        "horizonHours": horizon_hours,
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
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"})

    output = build_output(session, args.hours, args.step)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Skrev {output_path} med {len(output['locations'])} lokaliteter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
