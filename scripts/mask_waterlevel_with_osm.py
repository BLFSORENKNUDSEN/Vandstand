#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import mapbox_vector_tile
import mercantile
import numpy as np
import requests
from PIL import Image, ImageDraw

TILE_URL = "https://tiles.openfreemap.org/planet/latest/{z}/{x}/{y}.pbf"
USER_AGENT = "strandvejr.dk-water-mask/1.0"
DEFAULT_ZOOM = 9
REQUEST_TIMEOUT = 60


def mercator_normalized(lon: float, lat: float) -> Tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0
    return x, y


def target_mapper(bounds: List[List[float]], width: int, height: int):
    south, west = bounds[0]
    north, east = bounds[1]
    west_x, north_y = mercator_normalized(west, north)
    east_x, south_y = mercator_normalized(east, south)

    def to_pixel(world_x: float, world_y: float) -> Tuple[float, float]:
        px = (world_x - west_x) / (east_x - west_x) * width
        py = (world_y - north_y) / (south_y - north_y) * height
        return px, py

    return to_pixel


def draw_ring(
    draw: ImageDraw.ImageDraw,
    ring: Iterable[Iterable[float]],
    tile: mercantile.Tile,
    extent: int,
    to_pixel,
    fill: int,
) -> None:
    scale = 2 ** tile.z
    points = []
    for coord in ring:
        local_x, local_y = float(coord[0]), float(coord[1])
        world_x = (tile.x + local_x / extent) / scale
        world_y = (tile.y + local_y / extent) / scale
        points.append(to_pixel(world_x, world_y))
    if len(points) >= 3:
        draw.polygon(points, fill=fill)


def draw_polygon(
    draw: ImageDraw.ImageDraw,
    polygon: List[List[List[float]]],
    tile: mercantile.Tile,
    extent: int,
    to_pixel,
) -> None:
    if not polygon:
        return
    draw_ring(draw, polygon[0], tile, extent, to_pixel, 255)
    for hole in polygon[1:]:
        draw_ring(draw, hole, tile, extent, to_pixel, 0)


def draw_geometry(
    draw: ImageDraw.ImageDraw,
    geometry: Dict[str, Any],
    tile: mercantile.Tile,
    extent: int,
    to_pixel,
) -> None:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []

    if geometry_type == "Polygon":
        draw_polygon(draw, coordinates, tile, extent, to_pixel)
    elif geometry_type == "MultiPolygon":
        for polygon in coordinates:
            draw_polygon(draw, polygon, tile, extent, to_pixel)


def fetch_tile(session: requests.Session, tile: mercantile.Tile) -> Dict[str, Any]:
    url = TILE_URL.format(z=tile.z, x=tile.x, y=tile.y)
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return mapbox_vector_tile.decode(
        response.content,
        default_options={"y_coord_down": True},
    )


def build_water_mask(
    session: requests.Session,
    bounds: List[List[float]],
    width: int,
    height: int,
    zoom: int,
) -> Image.Image:
    south, west = bounds[0]
    north, east = bounds[1]
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    to_pixel = target_mapper(bounds, width, height)

    tiles = list(mercantile.tiles(west, south, east, north, zooms=[zoom]))
    print(f"OSM vandmaske: henter {len(tiles)} tiles på zoom {zoom}")

    water_features = 0
    for index, tile in enumerate(tiles, start=1):
        decoded = fetch_tile(session, tile)
        layer = decoded.get("water")
        if not layer:
            continue

        extent = int(layer.get("extent", 4096))
        for feature in layer.get("features", []):
            geometry = feature.get("geometry") or {}
            if geometry.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            draw_geometry(draw, geometry, tile, extent, to_pixel)
            water_features += 1

        if index % 25 == 0 or index == len(tiles):
            print(f"OSM vandmaske: {index}/{len(tiles)} tiles")

    if water_features == 0:
        raise RuntimeError("OpenFreeMap returnerede ingen water polygoner")

    print(f"OSM vandmaske: tegnede {water_features} water features")
    return mask


def apply_mask(image_path: Path, mask: Image.Image) -> None:
    with Image.open(image_path) as source:
        rgba = source.convert("RGBA")
        if rgba.size != mask.size:
            raise RuntimeError(
                f"Maskestørrelse {mask.size} matcher ikke {image_path.name} {rgba.size}"
            )

        pixels = np.asarray(rgba, dtype=np.uint8).copy()
        alpha = pixels[..., 3].astype(np.uint16)
        water = np.asarray(mask, dtype=np.uint16)
        pixels[..., 3] = ((alpha * water) // 255).astype(np.uint8)
        Image.fromarray(pixels).save(image_path, format="WEBP", lossless=True, method=6)


def main() -> int:
    parser = argparse.ArgumentParser(description="Klip DKSS kortframes mod OSM water polygoner")
    parser.add_argument("--data-dir", default="data/waterlevel-map")
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM)
    parser.add_argument("--collection", default="dkss_idw")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    collection = next(
        (item for item in metadata.get("collections", []) if item.get("id") == args.collection),
        None,
    )
    if not collection:
        raise RuntimeError(f"Collection {args.collection} findes ikke i metadata")

    sample_layer = None
    for frame in metadata.get("frames", []):
        sample_layer = next(
            (layer for layer in frame.get("layers", []) if layer.get("collection") == args.collection),
            None,
        )
        if sample_layer:
            break
    if not sample_layer:
        raise RuntimeError(f"Ingen frames fundet for {args.collection}")

    sample_path = data_dir / sample_layer["image"]
    with Image.open(sample_path) as sample:
        width, height = sample.size

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/x-protobuf"})
    mask = build_water_mask(session, sample_layer["bounds"], width, height, args.zoom)

    masked = 0
    for frame in metadata.get("frames", []):
        layer = next(
            (item for item in frame.get("layers", []) if item.get("collection") == args.collection),
            None,
        )
        if not layer:
            continue
        apply_mask(data_dir / layer["image"], mask)
        masked += 1

    collection["osmWaterMask"] = {
        "provider": "OpenFreeMap / OpenMapTiles / OpenStreetMap",
        "zoom": args.zoom,
        "maskedFrames": masked,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"OSM vandmaske anvendt på {masked} {args.collection} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
