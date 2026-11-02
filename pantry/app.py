"""Flask web app: JSON API under /api plus the single-page PWA in static/."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import __version__
from . import ai, inventory, jobs, lookup, notify, pdf, recipes, shelf
from . import db as dbm
from .inventory import InventoryError
from .webpush import Vapid

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("PANTRY_DATA_DIR", ROOT / "data"))
STATIC = Path(__file__).resolve().parent / "static"
PUBLIC_API = {"/api/me", "/api/login", "/api/setup"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
PDF_TYPES = {"application/pdf", "application/x-pdf"}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _SessionInterface(SecureCookieSessionInterface):
    """Mark the sign-in cookie Secure whenever the page was reached over https (directly or via a
    proxy that sends X-Forwarded-Proto), so the same install works on plain http on a LAN and behind
    https. PANTRY_HTTPS=1 forces Secure, PANTRY_HTTPS=0 never sets it."""

    def get_cookie_secure(self, app):
        mode = os.environ.get("PANTRY_HTTPS", "auto").lower()
        if mode in ("1", "true", "yes"):
            return True
        if mode in ("0", "false", "no"):
            return False
        return bool(request) and request.is_secure


def _secret_key(data_dir: Path) -> bytes:
    p = data_dir / "secret_key"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p.read_bytes()


class LoginLimiter:
    """At most 10 failed logins per IP per 15 minutes: the app is reachable from the internet."""

    def __init__(self, limit=10, window=900):
        self.limit, self.window = limit, window
        self.fails = defaultdict(list)
        self.lock = threading.Lock()

    def blocked(self, ip):
        with self.lock:
            now = time.time()
            self.fails[ip] = [t for t in self.fails[ip] if now - t < self.window]
            return len(self.fails[ip]) >= self.limit

    def fail(self, ip):
        with self.lock:
            self.fails[ip].append(time.time())

    def clear(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


def create_app(db_path=None, start_background: bool = True) -> Flask:
    data_dir = Path(db_path).parent if db_path else DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, static_folder=None)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # behind a reverse proxy
    app.session_interface = _SessionInterface()
    app.config.update(
        DB_PATH=str(db_path or data_dir / "pantry.sqlite"),
        BACKUP_DIR=str(data_dir / "backups"),
        DATA_DIR=str(data_dir),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        SECRET_KEY=_secret_key(data_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    )
    (data_dir / "receipts").mkdir(exist_ok=True)
    (data_dir / "tmp").mkdir(exist_ok=True)
    dbm.init_db(app.config["DB_PATH"])
    _c = dbm.connect(app.config["DB_PATH"])
    try:
        if dbm.get_setting(_c, "in_use_model") != "1":
            with _c:
                inventory.adopt_in_use_model(_c)
                dbm.set_setting(_c, "in_use_model", "1")
        if dbm.get_setting(_c, "web_45_cleared") != "1":
            with _c:      # earlier web recipes stored Spoonacular's stand-in 45 minutes as a real time
                _c.execute("UPDATE recipes SET time_minutes = NULL WHERE source = 'web' AND time_minutes = 45")
                dbm.set_setting(_c, "web_45_cleared", "1")
    finally:
        _c.close()
    vapid = Vapid.load_or_create(str(data_dir / "vapid_private.pem"),
                                 os.environ.get("PANTRY_VAPID_SUBJECT", "mailto:pantry@localhost"))
    app.config["VAPID"] = vapid
    limiter = LoginLimiter()
    runner = jobs.JobRunner(app.config["DB_PATH"])
    app.config["JOBS"] = runner
    _register_jobs(runner, data_dir)
    if start_background:
        runner.start()
        notify.start(app.config["DB_PATH"], app.config["BACKUP_DIR"], vapid)

    def con():
        if "db" not in g:
            g.db = dbm.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.errorhandler(ApiError)
    def _api_err(e):
        return jsonify(error=str(e)), e.status

    @app.errorhandler(pdf.PdfError)
    @app.errorhandler(InventoryError)
    @app.errorhandler(ai.AIError)
    @app.errorhandler(LookupError)
    def _known_err(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(413)
    def _too_big(_e):
        return jsonify(error="That file is too large (25 MB max)."), 413

    def body():
        return request.get_json(silent=True) or {}

    def uid():
        return g.user["id"] if g.get("user") else None

    def admin_only():
        if not g.user["is_admin"]:
            raise ApiError("Only an admin can do that.", 403)

    # ---------------- auth ----------------

    @app.before_request
    def _auth():
        if not request.path.startswith("/api/"):
            return None
        g.user = None
        if "uid" in session:
            g.user = con().execute("SELECT id, name, is_admin FROM users WHERE id = ?", (session["uid"],)).fetchone()
        if request.path in PUBLIC_API:
            pass
        elif not g.user:
            return jsonify(error="Please sign in.", login=True), 401
        # Cross-site forms can't set custom headers, so this blocks CSRF on every change.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "pantry":
            return jsonify(error="Missing X-Requested-With header."), 400
        return None

    def _login(user_id):
        session.clear()
        session.permanent = True
        session["uid"] = user_id

    @app.get("/api/me")
    def me():
        c = con()
        setup = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        u = dict(g.user) if g.user else None
        return jsonify(user=u, setup_needed=setup, version=__version__,
                       ai_enabled=ai.enabled(c) if u else False,
                       push_key=vapid.public_key_b64 if u else None)

    @app.post("/api/setup")
    def setup():
        c = con()
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise ApiError("Setup is already done. Sign in instead.", 409)
        b = body()
        user_id = _create_user(c, b.get("name"), b.get("password"), admin=True)
        _login(user_id)
        return jsonify(ok=True)

    def _create_user(c, name, password, admin=False):
        name = (name or "").strip()
        if not name or len(name) > 40:
            raise ApiError("Enter a name (up to 40 characters).")
        if len(password or "") < 8:
            raise ApiError("Use a password of at least 8 characters.")
        if c.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise ApiError("That name is taken.")
        with c:
            cur = c.execute("INSERT INTO users(name, password_hash, is_admin) VALUES (?,?,?)",
                            (name, generate_password_hash(password), 1 if admin else 0))
        return cur.lastrowid

    @app.post("/api/login")
    def login():
        ip = request.remote_addr or "?"
        if limiter.blocked(ip):
            raise ApiError("Too many failed sign-ins. Try again in 15 minutes.", 429)
        b = body()
        row = con().execute("SELECT * FROM users WHERE name = ?", ((b.get("name") or "").strip(),)).fetchone()
        if not row or not check_password_hash(row["password_hash"], b.get("password") or ""):
            limiter.fail(ip)
            raise ApiError("Wrong name or password.", 401)
        limiter.clear(ip)
        _login(row["id"])
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/users")
    def users_list():
        rows = con().execute("SELECT id, name, is_admin, created_at FROM users ORDER BY id").fetchall()
        return jsonify(users=[dict(r) for r in rows])

    @app.post("/api/users")
    def users_add():
        admin_only()
        b = body()
        user_id = _create_user(con(), b.get("name"), b.get("password"), bool(b.get("is_admin")))
        return jsonify(id=user_id)

    @app.delete("/api/users/<int:user_id>")
    def users_delete(user_id):
        admin_only()
        if user_id == uid():
            raise ApiError("You can't remove yourself.")
        c = con()
        with c:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return jsonify(ok=True)

    @app.post("/api/users/password")
    def users_password():
        b = body()
        c = con()
        target = int(b.get("user_id") or uid())
        if target != uid():
            admin_only()
        else:
            row = c.execute("SELECT password_hash FROM users WHERE id = ?", (uid(),)).fetchone()
            if not check_password_hash(row["password_hash"], b.get("current") or ""):
                raise ApiError("Your current password is wrong.")
        if len(b.get("password") or "") < 8:
            raise ApiError("Use a password of at least 8 characters.")
        with c:
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(b["password"]), target))
        return jsonify(ok=True)

    # ---------------- inventory ----------------

    @app.get("/api/categories")
    def categories():
        return jsonify(categories=shelf.all_categories(), locations=list(dbm.LOCATIONS))

    @app.get("/api/categories/guess")
    def category_guess():
        return jsonify(shelf.category_info(shelf.guess_category(request.args.get("name", ""))))

    @app.get("/api/summary")
    def summary():
        c = con()
        s = inventory.summary(c)
        s["finished"] = inventory.recently_finished(c, 8)
        return jsonify(s)

    @app.get("/api/items")
    def items():
        a = request.args
        return jsonify(items=inventory.list_items(con(), a.get("location") or None, a.get("q") or None,
                                                  a.get("status") or None, a.get("product_id", type=int)))

    def _product_for(c, b):
        """An existing product id, or create one from the given fields."""
        if b.get("product_id"):
            p = inventory.get_product(c, int(b["product_id"]))
            if not p:
                raise ApiError("Product not found.", 404)
            return p["id"], False
        fields = b.get("product") or {}
        if fields.get("barcode"):
            p = inventory.find_by_barcode(c, fields["barcode"])
            if p:
                return p["id"], False
        if not fields.get("barcode"):
            p = _match_existing(c, fields.get("name"), fields.get("brand"))
            if p:
                return p["id"], False
        return inventory.create_product(c, fields), True

    def _maybe_ai_shelf(c, product_id, created):
        if created and ai.enabled(c) and ai.get_settings(c)["ai_auto_shelf"] == "1":
            return runner.submit(c, "shelf", {"product_id": product_id}, uid())
        return None

    @app.post("/api/items")
    def items_add():
        b = body()
        c = con()
        with c:
            pid, created = _product_for(c, b)
            item_id = inventory.add_item(c, pid, b.get("location"), b.get("quantity") or 1, b.get("expires_on") or None,
                                         b.get("source") or "manual", uid(), opened=bool(b.get("opened")))
        job = _maybe_ai_shelf(c, pid, created)
        it = inventory.list_items(c, product_id=pid)
        return jsonify(item_id=item_id, product_id=pid, created_product=created, shelf_job=job,
                       item=next((x for x in it if x["id"] == item_id), None))

    @app.post("/api/items/bulk")
    def items_bulk():
        """Add several lines at once (receipt or photo review)."""
        b = body()
        c = con()
        added, jobs_started, new_products = [], [], []
        with c:
            for line in b.get("items") or []:
                if line.get("skip"):
                    continue
                pid, created = _product_for(c, line)
                added.append(inventory.add_item(c, pid, line.get("location"), line.get("quantity") or 1,
                                                line.get("expires_on") or None, b.get("source") or "manual", uid(),
                                                added_on=b.get("added_on") or None))
                if created:
                    new_products.append(pid)
            if b.get("receipt_id"):
                c.execute("UPDATE receipts SET status = 'done', items_added = ? WHERE id = ?",
                          (len(added), int(b["receipt_id"])))
        for pid in new_products:
            j = _maybe_ai_shelf(c, pid, True)
            if j:
                jobs_started.append(j)
        return jsonify(added=len(added), shelf_jobs=jobs_started)

    @app.patch("/api/items/<int:item_id>")
    def items_edit(item_id):
        c = con()
        with c:
            inventory.update_item(c, item_id, body(), uid())
        return jsonify(ok=True)

    @app.post("/api/items/<int:item_id>/use")
    def items_use(item_id):
        b = body()
        c = con()
        with c:
            r = inventory.use_item(c, item_id, b.get("quantity", 1), uid(), b.get("reason") or "used")
        return jsonify(r)

    @app.post("/api/items/<int:item_id>/restore")
    def items_restore(item_id):
        c = con()
        with c:
            inventory.undo_finish(c, item_id, uid())
        return jsonify(ok=True)

    @app.delete("/api/items/<int:item_id>")
    def items_delete(item_id):
        c = con()
        with c:
            inventory.delete_item(c, item_id)
        return jsonify(ok=True)

    @app.get("/api/finished")
    def finished():
        return jsonify(items=inventory.recently_finished(con()))

    @app.get("/api/history")
    def history():
        return jsonify(events=inventory.history(con(), request.args.get("limit", 100, type=int)))

    # ---------------- products & barcodes ----------------

    @app.get("/api/products")
    def products():
        return jsonify(products=inventory.search_products(con(), request.args.get("q", ""),
                                                          request.args.get("limit", 20, type=int)))

    @app.get("/api/products/<int:product_id>")
    def product_get(product_id):
        c = con()
        p = inventory.get_product(c, product_id)
        if not p:
            raise ApiError("Product not found.", 404)
        return jsonify(product=inventory.product_dict(c, p), items=inventory.list_items(c, product_id=product_id))

    @app.patch("/api/products/<int:product_id>")
    def product_edit(product_id):
        b = body()
        c0 = con()
        cur = inventory.get_product(c0, product_id)
        # Your own shelf-life numbers or storage place: the AI won't override them later.
        if any(k.endswith("_days") for k in b) or (cur and (
                b.get("default_location") not in (None, cur["default_location"])
                or ("opened_location" in b and (b["opened_location"] or None) != cur["opened_location"]))):
            b.setdefault("shelf_source", "manual")
        c = con()
        with c:
            inventory.update_product(c, product_id, b)
        return jsonify(ok=True)

    @app.delete("/api/products/<int:product_id>")
    def product_delete(product_id):
        c = con()
        with c:
            c.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return jsonify(ok=True)

    @app.post("/api/products/<int:product_id>/ai-shelf")
    def product_ai_shelf(product_id):
        c = con()
        if not ai.enabled(c):
            raise ApiError("Set up AI in Settings first.")
        return jsonify(job=runner.submit(c, "shelf", {"product_id": product_id, "force": True}, uid()))

    @app.get("/api/barcode/<code>")
    def barcode(code):
        c = con()
        code = lookup.normalize_barcode(code)
        if len(code) < 6:
            raise ApiError("That doesn't look like a barcode.")
        p = inventory.find_by_barcode(c, code)
        if p:
            return jsonify(found="local", product=inventory.product_dict(c, p),
                           items=inventory.list_items(c, product_id=p["id"]))
        try:
            sug = lookup.open_food_facts(code)
        except LookupError as e:
            return jsonify(found=None, barcode=code, error=str(e))
        if sug:
            info = shelf.category_info(sug["category"])
            sug["default_location"] = info["default_location"]
            sug["category_label"] = info["label"]
            return jsonify(found="openfoodfacts", suggestion=sug)
        return jsonify(found=None, barcode=code)

    @app.post("/api/scan/out")
    def scan_out():
        b = body()
        c = con()
        pid = b.get("product_id")
        if not pid and b.get("barcode"):
            p = inventory.find_by_barcode(c, b["barcode"])
            if not p:
                raise ApiError("That barcode isn't in your pantry.", 404)
            pid = p["id"]
        if not pid:
            raise ApiError("Scan a barcode or pick a product.")
        with c:
            r = inventory.use_product(c, int(pid), float(b.get("quantity") or 1), uid())
        p = inventory.get_product(c, int(pid))
        r["name"] = p["name"]
        return jsonify(r)

    # ---------------- photos & receipts (AI jobs) ----------------

    def _save_upload(folder: Path, name: str, field: str = "image", allow_pdf: bool = False) -> Path:
        f = request.files.get(field)
        if not f:
            raise ApiError("No image received.")
        if f.mimetype not in IMAGE_TYPES and not (allow_pdf and f.mimetype in PDF_TYPES):
            raise ApiError("Upload a PDF or a JPEG, PNG or WebP photo." if allow_pdf else "Upload a JPEG, PNG or WebP photo.")
        path = folder / name
        f.save(path)
        return path

    @app.post("/api/ai/identify")
    def ai_identify():
        c = con()
        if not ai.enabled(c):
            raise ApiError("Photo recognition needs AI. Set it up in Settings → AI.")
        path = _save_upload(data_dir / "tmp", f"{uuid.uuid4().hex}.jpg")
        return jsonify(job=runner.submit(c, "identify", {"path": str(path)}, uid()))

    @app.post("/api/receipts")
    def receipt_add():
        c = con()
        if not ai.enabled(c):
            raise ApiError("Receipt reading needs AI. Set it up in Settings → AI.")
        with c:
            rid = c.execute("INSERT INTO receipts(user_id) VALUES (?)", (uid(),)).lastrowid
        upload = request.files.get("image")
        if upload and upload.mimetype in PDF_TYPES:
            # An e-receipt PDF: it's small, so the PDF itself is what's kept.
            keep = _save_upload(data_dir / "receipts", f"{rid}.pdf", allow_pdf=True)
            if not pdf.is_pdf(keep.read_bytes()[:8]):
                keep.unlink(missing_ok=True)
                with c:
                    c.execute("DELETE FROM receipts WHERE id = ?", (rid,))
                raise ApiError("That file isn't a PDF.")
            job = runner.submit(c, "receipt", {"path": str(keep), "receipt_id": rid}, uid())
            with c:
                c.execute("UPDATE receipts SET image_path = ?, job_id = ? WHERE id = ?", (str(keep), job, rid))
            return jsonify(receipt_id=rid, job=job)
        # The full-size photo is only for the AI to read; it's deleted once read. What's kept is the small
        # black & white copy the phone made ("archive"), or the full photo if an older page sent none.
        full = _save_upload(data_dir / "tmp", f"receipt-{rid}-full.jpg")
        keep = data_dir / "receipts" / f"{rid}.jpg"
        if request.files.get("archive"):
            _save_upload(data_dir / "receipts", f"{rid}.jpg", "archive")
        else:
            shutil.copyfile(full, keep)
        job = runner.submit(c, "receipt", {"path": str(full), "receipt_id": rid}, uid())
        with c:
            c.execute("UPDATE receipts SET image_path = ?, job_id = ? WHERE id = ?", (str(keep), job, rid))
        return jsonify(receipt_id=rid, job=job)

    @app.get("/api/receipts")
    def receipts_list():
        rows = con().execute("SELECT r.*, u.name AS user_name FROM receipts r LEFT JOIN users u ON u.id = r.user_id "
                             "ORDER BY r.id DESC LIMIT 50").fetchall()
        return jsonify(receipts=[{**{k: r[k] for k in r.keys() if k != "image_path"},
                                  "has_image": bool(r["image_path"] and Path(r["image_path"]).exists()),
                                  "is_pdf": bool(r["image_path"] and r["image_path"].endswith(".pdf"))} for r in rows])

    @app.get("/api/receipts/<int:rid>")
    def receipt_get(rid):
        c = con()
        r = c.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
        if not r:
            raise ApiError("Receipt not found.", 404)
        d = {k: r[k] for k in r.keys() if k != "image_path"}
        d["is_pdf"] = bool(r["image_path"] and r["image_path"].endswith(".pdf"))
        d["job"] = jobs.get(c, r["job_id"]) if r["job_id"] else None
        return jsonify(receipt=d)

    @app.get("/api/receipts/<int:rid>/image")
    def receipt_image(rid):
        r = con().execute("SELECT image_path FROM receipts WHERE id = ?", (rid,)).fetchone()
        if not r or not r[0] or not Path(r[0]).exists():
            raise ApiError("No image.", 404)
        resp = send_file(r[0], mimetype="application/pdf" if r[0].endswith(".pdf") else "image/jpeg", max_age=86400)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    @app.post("/api/receipts/<int:rid>/retry")
    def receipt_retry(rid):
        c = con()
        r = c.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
        if not r or not r["image_path"]:
            raise ApiError("Receipt not found.", 404)
        full = data_dir / "tmp" / f"receipt-{rid}-full.jpg"      # still here if the last read failed
        job = runner.submit(c, "receipt", {"path": str(full if full.exists() else r["image_path"]), "receipt_id": rid}, uid())
        with c:
            c.execute("UPDATE receipts SET status = 'processing', job_id = ? WHERE id = ?", (job, rid))
        return jsonify(job=job)

    @app.delete("/api/receipts/<int:rid>")
    def receipt_delete(rid):
        c = con()
        r = c.execute("SELECT image_path FROM receipts WHERE id = ?", (rid,)).fetchone()
        if r and r[0]:
            Path(r[0]).unlink(missing_ok=True)
        (data_dir / "tmp" / f"receipt-{rid}-full.jpg").unlink(missing_ok=True)
        with c:
            c.execute("DELETE FROM receipts WHERE id = ?", (rid,))
        return jsonify(ok=True)

    @app.get("/api/jobs/<jid>")
    def job_get(jid):
        j = jobs.get(con(), jid)
        if not j:
            raise ApiError("Job not found.", 404)
        return jsonify(job=j)

    # ---------------- recipes ----------------

    def _refresh_job(c, force=False):
        """The running feed refresh, or a new one if the feed is stale (or forced)."""
        row = c.execute("SELECT id FROM jobs WHERE kind = 'feed_refresh' AND status IN ('queued', 'running') "
                        "LIMIT 1").fetchone()
        if row:
            return row[0]
        if force or recipes.mealdb_count(c) == 0 or recipes.needs_refresh(c):
            return runner.submit(c, "feed_refresh", {"force": force}, uid())
        return None

    @app.get("/api/recipes")
    def recipes_feed():
        c = con()
        a = request.args
        data = recipes.feed(c, a.get("q", ""), a.get("source", ""), a.get("can_make") == "1",
                            a.get("offset", 0, type=int), min(60, a.get("limit", 24, type=int)))
        for r in data["recipes"]:
            if r["source"] == "ai":
                r["search_url"] = recipes.web_search_url(r["title"])
        return jsonify(**data, counts=recipes.counts(c), in_stock=len(inventory.pantry_names(c)),
                       ai_enabled=ai.enabled(c), web_enabled=bool(dbm.get_setting(c, "spoonacular_key")),
                       refreshing=_refresh_job(c))

    @app.get("/api/recipes/<int:rid>")
    def recipe_get(rid):
        c = con()
        r = recipes.get(c, rid)
        if not r:
            raise ApiError("Recipe not found.", 404)
        if r["source"] == "ai":
            r["search_url"] = recipes.web_search_url(r["title"])
        return jsonify(recipe=r)

    @app.post("/api/recipes/refresh")
    def recipes_refresh():
        return jsonify(job=_refresh_job(con(), force=True))

    @app.post("/api/recipes/download")       # older pages call this; same as a refresh
    def recipes_download():
        return jsonify(job=_refresh_job(con(), force=True))

    @app.post("/api/recipes/ai")
    def recipes_ai():
        c = con()
        if not ai.enabled(c):
            raise ApiError("Set up AI in Settings first.")
        b = body()
        return jsonify(job=runner.submit(c, "recipes", {"max_missing": int(b.get("max_missing", 3)),
                                                        "request": str(b.get("request") or "")[:300]}, uid()))

    @app.post("/api/recipes/import")
    def recipes_import():
        """Add a recipe from a link. Order of attempts:
        1. the page's schema.org recipe data (instant);
        2. if the page was readable but had no such data and AI is on: the AI reads the page text (job);
        3. otherwise ask for the recipe text to be pasted (answer has paste=true), then parse that,
           with the AI when it's on (job), or by its headings when it isn't. The link is kept either way."""
        c = con()
        b = body()
        url = str(b.get("url") or "").strip()
        text = str(b.get("text") or "").strip()
        if url and not re.match(r"^https?://", url):
            raise ApiError("Paste a full link starting with http:// or https://.")
        if text:
            if ai.enabled(c):
                return jsonify(job=runner.submit(c, "import_recipe", {"url": url, "text": text[:30000]}, uid()))
            rid = recipes.store(c, recipes.parse_pasted(text, url))
            c.commit()
            return jsonify(recipe=recipes.get(c, rid))
        if not url:
            raise ApiError("Paste a link, or the recipe text.")
        try:
            rid = recipes.import_url(c, url)
            return jsonify(recipe=recipes.get(c, rid))
        except recipes.NeedsPaste as e:
            if e.page_text and ai.enabled(c):
                return jsonify(job=runner.submit(c, "import_recipe", {"url": url, "text": e.page_text[:30000],
                                                                      "from_page": True}, uid()))
            return jsonify(error=str(e), paste=True), 400
        except LookupError as e:          # couldn't open it at all: pasting still works
            return jsonify(error=f"{e} You can paste the recipe text instead.", paste=True), 400

    @app.post("/api/recipes/<int:rid>/hide")
    def recipe_hide(rid):
        c = con()
        with c:
            c.execute("UPDATE recipes SET hidden = ? WHERE id = ?", (0 if body().get("unhide") else 1, rid))
        return jsonify(ok=True)

    @app.delete("/api/recipes/<int:rid>")
    def recipe_delete(rid):
        c = con()
        with c:
            c.execute("DELETE FROM recipes WHERE id = ? AND source IN ('ai', 'link')", (rid,))
        return jsonify(ok=True)

    # ---------------- settings ----------------

    GENERAL = ("warn_days", "notify_hour", "notify_weekly_day", "staples", "recipes_auto", "recipes_ai_count")

    @app.get("/api/settings")
    def settings_get():
        c = con()
        st = {k: dbm.get_setting(c, k) for k in GENERAL}
        st["spoonacular_key_set"] = "1" if dbm.get_setting(c, "spoonacular_key") else "0"
        st["spoon_usage"] = recipes.spoon_usage(c)
        return jsonify(settings=st, ai=ai.get_settings(c))

    @app.put("/api/settings")
    def settings_put():
        b = body()
        c = con()
        with c:
            for k in GENERAL:
                if k in b and b[k] is not None:
                    v = str(b[k]).strip()
                    if k != "staples":
                        if not v.isdigit():
                            raise ApiError(f"{k} must be a whole number.")
                        lim = {"warn_days": 60, "notify_hour": 23, "notify_weekly_day": 6, "recipes_auto": 1,
                               "recipes_ai_count": 10}[k]
                        v = str(min(int(v), lim))
                    dbm.set_setting(c, k, v)
            if "warn_days" in b:
                c.execute("UPDATE items SET notified_stage = 0 WHERE finished_on IS NULL AND notified_stage = 1")
            if b.get("spoonacular_key"):
                dbm.set_setting(c, "spoonacular_key", str(b["spoonacular_key"]).strip())
                dbm.set_setting(c, "last_feed_refresh", "")      # search the web on the next visit
            if b.get("spoonacular_clear"):
                dbm.set_setting(c, "spoonacular_key", "")
            if isinstance(b.get("ai"), dict):
                ai.save_settings(c, b["ai"])
        return jsonify(ok=True)

    @app.get("/api/ai/models")
    def ai_models():
        c = con()
        kind = request.args.get("provider") or ai.get_settings(c)["ai_provider"]
        base = request.args.get("base_url")
        if base is None:
            base = ai.get_settings(c)["ai_base_url"]
        try:
            return jsonify(models=ai.list_models(c, kind, base))
        except ai.AIError as e:
            raise ApiError(str(e), 400)   # not 502: Cloudflare replaces 5xx bodies with its own page

    @app.get("/api/ai/shelf-status")
    def ai_shelf_status():
        c = con()
        counts = dict(c.execute("SELECT shelf_source, COUNT(*) FROM products GROUP BY shelf_source").fetchall())
        job = c.execute("SELECT id FROM jobs WHERE kind = 'shelf_all' AND status IN ('queued', 'running') "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
        return jsonify(defaults=counts.get("category", 0), ai=counts.get("ai", 0), manual=counts.get("manual", 0),
                       job=job[0] if job else None)

    @app.post("/api/ai/shelf-all")
    def ai_shelf_all():
        c = con()
        if not ai.enabled(c):
            raise ApiError("Set up AI first.")
        job = c.execute("SELECT id FROM jobs WHERE kind = 'shelf_all' AND status IN ('queued', 'running') "
                        "LIMIT 1").fetchone()
        if job:
            return jsonify(job=job[0])
        return jsonify(job=runner.submit(c, "shelf_all", {"redo": bool(body().get("redo"))}, uid()))

    @app.post("/api/ai/test")
    def ai_test():
        override = body().get("ai") or None
        return jsonify(ai.test_connection(con(), override))

    # ---------------- push ----------------

    @app.post("/api/push/subscribe")
    def push_subscribe():
        b = body()
        keys = b.get("keys") or {}
        if not b.get("endpoint", "").startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ApiError("Invalid push subscription.")
        c = con()
        with c:
            c.execute("INSERT INTO push_subs(user_id, endpoint, p256dh, auth, user_agent) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
                      "auth = excluded.auth, fail_count = 0",
                      (uid(), b["endpoint"], keys["p256dh"], keys["auth"], request.headers.get("User-Agent", "")[:200]))
        return jsonify(ok=True)

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe():
        c = con()
        with c:
            c.execute("DELETE FROM push_subs WHERE endpoint = ?", (body().get("endpoint") or "",))
        return jsonify(ok=True)

    @app.get("/api/push/devices")
    def push_devices():
        rows = con().execute("SELECT s.id, s.user_agent, s.created_at, u.name FROM push_subs s "
                             "JOIN users u ON u.id = s.user_id ORDER BY s.id").fetchall()
        return jsonify(devices=[dict(r) for r in rows])

    @app.post("/api/push/test")
    def push_test():
        r = notify.push_all(con(), vapid, {"title": "Pantry notifications work",
                                           "body": "You'll get expiry reminders on this device.", "url": "/",
                                           "tag": "pantry-test"}, uid())
        if not r["sent"] and not r["failed"]:
            raise ApiError("This account has no devices with notifications on.")
        return jsonify(r)

    @app.post("/api/notify/run")
    def notify_run():
        """Send today's reminder now (ignores the daily schedule)."""
        c = con()
        msg = notify.daily_message(c) or notify.weekly_message(c)
        if not msg:
            return jsonify(sent=0, message="Nothing is expiring soon.")
        r = notify.push_all(c, vapid, msg)
        return jsonify(message=msg["title"], **r)

    @app.post("/api/backup")
    def backup():
        admin_only()
        return jsonify(path=Path(dbm.backup(app.config["DB_PATH"], app.config["BACKUP_DIR"])).name)

    # ---------------- static ----------------

    @app.get("/")
    def index():
        resp = send_from_directory(STATIC, "index.html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/sw.js")
    def sw():
        resp = send_from_directory(STATIC, "sw.js", mimetype="text/javascript")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC / "icons", "favicon-32.png", mimetype="image/png")

    @app.get("/healthz")
    def healthz():
        return Response("ok", mimetype="text/plain")

    return app


# ---------------- job handlers ----------------

def _match_existing(c, name, brand=None):
    """An existing product with the same name (and brand, if both have one)."""
    name = (name or "").strip()
    if not name:
        return None
    rows = c.execute("SELECT * FROM products WHERE name = ? COLLATE NOCASE", (name,)).fetchall()
    for r in rows:
        if not brand or not r["brand"] or r["brand"].lower() == brand.lower():
            return r
    return None


def _suggest_product(c, name, brand=None):
    """Closest existing product for a receipt/photo line, so repeat purchases link up."""
    p = _match_existing(c, name, brand)
    if p:
        return p
    for r in c.execute("SELECT * FROM products ORDER BY updated_at DESC LIMIT 500"):
        if recipes.matches(name, r["name"]) and recipes.matches(r["name"], name):
            return r
    return None


def _annotate_lines(c, lines):
    out = []
    for ln in lines:
        p = _suggest_product(c, ln["name"], ln.get("brand"))
        info = shelf.category_info(p["category"] if p else ln["category"])
        out.append({**ln, "product_id": p["id"] if p else None, "product_name": p["name"] if p else None,
                    "location": p["default_location"] if p else info["default_location"],
                    "category_label": info["label"]})
    return out


def _register_jobs(runner: jobs.JobRunner, data_dir: Path) -> None:
    def identify(c, payload, **_):
        path = Path(payload["path"])
        try:
            res = ai.identify_food(c, path.read_bytes())
        finally:
            path.unlink(missing_ok=True)
        return {"items": _annotate_lines(c, res["items"])}

    def receipt(c, payload, **_):
        path = Path(payload["path"])
        if path.suffix == ".pdf":
            txt = pdf.text(path)
            # A real e-receipt has text: read that (exact, and no vision model needed). A scanned
            # PDF has none, so its pages go to the vision model as pictures.
            res = ai.parse_receipt_text(c, txt) if len(txt.strip()) >= 25 else ai.parse_receipt(c, pdf.page_images(path))
        else:
            res = ai.parse_receipt(c, path.read_bytes())
        if path.parent.name == "tmp":
            path.unlink(missing_ok=True)       # read: only the small copy is kept
        lines = _annotate_lines(c, res["items"])
        with c:
            c.execute("UPDATE receipts SET status = 'review', store = ?, purchased_on = ? WHERE id = ?",
                      (res["store"], res["date"], payload["receipt_id"]))
        return {"store": res["store"], "date": res["date"], "items": lines}

    def receipt_failed(c, payload, **_):
        c.execute("UPDATE receipts SET status = 'error' WHERE id = ?", (payload["receipt_id"],))

    def apply_ai_shelf(c, p):
        res = ai.shelf_life(c, p["name"], p["brand"], p["category"], p["size_text"])
        fields = {k: res[k] for k in ("shelf_pantry_days", "shelf_fridge_days", "shelf_freezer_days", "opened_days")}
        fields["shelf_source"] = "ai"
        if res.get("category") in shelf.CATEGORIES:
            fields["category"] = res["category"]
        moved = 0
        new_loc = res.get("default_location")
        fields["opened_location"] = None      # one location now: where it lives in use
        if res.get("ingredient_name") and not p["ingredient_name"]:
            fields["ingredient_name"] = res["ingredient_name"]
        with c:
            if new_loc in dbm.LOCATIONS and new_loc != p["default_location"]:
                fields["default_location"] = new_loc
                # Move items that are where Pantry guessed (the old default) and that nobody has moved
                # since. Items you put somewhere on purpose stay put. Their dates are recalculated
                # from when they were added, by update_product below.
                moved = c.execute(
                    "UPDATE items SET location = ?, notified_stage = 0 WHERE product_id = ? AND finished_on IS NULL "
                    "AND location = ? AND NOT EXISTS (SELECT 1 FROM events e WHERE e.item_id = items.id AND e.kind = 'move')",
                    (new_loc, p["id"], p["default_location"])).rowcount
            inventory.update_product(c, p["id"], fields)    # also re-dates items in stock (not ones you dated)
        return {**res, "moved": moved}

    def shelf_job(c, payload, **_):
        p = inventory.get_product(c, payload["product_id"])
        if not p:
            return {"skipped": "product deleted"}
        if p["shelf_source"] == "manual" and not payload.get("force"):
            return {"skipped": "you set this product's shelf life yourself"}
        res = apply_ai_shelf(c, p)
        return {**res, "product_id": p["id"], "name": p["name"]}

    def shelf_all(c, payload, jid, **_):
        """Ask the AI about every product still on category defaults (and, with redo, ones it
        already estimated). Never touches products whose shelf life you set yourself."""
        sources = ("category", "ai") if payload.get("redo") else ("category",)
        rows = c.execute(
            f"SELECT p.*, EXISTS(SELECT 1 FROM items i WHERE i.product_id = p.id AND i.finished_on IS NULL) AS stocked "
            f"FROM products p WHERE p.shelf_source IN ({','.join('?' * len(sources))}) "
            f"ORDER BY stocked DESC, p.name COLLATE NOCASE", sources).fetchall()
        done, updated, failed, streak, moved = 0, [], [], 0, 0
        for p in rows:
            with c:
                c.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(
                    {"done": done, "total": len(rows), "current": p["name"], "updated": len(updated),
                     "failed": len(failed)}), jid))
            try:
                r = apply_ai_shelf(c, p)
                updated.append(p["name"])
                moved += r["moved"]
                streak = 0
            except ai.AIError as e:
                failed.append({"name": p["name"], "error": str(e)})
                streak += 1
                # The server is down or unusable: stop rather than fail every product the same way.
                if isinstance(e, ai.NotConfigured) or "isn't answering" in str(e) or streak >= 3:
                    raise ai.AIError(f"Stopped after {len(updated)} of {len(rows)} products: {e}") from e
            done += 1
        return {"done": done, "total": len(rows), "updated": len(updated), "failed": failed, "moved": moved}

    def mealdb(c, payload, **_):
        return recipes.fetch_mealdb(c)

    def store_ai_recipes(c, rs):
        ids = []
        for r in rs:
            rid = recipes.store(c, {**r, "source": "ai"})
            if rid:
                ids.append(rid)
        recipes.prune_ai(c)
        c.commit()
        return ids

    def recipes_ai(c, payload, **_):
        pantry = inventory.pantry_names(c)
        if not pantry:
            raise InventoryError("Add some food first.")
        res = ai.suggest_recipes(c, pantry, recipes.staples(c), payload.get("max_missing", 3), 6,
                                 payload.get("request", ""))
        return {"ids": store_ai_recipes(c, res["recipes"]), "count": len(res["recipes"])}

    def import_recipe(c, payload, **_):
        try:
            rec = ai.extract_recipe(c, payload["text"])
        except ai.AIError as e:
            if payload.get("from_page"):
                raise ai.AIError("Couldn't find the recipe on that page. Paste the recipe text instead.") from e
            raise
        rid = recipes.store(c, {**rec, "source": "link", "url": payload.get("url") or None})
        c.commit()
        return {"id": rid, "title": rec["title"]}

    def feed_refresh(c, payload, **_):
        """Keep the recipe feed full: the library (first time), web recipes for what you have
        (with a Spoonacular key), and a few new AI recipes. One source failing doesn't stop the rest."""
        out, warnings = {"mealdb": 0, "web": 0, "ai": 0}, []
        if recipes.mealdb_count(c) == 0:
            try:
                out["mealdb"] = recipes.fetch_mealdb(c)["recipes"]
            except LookupError as e:
                warnings.append(f"Recipe library: {e}")
        pantry = inventory.pantry_names(c)
        key = dbm.get_setting(c, "spoonacular_key")
        if key and pantry:
            skip = recipes.web_search_due(c, force=bool(payload.get("force")))
            if skip:
                warnings.append(f"Web recipes skipped: {skip}")
            else:
                try:
                    out["web"] = recipes.fetch_spoonacular(c, key, recipes.focus_ingredients(c))
                except LookupError as e:
                    warnings.append(f"Web recipes: {e}")
        n = int(dbm.get_setting(c, "recipes_ai_count") or 0)
        if n and pantry and ai.enabled(c):
            try:
                res = ai.suggest_recipes(c, pantry, recipes.staples(c), 3, n, "")
                out["ai"] = len(store_ai_recipes(c, res["recipes"]))
            except ai.AIError as e:
                warnings.append(f"AI recipes: {e}")
        recipes.mark_refreshed(c)
        return {**out, "warnings": warnings}

    runner.register("identify", identify)
    runner.register("receipt", receipt)
    runner.register("receipt:failed", receipt_failed)
    runner.register("shelf", shelf_job)
    runner.register("shelf_all", shelf_all)
    runner.register("mealdb", mealdb)
    runner.register("recipes", recipes_ai)
    runner.register("feed_refresh", feed_refresh)
    runner.register("import_recipe", import_recipe)
