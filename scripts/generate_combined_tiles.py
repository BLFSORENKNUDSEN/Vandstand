#!/usr/bin/env python3
import argparse
import importlib.util
import json
import math
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import mapbox_vector_tile
import mercantile
import numpy as np
import requests
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree
from eccodes import codes_get, codes_grib_new_from_file, codes_release

BASE_PATH = Path(__file__).with_name("generate_waterlevel_map.py")
V2_PATH = Path(__file__).with_name("generate_waterlevel_map_v2.py")

base_spec = importlib.util.spec_from_file_location("strandvejr_waterlevel_base_combined", BASE_PATH)
if base_spec is None or base_spec.loader is None:
    raise RuntimeError(f"Kunne ikke indlæse {BASE_PATH}")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)

v2_spec = importlib.util.spec_from_file_location("strandvejr_waterlevel_v2_combined", V2_PATH)
if v2_spec is None or v2_spec.loader is None:
    raise RuntimeError(f"Kunne ikke indlæse {V2_PATH}")
v2 = importlib.util.module_from_spec(v2_spec)
v2_spec.loader.exec_module(v2)

COLLECTION_IDW = "dkss_idw"
COLLECTION_NSBS = "dkss_nsbs"
TILE_SIZE = 256
DEFAULT_ZOOM = 9
OSM_TILE_URL = "https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf"
USER_AGENT = "strandvejr.dk-waterlevel-combined-tiles/1.0"
REQUEST_TIMEOUT = 60
MIN_GAP_NEIGHBORS = 5
MIN_BILINEAR_WEIGHT = 0.45
HIGH_WATER_THRESHOLD_CM = 75.0

# DMI vandstandsskala aflæst fra DMI kortet. Værdier i cm, farver som RGBA.
DMI_COLOR_STOPS = [
    (-200, (0, 143, 233, 255)),
    (-180, (61, 171, 238, 255)),
    (-120, (109, 191, 242, 255)),
    (-90, (22, 225, 204, 255)),
    (-60, (125, 238, 226, 255)),
    (-30, (158, 242, 233, 255)),
    (0, (255, 236, 127, 255)),
    (30, (255, 217, 0, 255)),
    (60, (255, 178, 0, 255)),
    (90, (255, 142, 82, 255)),
    (120, (255, 181, 181, 255)),
    (150, (255, 157, 157, 255)),
    (180, (255, 124, 124, 255)),
    (240, (255, 82, 82, 255)),
    (300, (230, 57, 57, 255)),
    (360, (204, 31, 31, 255)),
    (420, (128, 0, 0, 255)),
    (480, (92, 0, 51, 255)),
]

DMI_UNDER_COLOR = (71, 96, 160, 255)


def read_model_grid(grib_path: Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not base.is_waterlevel_message(gid):
                    continue

                grid_type = str(codes_get(gid, "gridType"))
                if grid_type == "regular_ll":
                    lat_grid, lon_grid, values, bitmap_masked, bitmap_present = v2.read_regular_ll(gid)
                else:
                    # Fallback til ecCodes iterator for eventuelle andre grids.
                    triples = np.asarray(base.codes_get_array(gid, "latLonValues"), dtype=np.float64)
                    points = triples.reshape((-1, 3))
                    ni = int(codes_get(gid, "Ni"))
                    nj = int(codes_get(gid, "Nj"))
                    lat_grid = points[:, 0].reshape((nj, ni))
                    lon_grid = points[:, 1].reshape((nj, ni))
                    values = points[:, 2].reshape((nj, ni))
                    bitmap_present = False
                    bitmap_masked = 0

                info = {
                    "gridType": grid_type,
                    "Ni": int(codes_get(gid, "Ni")),
                    "Nj": int(codes_get(gid, "Nj")),
                    "bitmapPresent": bitmap_present,
                    "bitmapMaskedCells": bitmap_masked,
                }
                return lat_grid, lon_grid, values, info
            finally:
                codes_release(gid)

    raise RuntimeError(f"Fandt ikke DSLM/parameter 82 i {grib_path.name}")


def grid_bounds(lat_grid: np.ndarray, lon_grid: np.ndarray) -> List[List[float]]:
    valid = np.isfinite(lat_grid) & np.isfinite(lon_grid)
    lats = lat_grid[valid]
    lons = lon_grid[valid]
    return [[float(np.min(lats)), float(np.min(lons))], [float(np.max(lats)), float(np.max(lons))]]


def tile_pixel_lonlat(tile: mercantile.Tile) -> Tuple[np.ndarray, np.ndarray]:
    scale = 2 ** tile.z
    px = np.arange(TILE_SIZE, dtype=np.float64) + 0.5
    py = np.arange(TILE_SIZE, dtype=np.float64) + 0.5
    world_x = (tile.x + px / TILE_SIZE) / scale
    world_y = (tile.y + py / TILE_SIZE) / scale
    lons = world_x * 360.0 - 180.0
    merc_n = math.pi * (1.0 - 2.0 * world_y)
    lats = np.degrees(np.arctan(np.sinh(merc_n)))
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    return lat_grid, lon_grid


def draw_ring(draw: ImageDraw.ImageDraw, ring: List[List[float]], extent: int, fill: int) -> None:
    points = []
    for coord in ring:
        x = float(coord[0]) / extent * TILE_SIZE
        y = (extent - float(coord[1])) / extent * TILE_SIZE
        points.append((x, y))
    if len(points) >= 3:
        draw.polygon(points, fill=fill)


def draw_polygon(draw: ImageDraw.ImageDraw, polygon: List[List[List[float]]], extent: int) -> None:
    if not polygon:
        return
    draw_ring(draw, polygon[0], extent, 255)
    for hole in polygon[1:]:
        draw_ring(draw, hole, extent, 0)


def fetch_water_mask(session: requests.Session, tile: mercantile.Tile) -> np.ndarray:
    url = OSM_TILE_URL.format(z=tile.z, x=tile.x, y=tile.y)
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    decoded = mapbox_vector_tile.decode(response.content)
    layer = decoded.get("water")

    mask = Image.new("L", (TILE_SIZE, TILE_SIZE), 0)
    if not layer:
        return np.asarray(mask, dtype=np.uint8)

    draw = ImageDraw.Draw(mask)
    extent = int(layer.get("extent", 4096))
    for feature in layer.get("features", []):
        geometry = feature.get("geometry") or {}
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates") or []
        if geometry_type == "Polygon":
            draw_polygon(draw, coordinates, extent)
        elif geometry_type == "MultiPolygon":
            for polygon in coordinates:
                draw_polygon(draw, polygon, extent)

    return np.asarray(mask, dtype=np.uint8)


def fill_isolated_gaps(values: np.ndarray) -> Tuple[np.ndarray, int]:
    source = values.astype(np.float64, copy=True)
    missing = ~np.isfinite(source)
    if not np.any(missing):
        return source, 0

    padded = np.pad(source, 1, mode="constant", constant_values=np.nan)
    neighbors = []
    for dy in range(3):
        for dx in range(3):
            if dy == 1 and dx == 1:
                continue
            neighbors.append(padded[dy:dy + source.shape[0], dx:dx + source.shape[1]])

    stack = np.stack(neighbors, axis=0)
    valid_count = np.sum(np.isfinite(stack), axis=0)
    sums = np.nansum(stack, axis=0)
    fill_mask = missing & (valid_count >= MIN_GAP_NEIGHBORS)
    source[fill_mask] = sums[fill_mask] / valid_count[fill_mask]
    return source, int(np.count_nonzero(fill_mask))


def axis_fractional_indices(axis: np.ndarray, targets: np.ndarray):
    insertion = np.searchsorted(axis, targets, side="right")
    hi = np.clip(insertion, 1, len(axis) - 1)
    lo = hi - 1
    low_values = axis[lo]
    high_values = axis[hi]
    denominator = high_values - low_values
    fraction = np.divide(
        targets - low_values,
        denominator,
        out=np.zeros_like(targets, dtype=np.float64),
        where=np.abs(denominator) > 1e-15,
    )
    fraction = np.clip(fraction, 0.0, 1.0)
    spacing = float(np.median(np.diff(axis)))
    inside = (targets >= axis[0] - spacing / 2.0) & (targets <= axis[-1] + spacing / 2.0)
    return lo.astype(np.int32), hi.astype(np.int32), fraction, inside


def is_regular_separable(lat_grid: np.ndarray, lon_grid: np.ndarray) -> bool:
    return (
        lat_grid.ndim == 2
        and lon_grid.ndim == 2
        and np.allclose(lat_grid, lat_grid[:, :1], equal_nan=True)
        and np.allclose(lon_grid, lon_grid[:1, :], equal_nan=True)
    )


def prepare_model_geometry(lat_grid: np.ndarray, lon_grid: np.ndarray) -> Dict[str, Any]:
    if is_regular_separable(lat_grid, lon_grid):
        lat_axis = lat_grid[:, 0].astype(np.float64)
        lon_axis = lon_grid[0, :].astype(np.float64)
        if lat_axis[0] > lat_axis[-1]:
            lat_axis = lat_axis[::-1]
        if lon_axis[0] > lon_axis[-1]:
            lon_axis = lon_axis[::-1]
        return {"mode": "bilinear", "latAxis": lat_axis, "lonAxis": lon_axis}

    valid = np.isfinite(lat_grid) & np.isfinite(lon_grid)
    points = np.column_stack((lon_grid[valid], lat_grid[valid]))
    return {
        "mode": "nearest",
        "coordMask": valid,
        "tree": cKDTree(points),
        "points": points,
    }


def build_sampler_for_tile(model_geom: Dict[str, Any], target_lats: np.ndarray, target_lons: np.ndarray):
    if model_geom["mode"] == "bilinear":
        j0, j1, fy, inside_lat = axis_fractional_indices(model_geom["latAxis"], target_lats)
        i0, i1, fx, inside_lon = axis_fractional_indices(model_geom["lonAxis"], target_lons)
        return {
            "mode": "bilinear",
            "j0": j0, "j1": j1, "i0": i0, "i1": i1,
            "fx": fx, "fy": fy,
            "inside": inside_lat & inside_lon,
        }

    targets = np.column_stack((target_lons.ravel(), target_lats.ravel()))
    distance, index = model_geom["tree"].query(targets, k=1)
    index = index.reshape(target_lats.shape)
    distance = distance.reshape(target_lats.shape)
    return {
        "mode": "nearest",
        "index": index,
        "inside": distance < 0.08,
    }


def bilinear_sample(values: np.ndarray, sampler: Dict[str, np.ndarray]) -> np.ndarray:
    j0, j1 = sampler["j0"], sampler["j1"]
    i0, i1 = sampler["i0"], sampler["i1"]
    fx, fy = sampler["fx"], sampler["fy"]

    v00 = values[j0, i0]
    v10 = values[j0, i1]
    v01 = values[j1, i0]
    v11 = values[j1, i1]
    w00 = (1.0 - fx) * (1.0 - fy)
    w10 = fx * (1.0 - fy)
    w01 = (1.0 - fx) * fy
    w11 = fx * fy

    numerator = np.zeros_like(fx, dtype=np.float64)
    weight_sum = np.zeros_like(fx, dtype=np.float64)
    valid_neighbors = np.zeros_like(fx, dtype=np.uint8)

    for sample, weight in ((v00, w00), (v10, w10), (v01, w01), (v11, w11)):
        valid = np.isfinite(sample)
        numerator += np.where(valid, sample * weight, 0.0)
        weight_sum += np.where(valid, weight, 0.0)
        valid_neighbors += valid.astype(np.uint8)

    usable = sampler["inside"] & (valid_neighbors >= 2) & (weight_sum >= MIN_BILINEAR_WEIGHT)
    result = np.full_like(fx, np.nan, dtype=np.float64)
    result[usable] = numerator[usable] / weight_sum[usable]
    return result


def sample_model(values: np.ndarray, model_geom: Dict[str, Any], sampler: Dict[str, Any]) -> np.ndarray:
    if sampler["mode"] == "bilinear":
        return bilinear_sample(values, sampler)

    flat_values = values[model_geom["coordMask"]]
    sampled = flat_values[sampler["index"]]
    return np.where(sampler["inside"] & np.isfinite(sampled), sampled, np.nan)


def colorize(values_m: np.ndarray, water_mask: np.ndarray) -> Image.Image:
    values_cm = values_m * 100.0
    valid = np.isfinite(values_cm) & water_mask
    rgba = np.zeros(values_cm.shape + (4,), dtype=np.uint8)

    stop_values = np.array([item[0] for item in DMI_COLOR_STOPS], dtype=np.float64)
    stop_colors = np.array([item[1] for item in DMI_COLOR_STOPS], dtype=np.float64)
    safe = np.where(valid, np.clip(values_cm, stop_values[0], stop_values[-1]), stop_values[0])

    for channel in range(4):
        rgba[..., channel] = np.interp(safe, stop_values, stop_colors[:, channel]).astype(np.uint8)

    under = valid & (values_cm < -200.0)
    if np.any(under):
        rgba[under] = np.array(DMI_UNDER_COLOR, dtype=np.uint8)

    rgba[..., 3] = np.where(valid, 235, 0).astype(np.uint8)
    return Image.fromarray(rgba)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generer kombinerede DKSS XYZ tiles")
    parser.add_argument("--output-dir", default="data/waterlevel-tiles")
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    staging_dir = output_dir.with_name(output_dir.name + ".tmp")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    now = datetime.now(timezone.utc)

    idw_features = base.fetch_stac_items(session, COLLECTION_IDW)
    nsbs_features = base.fetch_stac_items(session, COLLECTION_NSBS)
    idw_run, idw_items = base.choose_run(idw_features, now, args.hours)
    nsbs_run, nsbs_items = base.choose_run(nsbs_features, now, args.hours)
    idw_steps = base.select_steps(idw_items, now, args.hours, args.step)
    nsbs_steps = base.select_steps(nsbs_items, now, args.hours, args.step)

    if len(idw_steps) != len(nsbs_steps):
        raise RuntimeError("IDW og NSBS har forskelligt antal forecasttrin")

    frames = []
    tiles: List[mercantile.Tile] = []
    tile_geometry = {}
    idw_geom = None
    nsbs_geom = None
    bounds = None
    grid_info = {}

    try:
        with tempfile.TemporaryDirectory(prefix="strandvejr-combined-tiles-") as tmpdir:
            tmp = Path(tmpdir)

            for frame_index, (idw_item, nsbs_item) in enumerate(zip(idw_steps, nsbs_steps)):
                idw_time = base.parse_dt(idw_item["properties"]["datetime"])
                nsbs_time = base.parse_dt(nsbs_item["properties"]["datetime"])
                if abs((idw_time - nsbs_time).total_seconds()) > 60:
                    raise RuntimeError(
                        f"Forecasttider matcher ikke: IDW {base.iso_z(idw_time)} NSBS {base.iso_z(nsbs_time)}"
                    )

                print(f"[DKSS COMBINED {frame_index + 1}/{len(idw_steps)}] {base.iso_z(idw_time)}")

                idw_path = tmp / f"idw-{frame_index:03d}.grib"
                nsbs_path = tmp / f"nsbs-{frame_index:03d}.grib"
                base.download_file(session, base.asset_href(idw_item), idw_path)
                base.download_file(session, base.asset_href(nsbs_item), nsbs_path)

                idw_lat, idw_lon, idw_values, idw_info = read_model_grid(idw_path)
                nsbs_lat, nsbs_lon, nsbs_values, nsbs_info = read_model_grid(nsbs_path)

                idw_values, idw_filled = fill_isolated_gaps(idw_values)
                nsbs_values, nsbs_filled = fill_isolated_gaps(nsbs_values)

                if idw_geom is None:
                    idw_geom = prepare_model_geometry(idw_lat, idw_lon)
                    nsbs_geom = prepare_model_geometry(nsbs_lat, nsbs_lon)
                    bounds = grid_bounds(idw_lat, idw_lon)
                    grid_info = {"idw": idw_info, "nsbs": nsbs_info}

                    south, west = bounds[0]
                    north, east = bounds[1]
                    tiles = list(mercantile.tiles(west, south, east, north, zooms=[args.zoom]))
                    print(
                        f"DKSS COMBINED: output bounds south={south:.6f} west={west:.6f} "
                        f"north={north:.6f} east={east:.6f}; tiles={len(tiles)}"
                    )

                    for n, tile in enumerate(tiles, start=1):
                        target_lats, target_lons = tile_pixel_lonlat(tile)
                        tile_geometry[(tile.z, tile.x, tile.y)] = {
                            "idw": build_sampler_for_tile(idw_geom, target_lats, target_lons),
                            "nsbs": build_sampler_for_tile(nsbs_geom, target_lats, target_lons),
                            "water": fetch_water_mask(session, tile) > 0,
                        }
                        if n % 25 == 0 or n == len(tiles):
                            print(f"DKSS COMBINED: geometri {n}/{len(tiles)} tiles")

                frame_dir = f"{frame_index:03d}"
                frame_root = staging_dir / frame_dir / str(args.zoom)
                idw_pixel_count = 0
                nsbs_pixel_count = 0
                frame_peak_cm = None

                for tile in tiles:
                    geom = tile_geometry[(tile.z, tile.x, tile.y)]
                    nsbs_sample = sample_model(nsbs_values, nsbs_geom, geom["nsbs"])
                    idw_sample = sample_model(idw_values, idw_geom, geom["idw"])

                    combined = nsbs_sample.copy()
                    idw_valid = np.isfinite(idw_sample)
                    combined[idw_valid] = idw_sample[idw_valid]

                    water = geom["water"]
                    idw_pixel_count += int(np.count_nonzero(idw_valid & water))
                    nsbs_pixel_count += int(np.count_nonzero(~idw_valid & np.isfinite(nsbs_sample) & water))

                    peak_mask = water & np.isfinite(combined)
                    if np.any(peak_mask):
                        tile_peak_cm = float(np.nanmax(combined[peak_mask]) * 100.0)
                        if frame_peak_cm is None or tile_peak_cm > frame_peak_cm:
                            frame_peak_cm = tile_peak_cm

                    image = colorize(combined, water)
                    target_dir = frame_root / str(tile.x)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    image.save(target_dir / f"{tile.y}.webp", format="WEBP", lossless=True, method=6)

                print(
                    f"[DKSS COMBINED {frame_index + 1}/{len(idw_steps)}] "
                    f"IDW pixels={idw_pixel_count} NSBS fallback pixels={nsbs_pixel_count} "
                    f"gapfill IDW={idw_filled} NSBS={nsbs_filled}"
                )

                frames.append({
                    "index": frame_index,
                    "time": base.iso_z(idw_time),
                    "directory": frame_dir,
                    "tileTemplate": f"{frame_dir}/{args.zoom}/{{x}}/{{y}}.webp",
                    "idwPixels": idw_pixel_count,
                    "nsbsFallbackPixels": nsbs_pixel_count,
                    "maxWaterLevelCm": (
                        round(frame_peak_cm, 1) if frame_peak_cm is not None else None
                    ),
                })

        peak_frames = [
            frame for frame in frames
            if frame.get("maxWaterLevelCm") is not None
        ]
        peak_frame = max(
            peak_frames,
            key=lambda frame: frame["maxWaterLevelCm"],
        ) if peak_frames else None

        peak_cm = peak_frame["maxWaterLevelCm"] if peak_frame else None
        high_water_alert = {
            "thresholdCm": HIGH_WATER_THRESHOLD_CM,
            "active": bool(
                peak_cm is not None and peak_cm > HIGH_WATER_THRESHOLD_CM
            ),
            "peakCm": peak_cm,
            "peakTime": peak_frame["time"] if peak_frame else None,
        }

        metadata = {
            "generated": base.iso_z(datetime.now(timezone.utc)),
            "source": "DMI DKSS combined dkss_idw + dkss_nsbs via Forecast Data STAC API",
            "collection": "dkss_combined",
            "modelRuns": {"dkss_idw": idw_run, "dkss_nsbs": nsbs_run},
            "priority": ["dkss_idw", "dkss_nsbs"],
            "projection": "EPSG:3857 XYZ",
            "tileSize": TILE_SIZE,
            "nativeZoom": args.zoom,
            "bounds": bounds,
            "grid": grid_info,
            "parameter": {
                "id": base.PARAMETER_ID,
                "code": base.PARAMETER_CODE,
                "description": "Deviation of sea level from mean",
                "unit": "cm",
            },
            "horizonHours": args.hours,
            "stepHours": args.step,
            "highWaterAlert": high_water_alert,
            "colorStops": [
                {"cm": value, "rgba": list(color)} for value, color in DMI_COLOR_STOPS
            ],
            "underColor": {
                "belowCm": -200,
                "rgba": list(DMI_UNDER_COLOR),
            },
            "sampling": {
                "idwPriority": True,
                "nsbsFallback": True,
                "method": "bilinear on regular_ll, nearest fallback on irregular grids",
                "osmWaterMask": True,
            },
            "frames": frames,
        }

        (staging_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging_dir.rename(output_dir)

    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise

    print(
        f"DKSS COMBINED: skrev {len(frames)} frames og {len(tiles)} tiles pr. frame til {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
