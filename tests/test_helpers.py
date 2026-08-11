import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fetch_waterlevel.py"
spec = importlib.util.spec_from_file_location("fetch_waterlevel", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_dt_and_iso_z():
    value = module.parse_dt("2026-08-11T06:00:00Z")
    assert value == datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    assert module.iso_z(value) == "2026-08-11T06:00:00Z"


def test_asset_href_accepts_asset_and_assets():
    assert module.asset_href({"asset": {"data": {"href": "https://example.test/a.grib"}}}) == "https://example.test/a.grib"
    assert module.asset_href({"assets": {"data": {"href": "https://example.test/b.grib"}}}) == "https://example.test/b.grib"
