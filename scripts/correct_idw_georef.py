#!/usr/bin/env python3
import argparse
import json
import math
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from eccodes import codes_get, codes_grib_new_from_file, codes_release

STAC_ITEMS_URL = "https://opendataapi.dmi.dk/v1/forecastdata/collections/{collection}/items"
USER_AGENT = "strandvejr.dk-idw-georef/1.0"
PARAMETER_ID = 82
PARAMETER_CODE = "DSLM"
REQUEST_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180


def get_optional(gid: int, key: str, cast=float) -> Optional[Any]:
    try:
        return cast(codes_get(gid, key))
    except Exception:
        return None


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


def fetch_items(session: requests.Session, collection: str) -> list[Dict[str, Any]]:
    response = session.get(
        STAC_ITEMS_URL.format(collection=collection),
        params={"limit": 1000},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("features") or []


def find_item(
    items: list[Dict[str, Any]],
    model_run: str,
    valid_time: str,
) -> Dict[str, Any]:
    exact = [
        item for item in items
        if (item.get("properties") or {}).get("modelRun") == model_run
        and (item.get("properties") or {}).get("datetime") == valid_time
    ]
    if exact:
        return exact[0]

    by_time = [
        item for item in items
        if (item.get("properties") or {}).get("datetime") == valid_time
    ]
    if by_time:
        return by_time[0]

    raise RuntimeError(
        f"Fandt ikke STAC item for modelRun={model_run} datetime={valid_time}"
    )


def asset_href(item: Dict[str, Any]) -> str:
    assets = item.get("asset") or item.get("assets") or {}
    href = (assets.get("data") or {}).get("href")
    if not href:
        raise RuntimeError("STAC item mangler data href")
    return href


def download(session: requests.Session, url: str, destination: Path) -> None:
    with session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def read_grid_definition(grib_path: Path) -> Dict[str, Any]:
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not is_waterlevel_message(gid):
                    continue

                grid_type = get_optional(gid, "gridType", str)
                ni = get_optional(gid, "Ni", int)
                nj = get_optional(gid, "Nj", int)
                lat1 = get_optional(gid, "latitudeOfFirstGridPointInDegrees", float)
                lon1 = get_optional(gid, "longitudeOfFirstGridPointInDegrees", float)
                lat2 = get_optional(gid, "latitudeOfLastGridPointInDegrees", float)
                lon2 = get_optional(gid, "longitudeOfLastGridPointInDegrees", float)
                i_inc = get_optional(gid, "iDirectionIncrementInDegrees", float)
                j_inc = get_optional(gid, "jDirectionIncrementInDegrees", float)
                i_neg = get_optional(gid, "iScansNegatively", int)
                j_pos = get_optional(gid, "jScansPositively", int)
                j_consecutive = get_optional(gid, "jPointsAreConsecutive", int)
                alternative = get_optional(gid, "alternativeRowScanning", int)

                required = {
                    "Ni": ni,
                    "Nj": nj,
                    "lat1": lat1,
                    "lon1": lon1,
                    "lat2": lat2,
                    "lon2": lon2,
                }
                missing = [key for key, value in required.items() if value is None]
                if missing:
                    raise RuntimeError(f"GRIB mangler gridnøgler: {', '.join(missing)}")

                if i_inc is None or not math.isfinite(i_inc) or i_inc <= 0:
                    i_inc = abs(lon2 - lon1) / max(ni - 1, 1)
                if j_inc is None or not math.isfinite(j_inc) or j_inc <= 0:
                    j_inc = abs(lat2 - lat1) / max(nj - 1, 1)

                west_center = min(lon1, lon2)
                east_center = max(lon1, lon2)
                south_center = min(lat1, lat2)
                north_center = max(lat1, lat2)

                bounds = [
                    [south_center - j_inc / 2.0, west_center - i_inc / 2.0],
                    [north_center + j_inc / 2.0, east_center + i_inc / 2.0],
                ]

                return {
                    "gridType": grid_type,
                    "Ni": ni,
                    "Nj": nj,
                    "latitudeOfFirstGridPointInDegrees": lat1,
                    "longitudeOfFirstGridPointInDegrees": lon1,
                    "latitudeOfLastGridPointInDegrees": lat2,
                    "longitudeOfLastGridPointInDegrees": lon2,
                    "iDirectionIncrementInDegrees": i_inc,
                    "jDirectionIncrementInDegrees": j_inc,
                    "iScansNegatively": i_neg,
                    "jScansPositively": j_pos,
                    "jPointsAreConsecutive": j_consecutive,
                    "alternativeRowScanning": alternative,
                    "bounds": bounds,
                }
            finally:
                codes_release(gid)

    raise RuntimeError("Fandt ikke DSLM/parameter 82 i GRIB filen")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ret IDW overlay bounds ud fra GRIB filens grid metadata"
    )
    parser.add_argument("--data-dir", default="data/waterlevel-map")
    parser.add_argument("--collection", default="dkss_idw")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    metadata_path = data_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    collection_meta = next(
        (item for item in metadata.get("collections", []) if item.get("id") == args.collection),
        None,
    )
    if not collection_meta:
        raise RuntimeError(f"Collection {args.collection} findes ikke i metadata")

    first_frame = metadata.get("frames", [])[0]
    first_layer = next(
        layer for layer in first_frame.get("layers", [])
        if layer.get("collection") == args.collection
    )
    model_run = first_layer.get("modelRun") or collection_meta.get("modelRun")
    valid_time = first_frame.get("time")
    if not model_run or not valid_time:
        raise RuntimeError("Metadata mangler modelRun eller første forecasttid")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    item = find_item(fetch_items(session, args.collection), model_run, valid_time)

    with tempfile.TemporaryDirectory(prefix="strandvejr-idw-georef-") as tmpdir:
        grib_path = Path(tmpdir) / "sample.grib"
        download(session, asset_href(item), grib_path)
        grid = read_grid_definition(grib_path)

    bounds = grid["bounds"]
    print(
        f"[{args.collection}] GRIB grid: type={grid['gridType']} "
        f"Ni={grid['Ni']} Nj={grid['Nj']} "
        f"first=({grid['latitudeOfFirstGridPointInDegrees']:.6f},"
        f"{grid['longitudeOfFirstGridPointInDegrees']:.6f}) "
        f"last=({grid['latitudeOfLastGridPointInDegrees']:.6f},"
        f"{grid['longitudeOfLastGridPointInDegrees']:.6f}) "
        f"di={grid['iDirectionIncrementInDegrees']:.6f} "
        f"dj={grid['jDirectionIncrementInDegrees']:.6f}"
    )
    print(
        f"[{args.collection}] korrigerede bounds: "
        f"south={bounds[0][0]:.6f} west={bounds[0][1]:.6f} "
        f"north={bounds[1][0]:.6f} east={bounds[1][1]:.6f}"
    )

    old_bounds = collection_meta.get("bounds")
    collection_meta["generatorBounds"] = old_bounds
    collection_meta["bounds"] = bounds
    collection_meta["gribGridDefinition"] = {
        key: value for key, value in grid.items() if key != "bounds"
    }

    for frame in metadata.get("frames", []):
        for layer in frame.get("layers", []):
            if layer.get("collection") == args.collection:
                layer["bounds"] = bounds

    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
