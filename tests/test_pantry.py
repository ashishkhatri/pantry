"""Run with:  python -m unittest discover tests"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from pantry import ai, inventory, lookup, notify, recipes, shelf, webpush
from pantry import db as dbm
from pantry.app import create_app

H = {"X-Requested-With": "pantry"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.app = create_app(os.path.join(self.dir, "pantry.sqlite"), start_background=False)
        self.app.testing = True
        self.c = self.app.test_client()
        self.runner = self.app.config["JOBS"]

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def con(self):
        return dbm.connect(self.app.config["DB_PATH"])

    def login(self):
        r = self.c.post("/api/setup", json={"name": "Ashish", "password": "correct horse"}, headers=H)
        self.assertEqual(r.status_code, 200, r.json)

    def post(self, url, data=None):
        r = self.c.post(url, json=data or {}, headers=H)
        self.assertLess(r.status_code, 300, (url, r.json))
        return r.json

    def run_jobs(self):
        con = self.con()
        ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE status = 'queued'")]
        con.close()
        for j in ids:
            self.runner.run_now(j)


class AuthTests(Base):
    def test_setup_login_and_csrf(self):
        self.assertTrue(self.c.get("/api/me").json["setup_needed"])
        self.assertEqual(self.c.get("/api/items").status_code, 401)
        # Changes need the custom header (CSRF guard)
        self.assertEqual(self.c.post("/api/setup", json={"name": "A", "password": "longenough"}).status_code, 400)
        self.login()
        me = self.c.get("/api/me").json
        self.assertEqual(me["user"]["name"], "Ashish")
        self.assertTrue(me["push_key"])
        self.assertEqual(self.c.post("/api/setup", json={"name": "B", "password": "longenough"}, headers=H).status_code, 409)
        self.post("/api/users", {"name": "Priya", "password": "another one"})
        self.post("/api/logout")
        self.assertEqual(self.c.get("/api/items").status_code, 401)
        r = self.c.post("/api/login", json={"name": "priya", "password": "wrong"}, headers=H)
        self.assertEqual(r.status_code, 401)
        self.post("/api/login", {"name": "priya", "password": "another one"})
        self.assertFalse(self.c.get("/api/me").json["user"]["is_admin"])
        self.assertEqual(self.c.post("/api/users", json={"name": "X", "password": "12345678"}, headers=H).status_code, 403)

    def test_cookie_secure_follows_https(self):
        r = self.c.post("/api/setup", json={"name": "A", "password": "longenough"}, headers=H)
        self.assertNotIn("Secure", r.headers["Set-Cookie"])
        self.post("/api/logout")
        r = self.c.post("/api/login", json={"name": "A", "password": "longenough"},
                        headers={**H, "X-Forwarded-Proto": "https"})
        self.assertIn("Secure", r.headers["Set-Cookie"])

    def test_login_rate_limit(self):
        self.login()
        self.post("/api/logout")
        for _ in range(10):
            self.c.post("/api/login", json={"name": "Ashish", "password": "nope"}, headers=H)
        r = self.c.post("/api/login", json={"name": "Ashish", "password": "correct horse"}, headers=H)
        self.assertEqual(r.status_code, 429)


class ShelfTests(unittest.TestCase):
    def test_guess_category(self):
        cases = {"Whole Milk 1 gal": "milk", "Peanut Butter Creamy": "nut_butter", "Black Beans 15 oz": "canned",
                 "Boneless Skinless Chicken Breasts": "poultry_raw", "Baby Spinach": "leafy_greens",
                 "Frozen Peas": "frozen", "Sharp Cheddar": "cheese_hard", "Greek Yogurt": "yogurt",
                 "Jasmine Rice": "dry_goods", "Bananas": "fruit_counter", "Mystery thing": "other",
                 "Almond Milk": "plant_milk", "Ground Beef 80/20": "ground_meat", "Sourdough Bread": "bread"}
        for name, cat in cases.items():
            self.assertEqual(shelf.guess_category(name), cat, name)
        self.assertEqual(shelf.guess_category("Something", ["en:breakfast-cereals", "en:cereals-and-their-products"]),
                         "cereal")

    def test_fallback_days_for_odd_locations(self):
        chicken = {"category": "poultry_raw", "shelf_pantry_days": None, "shelf_fridge_days": None,
                   "shelf_freezer_days": None, "opened_days": None}
        self.assertEqual(shelf.shelf_days(chicken, "pantry"), 1)
        self.assertEqual(shelf.shelf_days(chicken, "freezer"), 270)
        frozen = dict(chicken, category="frozen")
        self.assertEqual(shelf.shelf_days(frozen, "fridge"), 3)

    def test_barcode_normalize(self):
        self.assertEqual(lookup.normalize_barcode("0 12345 67890 5"), "0012345678905")
        self.assertEqual(lookup.normalize_barcode("012345678905"), "0012345678905")
        self.assertIn("012345678905", lookup.barcode_variants("012345678905"))


class InventoryTests(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def add(self, name, **kw):
        return self.post("/api/items", {"product": {"name": name, **kw.pop("product", {})}, **kw})

    def test_add_sets_expiry_from_category(self):
        r = self.add("Whole Milk")
        it = r["item"]
        self.assertEqual(it["location"], "fridge")
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=5)).isoformat())   # 7 days, 5 once opened
        self.assertEqual(it["status"], "ok")
        # Same name again reuses the product
        r2 = self.add("whole milk")
        self.assertEqual(r2["product_id"], r["product_id"])
        self.assertFalse(r2["created_product"])

    def test_manual_date_and_status(self):
        r = self.add("Yogurt", expires_on=(date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(r["item"]["status"], "soon")
        r = self.add("Old bread", expires_on=(date.today() - timedelta(days=2)).isoformat())
        self.assertEqual(r["item"]["status"], "expired")
        s = self.c.get("/api/summary").json
        self.assertEqual(len(s["soon"]), 1)
        self.assertEqual(len(s["expired"]), 1)

    def test_scan_out_takes_soonest_first(self):
        p = self.add("Black beans", product={"barcode": "012345678905"}, quantity=2)
        pid = p["product_id"]
        self.post("/api/items", {"product_id": pid, "quantity": 1,
                                 "expires_on": (date.today() + timedelta(days=1)).isoformat()})
        r = self.post("/api/scan/out", {"barcode": "0012345678905"})
        self.assertEqual(r["in_stock"], 2)
        items = self.c.get(f"/api/items?product_id={pid}").json["items"]
        # The one expiring tomorrow was used first
        self.assertEqual([i["quantity"] for i in items], [2])
        self.post("/api/scan/out", {"product_id": pid, "quantity": 2})
        self.assertEqual(self.c.get(f"/api/items?product_id={pid}").json["items"], [])
        fin = self.c.get("/api/finished").json["items"]
        self.assertEqual(fin[0]["name"], "Black beans")
        r = self.c.post("/api/scan/out", json={"product_id": pid}, headers=H)
        self.assertEqual(r.status_code, 400)

    def test_plant_milk_defaults_to_fridge_and_manual_location_sticks(self):
        r = self.add("Almond Milk Unsweetened")
        self.assertEqual(r["item"]["location"], "fridge")
        self.c.patch(f"/api/products/{r['product_id']}", json={"default_location": "pantry"}, headers=H)
        p = self.c.get(f"/api/products/{r['product_id']}").json["product"]
        self.assertEqual((p["default_location"], p["shelf_source"]), ("pantry", "manual"))
        # Saving other fields with the same storage place doesn't count as overriding it
        r2 = self.add("Rice cakes")
        self.c.patch(f"/api/products/{r2['product_id']}", json={"name": "Rice cakes", "default_location": "pantry"}, headers=H)
        self.assertEqual(self.c.get(f"/api/products/{r2['product_id']}").json["product"]["shelf_source"], "category")

    def test_move_to_freezer(self):
        r = self.add("Chicken thighs")
        self.assertEqual(r["item"]["expires_on"], (date.today() + timedelta(days=2)).isoformat())
        self.c.patch(f"/api/items/{r['item_id']}", json={"location": "freezer"}, headers=H)
        it = self.c.get("/api/items").json["items"][0]
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=270)).isoformat())   # freezing isn't capped

    def test_everything_counts_as_opened(self):
        cases = {"Salsa": ("fridge", 7), "Sriracha hot sauce": ("fridge", 60), "Black beans": ("fridge", 4),
                 "Jasmine rice": ("pantry", 365), "Whole milk": ("fridge", 5), "Frozen peas": ("freezer", 240),
                 "Corn Nuts": ("pantry", 14)}
        for name, (loc, days) in cases.items():
            it = self.add(name)["item"]
            self.assertEqual((it["location"], it["expires_on"]),
                             (loc, (date.today() + timedelta(days=days)).isoformat()), name)
        r = self.add("Pickles")                       # your own numbers aren't capped ("other": pantry)
        self.c.patch(f"/api/products/{r['product_id']}", json={"shelf_pantry_days": 90}, headers=H)
        it = [i for i in self.c.get("/api/items").json["items"] if i["id"] == r["item_id"]][0]
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=90)).isoformat())
        # ...but category defaults for the other places still are ("other": fridge 14, opened 14)
        self.c.patch(f"/api/items/{r['item_id']}", json={"location": "fridge"}, headers=H)
        it = [i for i in self.c.get("/api/items").json["items"] if i["id"] == r["item_id"]][0]
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=14)).isoformat())

    def test_conversion_to_in_use_model(self):
        con = self.con()
        with con:
            pid = con.execute("INSERT INTO products(name, category, default_location) VALUES ('Ketchup', 'condiments', 'pantry')").lastrowid
            mine = con.execute("INSERT INTO products(name, category, default_location, shelf_source) "
                               "VALUES ('Mustard', 'condiments', 'pantry', 'manual')").lastrowid
            for p_ in (pid, mine):
                con.execute("INSERT INTO items(product_id, location, added_on, expires_on) VALUES (?, 'pantry', ?, ?)",
                            (p_, date.today().isoformat(), (date.today() + timedelta(days=365)).isoformat()))
            r = inventory.adopt_in_use_model(con)
        locs = dict(con.execute("SELECT p.name, i.location FROM items i JOIN products p ON p.id = i.product_id").fetchall())
        self.assertEqual(locs, {"Ketchup": "fridge", "Mustard": "pantry"})       # yours stays
        exp = con.execute("SELECT expires_on FROM items WHERE product_id = ?", (pid,)).fetchone()[0]
        self.assertEqual(exp, (date.today() + timedelta(days=60)).isoformat())
        self.assertEqual(r["moved"], 1)
        con.close()

    def test_product_shelf_override_recalculates(self):
        r = self.add("Sourdough")
        self.c.patch(f"/api/products/{r['product_id']}", json={"shelf_pantry_days": 3}, headers=H)
        it = self.c.get("/api/items").json["items"][0]
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=3)).isoformat())
        p = self.c.get(f"/api/products/{r['product_id']}").json["product"]
        self.assertEqual(p["shelf_source"], "manual")

    def test_use_discard_restore(self):
        r = self.add("Lettuce")
        self.post(f"/api/items/{r['item_id']}/use", {"quantity": "all", "reason": "discarded"})
        self.assertEqual(self.c.get("/api/items").json["items"], [])
        self.post(f"/api/items/{r['item_id']}/restore")
        self.assertEqual(len(self.c.get("/api/items").json["items"]), 1)
        kinds = [e["kind"] for e in self.c.get("/api/history").json["events"]]
        self.assertIn("discard", kinds)

    def test_barcode_lookup_local_then_off(self):
        off = {"status": 1, "product": {"product_name": "Organic Black Beans", "brands": "365,Whole Foods",
                                         "quantity": "15 oz", "categories_tags": ["en:canned-foods"]}}
        with mock.patch.object(lookup, "_fetch", return_value=off):
            r = self.c.get("/api/barcode/012345678905").json
        self.assertEqual(r["found"], "openfoodfacts")
        self.assertEqual(r["suggestion"]["brand"], "365")
        self.assertEqual(r["suggestion"]["category"], "canned")
        self.add(r["suggestion"]["name"], product=r["suggestion"])
        r = self.c.get("/api/barcode/012345678905").json
        self.assertEqual(r["found"], "local")
        self.assertEqual(len(r["items"]), 1)
        with mock.patch.object(lookup, "_fetch", return_value=None):
            self.assertIsNone(self.c.get("/api/barcode/99999999").json["found"])

    def test_bulk_add_from_receipt_review(self):
        con = self.con()
        with con:
            rid = con.execute("INSERT INTO receipts(user_id, status) VALUES (1, 'review')").lastrowid
        con.close()
        r = self.post("/api/items/bulk", {"source": "receipt", "receipt_id": rid, "items": [
            {"product": {"name": "Bananas", "category": "fruit_counter"}, "quantity": 6},
            {"product": {"name": "Paper towels"}, "skip": True},
            {"product": {"name": "Eggs"}, "quantity": 12, "location": "fridge"}]})
        self.assertEqual(r["added"], 2)
        con = self.con()
        self.assertEqual(con.execute("SELECT status FROM receipts WHERE id = ?", (rid,)).fetchone()[0], "done")
        con.close()


class RecipeTests(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_matching(self):
        yes = [("chicken breast", "Boneless Skinless Chicken Breasts"), ("olive oil", "Extra Virgin Olive Oil"),
               ("cheddar cheese", "Cheddar"), ("minced beef", "Ground beef 80/20"), ("eggs", "Large Eggs 12 ct"),
               ("aubergine", "Eggplant"), ("chicken stock", "Chicken Broth")]
        no = [("butter", "Peanut Butter"), ("milk", "Coconut milk"), ("cream", "Sour cream"),
              ("garlic", "Garlic powder"), ("potatoes", "Sweet potatoes"), ("chicken", "Chicken broth"),
              ("butter", "Butter tortillas"), ("flour", "Flour tortillas"), ("tomato", "Tomato paste"),
              ("garlic", "Garlic bread"), ("cheese", "Cheese crackers"), ("goat cheese", "Feta cheese"),
              ("cream cheese", "Cheddar cheese")]
        yes += [("cheese", "Cheddar cheese"), ("cheddar cheese", "Sharp Cheddar"), ("parmesan cheese", "Parmesan"), ("milk", "Whole Milk Vitamin D"), ("chicken", "Chicken thighs"), ("tortillas", "Butter tortillas")]
        for a, b in yes:
            self.assertTrue(recipes.matches(a, b), (a, b))
        for a, b in no:
            self.assertFalse(recipes.matches(a, b), (a, b))

    def _seed_library(self):
        def meal(i, name, ings):
            m = {"idMeal": str(i), "strMeal": name, "strCategory": "Test", "strArea": "X", "strMealThumb": None,
                 "strInstructions": "Cook."}
            for n, ing in enumerate(ings, 1):
                m[f"strIngredient{n}"] = ing
                m[f"strMeasure{n}"] = "1"
            return m

        meals = {"o": [meal(1, "Omelette", ["Eggs", "Butter", "Salt"])],
                 "c": [meal(2, "Chicken Rice", ["Chicken breast", "Rice", "Onion", "Garlic"]),
                       meal(3, "Cake", ["Flour", "Sugar", "Eggs", "Butter", "Milk", "Vanilla"]),
                       meal(4, "Clam chowder", ["Clams", "Bacon", "Potatoes", "Cream"])]}
        con = self.con()
        self.assertEqual(recipes.fetch_mealdb(con, lambda url: {"meals": meals.get(url[-1])})["recipes"], 4)
        dbm.set_setting(con, "last_feed_refresh", datetime.now().isoformat())
        dbm.set_setting(con, "feed_signature", recipes.pantry_signature(con))
        con.commit()
        con.close()

    def test_feed_sorted_by_missing(self):
        self._seed_library()
        for n in ("Large eggs", "Unsalted butter", "Chicken breasts", "Jasmine rice"):
            self.post("/api/items", {"product": {"name": n}})
        self.post("/api/items", {"product": {"name": "Yellow onion"},
                                 "expires_on": (date.today() + timedelta(days=1)).isoformat()})
        d = self.c.get("/api/recipes").json
        titles = [x["title"] for x in d["recipes"]]
        self.assertEqual(titles, ["Omelette", "Chicken Rice", "Cake"])       # 0, 1, 3 missing; chowder uses nothing
        self.assertEqual([m["name"] for m in d["recipes"][1]["missing"]], ["Garlic"])
        self.assertEqual(d["recipes"][1]["uses_soon"], ["Chicken breasts", "Yellow onion"])   # raw chicken keeps 2 days
        self.assertEqual([x["title"] for x in self.c.get("/api/recipes?can_make=1").json["recipes"]], ["Omelette"])
        self.assertEqual([x["title"] for x in self.c.get("/api/recipes?q=cake").json["recipes"]], ["Cake"])
        # Hiding takes it out of the feed
        rid = d["recipes"][2]["id"]
        self.post(f"/api/recipes/{rid}/hide")
        self.assertNotIn("Cake", [x["title"] for x in self.c.get("/api/recipes").json["recipes"]])

    def test_ingredient_name_is_used_for_matching(self):
        self._seed_library()
        r = self.post("/api/items", {"product": {"name": "Kerrygold Pure Irish"}})      # name doesn't say butter
        self.post("/api/items", {"product": {"name": "Large eggs"}})
        self.assertEqual(self.c.get("/api/recipes?can_make=1").json["recipes"], [])
        self.c.patch(f"/api/products/{r['product_id']}", json={"ingredient_name": "butter"}, headers=H)
        d = self.c.get("/api/recipes?can_make=1").json["recipes"]
        self.assertEqual([x["title"] for x in d], ["Omelette"])
        self.assertIn("Kerrygold Pure Irish", [h.get("product") for h in d[0]["have"]])

    def test_import_from_link(self):
        page = """<html><head><script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[{"@type":"WebPage","name":"x"},
         {"@type":["Recipe"],"name":"Garlic Butter Rice","image":[{"url":"https://img.example/rice.jpg"}],
          "description":"Simple &amp; quick.","totalTime":"PT1H5M","recipeYield":["4 servings"],
          "recipeIngredient":["2 cups jasmine rice, rinsed","3 tablespoons unsalted butter","4 cloves garlic, minced",
                              "1 teaspoon kosher salt"],
          "recipeInstructions":[{"@type":"HowToSection","itemListElement":[{"@type":"HowToStep","text":"Rinse the rice."},
                                {"@type":"HowToStep","text":"Cook with <b>butter</b>."}]}]}]}
        </script></head><body></body></html>"""
        for n in ("Jasmine rice", "Unsalted butter"):
            self.post("/api/items", {"product": {"name": n}})
        con = self.con()
        rid = recipes.import_url(con, "https://www.example.com/garlic-rice", lambda u: page)
        r = recipes.get(con, rid)
        con.close()
        self.assertEqual((r["title"], r["site"], r["time_minutes"], r["servings"]), ("Garlic Butter Rice", "example.com", 65, 4))
        self.assertEqual(r["image"], "https://img.example/rice.jpg")
        self.assertEqual(r["summary"], "Simple & quick.")
        self.assertEqual(r["steps"], ["Rinse the rice.", "Cook with butter."])
        self.assertEqual([m["name"] for m in r["missing"]], ["4 cloves garlic, minced"])     # salt is a staple
        with self.assertRaises(LookupError):
            recipes.parse_recipe_page("<html>no recipe here</html>", "https://x.example")

    PASTED = """Senate Bean Soup
By A Cook

Ingredients
1 pound dried navy beans, soaked overnight
1 smoked ham hock
1 large onion, chopped
Salt and pepper

Preparation
1. Drain the beans and put them in a pot with the ham hock and 3 quarts water.
2. Simmer 2 hours, add the onion, and cook 30 minutes more.
Cooking Notes
Great with cornbread.
"""

    def test_blocked_link_asks_for_paste(self):
        bot = "<html><head><title>Are you a robot?</title></head><body>Please verify you are a human. captcha</body></html>"
        with mock.patch.object(recipes, "fetch_html", return_value=bot):
            r = self.c.post("/api/recipes/import", json={"url": "https://cooking.nytimes.com/recipes/1-soup"}, headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json["paste"])
        self.assertIn("bot check", r.json["error"])
        with mock.patch.object(recipes, "fetch_html", side_effect=recipes.NeedsPaste("blocked (403)")):
            r = self.c.post("/api/recipes/import", json={"url": "https://x.example/r"}, headers=H)
        self.assertTrue(r.json["paste"])

    def test_paste_without_ai(self):
        self.post("/api/items", {"product": {"name": "Yellow onion"}})
        r = self.post("/api/recipes/import", {"url": "https://cooking.nytimes.com/recipes/1-soup", "text": self.PASTED})
        rec = r["recipe"]
        self.assertEqual((rec["title"], rec["site"], rec["source"]), ("Senate Bean Soup", "cooking.nytimes.com", "link"))
        self.assertEqual(len(rec["ingredients"]), 4)
        self.assertEqual(len(rec["steps"]), 2)                       # cooking notes left out
        self.assertIn("1 large onion, chopped", [h["name"] for h in rec["have"]])
        self.assertIn("Salt and pepper", [h["name"] for h in rec["have"]])     # staples
        # No headings at all: quantity lines become ingredients
        loose = "Quick Beans\n2 cups beans\n1 onion\nCook the beans with the onion until soft, about 20 minutes."
        rec = recipes.parse_pasted(loose)
        self.assertEqual(([i["name"] for i in rec["ingredients"]], len(rec["steps"])), (["2 cups beans", "1 onion"], 1))

    def test_spoonacular(self):
        self._seed_library()
        self.post("/api/items", {"product": {"name": "Baby spinach"}})
        self.post("/api/items", {"product": {"name": "Large eggs"}})
        calls = []

        def fetch(url):
            calls.append(url)
            if "findByIngredients" in url:
                return [{"id": 77}]
            return [{"id": 77, "title": "Spinach Frittata", "image": "https://img/77.jpg", "readyInMinutes": 25,
                     "servings": 2, "sourceUrl": "https://www.someblog.com/frittata",
                     "summary": "A <b>quick</b> frittata. More text.", "dishTypes": ["breakfast"], "cuisines": [],
                     "extendedIngredients": [{"nameClean": "spinach", "original": "2 cups spinach"},
                                             {"nameClean": "eggs", "original": "6 eggs"},
                                             {"nameClean": "feta", "original": "1/4 cup feta"}],
                     "analyzedInstructions": [{"steps": [{"step": "Whisk."}, {"step": "Bake."}]}]}]
        con = self.con()
        self.assertEqual(recipes.fetch_spoonacular(con, "KEY", recipes.focus_ingredients(con), fetch=fetch), 1)
        con.close()
        self.assertIn("ingredients=", calls[0])
        self.assertIn("apiKey=KEY", calls[1])
        d = self.c.get("/api/recipes?source=web").json["recipes"]
        self.assertEqual((d[0]["title"], d[0]["site"], d[0]["summary"]), ("Spinach Frittata", "someblog.com", "A quick frittata."))
        self.assertEqual([m["name"] for m in d[0]["missing"]], ["feta"])

    def test_spoonacular_placeholder_time(self):
        self.assertIsNone(recipes.spoon_minutes({"readyInMinutes": 45, "preparationMinutes": -1, "cookingMinutes": None}))
        self.assertEqual(recipes.spoon_minutes({"readyInMinutes": 45, "preparationMinutes": 15, "cookingMinutes": 30}), 45)
        self.assertEqual(recipes.spoon_minutes({"readyInMinutes": 25}), 25)
        self.assertIsNone(recipes.spoon_minutes({}))

    def test_spoonacular_daily_budget(self):
        con = self.con()
        self.assertIsNone(recipes.web_search_due(con))
        recipes._spoon_record(con, 45)                       # 5 points left: not enough for a search (~8)
        self.assertIn("nearly used up", recipes.web_search_due(con))
        recipes._spoon_record(con, 10)
        dbm.set_setting(con, "last_web_search", datetime.now().isoformat())
        con.commit()
        self.assertIn("4 hours", recipes.web_search_due(con))
        self.assertIsNone(recipes.web_search_due(con, force=True))   # "Find new recipes" still works
        self.assertEqual(recipes.spoon_usage(con)["used"], 10.0)
        dbm.set_setting(con, "spoon_day", "2000-01-01")               # yesterday's count doesn't carry over
        con.commit()
        self.assertEqual(recipes.spoon_usage(con)["used"], 0.0)
        con.close()

    def test_spoonacular_quota_headers_and_402(self):
        import io, urllib.error
        class Resp(io.BytesIO):
            headers = {"X-API-Quota-Used": "17.5"}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        con = self.con()
        with mock.patch.object(recipes.urllib.request, "urlopen", return_value=Resp(b"[]")):
            self.assertEqual(recipes.spoon_get(con, "https://api.spoonacular.com/x"), [])
        self.assertEqual(recipes.spoon_usage(con)["used"], 17.5)
        err = urllib.error.HTTPError("https://api.spoonacular.com/x", 402, "Payment Required", {}, io.BytesIO(b""))
        with mock.patch.object(recipes.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(LookupError) as cm:
                recipes.spoon_get(con, "https://api.spoonacular.com/x")
        self.assertIn("used up", str(cm.exception))
        self.assertEqual(recipes.spoon_usage(con)["left"], 0)
        con.close()

    def test_refresh_job_fills_feed(self):
        self.post("/api/items", {"product": {"name": "Large eggs"}})
        with mock.patch.object(recipes, "_get_json", side_effect=lambda url, timeout=20: {
                "meals": [{"idMeal": "9", "strMeal": "Boiled eggs", "strIngredient1": "Eggs", "strMeasure1": "2"}]
                if url.endswith("f=b") else None}):
            d = self.c.get("/api/recipes").json
            self.assertTrue(d["refreshing"])                    # empty library: refresh starts by itself
            self.assertEqual(self.c.get("/api/recipes").json["refreshing"], d["refreshing"])   # not twice
            self.run_jobs()
        j = self.c.get(f"/api/jobs/{d['refreshing']}").json["job"]
        self.assertEqual((j["status"], j["result"]["mealdb"]), ("done", 1))
        d = self.c.get("/api/recipes").json
        self.assertIsNone(d["refreshing"])                      # fresh now
        self.assertEqual([x["title"] for x in d["recipes"]], ["Boiled eggs"])

    def test_old_library_moves_to_recipes_table(self):
        import sqlite3
        path = os.path.join(self.dir, "old.sqlite")
        con = sqlite3.connect(path)
        con.executescript(dbm.MIGRATIONS[1] + dbm.MIGRATIONS[2] + "PRAGMA user_version = 2;")
        con.execute("INSERT INTO mealdb(id, name, ingredients) VALUES ('5', 'Old soup', '[{\"name\": \"Water\"}]')")
        con.commit()
        con.close()
        dbm.init_db(path)
        con = dbm.connect(path)
        self.assertEqual(con.execute("SELECT source, ext_id, title FROM recipes").fetchone()[:], ("mealdb", "5", "Old soup"))
        con.close()


class NotifyTests(Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_daily_message_announces_each_stage_once(self):
        t = date.today()
        self.post("/api/items", {"product": {"name": "Milk"}, "expires_on": (t + timedelta(days=2)).isoformat()})
        self.post("/api/items", {"product": {"name": "Bread"}, "expires_on": (t - timedelta(days=1)).isoformat()})
        self.post("/api/items", {"product": {"name": "Rice"}})
        con = self.con()
        msg = notify.daily_message(con, t)
        self.assertIn("Milk", msg["body"])
        self.assertIn("Bread", msg["body"])
        self.assertNotIn("Rice", msg["body"])
        self.assertIsNone(notify.daily_message(con, t))
        # Three days later the milk has expired: announced again, as expired
        msg = notify.daily_message(con, t + timedelta(days=3))
        self.assertIn("Past date: Milk", msg["body"])
        con.close()

    def test_tick_runs_once_per_day_after_hour(self):
        self.post("/api/items", {"product": {"name": "Milk"},
                                 "expires_on": (date.today() + timedelta(days=1)).isoformat()})
        con = self.con()
        con.execute("INSERT INTO push_subs(user_id, endpoint, p256dh, auth) VALUES (1, 'https://push.example/x', 'k', 'a')")
        con.commit()
        con.close()
        sent = []
        with mock.patch.object(notify, "send", side_effect=lambda v, s, d: sent.append(d)):
            early = datetime.combine(date.today(), datetime.min.time()).replace(hour=7)
            self.assertEqual(notify.tick(self.app.config["DB_PATH"], self.app.config["VAPID"], early), [])
            late = early.replace(hour=10)
            notify.tick(self.app.config["DB_PATH"], self.app.config["VAPID"], late)
            notify.tick(self.app.config["DB_PATH"], self.app.config["VAPID"], late)
        self.assertEqual(len(sent), 1)
        self.assertIn("Milk", sent[0]["body"])

    def test_gone_subscription_is_removed(self):
        self.post("/api/push/subscribe", {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}})
        con = self.con()
        with mock.patch.object(notify, "send", side_effect=webpush.PushError("410", gone=True)):
            r = notify.push_all(con, self.app.config["VAPID"], {"title": "t"})
        self.assertEqual(r["failed"], 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0], 0)
        con.close()


class WebPushTests(unittest.TestCase):
    def test_rfc8291_vector(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        as_priv = ec.derive_private_key(int.from_bytes(webpush.b64u_decode(
            "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        out = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                              "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                              "BTBZMqHH6r4Tts7J_aSIgg", salt=webpush.b64u_decode("DGv6ra1nlYgDCS1FRnbzlw"),
                              as_private=as_priv)
        self.assertEqual(webpush.b64u(out),
                         "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzA"
                         "C8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

    def test_vapid_header(self):
        d = tempfile.mkdtemp()
        v = webpush.Vapid.load_or_create(os.path.join(d, "k.pem"))
        self.assertEqual(len(webpush.b64u_decode(v.public_key_b64)), 65)
        h = v.auth_header("https://fcm.googleapis.com/fcm/send/abc")
        self.assertTrue(h.startswith("vapid t="))
        claims = json.loads(webpush.b64u_decode(h.split("t=")[1].split(".")[1]))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        # Reloading gives the same key
        self.assertEqual(webpush.Vapid.load_or_create(os.path.join(d, "k.pem")).public_key_b64, v.public_key_b64)
        shutil.rmtree(d)


# ---------------- AI harness against a fake Ollama ----------------

class FakeOllama(BaseHTTPRequestHandler):
    answers = {}
    requests = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen2.5vl:3b"}, {"name": "llama3.2:3b"}]})
        else:
            self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        prompt = body["messages"][-1]["content"]
        for key, ans in FakeOllama.answers.items():
            if key in prompt:
                return self._send({"message": {"role": "assistant", "content": ans}})
        self._send({"message": {"content": "{\"ok\": true}"}})


class AITests(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        super().setUp()
        self.login()
        FakeOllama.requests.clear()
        self.c.put("/api/settings", json={"ai": {"ai_provider": "ollama", "ai_base_url": self.url,
                                                 "ai_model": "llama3.2:3b", "ai_vision_model": "qwen2.5vl:3b"}},
                   headers=H)

    def test_model_dropdown_and_first_loaded_model(self):
        r = self.c.get("/api/ai/models?" + __import__("urllib.parse").parse.urlencode(
            {"provider": "ollama", "base_url": self.url.replace("http://", "")})).json
        self.assertEqual(r["models"], ["llama3.2:3b", "qwen2.5vl:3b"])
        r = self.c.get("/api/ai/models?provider=ollama&base_url=127.0.0.1:1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't answering", r.json["error"])
        # Blank model = first one the server lists (like Budget's "First loaded model")
        self.c.put("/api/settings", json={"ai": {"ai_model": "", "ai_vision_model": ""}}, headers=H)
        FakeOllama.answers = {"Estimate how long": json.dumps({"pantry_days": 10})}
        con = self.con()
        ai.shelf_life(con, "Bread")
        con.close()
        self.assertEqual(FakeOllama.requests[-1]["model"], "llama3.2:3b")

    def test_address_without_http(self):
        self.assertEqual(ai.normalize_url("192.168.1.25:1234/v1/"), "http://192.168.1.25:1234/v1")
        self.assertEqual(ai.normalize_url("https://api.anthropic.com"), "https://api.anthropic.com")
        bare = self.url.replace("http://", "")
        self.c.put("/api/settings", json={"ai": {"ai_base_url": bare}}, headers=H)
        self.assertEqual(self.c.get("/api/settings").json["ai"]["ai_base_url"], self.url)
        r = self.post("/api/ai/test", {"ai": {"ai_provider": "ollama", "ai_base_url": bare, "ai_model": "llama3.2:3b"}})
        self.assertTrue(r["reply_ok"])

    def test_parse_json_variants(self):
        self.assertEqual(ai.parse_json('<think>hmm {x}</think>\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ai.parse_json('Sure! Here it is: {"a": [1,2]} hope that helps'), {"a": [1, 2]})
        with self.assertRaises(ai.AIError):
            ai.parse_json("no json here")

    def test_connection_lists_models(self):
        r = self.post("/api/ai/test")
        self.assertEqual(r["models"], ["llama3.2:3b", "qwen2.5vl:3b"])
        r = self.post("/api/ai/test", {"ai": {"ai_provider": "ollama", "ai_base_url": self.url, "ai_model": "llama3.2:3b"}})
        self.assertTrue(r["reply_ok"])
        self.assertFalse(FakeOllama.requests[-1]["think"])

    def test_settings_hide_api_key(self):
        self.c.put("/api/settings", json={"ai": {"ai_api_key": "sk-secret"}}, headers=H)
        s = self.c.get("/api/settings").json["ai"]
        self.assertEqual(s["ai_api_key"], "")
        self.assertEqual(s["ai_api_key_set"], "1")
        self.c.put("/api/settings", json={"ai": {"ai_api_key": ""}}, headers=H)   # blank keeps it
        con = self.con()
        self.assertEqual(ai.get_settings(con, True)["ai_api_key"], "sk-secret")
        con.close()

    def test_new_product_gets_ai_shelf_life(self):
        FakeOllama.answers = {"Estimate how long": json.dumps(
            {"category": "condiments", "pantry_days": 300, "fridge_days": 200, "freezer_days": None,
             "opened_days": 30, "best_location": "pantry", "ingredient_name": "Gochujang",
             "note": "Refrigerate after opening."})}
        r = self.post("/api/items", {"product": {"name": "Gochujang"}})
        self.assertTrue(r["shelf_job"])
        self.run_jobs()
        j = self.c.get(f"/api/jobs/{r['shelf_job']}").json["job"]
        self.assertEqual(j["status"], "done", j)
        p = self.c.get(f"/api/products/{r['product_id']}").json["product"]
        self.assertEqual((p["shelf_source"], p["shelf_pantry_days"], p["category"]), ("ai", 300, "condiments"))
        self.assertEqual(p["ingredient_name"], "gochujang")
        it = self.c.get("/api/items").json["items"][0]
        self.assertEqual(it["expires_on"], (date.today() + timedelta(days=300)).isoformat())

    def test_estimate_existing_products(self):
        self.c.put("/api/settings", json={"ai": {"ai_auto_shelf": False}}, headers=H)
        for n in ("Gochujang", "Fish sauce", "Mystery jar"):
            self.post("/api/items", {"product": {"name": n}})
        mine = self.post("/api/items", {"product": {"name": "Homemade jam"}})
        self.c.patch(f"/api/products/{mine['product_id']}", json={"shelf_pantry_days": 20}, headers=H)
        st = self.c.get("/api/ai/shelf-status").json
        self.assertEqual((st["defaults"], st["ai"], st["manual"]), (3, 0, 1))
        FakeOllama.answers = {"Estimate how long": json.dumps(
            {"category": "condiments", "pantry_days": 400, "fridge_days": None, "freezer_days": None,
             "opened_days": 60, "best_location": "pantry"})}
        job = self.post("/api/ai/shelf-all")["job"]
        self.assertEqual(self.post("/api/ai/shelf-all")["job"], job)      # no duplicate runs
        self.run_jobs()
        r = self.c.get(f"/api/jobs/{job}").json["job"]
        self.assertEqual(r["status"], "done", r)
        self.assertEqual((r["result"]["updated"], r["result"]["total"]), (3, 3))
        st = self.c.get("/api/ai/shelf-status").json
        self.assertEqual((st["defaults"], st["ai"], st["manual"], st["job"]), (0, 3, 1, None))
        items = {i["name"]: i for i in self.c.get("/api/items").json["items"]}
        self.assertEqual(items["Gochujang"]["expires_on"], (date.today() + timedelta(days=400)).isoformat())
        self.assertEqual(items["Homemade jam"]["expires_on"], (date.today() + timedelta(days=20)).isoformat())

    def test_ai_sets_storage_location(self):
        self.c.put("/api/settings", json={"ai": {"ai_auto_shelf": False}}, headers=H)
        guessed = self.post("/api/items", {"product": {"name": "Tortilla chips"}})          # pantry by category
        chosen = self.post("/api/items", {"product_id": guessed["product_id"], "location": "freezer"})  # you chose
        moved_by_you = self.post("/api/items", {"product_id": guessed["product_id"]})
        self.c.patch(f"/api/items/{moved_by_you['item_id']}", json={"location": "freezer"}, headers=H)
        self.c.patch(f"/api/items/{moved_by_you['item_id']}", json={"location": "pantry"}, headers=H)
        FakeOllama.answers = {"Estimate how long": json.dumps(
            {"category": "snacks", "pantry_days": 60, "fridge_days": 90, "freezer_days": None,
             "opened_days": 14, "best_location": "fridge", "opened_location": "fridge"})}
        job = self.post(f"/api/products/{guessed['product_id']}/ai-shelf")["job"]
        self.run_jobs()
        r = self.c.get(f"/api/jobs/{job}").json["job"]["result"]
        self.assertEqual((r["default_location"], r["moved"]), ("fridge", 1))
        locs = {i["id"]: i for i in self.c.get("/api/items").json["items"]}
        self.assertEqual(locs[guessed["item_id"]]["location"], "fridge")
        # Dated from when it was added, with the fridge's days
        self.assertEqual(locs[guessed["item_id"]]["expires_on"], (date.today() + timedelta(days=90)).isoformat())
        self.assertEqual(locs[chosen["item_id"]]["location"], "freezer")
        self.assertEqual(locs[moved_by_you["item_id"]]["location"], "pantry")
        p = self.c.get(f"/api/products/{guessed['product_id']}").json["product"]
        self.assertEqual(p["default_location"], "fridge")

    def test_estimate_existing_stops_when_server_down(self):
        self.c.put("/api/settings", json={"ai": {"ai_auto_shelf": False}}, headers=H)
        for n in ("A thing", "B thing"):
            self.post("/api/items", {"product": {"name": n}})
        self.c.put("/api/settings", json={"ai": {"ai_base_url": "http://127.0.0.1:1"}}, headers=H)
        job = self.post("/api/ai/shelf-all")["job"]
        self.run_jobs()
        r = self.c.get(f"/api/jobs/{job}").json["job"]
        self.assertEqual(r["status"], "error")
        self.assertIn("Stopped after 0 of 2", r["error"])

    def test_receipt_flow(self):
        self.post("/api/items", {"product": {"name": "Whole milk", "brand": "Great Value"}})
        FakeOllama.answers = {"grocery store receipt": json.dumps({
            "store": "Walmart", "date": "2026-10-28", "items": [
                {"raw": "GV WHL MLK GAL", "name": "Whole milk", "brand": "Great Value", "size": "1 gal",
                 "quantity": 1, "category": "milk", "is_food": True},
                {"raw": "BNNA", "name": "Bananas", "quantity": "6", "category": "fruit_counter", "is_food": True},
                {"raw": "BOUNTY", "name": "Paper towels", "quantity": 1, "category": "other", "is_food": False}]})}
        r = self.c.post("/api/receipts", data={"image": (open(__file__, "rb"), "r.jpg", "image/jpeg")},
                        headers=H, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.json)
        self.run_jobs()
        rec = self.c.get(f"/api/receipts/{r.json['receipt_id']}").json["receipt"]
        self.assertEqual(rec["status"], "review")
        self.assertEqual(rec["store"], "Walmart")
        lines = rec["job"]["result"]["items"]
        self.assertEqual(lines[0]["product_name"], "Whole milk")      # linked to the existing product
        self.assertEqual(lines[1]["quantity"], 6)
        self.assertFalse(lines[2]["is_food"])
        self.assertTrue(FakeOllama.requests[-1]["messages"][-1]["images"])
        self.assertEqual(FakeOllama.requests[-1]["model"], "qwen2.5vl:3b")

    def test_receipt_keeps_small_copy_only(self):
        FakeOllama.answers = {"grocery store receipt": json.dumps({"store": "H-E-B", "items": [
            {"raw": "MILK", "name": "Milk", "quantity": 1, "category": "milk", "is_food": True}]})}
        big, small = b"\xff\xd8" + b"F" * 50000, b"\xff\xd8" + b"s" * 900
        r = self.c.post("/api/receipts", data={"image": (io.BytesIO(big), "r.jpg", "image/jpeg"),
                                                "archive": (io.BytesIO(small), "s.jpg", "image/jpeg")},
                        headers=H, content_type="multipart/form-data")
        rid = r.json["receipt_id"]
        full = os.path.join(self.dir, "tmp", f"receipt-{rid}-full.jpg")
        self.assertTrue(os.path.exists(full))                           # kept until the AI reads it
        self.run_jobs()
        self.assertFalse(os.path.exists(full))                          # then deleted
        img = self.c.get(f"/api/receipts/{rid}/image")
        self.assertEqual(img.data, small)                               # the small copy is what's kept
        self.assertTrue(self.c.get("/api/receipts").json["receipts"][0]["has_image"])
        self.assertEqual(FakeOllama.requests[-1]["messages"][-1]["images"][0],
                         __import__("base64").b64encode(big).decode())   # the AI got the full photo

    @staticmethod
    def text_pdf(lines):
        """A minimal real PDF with a text layer, like a store's e-receipt."""
        stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
        objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                "/Resources << /Font << /F1 5 0 R >> >> >>",
                f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
                "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        out, offs = "%PDF-1.4\n", []
        for i, o in enumerate(objs, 1):
            offs.append(len(out))
            out += f"{i} 0 obj\n{o}\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
        out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        return out.encode("latin-1")

    def test_pdf_receipt_with_text(self):
        FakeOllama.answers = {"RECEIPT TEXT": json.dumps({"store": "Target", "date": "2026-11-01", "items": [
            {"raw": "GG MILK 1GAL", "name": "Whole milk", "quantity": 1, "category": "milk", "is_food": True}]})}
        data = self.text_pdf(["TARGET STORE 1234", "11/01/2026", "GG MILK 1GAL   3.49", "TOTAL 3.49"])
        r = self.c.post("/api/receipts", data={"image": (io.BytesIO(data), "receipt.pdf", "application/pdf")},
                        headers=H, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.json)
        self.run_jobs()
        rec = self.c.get(f"/api/receipts/{r.json['receipt_id']}").json["receipt"]
        self.assertEqual((rec["status"], rec["store"], rec["is_pdf"]), ("review", "Target", True))
        sent = FakeOllama.requests[-1]
        self.assertIn("GG MILK 1GAL", sent["messages"][-1]["content"])     # the PDF's text went to the AI
        self.assertNotIn("images", sent["messages"][-1])                    # no picture needed
        self.assertEqual(sent["model"], "llama3.2:3b")                       # text model
        img = self.c.get(f"/api/receipts/{r.json['receipt_id']}/image")
        self.assertEqual((img.mimetype, img.data[:5]), ("application/pdf", b"%PDF-"))   # the PDF is what's kept

    def test_scanned_pdf_receipt(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (600, 1000), "white")
        ImageDraw.Draw(im).text((50, 50), "SCANNED RECEIPT  MILK 3.49", fill="black")
        buf = io.BytesIO()
        im.save(buf, "PDF")                                                  # image-only PDF, no text layer
        FakeOllama.answers = {"grocery store receipt": json.dumps({"store": "Kroger", "items": [
            {"raw": "MILK", "name": "Milk", "quantity": 1, "category": "milk", "is_food": True}]})}
        r = self.c.post("/api/receipts", data={"image": (io.BytesIO(buf.getvalue()), "scan.pdf", "application/pdf")},
                        headers=H, content_type="multipart/form-data")
        self.run_jobs()
        rec = self.c.get(f"/api/receipts/{r.json['receipt_id']}").json["receipt"]
        self.assertEqual((rec["status"], rec["store"]), ("review", "Kroger"), rec)
        sent = FakeOllama.requests[-1]
        self.assertEqual(len(sent["messages"][-1]["images"]), 1)            # the page went as a picture
        self.assertEqual(sent["model"], "qwen2.5vl:3b")                      # vision model

    def test_not_really_a_pdf(self):
        r = self.c.post("/api/receipts", data={"image": (io.BytesIO(b"hello"), "x.pdf", "application/pdf")},
                        headers=H, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/api/receipts").json["receipts"], [])

    def test_receipt_failure_marks_error(self):
        self.c.put("/api/settings", json={"ai": {"ai_base_url": "http://127.0.0.1:1"}}, headers=H)
        r = self.c.post("/api/receipts", data={"image": (open(__file__, "rb"), "r.jpg", "image/jpeg")},
                        headers=H, content_type="multipart/form-data")
        self.run_jobs()
        rec = self.c.get(f"/api/receipts/{r.json['receipt_id']}").json["receipt"]
        self.assertEqual(rec["status"], "error")
        self.assertIn("isn't answering", rec["job"]["error"])
        # The full photo stays for a retry
        self.assertTrue(os.path.exists(os.path.join(self.dir, "tmp", f"receipt-{r.json['receipt_id']}-full.jpg")))

    def test_unreachable_ai_fails_fast(self):
        # 192.0.2.1 is a reserved test address that never answers: the probe gives up in ~5s.
        self.c.put("/api/settings", json={"ai": {"ai_base_url": "http://192.0.2.1:1234"}}, headers=H)
        con = self.con()
        t0 = __import__("time").time()
        with self.assertRaises(ai.AIError) as cm:
            ai.shelf_life(con, "Milk")
        con.close()
        self.assertLess(__import__("time").time() - t0, 8)
        self.assertIn("isn't answering", str(cm.exception))

    def test_identify_photo(self):
        FakeOllama.answers = {"Identify the grocery": json.dumps({"items": [
            {"name": "Black beans", "brand": "Goya", "quantity": 3, "category": "canned"},
            {"name": "Stapler", "category": "other", "is_food": False}]})}
        r = self.c.post("/api/ai/identify", data={"image": (open(__file__, "rb"), "p.jpg", "image/jpeg")},
                        headers=H, content_type="multipart/form-data")
        self.run_jobs()
        j = self.c.get(f"/api/jobs/{r.json['job']}").json["job"]
        self.assertEqual([i["name"] for i in j["result"]["items"]], ["Black beans"])
        self.assertEqual(j["result"]["items"][0]["quantity"], 3)
        self.assertEqual(os.listdir(os.path.join(self.dir, "tmp")), [])     # temp photo deleted

    def test_paste_with_ai_and_page_without_recipe_data(self):
        FakeOllama.answers = {"Below is the text of a recipe": json.dumps({
            "title": "Senate Bean Soup", "summary": "A classic.", "time_minutes": 180, "servings": 8,
            "ingredients": [{"name": "navy beans", "amount": "1 pound, soaked"}, {"name": "ham hock", "amount": "1"},
                            {"name": "onion", "amount": "1 large"}],
            "steps": ["Simmer the beans with the ham hock.", "Add onion."]})}
        r = self.post("/api/recipes/import", {"url": "https://cooking.nytimes.com/recipes/1-soup", "text": RecipeTests.PASTED})
        self.run_jobs()
        res = self.c.get(f"/api/jobs/{r['job']}").json["job"]["result"]
        rec = self.c.get(f"/api/recipes/{res['id']}").json["recipe"]
        self.assertEqual((rec["title"], rec["site"], rec["time_minutes"]), ("Senate Bean Soup", "cooking.nytimes.com", 180))
        self.assertEqual(rec["ingredients"][0], {"name": "navy beans", "amount": "1 pound, soaked"})
        # A readable page with no schema.org data: the AI reads the page text
        page = "<html><body><h1>Senate Bean Soup</h1>" + "<p>Some story about soup.</p>" * 30 + \
               "<h2>Ingredients</h2><ul><li>1 pound navy beans</li><li>1 ham hock</li></ul></body></html>"
        with mock.patch.object(recipes, "fetch_html", return_value=page):
            r = self.post("/api/recipes/import", {"url": "https://blog.example/soup"})
        self.assertIn("job", r)
        self.run_jobs()
        self.assertEqual(self.c.get(f"/api/jobs/{r['job']}").json["job"]["status"], "done")
        self.assertIn("Some story about soup", FakeOllama.requests[-1]["messages"][-1]["content"])

    def test_ai_recipes_stored_and_rechecked(self):
        self.post("/api/items", {"product": {"name": "Spaghetti"}})
        self.post("/api/items", {"product": {"name": "Canned tomatoes"}})
        FakeOllama.answers = {"Suggest 6 recipes": json.dumps({"recipes": [
            {"title": "Pasta pomodoro", "summary": "Quick.", "time_minutes": 20, "servings": 2,
             "ingredients": [{"name": "spaghetti", "amount": "200 g", "in_house": True},
                             {"name": "tomatoes", "amount": "1 can", "in_house": True},
                             {"name": "basil", "amount": "a few leaves", "in_house": True},
                             {"name": "salt", "amount": "", "in_house": True}],
             "steps": ["Boil pasta.", "Make sauce."]}]})}
        r = self.post("/api/recipes/ai", {"max_missing": 2})
        self.run_jobs()
        self.assertEqual(self.c.get(f"/api/jobs/{r['job']}").json["job"]["result"]["count"], 1)
        feed = self.c.get("/api/recipes?source=ai").json["recipes"]
        self.assertEqual([m["name"] for m in feed[0]["missing"]], ["basil"])   # model said in_house; it isn't
        self.assertIn("google.com/search", feed[0]["search_url"])
        full = self.c.get(f"/api/recipes/{feed[0]['id']}").json["recipe"]
        self.assertEqual(full["steps"], ["Boil pasta.", "Make sauce."])

    def test_ai_not_configured(self):
        self.c.put("/api/settings", json={"ai": {"ai_provider": "none"}}, headers=H)
        r = self.c.post("/api/ai/identify", data={"image": (open(__file__, "rb"), "p.jpg", "image/jpeg")},
                        headers=H, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        r = self.post("/api/items", {"product": {"name": "Thing"}})
        self.assertIsNone(r["shelf_job"])


if __name__ == "__main__":
    unittest.main()
