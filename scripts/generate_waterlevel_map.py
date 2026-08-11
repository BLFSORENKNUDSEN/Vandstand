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
from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release

STAC_ITEMS_URL = "https://opendataapi.dmi.dk/v1/forecastdata/collections/{collection}/items"
COLLECTIONS = ["dkss_nsbs", "dkss_idw"]
PARAMETER_ID = 82
PARAMETER_CODE = "DSLM"
USER_AGENT = "strandvejr.dk-waterlevel-map/1.0