#!/usr/bin/env python3
import argparse
import json
import math
import shutil
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import requests
from PIL import Image
from scipy.spatial import cKDTree
from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release

STAC_ITEMS_URL = "https://opendataapi.dmi.dk/v1/forecastdata/collections/{collection}/items"
COLLECTIONS = ["dkss_nsbs", "dkss_idw"]
PARAMETER_ID = 82
PARAMETER_CODE = "DSLM"
USER_AGENT = "strandvejr.dk-waterlevel-map/1.0"
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180
MERCATOR_LAT_LIMIT = 85.05112878

COLOR_STOPS = [
    (-200, (36, 124, 201, 220)),
    (-120, (65, 182, 196, 220)),
    (-60, (88, 210, 192, 220)),
    (-30, (144, 226, 218, 220)),
    (0, (255, 245, 184, 210)),
    (30, (255, 224, 72, 220)),
    (60, (255, 194, 38, 225)),
    (120, (255, 128, 54, 230)),
    (180, (244, 75, 75, 235)),
    (300, (196, 44, 87, 240)),
    (480, (83, 24, 91, 245)),
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
                print(f"DMI svarede 429. Nyt forsøg om {delay} sekunder.")
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
    payload = get_json(session, STAC_ITEMS_URL.format(collection=collection), params={"limit": 1000})
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

    horizon = now + timedelta(hours=horizon_hours)
    candidates = []
    for run, items in grouped.items():
        valid_times = sorted(parse_dt(item["properties"]["datetime"]) for item in items)
        if not valid_times:
            continue
        candidates.append((
            parse_dt(run), run, items,
            valid_times[0] <= now <= valid_times[-1],
            valid_times[-1] >= horizon,
            valid_times[-1],
        ))

    fully_usable = [candidate for candidate in candidates if candidate[3] and candidate[4]]
    if fully_usable:
        chosen = max(fully_usable, key=lambda candidate: candidate[0])
        return chosen[1], chosen[2]

    usable_now = [candidate for candidate in candidates if candidate[3]]
    if usable_now:
        chosen = max(usable_now, key=lambda candidate: (candidate[5], candidate[0]))
        return chosen[1], chosen[2]

    chosen = max(candidates, key=lambda candidate: candidate[0])
    return chosen[1], chosen[2]


def select_steps(items: List[Dict[str, Any]], now: datetime, horizon_hours: int, step_hours: int) -> List[Dict[str, Any]]:
    sorted_items = sorted(items, key=lambda item: parse_dt(item["properties"]["datetime"]))
    end = now + timedelta(hours=horizon_hours)
    selected: List[Dict[str, Any]] = []
    target = now

    while target <= end:
        closest = min(sorted_items, key=lambda item: abs((parse_dt(item["properties"]["datetime"]) - target).total_seconds()))
        valid_time = parse_dt(closest["properties"]["datetime"])
        if abs((valid_time - target).total_seconds()) > 90 * 60:
            raise RuntimeError(f"Mangler forecasttrin tæt på {iso_z(target)}")
        if not selected or closest.get("id") != selected[-1].get("id"):
            selected.append(closest)
        target += timedelta(hours=step_hours)

    return selected


def asset_href(feature: Dict[str, Any]) -> str:
    assets = feature.get("asset") or feature.get("assets") or {}
    data_asset = assets.get("data") or {}
    href = data_asset.get("href")
    if not href:
        raise RuntimeError(f"STAC item mangler data href: {feature.get('id')}")
    return href


def download_file(session: requests.Session, url: str, destination: Path) -> None:
    last_error = None
    for attempt in range(4):
        try:
            with session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
                if response.status_code == 429:
                    delay = 2 ** attempt
                    print(f"GRIB download fik 429. Nyt forsøg om {delay} sekunder.")
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
    for key in ("indicatorOfParameter", "paramId"):
        try:
            if int(codes_get(gid, key)) == PARAMETER_ID:
                return True
        except Exception:
            pass
    try:
        return str(codes_get(gid, "shortName")).lower() == PARAMETER_CODE.lower()
    except Exception:
        return False


def get_missing_value(gid: int) -> Optional[float]:
    try:
        value = float(codes_get(gid, "missingValue"))
        return value if math.isfinite(value) else None
    except Exception:
        return None


def read_grid(grib_path: Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int, bool]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not is_waterlevel_message(gid):
                    continue

                triples = np.asarray(codes_get_array(gid, "latLonValues"), dtype=np.float64)
                if triples.size % 3 != 0:
                    raise RuntimeError("latLonValues har uventet længde")

                points = triples.reshape((-1, 3))
                lats = points[:, 0]
                lons = points[:, 1]
                values = points[:, 2]
                missing_value = get_missing_value(gid)

                valid = np.isfinite(values) & (np.abs(values) < 100.0)
                if missing_value is not None:
                    valid &= ~np.isclose(values, missing_value, rtol=0.0, atol=1e-9)

                bitmap_present = False
                bitmap_masked = 0
                try:
                    bitmap_present = bool(int(codes_get(gid, "bitmapPresent")))
                except Exception:
                    bitmap_present = False

                if bitmap_present:
                    bitmap = np.asarray(codes_get_array(gid, "bitmap"), dtype=np.int8)
                    if bitmap.size != values.size:
                        raise RuntimeError(
                            f"Bitmap har {bitmap.size} celler, men data har {values.size}"
                        )
                    bitmap_valid = bitmap.astype(bool)
                    bitmap_masked = int(np.count_nonzero(~bitmap_valid))
                    valid &= bitmap_valid

                values = np.where(valid, values, np.nan)

                ni = int(codes_get(gid, "Ni"))
                nj = int(codes_get(gid, "Nj"))
                if ni * nj != len(values):
                    raise RuntimeError(f"Ni*Nj ({ni}*{nj}) matcher ikke antal gridpunkter ({len(values)})")

                return (
                    lats.reshape((nj, ni)),
                    lons.reshape((nj, ni)),
                    values.reshape((nj, ni)),
                    bitmap_masked,
                    bitmap_present,
                )
            finally:
                codes_release(gid)

    raise RuntimeError(f"Fandt ikke DSLM/parameter 82 i {grib_path.name}")


def mercator_y_from_lat(lat_deg: np.ndarray) -> np.ndarray:
    lat = np.clip(lat_deg, -MERCATOR_LAT_LIMIT, MERCATOR_LAT_LIMIT)
    lat_rad = np.radians(lat)
    return np.log(np.tan(np.pi / 4.0 + lat_rad / 2.0))


def lat_from_mercator_y(y: np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(y)) - np.pi / 2.0)


def projected_xy(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    return np.column_stack((np.radians(lons).ravel(), mercator_y_from_lat(lats).ravel()))


def estimate_source_spacing(source_xy: np.ndarray) -> float:
    if len(source_xy) < 2:
        return 0.001
    tree = cKDTree(source_xy)
    distances, _ = tree.query(source_xy, k=2)
    spacing = float(np.nanmedian(distances[:, 1]))
    if not math.isfinite(spacing) or spacing <= 0:
        return 0.001
    return spacing


def resample_to_web_mercator(
    lat_grid: np.ndarray,
    lon_grid: np.ndarray,
    value_grid: np.ndarray,
) -> Tuple[np.ndarray, List[List[float]]]:
    finite_coords = np.isfinite(lat_grid) & np.isfinite(lon_grid)
    if not np.any(finite_coords):
        raise RuntimeError("DKSS gridet indeholder ingen gyldige koordinater")

    source_lats = lat_grid[finite_coords]
    source_lons = lon_grid[finite_coords]
    source_values = value_grid[finite_coords]
    source_xy = projected_xy(source_lats, source_lons)
    tree = cKDTree(source_xy)
    spacing = estimate_source_spacing(source_xy)

    source_x = source_xy[:, 0]
    source_y = source_xy[:, 1]
    west_x = float(np.nanmin(source_x))
    east_x = float(np.nanmax(source_x))
    south_y = float(np.nanmin(source_y))
    north_y = float(np.nanmax(source_y))

    height, width = value_grid.shape
    dx = (east_x - west_x) / max(width - 1, 1)
    dy = (north_y - south_y) / max(height - 1, 1)

    west_edge = west_x - dx / 2.0
    east_edge = east_x + dx / 2.0
    south_edge_y = south_y - dy / 2.0
    north_edge_y = north_y + dy / 2.0

    target_x = west_edge + (np.arange(width, dtype=np.float64) + 0.5) * ((east_edge - west_edge) / width)
    target_y = north_edge_y - (np.arange(height, dtype=np.float64) + 0.5) * ((north_edge_y - south_edge_y) / height)
    target_x_grid, target_y_grid = np.meshgrid(target_x, target_y)
    target_xy = np.column_stack((target_x_grid.ravel(), target_y_grid.ravel()))

    distances, indices = tree.query(target_xy, k=1)
    sampled = source_values[indices].reshape((height, width)).astype(np.float64)
    distance_grid = distances.reshape((height, width))
    sampled[distance_grid > spacing * 1.8] = np.nan

    south = float(lat_from_mercator_y(np.array([south_edge_y]))[0])
    north = float(lat_from_mercator_y(np.array([north_edge_y]))[0])
    west = math.degrees(west_edge)
    east = math.degrees(east_edge)
    return sampled, [[south, west], [north, east]]


def colorize(values_m: np.ndarray) -> Image.Image:
    values_cm = values_m * 100.0
    rgba = np.zeros(values_cm.shape + (4,), dtype=np.uint8)
    valid = np.isfinite(values_cm)
    if not np.any(valid):
        return Image.fromarray(rgba, mode="RGBA")

    stop_values = np.array([stop[0] for stop in COLOR_STOPS], dtype=np.float64)
    stop_colors = np.array([stop[1] for stop in COLOR_STOPS], dtype=np.float64)
    clipped = np.clip(values_cm, stop_values[0], stop_values[-1])

    for channel in range(4):
        rgba[..., channel] = np.interp(clipped, stop_values, stop_colors[:, channel]).astype(np.uint8)

    rgba[..., 3] = np.where(valid, rgba[..., 3], 0)
    return Image.fromarray(rgba, mode="RGBA")


def frame_name(index: int, collection: str) -> str:
    return f"{index:03d}-{collection}.webp"


def process_collection(
    session: requests.Session,
    collection: str,
    now: datetime,
    horizon_hours: int,
    step_hours: int,
    output_dir: Path,
) -> Dict[str, Any]:
    features = fetch_stac_items(session, collection)
    model_run, run_items = choose_run(features, now, horizon_hours)
    steps = select_steps(run_items, now, horizon_hours, step_hours)

    frames = []
    bounds = None
    first_bitmap_masked = 0
    bitmap_present = False

    with tempfile.TemporaryDirectory(prefix=f"strandvejr-map-{collection}-") as tmpdir:
        tmp_path = Path(tmpdir)
        for index, item in enumerate(steps):
            valid_time = parse_dt(item["properties"]["datetime"])
            grib_path = tmp_path / f"{index:03d}.grib"
            print(f"[{collection} {index + 1}/{len(steps)}] {iso_z(valid_time)}")
            download_file(session, asset_href(item), grib_path)
            lat_grid, lon_grid, values, bitmap_masked, frame_bitmap_present = read_grid(grib_path)

            if index == 0:
                first_bitmap_masked = bitmap_masked
                bitmap_present = frame_bitmap_present
                if frame_bitmap_present:
                    print(f"[{collection}] GRIB bitmap maskerede {bitmap_masked} celler")
                else:
                    print(f"[{collection}] GRIB indeholder ingen bitmap")

            projected_values, current_bounds = resample_to_web_mercator(lat_grid, lon_grid, values)
            if bounds is None:
                bounds = current_bounds

            image = colorize(projected_values)
            image_path = output_dir / frame_name(index, collection)
            image.save(image_path, format="WEBP", lossless=True, method=6)
            frames.append({
                "time": iso_z(valid_time),
                "image": image_path.name,
                "width": image.width,
                "height": image.height,
            })

    return {
        "collection": collection,
        "modelRun": model_run,
        "bounds": bounds,
        "bitmapPresent": bitmap_present,
        "bitmapMaskedCells": first_bitmap_masked,
        "frames": frames,
    }


def build_metadata(
    collection_results: Dict[str, Dict[str, Any]],
    generated: datetime,
    horizon_hours: int,
    step_hours: int,
) -> Dict[str, Any]:
    frame_count = min(len(result["frames"]) for result in collection_results.values())
    frames = []
    for index in range(frame_count):
        times = [parse_dt(result["frames"][index]["time"]) for result in collection_results.values()]
        frame_time = min(times)
        layers = []
        for collection in COLLECTIONS:
            result = collection_results[collection]
            frame = result["frames"][index]
            layers.append({
                "collection": collection,
                "image": frame["image"],
                "bounds": result["bounds"],
                "modelRun": result["modelRun"],
            })
        frames.append({"index": index, "time": iso_z(frame_time), "layers": layers})

    return {
        "generated": iso_z(generated),
        "source": "DMI DKSS via Forecast Data STAC API",
        "projection": "EPSG:3857 raster in Leaflet imageOverlay bounds",
        "parameter": {
            "id": PARAMETER_ID,
            "code": PARAMETER_CODE,
            "description": "Deviation of sea level from mean",
            "unit": "cm",
        },
        "horizonHours": horizon_hours,
        "stepHours": step_hours,
        "collections": [
            {
                "id": collection,
                "modelRun": collection_results[collection]["modelRun"],
                "bounds": collection_results[collection]["bounds"],
                "bitmapPresent": collection_results[collection].get("bitmapPresent", False),
                "bitmapMaskedCells": collection_results[collection].get("bitmapMaskedCells", 0),
            }
            for collection in COLLECTIONS
        ],
        "colorStops": [{"cm": value, "rgba": list(color)} for value, color in COLOR_STOPS],
        "frames": frames,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generer animeret DMI DKSS vandstandskort")
    parser.add_argument("--output-dir", default="data/waterlevel-map")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--step", type=int, default=1)
    args = parser.parse_args()

    if args.hours < 6 or args.hours > 120:
        parser.error("--hours skal være mellem 6 og 120")
    if args.step < 1 or args.step > 6:
        parser.error("--step skal være mellem 1 og 6")

    output_dir = Path(args.output_dir)
    staging_dir = output_dir.with_name(output_dir.name + ".tmp")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"})

    now = datetime.now(timezone.utc)
    results = {}
    try:
        for collection in COLLECTIONS:
            results[collection] = process_collection(session, collection, now, args.hours, args.step, staging_dir)

        metadata = build_metadata(results, datetime.now(timezone.utc), args.hours, args.step)
        (staging_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging_dir.rename(output_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise

    print(f"Skrev {len(metadata['frames'])} frames til {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
