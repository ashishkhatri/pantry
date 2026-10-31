"""SQLite storage: connection, schema migrations, settings, backups.

Schema changes go in MIGRATIONS as a new numbered entry. Never edit an old
one: existing databases have already run it. ``PRAGMA user_version`` records
which migrations a database file has.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

LOCATIONS = ("pantry", "fridge", "freezer")

MIGRATIONS = {
    1: """
    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    CREATE TABLE users (
        id             INTEGER PRIMARY KEY,
        name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash  TEXT NOT NULL,
        is_admin       INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    -- A product is a kind of thing you buy ("Kroger Black Beans 15 oz"). Items are the
    -- physical units of it in the house.
    CREATE TABLE products (
        id                  INTEGER PRIMARY KEY,
        barcode             TEXT UNIQUE,
        name                TEXT NOT NULL,
        brand               TEXT,
        category            TEXT NOT NULL DEFAULT 'other',
        size_text           TEXT,
        image_url           TEXT,
        default_location    TEXT NOT NULL DEFAULT 'pantry' CHECK (default_location IN ('pantry','fridge','freezer')),
        -- Shelf life in days per location. NULL = use the category default.
        shelf_pantry_days   INTEGER,
        shelf_fridge_days   INTEGER,
        shelf_freezer_days  INTEGER,
        opened_days         INTEGER,
        shelf_source        TEXT NOT NULL DEFAULT 'category',   -- category | ai | manual
        created_at          TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_products_name ON products(name COLLATE NOCASE);
    CREATE TABLE items (
        id              INTEGER PRIMARY KEY,
        product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        location        TEXT NOT NULL DEFAULT 'pantry' CHECK (location IN ('pantry','fridge','freezer')),
        quantity        REAL NOT NULL DEFAULT 1,
        added_on        TEXT NOT NULL,                      -- YYYY-MM-DD
        expires_on      TEXT,                               -- YYYY-MM-DD, NULL = doesn't expire
        expires_source  TEXT NOT NULL DEFAULT 'auto',       -- auto | manual
        opened_on       TEXT,
        source          TEXT NOT NULL DEFAULT 'manual',     -- barcode | receipt | photo | manual
        added_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,
        note            TEXT,
        notified_stage  INTEGER NOT NULL DEFAULT 0,         -- 0 none, 1 "expiring soon" sent, 2 "expired" sent
        finished_on     TEXT,                               -- set when used up or thrown out
        finish_reason   TEXT                                -- used | discarded
    );
    CREATE INDEX ix_items_active ON items(finished_on, expires_on);
    CREATE INDEX ix_items_product ON items(product_id);
    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        product_id  INTEGER REFERENCES products(id) ON DELETE CASCADE,
        item_id     INTEGER REFERENCES items(id) ON DELETE SET NULL,
        user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        kind        TEXT NOT NULL,                          -- add | use | discard | edit | move | open
        quantity    REAL,
        note        TEXT,
        at          TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_events_at ON events(at);
    CREATE TABLE jobs (
        id           TEXT PRIMARY KEY,
        kind         TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'queued',        -- queued | running | done | error
        payload      TEXT,
        result       TEXT,
        error        TEXT,
        user_id      INTEGER,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at  TEXT
    );
    CREATE TABLE receipts (
        id            INTEGER PRIMARY KEY,
        user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
        store         TEXT,
        purchased_on  TEXT,
        status        TEXT NOT NULL DEFAULT 'processing',   -- processing | review | done | error
        job_id        TEXT,
        image_path    TEXT,
        items_added   INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE push_subs (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint    TEXT NOT NULL UNIQUE,
        p256dh      TEXT NOT NULL,
        auth        TEXT NOT NULL,
        user_agent  TEXT,
        fail_count  INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE mealdb (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        category      TEXT,
        area          TEXT,
        thumb         TEXT,
        source_url    TEXT,
        instructions  TEXT,
        ingredients   TEXT NOT NULL,                        -- JSON [{name, measure}]
        fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    2: """
    -- Where a product goes once opened (e.g. sauces: pantry -> fridge). NULL = the category's default.
    ALTER TABLE products ADD COLUMN opened_location TEXT;
    """,
    3: """
    -- Every recipe the feed can show, whatever its source:
    --   mealdb (the free TheMealDB library), web (Spoonacular search), link (imported from a URL), ai.
    CREATE TABLE recipes (
        id            INTEGER PRIMARY KEY,
        source        TEXT NOT NULL,
        ext_id        TEXT,                                 -- id at the source (TheMealDB / Spoonacular)
        title         TEXT NOT NULL,
        summary       TEXT,
        image         TEXT,
        url           TEXT,                                 -- the original page, when there is one
        site          TEXT,                                 -- e.g. "allrecipes.com"
        category      TEXT,
        area          TEXT,
        time_minutes  INTEGER,
        servings      INTEGER,
        ingredients   TEXT NOT NULL,                        -- JSON [{name, amount}]
        instructions  TEXT,                                 -- free text
        steps         TEXT,                                 -- JSON [step, ...]
        hidden        INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE UNIQUE INDEX ux_recipes_ext ON recipes(source, ext_id) WHERE ext_id IS NOT NULL;
    CREATE INDEX ix_recipes_source ON recipes(source, created_at);
    INSERT INTO recipes(source, ext_id, title, image, url, category, area, ingredients, instructions, created_at)
        SELECT 'mealdb', id, name, thumb, source_url, category, area, ingredients, instructions, fetched_at FROM mealdb;
    """,
    4: """
    -- What a recipe would call this product ("Kirkland Butter Tortillas" -> "flour tortillas"). From the AI
    -- or you; recipes match against it instead of the product name.
    ALTER TABLE products ADD COLUMN ingredient_name TEXT;
    """,
}

DEFAULT_SETTINGS: Dict[str, str] = {
    "warn_days": "3",            # "expiring soon" means within this many days
    "notify_hour": "9",          # daily reminder time, 24h, server's local time
    "notify_weekly_day": "6",    # weekly "still past date" digest: 0=Mon .. 6=Sun
    "staples": "salt\npepper\nblack pepper\nwater\nice",
    "last_daily_run": "",
    "last_weekly_run": "",
    "recipes_auto": "1",         # refresh the recipe feed in the background
    "recipes_ai_count": "4",     # AI recipes written per refresh
    "spoonacular_key": "",       # optional: web recipe search (free key at spoonacular.com/food-api)
    "last_feed_refresh": "",
    "feed_signature": "",
}


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    try:
        current = con.execute("PRAGMA user_version").fetchone()[0]
        for n in sorted(MIGRATIONS):
            if n > current:
                with con:
                    con.executescript(MIGRATIONS[n])
                    con.execute(f"PRAGMA user_version = {n}")
        with con:
            for k, v in DEFAULT_SETTINGS.items():
                con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    finally:
        con.close()


def get_setting(con, key: str, default: Optional[str] = None) -> Optional[str]:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row[0]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(con, key: str, value) -> None:
    con.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def backup(path: str, backup_dir: str, label: str = "manual", keep: int = 14) -> str:
    """Copy the database with SQLite's online backup (safe while the app runs)."""
    Path(backup_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(backup_dir) / f"pantry_{datetime.now():%Y-%m-%d_%H%M%S}_{label}.sqlite"
    src = connect(path)
    try:
        out = sqlite3.connect(str(dest))
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    autos = sorted(Path(backup_dir).glob("pantry_*_auto.sqlite"))
    for old in autos[:-keep]:
        old.unlink(missing_ok=True)
    return str(dest)


def auto_backup(path: str, backup_dir: str) -> Optional[str]:
    """One automatic backup per day, made at startup or by the daily scheduler."""
    today = f"pantry_{datetime.now():%Y-%m-%d}_"
    if any(p.name.startswith(today) and p.name.endswith("_auto.sqlite") for p in Path(backup_dir).glob("*.sqlite")):
        return None
    if not Path(path).exists():
        return None
    return backup(path, backup_dir, "auto")
