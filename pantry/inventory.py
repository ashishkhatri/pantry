"""Products and items: adding, using up, moving, expiry dates.

Every change to what's in the house goes through this module, so expiry
calculation and the event history stay consistent whether it came from a
barcode scan, a receipt, a photo or typing.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

from . import db as dbm
from . import shelf
from .lookup import normalize_barcode

PRODUCT_FIELDS = ("barcode", "name", "brand", "category", "size_text", "image_url", "default_location", "opened_location", "ingredient_name",
                  "shelf_pantry_days", "shelf_fridge_days", "shelf_freezer_days", "opened_days", "shelf_source")


class InventoryError(ValueError):
    pass


def today() -> date:
    return date.today()


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _check_location(loc: Optional[str]) -> str:
    if loc not in dbm.LOCATIONS:
        raise InventoryError(f"Location must be one of: {', '.join(dbm.LOCATIONS)}")
    return loc


# ---------------- products ----------------

def get_product(con, product_id: int):
    return con.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def find_by_barcode(con, code: str):
    code = normalize_barcode(code)
    if not code:
        return None
    return con.execute("SELECT * FROM products WHERE barcode = ?", (code,)).fetchone()


def search_products(con, q: str, limit: int = 20) -> List[dict]:
    q = (q or "").strip()
    if not q:
        rows = con.execute("SELECT * FROM products ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    else:
        like = f"%{q}%"
        rows = con.execute(
            "SELECT * FROM products WHERE name LIKE ? OR brand LIKE ? OR barcode = ? "
            "ORDER BY (name LIKE ?) DESC, updated_at DESC LIMIT ?", (like, like, q, f"{q}%", limit)).fetchall()
    return [product_dict(con, r) for r in rows]


def product_dict(con, p) -> dict:
    d = dict(p)
    info = shelf.category_info(p["category"])
    d["category_label"] = info["label"]
    d["effective_days"] = {loc: shelf.shelf_days(p, loc) for loc in dbm.LOCATIONS}
    d["effective_opened_days"] = shelf.opened_days(p)
    d["effective_opened_location"] = shelf.opened_location(p)
    row = con.execute("SELECT COALESCE(SUM(quantity),0) FROM items WHERE product_id = ? AND finished_on IS NULL",
                      (p["id"],)).fetchone()
    d["in_stock"] = row[0]
    return d


def create_product(con, fields: dict) -> int:
    name = (fields.get("name") or "").strip()
    if not name:
        raise InventoryError("A product needs a name.")
    vals = {k: fields.get(k) for k in PRODUCT_FIELDS}
    vals["name"] = name
    vals["barcode"] = normalize_barcode(vals["barcode"]) if vals.get("barcode") else None
    if vals["barcode"] and find_by_barcode(con, vals["barcode"]):
        raise InventoryError("A product with that barcode already exists.")
    vals["category"] = vals.get("category") if vals.get("category") in shelf.CATEGORIES else shelf.guess_category(name)
    if not vals.get("default_location"):
        vals["default_location"] = shelf.category_info(vals["category"])["default_location"]
    _check_location(vals["default_location"])
    vals["shelf_source"] = vals.get("shelf_source") or "category"
    cols = [k for k in PRODUCT_FIELDS if vals.get(k) is not None]
    cur = con.execute(f"INSERT INTO products({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [vals[k] for k in cols])
    return cur.lastrowid


def update_product(con, product_id: int, fields: dict, recalc: bool = True) -> None:
    p = get_product(con, product_id)
    if not p:
        raise InventoryError("Product not found.")
    sets, vals = [], []
    for k in PRODUCT_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if k == "name" and not (v or "").strip():
            raise InventoryError("A product needs a name.")
        if k == "barcode":
            v = normalize_barcode(v) if v else None
            if v:
                other = find_by_barcode(con, v)
                if other and other["id"] != product_id:
                    raise InventoryError(f"That barcode already belongs to {other['name']}.")
        if k == "category" and v not in shelf.CATEGORIES:
            raise InventoryError("Unknown category.")
        if k == "default_location" or (k == "opened_location" and v):
            _check_location(v)
        if k.endswith("_days") and v not in (None, ""):
            v = max(0, int(v))
        if v == "":
            v = None
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    con.execute(f"UPDATE products SET {', '.join(sets)}, updated_at = datetime('now') WHERE id = ?", vals + [product_id])
    if recalc:
        recalc_product_items(con, product_id)


def recalc_product_items(con, product_id: int) -> int:
    """Recompute auto expiry dates for a product's items (after its shelf life changed)."""
    p = get_product(con, product_id)
    n = 0
    for it in con.execute("SELECT * FROM items WHERE product_id = ? AND finished_on IS NULL AND expires_source = 'auto'",
                          (product_id,)).fetchall():
        new = compute_expiry(p, it["location"], date.fromisoformat(it["added_on"]),
                             date.fromisoformat(it["opened_on"]) if it["opened_on"] else None)
        if _iso(new) != it["expires_on"]:
            con.execute("UPDATE items SET expires_on = ?, notified_stage = 0 WHERE id = ?", (_iso(new), it["id"]))
            n += 1
    return n


# ---------------- expiry ----------------

def compute_expiry(product, location: str, added_on: date, opened_on: Optional[date] = None) -> Optional[date]:
    days = shelf.shelf_days(product, location)
    exp = added_on + timedelta(days=days) if days is not None else None
    if opened_on is not None:
        od = shelf.opened_days(product)
        if od is not None:
            o = opened_on + timedelta(days=od)
            exp = min(exp, o) if exp else o
    return exp


def expiry_status(expires_on: Optional[str], warn_days: int, ref: Optional[date] = None) -> dict:
    if not expires_on:
        return {"days_left": None, "status": "ok"}
    ref = ref or today()
    left = (date.fromisoformat(expires_on) - ref).days
    status = "expired" if left < 0 else "soon" if left <= warn_days else "ok"
    return {"days_left": left, "status": status}


# ---------------- items ----------------

def _event(con, kind, product_id, item_id=None, user_id=None, quantity=None, note=None):
    con.execute("INSERT INTO events(product_id, item_id, user_id, kind, quantity, note) VALUES (?,?,?,?,?,?)",
                (product_id, item_id, user_id, kind, quantity, note))


def add_item(con, product_id: int, location: Optional[str] = None, quantity: float = 1, expires_on: Optional[str] = None,
             source: str = "manual", user_id: Optional[int] = None, added_on: Optional[str] = None,
             note: Optional[str] = None, opened: bool = False) -> int:
    """Add stock. ``opened`` = it's already open (e.g. a half-used sauce): it goes where opened ones
    are kept, unless you chose a location, and gets the shorter opened date."""
    p = get_product(con, product_id)
    if not p:
        raise InventoryError("Product not found.")
    default_loc = shelf.opened_location(p) if opened else p["default_location"]
    location = _check_location(location or default_loc)
    quantity = float(quantity or 1)
    if quantity <= 0:
        raise InventoryError("Quantity must be more than zero.")
    added = date.fromisoformat(added_on) if added_on else today()
    if expires_on:
        exp, src = date.fromisoformat(expires_on), "manual"
    else:
        exp, src = compute_expiry(p, location, added, today() if opened else None), "auto"
    cur = con.execute(
        "INSERT INTO items(product_id, location, quantity, added_on, expires_on, expires_source, source, added_by, note, "
        "opened_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (product_id, location, quantity, added.isoformat(), _iso(exp), src, source, user_id, note,
         today().isoformat() if opened else None))
    _event(con, "add", product_id, cur.lastrowid, user_id, quantity, source)
    con.execute("UPDATE products SET updated_at = datetime('now') WHERE id = ?", (product_id,))
    return cur.lastrowid


def get_item(con, item_id: int):
    it = con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not it:
        raise InventoryError("Item not found.")
    return it


def use_item(con, item_id: int, quantity: float = 1, user_id: Optional[int] = None, reason: str = "used") -> dict:
    """Take some quantity out. When it reaches zero the item is finished (kept for history)."""
    if reason not in ("used", "discarded"):
        raise InventoryError("Reason must be used or discarded.")
    it = get_item(con, item_id)
    if it["finished_on"]:
        raise InventoryError("That item is already finished.")
    q = it["quantity"] if quantity in (None, "all") else float(quantity)
    if q <= 0:
        raise InventoryError("Quantity must be more than zero.")
    left = round(max(0.0, it["quantity"] - q), 3)
    if left <= 0:
        con.execute("UPDATE items SET quantity = 0, finished_on = ?, finish_reason = ? WHERE id = ?",
                    (today().isoformat(), reason, item_id))
    else:
        con.execute("UPDATE items SET quantity = ? WHERE id = ?", (left, item_id))
    _event(con, "use" if reason == "used" else "discard", it["product_id"], item_id, user_id, min(q, it["quantity"]))
    return {"item_id": item_id, "left": left, "finished": left <= 0}


def use_product(con, product_id: int, quantity: float = 1, user_id: Optional[int] = None) -> dict:
    """Scan-out: take from the item that expires first (then the oldest)."""
    rows = con.execute(
        "SELECT * FROM items WHERE product_id = ? AND finished_on IS NULL "
        "ORDER BY expires_on IS NULL, expires_on, added_on, id", (product_id,)).fetchall()
    if not rows:
        raise InventoryError("None of that is in stock.")
    remaining = float(quantity)
    last = None
    for it in rows:
        if remaining <= 0:
            break
        take = min(remaining, it["quantity"])
        last = use_item(con, it["id"], take, user_id)
        remaining -= take
    total = con.execute("SELECT COALESCE(SUM(quantity),0) FROM items WHERE product_id = ? AND finished_on IS NULL",
                        (product_id,)).fetchone()[0]
    return {"product_id": product_id, "in_stock": total, "last_item": last}


def update_item(con, item_id: int, fields: dict, user_id: Optional[int] = None) -> None:
    it = get_item(con, item_id)
    p = get_product(con, it["product_id"])
    location = it["location"]
    opened_on = date.fromisoformat(it["opened_on"]) if it["opened_on"] else None
    expires_on, expires_source = it["expires_on"], it["expires_source"]
    changed_loc = False
    if "location" in fields and fields["location"] != location:
        location = _check_location(fields["location"])
        changed_loc = True
        _event(con, "move", p["id"], item_id, user_id, note=f"{it['location']} -> {location}")
    if "opened" in fields:
        if fields["opened"] and not opened_on:
            opened_on = today()
            _event(con, "open", p["id"], item_id, user_id)
        elif not fields["opened"]:
            opened_on = None
    newly_opened = "opened" in fields and fields["opened"] and not it["opened_on"]
    if newly_opened and not changed_loc:
        # Opening moves it where opened ones live (sauce -> fridge). The date counts from when it was
        # bought and from opening, whichever runs out first, so this isn't a fresh start.
        target = shelf.opened_location(p)
        if target != location:
            _event(con, "move", p["id"], item_id, user_id, note=f"{location} -> {target} (opened)")
            location = target
            if expires_source == "auto":
                expires_on = _iso(compute_expiry(p, location, date.fromisoformat(it["added_on"]), opened_on))
    if changed_loc:
        # Moving to/from the freezer restarts the clock from today; a printed date no longer applies.
        expires_on, expires_source = _iso(compute_expiry(p, location, today(), opened_on)), "auto"
    elif fields.get("expires_on"):
        expires_on, expires_source = date.fromisoformat(fields["expires_on"]).isoformat(), "manual"
    elif "expires_on" in fields or expires_source == "auto":
        # Cleared date (back to automatic), or an auto date that opening may shorten.
        expires_on = _iso(compute_expiry(p, location, date.fromisoformat(it["added_on"]), opened_on))
        expires_source = "auto"
    elif newly_opened:
        od = shelf.opened_days(p)
        if od is not None:
            o = opened_on + timedelta(days=od)
            expires_on = _iso(min(date.fromisoformat(expires_on), o) if expires_on else o)
    qty = it["quantity"]
    if "quantity" in fields:
        qty = float(fields["quantity"])
        if qty <= 0:
            raise InventoryError("Quantity must be more than zero. Use 'Used up' to finish an item.")
    note = fields.get("note", it["note"])
    stage = 0 if expires_on != it["expires_on"] else it["notified_stage"]
    con.execute("UPDATE items SET location = ?, opened_on = ?, expires_on = ?, expires_source = ?, quantity = ?, "
                "note = ?, notified_stage = ? WHERE id = ?",
                (location, _iso(opened_on), expires_on, expires_source, qty, note, stage, item_id))


def undo_finish(con, item_id: int, user_id: Optional[int] = None) -> None:
    """Put a finished item back (e.g. scanned out by mistake), with quantity 1."""
    it = get_item(con, item_id)
    if not it["finished_on"]:
        return
    con.execute("UPDATE items SET finished_on = NULL, finish_reason = NULL, quantity = 1 WHERE id = ?", (item_id,))
    _event(con, "edit", it["product_id"], item_id, user_id, 1, "restored")


def delete_item(con, item_id: int) -> None:
    con.execute("DELETE FROM items WHERE id = ?", (item_id,))


# ---------------- queries ----------------

ITEM_SELECT = """
SELECT i.*, p.name, p.brand, p.category, p.size_text, p.image_url, p.barcode, p.opened_location AS product_opened_location,
       u.name AS added_by_name
FROM items i JOIN products p ON p.id = i.product_id LEFT JOIN users u ON u.id = i.added_by
"""


def _item_out(r, warn_days: int) -> dict:
    d = dict(r)
    d.update(expiry_status(r["expires_on"], warn_days))
    d["category_label"] = shelf.category_info(r["category"])["label"]
    d["opened_location"] = r["product_opened_location"] or shelf.opened_location_for_category(r["category"])
    d.pop("product_opened_location", None)
    return d


def warn_days(con) -> int:
    try:
        return int(dbm.get_setting(con, "warn_days") or 3)
    except ValueError:
        return 3


def list_items(con, location: Optional[str] = None, q: Optional[str] = None, status: Optional[str] = None,
               product_id: Optional[int] = None) -> List[dict]:
    where, args = ["i.finished_on IS NULL"], []
    if location:
        where.append("i.location = ?")
        args.append(_check_location(location))
    if q:
        where.append("(p.name LIKE ? OR p.brand LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if product_id:
        where.append("i.product_id = ?")
        args.append(product_id)
    rows = con.execute(ITEM_SELECT + " WHERE " + " AND ".join(where) +
                       " ORDER BY i.expires_on IS NULL, i.expires_on, p.name COLLATE NOCASE", args).fetchall()
    wd = warn_days(con)
    out = [_item_out(r, wd) for r in rows]
    if status:
        out = [x for x in out if x["status"] == status]
    return out


def summary(con) -> dict:
    items = list_items(con)
    by_loc: Dict[str, int] = {loc: 0 for loc in dbm.LOCATIONS}
    for it in items:
        by_loc[it["location"]] += 1
    return {
        "total": len(items),
        "by_location": by_loc,
        "expired": [i for i in items if i["status"] == "expired"],
        "soon": [i for i in items if i["status"] == "soon"],
        "warn_days": warn_days(con),
    }


def recently_finished(con, limit: int = 40) -> List[dict]:
    """Products that ran out recently and aren't back in stock: a ready-made shopping list."""
    rows = con.execute("""
        SELECT p.id AS product_id, p.name, p.brand, p.category, p.image_url, MAX(i.finished_on) AS finished_on,
               (SELECT i2.finish_reason FROM items i2 WHERE i2.product_id = p.id AND i2.finished_on IS NOT NULL
                ORDER BY i2.finished_on DESC, i2.id DESC LIMIT 1) AS finish_reason
        FROM items i JOIN products p ON p.id = i.product_id
        WHERE i.finished_on IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM items a WHERE a.product_id = p.id AND a.finished_on IS NULL)
        GROUP BY p.id ORDER BY finished_on DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def history(con, limit: int = 100) -> List[dict]:
    rows = con.execute("""
        SELECT e.*, p.name, u.name AS user_name FROM events e
        LEFT JOIN products p ON p.id = e.product_id LEFT JOIN users u ON u.id = e.user_id
        ORDER BY e.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def pantry_names(con) -> List[dict]:
    """What's in stock, one row per product, for recipe matching."""
    wd = warn_days(con)
    rows = con.execute("""
        SELECT p.id, p.name, p.ingredient_name, p.category, SUM(i.quantity) AS qty, MIN(i.expires_on) AS first_exp
        FROM items i JOIN products p ON p.id = i.product_id
        WHERE i.finished_on IS NULL GROUP BY p.id ORDER BY p.name COLLATE NOCASE""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(expiry_status(r["first_exp"], wd))
        out.append(d)
    return out
