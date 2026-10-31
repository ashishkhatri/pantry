"""AI harness: one interface over several model servers, plus the pantry's AI tasks.

Providers (pick one in Settings → AI):
  ollama     Ollama's native API (http://host:11434). Images, JSON mode, thinking off.
  openai     Any OpenAI-compatible server: LM Studio, llama.cpp, vLLM, Ollama's /v1, OpenAI.
  anthropic  Claude via api.anthropic.com (needs an API key).

Tasks (all return parsed JSON):
  identify_food(image)    what grocery items are in a photo
  parse_receipt(image)    store, date and food lines from a receipt photo
  shelf_life(product)     how many days it keeps in pantry/fridge/freezer, and once opened
  suggest_recipes(...)    recipes from what's in stock

Every request is logged to data/ai.log (timing, size, first part of the answer).
Calls can take minutes on a CPU-only server, so the web app always runs them as
background jobs (see jobs.py) rather than inside a web request.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import socket
import time
import urllib.parse
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import db as dbm
from . import shelf

log = logging.getLogger("pantry.ai")

DEFAULTS: Dict[str, str] = {
    "ai_provider": "none",          # none | ollama | openai | anthropic
    "ai_base_url": "",              # blank = the provider's usual address
    "ai_api_key": "",
    "ai_model": "",                 # text tasks (shelf life, recipes)
    "ai_vision_model": "",          # photos and receipts; blank = same as ai_model
    "ai_timeout": "600",            # seconds per request; CPU inference is slow
    "ai_think": "0",                # let reasoning models think (slower, rarely needed here)
    "ai_auto_shelf": "1",           # ask the AI for shelf life when a new product is saved
    "ai_num_ctx": "8192",           # Ollama context window
}
SECRET_KEYS = {"ai_api_key"}
PROVIDER_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "http://127.0.0.1:1234/v1",
    "anthropic": "https://api.anthropic.com",
}
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-5-5"


def normalize_url(url: str) -> str:
    """Accept what people type: "192.168.1.25:1234/v1" -> "http://192.168.1.25:1234/v1"."""
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    return url


class AIError(Exception):
    pass


class NotConfigured(AIError):
    pass


# ---------------- settings ----------------

def get_settings(con, include_secret: bool = False) -> Dict[str, str]:
    rows = dict(con.execute("SELECT key, value FROM settings WHERE key LIKE 'ai_%'").fetchall())
    s = {**DEFAULTS, **{k: v for k, v in rows.items() if k in DEFAULTS}}
    if not include_secret:
        s["ai_api_key_set"] = "1" if s.get("ai_api_key") else "0"
        s["ai_api_key"] = ""
    return s


def save_settings(con, values: dict) -> None:
    for k in DEFAULTS:
        if k not in values or values[k] is None:
            continue
        v = values[k]
        if k in SECRET_KEYS and v == "":
            continue                     # blank = keep the saved key
        if isinstance(v, bool):
            v = "1" if v else "0"
        if k == "ai_base_url":
            v = normalize_url(str(v))
        if k == "ai_provider" and v not in ("none", "ollama", "openai", "anthropic"):
            raise AIError("Unknown AI provider.")
        dbm.set_setting(con, k, str(v).strip())


def enabled(con) -> bool:
    return get_settings(con)["ai_provider"] != "none"


# ---------------- HTTP ----------------

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # LAN traffic never via a proxy


def _http(url: str, payload: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 30,
          use_proxy: bool = False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=h)
    opener = urllib.request.build_opener() if use_proxy else _opener
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise AIError(f"The AI server returned an error ({e.code}): {body}") from e
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise AIError(f"Can't reach the AI server at {url} ({reason}).") from e
    except ValueError as e:
        raise AIError(f"The AI server at {url} didn't answer with JSON.") from e


# ---------------- providers ----------------

class Provider:
    name = "base"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 600, think: bool = False,
                 num_ctx: int = 8192):
        self.base_url = normalize_url(base_url or PROVIDER_URLS.get(self.name, ""))
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.think = think
        self.num_ctx = num_ctx

    def chat(self, system: str, prompt: str, images: Optional[List[bytes]] = None, json_mode: bool = True,
             max_tokens: int = 4000) -> str:
        raise NotImplementedError

    def probe(self, timeout: float = 5) -> None:
        """Fail in seconds, not after the full timeout, when the AI machine is asleep or off.
        Only for servers on your network; cloud APIs are always up."""
        u = urllib.parse.urlsplit(self.base_url)
        if self.name == "anthropic" or not u.hostname:
            return
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            socket.create_connection((u.hostname, port), timeout=timeout).close()
        except OSError as e:
            raise AIError(f"The AI server at {u.hostname}:{port} isn't answering. Is that PC on and awake, "
                          f"with LM Studio or Ollama running? ({e})") from e

    def models(self) -> List[str]:
        raise NotImplementedError


class OllamaProvider(Provider):
    name = "ollama"

    def chat(self, system, prompt, images=None, json_mode=True, max_tokens=4000):
        msg = {"role": "user", "content": prompt}
        if images:
            msg["images"] = [base64.b64encode(i).decode("ascii") for i in images]
        payload = {"model": self.model, "stream": False, "keep_alive": "10m",
                   "messages": [{"role": "system", "content": system}, msg],
                   "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens, "temperature": 0.2}}
        if json_mode:
            payload["format"] = "json"
        if not self.think:
            payload["think"] = False
        try:
            r = _http(f"{self.base_url}/api/chat", payload, timeout=self.timeout)
        except AIError as e:
            # Older Ollama versions, or models without a thinking switch, reject "think".
            if "think" in str(e).lower() and "think" in payload:
                payload.pop("think")
                r = _http(f"{self.base_url}/api/chat", payload, timeout=self.timeout)
            else:
                raise
        return (r.get("message") or {}).get("content") or ""

    def models(self):
        r = _http(f"{self.base_url}/api/tags", timeout=10)
        return sorted(m.get("name") or m.get("model") for m in r.get("models", []))


class OpenAIProvider(Provider):
    name = "openai"

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key or 'local'}"}

    def chat(self, system, prompt, images=None, json_mode=True, max_tokens=4000):
        content = prompt
        if images:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
                for i in images]
        payload = {"model": self.model or "default", "temperature": 0.2, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        remote = not re.match(r"https?://(127\.|localhost|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", self.base_url)
        try:
            r = _http(f"{self.base_url}/chat/completions", payload, self._headers(), self.timeout, use_proxy=remote)
        except AIError as e:
            # Some local servers (LM Studio) don't accept json_object; the prompt asks for JSON anyway.
            if json_mode and "response_format" in str(e):
                payload.pop("response_format")
                r = _http(f"{self.base_url}/chat/completions", payload, self._headers(), self.timeout, use_proxy=remote)
            else:
                raise
        choices = r.get("choices") or [{}]
        return (choices[0].get("message") or {}).get("content") or ""

    def models(self):
        r = _http(f"{self.base_url}/models", headers=self._headers(), timeout=10)
        return sorted(m["id"] for m in r.get("data", []))


class AnthropicProvider(Provider):
    name = "anthropic"

    def _headers(self):
        if not self.api_key:
            raise NotConfigured("Add your Anthropic API key in Settings → AI.")
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def chat(self, system, prompt, images=None, json_mode=True, max_tokens=4000):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(i).decode()}} for i in images or []]
        content.append({"type": "text", "text": prompt})
        payload = {"model": self.model or ANTHROPIC_DEFAULT_MODEL, "max_tokens": max_tokens, "system": system,
                   "messages": [{"role": "user", "content": content}]}
        r = _http(f"{self.base_url}/v1/messages", payload, self._headers(), self.timeout, use_proxy=True)
        return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")

    def models(self):
        r = _http(f"{self.base_url}/v1/models?limit=100", headers=self._headers(), timeout=15, use_proxy=True)
        return [m["id"] for m in r.get("data", [])]


PROVIDERS = {"ollama": OllamaProvider, "openai": OpenAIProvider, "anthropic": AnthropicProvider}


def list_models(con, kind: str, base_url: str = "") -> List[str]:
    """Models the server offers, for the Settings dropdowns (embedding models left out)."""
    if kind not in PROVIDERS:
        raise NotConfigured("Choose a provider first.")
    p = PROVIDERS[kind](base_url, "", get_settings(con, include_secret=True)["ai_api_key"], 15)
    p.probe()
    return [m for m in p.models() if m and "embed" not in m.lower()]


def provider(con, vision: bool = False, override: Optional[dict] = None) -> Provider:
    s = {**get_settings(con, include_secret=True), **(override or {})}
    if override and not override.get("ai_api_key"):
        s["ai_api_key"] = get_settings(con, include_secret=True)["ai_api_key"]
    kind = s["ai_provider"]
    if kind not in PROVIDERS:
        raise NotConfigured("AI isn't set up. Choose a provider in Settings → AI.")
    model = (s["ai_vision_model"] or s["ai_model"]) if vision else s["ai_model"]
    p = PROVIDERS[kind](s["ai_base_url"], model, s["ai_api_key"], float(s["ai_timeout"] or 600),
                        s["ai_think"] == "1", int(s["ai_num_ctx"] or 8192))
    if not model and kind != "anthropic":
        # "First loaded model", like Budget: whatever the server lists first.
        p.probe()
        models = [m for m in p.models() if m and "embed" not in m.lower()]
        if not models:
            raise NotConfigured("The AI server has no models available. Load one, or pick a model in Settings → AI.")
        p.model = models[0]
    return p


# ---------------- JSON answers ----------------

def parse_json(text: str):
    """Pull a JSON value out of a model's answer: strips <think> blocks and ``` fences, then
    falls back to the outermost {...} or [...]."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    raise AIError("The model's answer wasn't valid JSON. Try again, or a larger model.")


def ask_json(p: Provider, task: str, system: str, prompt: str, images=None, max_tokens: int = 4000):
    t0 = time.time()
    kb = sum(len(i) for i in images or []) // 1024
    try:
        p.probe()
        text = p.chat(system, prompt, images, json_mode=True, max_tokens=max_tokens)
    except Exception as e:
        log.warning("%s %s/%s failed after %.1fs: %s", task, p.name, p.model, time.time() - t0, e)
        raise
    log.info("%s %s/%s %.1fs img=%dKB answer=%r", task, p.name, p.model, time.time() - t0, kb, text[:600])
    return parse_json(text)


# ---------------- tasks ----------------

CATEGORY_LIST = ", ".join(shelf.CATEGORIES)
SYSTEM = ("You are the assistant inside a household pantry-tracking app. "
          "Answer with a single JSON object only, no prose, no markdown.")


def _clean_category(c) -> str:
    c = str(c or "").strip().lower().replace(" ", "_")
    return c if c in shelf.CATEGORIES else "other"


def _int_or_none(v, lo=0, hi=3650):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _clean_items(raw) -> List[dict]:
    out = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            continue
        name = str(it["name"]).strip()[:120]
        cat = _clean_category(it.get("category"))
        if cat == "other":
            cat = shelf.guess_category(name)
        out.append({
            "name": name,
            "brand": (str(it.get("brand")).strip()[:80] or None) if it.get("brand") else None,
            "category": cat,
            "quantity": _int_or_none(it.get("quantity"), 1, 100) or 1,
            "size_text": (str(it.get("size")).strip()[:40] or None) if it.get("size") else None,
            "raw": (str(it.get("raw")).strip()[:120] or None) if it.get("raw") else None,
            "is_food": bool(it.get("is_food", True)),
        })
    return out


def identify_food(con, image: bytes) -> dict:
    p = provider(con, vision=True)
    prompt = (
        "Identify the grocery or food items in this photo so they can be added to a pantry inventory.\n"
        "Return JSON: {\"items\": [{\"name\": generic product name without brand, e.g. \"Black beans\", "
        "\"brand\": brand or null, \"size\": package size if readable e.g. \"15 oz\" or null, "
        "\"quantity\": how many of that item are visible, "
        f"\"category\": one of [{CATEGORY_LIST}]}}]}}\n"
        "List each distinct product once. Ignore things that aren't food or drink. If nothing is food, return {\"items\": []}.")
    data = ask_json(p, "identify", SYSTEM, prompt, [image], 1500)
    items = [i for i in _clean_items(data.get("items") if isinstance(data, dict) else data) if i["is_food"]]
    return {"items": items}


def parse_receipt(con, image: bytes) -> dict:
    p = provider(con, vision=True)
    prompt = (
        "This is a photo of a grocery store receipt. Read every purchased line.\n"
        "Receipt lines are abbreviated: expand them into plain product names, e.g. \"GV WHL MLK GAL\" -> "
        "name \"Whole milk\", brand \"Great Value\", size \"1 gal\". A line with a quantity like \"2 @ 1.29\" means quantity 2.\n"
        "Return JSON: {\"store\": store name or null, \"date\": purchase date as YYYY-MM-DD or null, "
        "\"items\": [{\"raw\": the line as printed, \"name\": expanded generic name, \"brand\": brand or null, "
        "\"size\": size or null, \"quantity\": integer, "
        f"\"category\": one of [{CATEGORY_LIST}], "
        "\"is_food\": false for non-food such as paper towels, soap, bags, deposits, coupons}]}\n"
        "Skip subtotal, tax, total, payment and savings lines.")
    data = ask_json(p, "receipt", SYSTEM, prompt, [image], 4000)
    if not isinstance(data, dict):
        data = {"items": data}
    d = str(data.get("date") or "")
    return {"store": (str(data.get("store")).strip()[:80] or None) if data.get("store") else None,
            "date": d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None,
            "items": _clean_items(data.get("items"))}


def shelf_life(con, name: str, brand: Optional[str] = None, category: Optional[str] = None,
               size_text: Optional[str] = None) -> dict:
    p = provider(con)
    desc = " ".join(x for x in (brand, name, f"({size_text})" if size_text else None) if x)
    prompt = (
        f"Product: {desc}\nCurrent category guess: {category or 'unknown'}\n"
        "Estimate how long this product, bought fresh/unopened, stays good for eating in each place, "
        "using food-safety guidance such as USDA FoodKeeper. Be conservative.\n"
        "Return JSON: {\"category\": one of [" + CATEGORY_LIST + "], "
        "\"pantry_days\": integer or null if it must not be kept at room temperature, "
        "\"fridge_days\": integer or null if not suitable, \"freezer_days\": integer or null if it shouldn't be frozen, "
        "\"opened_days\": days it keeps once opened (in its usual place) or null if not applicable, "
        "\"best_location\": where a household should normally keep it unopened, \"pantry\" (room temperature, "
        "including produce like potatoes, onions, bananas and bread) | \"fridge\" | \"freezer\" (only for frozen foods), "
        "\"note\": one short storage tip}")
    d = ask_json(p, "shelf", SYSTEM, prompt, None, 600)
    if not isinstance(d, dict):
        raise AIError("Unexpected answer for shelf life.")
    loc = d.get("best_location") if d.get("best_location") in dbm.LOCATIONS else None
    return {"category": _clean_category(d.get("category")) if d.get("category") else category,
            "shelf_pantry_days": _int_or_none(d.get("pantry_days")),
            "shelf_fridge_days": _int_or_none(d.get("fridge_days")),
            "shelf_freezer_days": _int_or_none(d.get("freezer_days")),
            "opened_days": _int_or_none(d.get("opened_days")),
            "default_location": loc,
            "note": str(d.get("note") or "")[:200]}


def suggest_recipes(con, pantry: List[dict], staples: List[str], max_missing: int = 3, count: int = 6,
                    request: str = "") -> dict:
    p = provider(con)
    lines = []
    for it in pantry:
        flag = " (use soon!)" if it.get("status") in ("soon", "expired") else ""
        lines.append(f"- {it['name']}{flag}")
    prompt = (
        "Ingredients in the house:\n" + "\n".join(lines[:200]) + "\n"
        f"Always available: {', '.join(staples) or 'salt, pepper, water'}.\n"
        + (f"The cook asks: {request.strip()[:300]}\n" if request.strip() else "") +
        f"Suggest {count} recipes. Prefer recipes that use the 'use soon' items. Mix recipes that need nothing extra "
        f"with ones that need at most {max_missing} ingredients not in the house.\n"
        "Return JSON: {\"recipes\": [{\"title\": str, \"summary\": one sentence, \"time_minutes\": integer, "
        "\"servings\": integer, \"ingredients\": [{\"name\": plain ingredient name, \"amount\": e.g. \"2 cups\", "
        "\"in_house\": true/false}], \"steps\": [short instruction strings]}]}")
    d = ask_json(p, "recipes", SYSTEM, prompt, None, 6000)
    recipes = d.get("recipes") if isinstance(d, dict) else d
    out = []
    for r in recipes if isinstance(recipes, list) else []:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        ings = [{"name": str(i.get("name") or "").strip()[:80], "amount": str(i.get("amount") or "").strip()[:40]}
                for i in r.get("ingredients") or [] if isinstance(i, dict) and i.get("name")]
        out.append({"title": str(r["title"])[:120], "summary": str(r.get("summary") or "")[:300],
                    "time_minutes": _int_or_none(r.get("time_minutes"), 0, 1440),
                    "servings": _int_or_none(r.get("servings"), 1, 50),
                    "ingredients": ings, "steps": [str(s)[:500] for s in r.get("steps") or []][:30]})
    return {"recipes": out}


def test_connection(con, override: Optional[dict] = None) -> dict:
    """List the server's models and send a tiny prompt, so Settings can show it works."""
    p = provider(con, override=override) if override and override.get("ai_model") else None
    s = {**get_settings(con, include_secret=True), **(override or {})}
    if not override or not override.get("ai_api_key"):
        s["ai_api_key"] = get_settings(con, include_secret=True)["ai_api_key"]
    kind = s["ai_provider"]
    if kind not in PROVIDERS:
        raise NotConfigured("Choose a provider first.")
    lister = PROVIDERS[kind](s["ai_base_url"], s["ai_model"], s["ai_api_key"], 15)
    lister.probe()
    models = lister.models()
    out = {"models": models, "base_url": lister.base_url}
    if p:
        p.timeout = min(p.timeout, 300)
        t0 = time.time()
        d = ask_json(p, "test", SYSTEM, "Reply with {\"ok\": true}.", None, 50)
        out["reply_ok"] = bool(isinstance(d, dict) and d.get("ok"))
        out["seconds"] = round(time.time() - t0, 1)
    return out
