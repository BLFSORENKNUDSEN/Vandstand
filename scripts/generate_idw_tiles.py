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
from eccodes import codes_get, codes_grib_new_from_file, codes_release

BASE_PATH = Path(__file__).with_name("generate_waterlevel_map.py")
V2_PATH = Path(__file__).with_name("generate_waterlevel_map_v2.py")

base_spec = importlib.util.spec_from_file_location("strandvejr_waterlevel_base_tiles", BASE_PATH)
if base_spec is None or base_spec.loader is None:
    raise RuntimeError(f"Kunne ikke indlæse {BASE_PATH}")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)

v2_spec = importlib.util.spec_from_file_location("strandvejr_waterlevel_v2_tiles", V2_PATH)
if v2_spec is None or v2_spec.loader is None:
    raise RuntimeError(f"Kunne ikke indlæse {V2_PATH}")
v2 = importlib.util.module_from_spec(v2_spec)
v2_spec.loader.exec_module(v2)

COLLECTION = "dkss_idw"
TILE_SIZE = 256
DEFAULT_ZOOM = 9
OSM_TILE_URL = "https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf"
USER_AGENT = "strandvejr.dk-waterlevel-idw-tiles/1.0"
REQUEST_TIMEOUT = 60


def read_idw_grid(grib_path: Path) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not base.is_waterlevel_message(gid):
                    continue
                grid_type = str(codes_get(gid, "gridType"))
                if grid_type != "regular_ll":
                    raise RuntimeError(f"Forventede regular_ll for {COLLECTION}, fik {grid_type}")

                lat_grid, lon_grid, values, bitmap_masked, bitmap_present = v2.read_regular_ll(gid)
                info = {
                    "gridType": grid_type,
                    "Ni": int(codes_get(gid, "Ni")),
                    "Nj": int(codes_get(gid, "Nj")),
                    "firstLat": float(codes_get(gid, "latitudeOfFirstGridPointInDegrees")),
                    "firstLon": float(codes_get(gid, "longitudeOfFirstGridPointInDegrees")),
                    "lastLat": float(codes_get(gid, "latitudeOfLastGridPointInDegrees")),
                    "lastLon": float(codes_get(gid, "longitudeOfLastGridPointInDegrees")),
                    "bitmapPresent": bitmap_present,
                    "bitmapMaskedCells": bitmap_masked,
                }
                return lat_grid, lon_grid, values, info
            finally:
                codes_release(gid)

    raise RuntimeError(f"Fandt ikke DSLM/parameter 82 i {grib_path.name}")


def grid_bounds(lat_axis: np.ndarray, lon_axis: np.ndarray) -> List[List[float]]:
    if lat_axis.size < 2 or lon_axis.size < 2:
        raise RuntimeError("IDW gridet er for lille")
    dlat = float(np.median(np.diff(lat_axis)))
    dlon = float(np.median(np.diff(lon_axis)))
    return [
        [float(lat_axis[0] - dlat / 2.0), float(lon_axis[0] - dlon / 2.0)],
        [float(lat_axis[-1] + dlat / 2.0), float(lon_axis[-1] + dlon / 2.0)],
    ]


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


def nearest_indices(axis: np.ndarray, targets: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    insertion = np.searchsorted(axis, targets, side="left")
    hi = np.clip(insertion, 0, len(axis) - 1)
    lo = np.clip(insertion - 1, 0, len(axis) - 1)
    choose_hi = np.abs(axis[hi] - targets) < np.abs(axis[lo] - targets)
    indices = np.where(choose_hi, hi, lo)

    spacing = float(np.median(np.diff(axis)))
    minimum = float(axis[0] - spacing / 2.0)
    maximum = float(axis[-1] + spacing / 2.0)
    inside = (targets >= minimum) & (targets <= maximum)
    return indices.astype(np.int32), inside


def build_tile_samplers(
    session: requests.Session,
    tiles: List[mercantile.Tile],
    lat_axis: np.ndarray,
    lon_axis: np.ndarray,
) -> Dict[Tuple[int, int, int], Dict[str, np.ndarray]]:
    samplers: Dict[Tuple[int, int, int], Dict[str, np.ndarray]] = {}
    print(f"IDW XYZ: bygger geometri og OSM vandmasker for {len(tiles)} tiles")

    for index, tile in enumerate(tiles, start=1):
        target_lats, target_lons = tile_pixel_lonlat(tile)
        j_idx, inside_lat = nearest_indices(lat_axis, target_lats)
        i_idx, inside_lon = nearest_indices(lon_axis, target_lons)
        domain = inside_lat & inside_lon
        water = fetch_water_mask(session, tile) > 0

        samplers[(tile.z, tile.x, tile.y)] = {
            "j": j_idx,
            "i": i_idx,
            "visible": domain & water,
        }

        if index % 25 == 0 or index == len(tiles):
            print(f"IDW XYZ: geometri {index}/{len(tiles)} tiles")

    return samplers


def colorize_tile(values_m: np.ndarray, visible: np.ndarray) -> Image.Image:
    values_cm = values_m * 100.0
    valid = np.isfinite(values_cm) & visible
    rgba = np.zeros(values_cm.shape + (4,), dtype=np.uint8)

    stop_values = np.array([item[0] for item in base.COLOR_STOPS], dtype=np.float64)
    stop_colors = np.array([item[1] for item in base.COLOR_STOPS], dtype=np.float64)
    safe_values = np.where(valid, np.clip(values_cm, stop_values[0], stop_values[-1]), stop_values[0])

    for channel in range(4):
        rgba[..., channel] = np.interp(safe_values, stop_values, stop_colors[:, channel]).astype(np.uint8)
    rgba[..., 3] = np.where(valid, rgba[..., 3], 0)
    return Image.fromarray(rgba)


def save_frame_tiles(
    output_dir: Path,
    frame_index: int,
    zoom: int,
    tiles: List[mercantile.Tile],
    samplers: Dict[Tuple[int, int, int], Dict[str, np.ndarray]],
    values: np.ndarray,
) -> str:
    frame_dir_name = f"{frame_index:03d}"
    frame_root = output_dir / frame_dir_name / str(zoom)

    for tile in tiles:
        sampler = samplers[(tile.z, tile.x, tile.y)]
        sampled = values[sampler["j"], sampler["i"]]
        image = colorize_tile(sampled, sampler["visible"])

        target_dir = frame_root / str(tile.x)
        target_dir.mkdir(parents=True, exist_ok=True)
        image.save(target_dir / f"{tile.y}.webp", format="WEBP", lossless=True, method=6)

    return frame_dir_name


def main() -> int:
    parser = argparse.ArgumentParser(description="Generer dkss_idw som XYZ Web Mercator tiles")
    parser.add_argument("--output-dir", default="data/waterlevel-tiles")
    parser.add_argument("--hours", type=int, default=48)
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
    features = base.fetch_stac_items(session, COLLECTION)
    model_run, run_items = base.choose_run(features, now, args.hours)
    steps = base.select_steps(run_items, now, args.hours, args.step)

    frames = []
    bounds = None
    grid_info = None
    samplers = None
    tiles: List[mercantile.Tile] = []
    lat_axis = None
    lon_axis = None

    try:
        with tempfile.TemporaryDirectory(prefix="strandvejr-idw-tiles-") as tmpdir:
            tmp_path = Path(tmpdir)

            for frame_index, item in enumerate(steps):
                valid_time = base.parse_dt(item["properties"]["datetime"])
                grib_path = tmp_path / f"{frame_index:03d}.grib"
                print(f"[IDW XYZ {frame_index + 1}/{len(steps)}] {base.iso_z(valid_time)}")
                base.download_file(session, base.asset_href(item), grib_path)

                lat_grid, lon_grid, values, current_info = read_idw_grid(grib_path)

                if samplers is None:
                    lat_axis = lat_grid[:, 0].astype(np.float64)
                    lon_axis = lon_grid[0, :].astype(np.float64)
                    bounds = grid_bounds(lat_axis, lon_axis)
                    grid_info = current_info

                    south, west = bounds[0]
                    north, east = bounds[1]
                    tiles = list(mercantile.tiles(west, south, east, north, zooms=[args.zoom]))
                    samplers = build_tile_samplers(session, tiles, lat_axis, lon_axis)

                    print(
                        f"IDW XYZ: bounds south={south:.6f} west={west:.6f} "
                        f"north={north:.6f} east={east:.6f}; "
                        f"zoom={args.zoom}; tiles={len(tiles)}"
                    )

                frame_dir = save_frame_tiles(
                    staging_dir,
                    frame_index,
                    args.zoom,
                    tiles,
                    samplers,
                    values,
                )
                frames.append({
                    "index": frame_index,
                    "time": base.iso_z(valid_time),
                    "directory": frame_dir,
                    "tileTemplate": f"{frame_dir}/{args.zoom}/{{x}}/{{y}}.webp",
                })

        metadata = {
            "generated": base.iso_z(datetime.now(timezone.utc)),
            "source": "DMI DKSS dkss_idw via Forecast Data STAC API",
            "collection": COLLECTION,
            "modelRun": model_run,
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
            "colorStops": [
                {"cm": value, "rgba": list(color)} for value, color in base.COLOR_STOPS
            ],
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

    print(f"IDW XYZ: skrev {len(frames)} frames og {len(tiles)} tiles pr. frame til {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
