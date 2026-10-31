"""Barcode lookup in Open Food Facts (free, no key, world.openfoodfacts.org).

Only called for barcodes that aren't in the local products table yet; once you
save a product, it's looked up locally from then on.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from . import __version__
from .shelf import guess_category

log = logging.getLogger("pantry.lookup")
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,product_name_en,generic_name,brands,quantity,categories_tags,image_front_small_url,image_url"
USER_AGENT = f"PantryApp/{__version__} (self-hosted household app)"


def normalize_barcode(code: str) -> str:
    """Digits only; UPC-A (12) is stored as EAN-13 with a leading 0 so both scans match."""
    d = re.sub(r"\D", "", code or "")
    if len(d) == 12:
        d = "0" + d
    return d


def barcode_variants(code: str):
    d = normalize_barcode(code)
    out = {d}
    if len(d) == 13 and d.startswith("0"):
        out.add(d[1:])
    return list(out)


def _fetch(url: str, timeout: float = 8) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.warning("Open Food Facts error %s for %s", e.code, url)
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("Open Food Facts unreachable: %s", e)
        raise LookupError("Couldn't reach Open Food Facts. Check the server's internet connection.") from e


def open_food_facts(code: str) -> Optional[dict]:
    """Return a product suggestion dict, or None if OFF doesn't know the barcode."""
    for c in barcode_variants(code):
        data = _fetch(OFF_URL.format(code=c))
        if not data or data.get("status") != 1 or not data.get("product"):
            continue
        p = data["product"]
        name = (p.get("product_name_en") or p.get("product_name") or p.get("generic_name") or "").strip()
        if not name:
            continue
        brand = (p.get("brands") or "").split(",")[0].strip() or None
        tags = p.get("categories_tags") or []
        return {
            "barcode": normalize_barcode(code),
            "name": name,
            "brand": brand,
            "size_text": (p.get("quantity") or "").strip() or None,
            "image_url": p.get("image_front_small_url") or p.get("image_url"),
            "category": guess_category(name, tags),
            "source": "openfoodfacts",
        }
    return None
