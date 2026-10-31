# Pantry

A self-hosted household pantry tracker. Scan barcodes or receipts to add food, scan again when it's used up, see what expires soon, get reminder notifications on your phone, and find recipes you can make with what's in the house.

It's a small Python (Flask + SQLite) server with a web app you install on your phone like a normal app (a PWA). The camera, barcode scanning and notifications all work from Chrome. No app store is involved.

## What it does

| Feature | How | Needs AI? |
|---|---|---|
| **Scan in** | Point the camera at a barcode. Known products are added with one tap. New ones are looked up in [Open Food Facts](https://world.openfoodfacts.org) (free) and you confirm the name. | No |
| **Used up** | Switch the scanner to *Used up*. Each scan takes one off, from the item that expires first. Undo is in the toast. | No |
| **Add by name** | **+ Add** in the header, for loose produce and leftovers. Autocompletes from things you've bought before. | No |
| **Receipts** | Photo of a grocery receipt. The AI expands lines like `GV WHL MLK GAL` into "Whole milk", and you tick what to keep. Lines match products you've bought before. | Yes |
| **Photo of food** | Snap the counter after shopping. The AI lists what it sees, and you review it the same way. | Yes |
| **Expiry dates** | Each item gets an estimated date from its category and where it's stored (see *Shelf life*). You can type the printed date instead. Opening an item or moving it to the freezer updates the date. | Optional |
| **Reminders** | A push notification at a set time each day for items expiring within *N* days (default 3) or just past date. Each item is announced once per stage. A weekly digest lists everything still past date. | No |
| **Recipes** | **Make now** shows recipes that need nothing you don't have. **Need a few more** allows up to 3 missing ingredients. These come from [TheMealDB](https://www.themealdb.com) (about 300 recipes, downloaded once). **AI ideas** writes recipes around what you have, starting with what expires soon. | Optional |
| **Household** | Everyone has their own sign-in and shares one pantry. History shows who added or used what. | No |
| **Ran out recently** | On the home page, with a **Bought again** button. Works as a ready-made shopping list. | No |

## Install

Pick whichever fits. Every option keeps your data when you update.

### Proxmox (one command)

In the Proxmox host's shell:
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/proxmox.sh)"
```
It works like the community helper scripts.
1. **Default or advanced:** choose *default settings*, or *advanced* to set the container ID, hostname, disk, CPU, RAM, bridge, static IP and port.
2. **Storage:** pick where the container goes from the storages on your host that can hold containers, such as `local-zfs` or `local-lvm`. If there's only one, it's used automatically.
3. **Result:** an unprivileged Debian 12 container that starts on boot, with Pantry on port **8790**. The defaults are 1 CPU, 512 MB RAM, 4 GB disk and DHCP on `vmbr0`.

For an unattended install, set the values and skip the menus: `NONINTERACTIVE=1 STORAGE=local-zfs CTID=150 bash -c "$(curl …)"`.

To update: `pct exec <CTID> -- pantry-update` from the Proxmox shell.

### Docker

```bash
curl -fsSLO https://raw.githubusercontent.com/ashishkhatri/pantry/main/docker-compose.yml
# edit TZ in it, then:
docker compose up -d
```
Or run it directly: `docker run -d -p 8790:8790 -v ./pantry-data:/data -e TZ=America/New_York ghcr.io/ashishkhatri/pantry`.

The image is built for amd64 and arm64, so it also runs on a Raspberry Pi. Data lives in the `/data` volume. Update with `docker compose pull && docker compose up -d`.

### Any Debian or Ubuntu machine or container

As root:
```bash
curl -fsSL https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/linux.sh | bash
```
This installs to `/opt/pantry` with data in `/var/lib/pantry`, and runs a `pantry` systemd service. Update with `pantry-update`.

### Windows PC

In PowerShell (no admin needed):
```powershell
irm https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/windows.ps1 | iex
```
This installs Python if you don't have it, puts Pantry in `%LOCALAPPDATA%\Pantry`, adds it to the Start menu, and opens it at http://127.0.0.1:8790.

For options, run it like this:
```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/windows.ps1))) -StartAtLogin -Lan
```
- `-StartAtLogin` runs Pantry in the background whenever you sign in.
- `-Lan` makes it reachable from other devices on your network.
- `-Uninstall` removes Pantry and keeps your data.

To update, run the install command again.

### Then: https, so phones get the camera and notifications

Browsers only allow the camera and push notifications on **https** pages. On the PC itself, `http://127.0.0.1` counts as secure, so it works there. For phones, put Pantry behind one of these:
- **A reverse proxy with a certificate:** Nginx Proxy Manager, Caddy or Traefik. Forward a hostname to `http://<pantry-ip>:8790`.
- **Tailscale:** run `tailscale serve --https=443 http://localhost:8790` on the Pantry machine. Your phones on the tailnet get a real https address, with nothing open to the internet.

**If you use Cloudflare in front:**
- Set SSL/TLS to **Full (strict)**.
- Turn **Rocket Loader** off for the hostname, because it rewrites the app's JavaScript.
- Cloudflare's 100-second request limit doesn't matter: slow AI work runs in the background and the page checks back.

### First run

1. Open the address and create the first account. It becomes the admin.
2. Add everyone else in **Settings → Household**.
3. On each Android phone:
   - In Chrome, open the https address and choose **⋮ → Add to Home screen → Install**.
   - In the app, go to **More → Turn on** under *Notifications on this device*.
   - Long-pressing the app icon gives shortcuts for *Scan in*, *Used up* and *Scan receipt*.

### Security notes

- **Exposure:** if you expose it to the internet, every page and API call needs a sign-in either way.
- **Brute force:** failed sign-ins are limited to 10 per IP address per 15 minutes.
- **Passwords:** stored as salted hashes. Sessions last a year per device. **Sign out** ends the session on that device.
- **Cross-site requests:** every change requires an `X-Requested-With` header, which blocks forged requests from other sites.
- **Cookies:** the sign-in cookie is marked Secure whenever the page came in over https, either directly or through a proxy that sends `X-Forwarded-Proto`. To force it on or off, set `PANTRY_HTTPS=1` or `0`.
- **Proxy headers:** Pantry trusts one proxy's `X-Forwarded-*` headers, so don't expose port 8790 straight to the internet. Put it behind your proxy.
- **Tighter option:** keep it off the internet and reach it over Tailscale, or limit it to your LAN in your proxy's access lists. Notifications still work, because they come from Google's push service rather than your server.

## AI setup (Settings → AI)

Every AI feature goes through one swappable interface (`pantry/ai.py`). Pick a provider:

| Provider | Address | Notes |
|---|---|---|
| **Ollama** | `http://<host>:11434` | Native API: sends images, asks for JSON, and switches "thinking" off for reasoning models. On the Ollama machine set `OLLAMA_HOST=0.0.0.0` so the container can reach it. |
| **OpenAI-compatible** | e.g. `http://<host>:1234/v1` | LM Studio, llama.cpp server, vLLM, Ollama's `/v1`, or OpenAI itself (with an API key). |
| **Anthropic** | `https://api.anthropic.com` | Claude through the API, with your API key. Defaults to `claude-haiku-5-5`. A receipt costs a fraction of a cent. |

The panel works like Budget's, and changes save automatically.
- **Connect:** enter the server URL (`http://` is optional) and click **Connect**. Both model lists fill in from the server.
- **Text model:** used for shelf life and recipes. *First loaded model* uses whatever the server lists first.
- **Image model:** used for receipts and food photos. *Same as text model* works if your text model is a vision model.

Pick models explicitly if your server unloads idle models, so the right one gets loaded again.

**Without a GPU (now):**
- Ollama runs on the CPU. Small models work but are slow, so a receipt can take a few minutes on a CPU-only server.
- Suggested models: `qwen2.5vl:3b` (vision) and `llama3.2:3b` or `qwen2.5:3b` (text).
- Leave the timeout at 600 seconds.
- Jobs run one at a time in the background, so you can close the page and come back.
- With the provider set to **Off**, everything except receipts, food photos and AI recipes still works.

### Using your Windows PC as the AI hub

The app stays on Proxmox, and only AI requests go to the PC. When the PC is off or asleep, everything still works except receipts, food photos and AI recipe ideas. Those fail within about 5 seconds with "isn't answering", and you can retry a receipt from its row later.

**1. Let the model server accept LAN requests.**
- **LM Studio:** Developer tab → Settings → turn on **Serve on Local Network**, then start the server (port 1234). Also turn on **Just-in-Time model loading**, so the app can load the text and vision models itself when a request asks for them.
- **Ollama:** add a user environment variable `OLLAMA_HOST` = `0.0.0.0`, then quit and restart Ollama (port 11434).

**2. Give the PC a fixed IP address** with a DHCP reservation in your router, so the address in Settings keeps working.

**3. Open the port to the Pantry container only.** In PowerShell as administrator, with your container's IP filled in:
```
New-NetFirewallRule -DisplayName "LM Studio for Pantry" -Direction Inbound -Protocol TCP -LocalPort 1234 -RemoteAddress <pantry-server-ip> -Action Allow
```
For Ollama, use port 11434 instead.

**4. Check it from the container:** `curl http://<pc-ip>:1234/v1/models` (Ollama: `curl http://<pc-ip>:11434/api/tags`). You should get a list of models.

**5. Fill in Settings → AI.**
- **Provider:** OpenAI-compatible for LM Studio, or Ollama.
- **Address:** `http://<pc-ip>:1234/v1` for LM Studio, or `http://<pc-ip>:11434` for Ollama.
- Click **Connect**, then pick the text model, and an image model if it's a different one.

A vision model around 7B handles receipts well on a GPU, such as Qwen2.5-VL 7B or Gemma 3 12B. A single vision model can do both jobs: leave the vision field blank and put it in the text field.

**Sleep:** a receipt sent while the PC sleeps fails instead of waiting. If you'd rather the PC wake for it, enable Wake-on-LAN. That's outside the app.

**With a GPU (later):** point the address at the GPU machine and switch to something like `qwen2.5vl:7b` for receipts and an 8B text model. Nothing else changes.

Every AI request is logged to `/var/lib/pantry/ai.log` with timing and the start of the answer, which helps when a model gives poor or slow results.

## Shelf life

Each product has a **category**, such as "Raw poultry" or "Canned goods". The category has default days for pantry, fridge and freezer, plus how long it keeps once opened. The defaults are conservative and loosely based on USDA FoodKeeper guidance. They're plain data in `pantry/shelf.py`, which you can edit.

An item's estimated date works like this:
- **Normally:** the day it was added plus the product's days for where it's stored.
- **Opened:** the date becomes the opened date plus the "once opened" days, if that's sooner.
- **Moved to or from the freezer:** the clock restarts from today, because the printed date no longer applies.
- **Typed by you:** your date is kept as it is.

With AI on, each **new** product is sent to the AI once.
- **What it decides:** how long the product keeps in each place, and **where to store it** (pantry, fridge or freezer).
- **Example:** gochujang gets a year in the pantry instead of the generic "condiments" default.
- **In the app:** these show as *AI estimate*.

**Products added before AI was set up:** use **Settings → AI → ✦ Estimate shelf life & storage for existing products**.
- It works through every product still on category defaults, in the background, with progress in the top bar.
- Tick *Also re-check…* to redo ones the AI already estimated.

**Moving items:** when the AI picks a different storage place, items in stock move there only if they're still where Pantry guessed. Anything you put somewhere yourself, or moved, stays put. Moved items are re-dated for their new place, counted from when they were added.

**Your own numbers win:** you can change any product's numbers under **item → Product & shelf-life settings**, and the AI never overwrites them.

## Recipes and matching

Recipe ingredients are matched to what's in stock by name, with a little sense:
- **Matches:** "chicken breast" matches *Boneless Skinless Chicken Breasts*, "minced beef" matches *Ground beef*, and "cheddar cheese" matches *Sharp Cheddar*.
- **Doesn't match:** "butter" doesn't match *Peanut Butter*, and "milk" doesn't match *Coconut milk*.
- **Staples:** things in Settings → Staples (salt, pepper, water by default) never count as missing.
- **Ordering:** recipes that use items expiring soon come first.

The AI's own "you have this" claims are re-checked the same way.

## Where your data lives

| Install | Data folder |
|---|---|
| Proxmox / Linux | `/var/lib/pantry` |
| Docker | the `/data` volume (`./data` with the compose file) |
| Windows | `%LOCALAPPDATA%\Pantry\data` |
| Running from a checkout | `data/` next to the code |

The data folder holds:

| File | What |
|---|---|
| `pantry.sqlite` | The database. |
| `backups/` | Automatic daily backups (the newest 14 are kept), plus any you make with **Back up database now**. |
| `receipts/` | Receipt photos, kept so you can re-run a receipt. Deleting a receipt in the app deletes its photo. |
| `vapid_private.pem` | The key that signs push notifications. If you lose it, each phone has to turn notifications on again. |
| `secret_key` | Signs sign-in cookies. Deleting it signs everyone out. |
| `ai.log` | Every AI request, with timing and the start of the answer. |

To restore a backup, stop Pantry, copy the backup over `pantry.sqlite`, and start it again.

## Settings you can change with environment variables

| Variable | Default | What |
|---|---|---|
| `PANTRY_DATA_DIR` | `data/` beside the code | Where everything above lives. |
| `PANTRY_HOST` / `PANTRY_PORT` | `127.0.0.1` / `8790` | Address to listen on. Docker and Linux installs use `0.0.0.0`. |
| `PANTRY_HTTPS` | `auto` | Secure cookie: `auto` follows the request, `1` always, `0` never. |
| `PANTRY_VAPID_SUBJECT` | `mailto:pantry@localhost` | Contact address sent to push services. |
| `TZ` | system | Time zone for the daily reminder. |

## Developing

```
git clone https://github.com/ashishkhatri/pantry && cd pantry
pip install -r requirements.txt
python -m unittest discover tests
python -m pantry --browser          # or start.bat on Windows
```

**Releasing:** bump `__version__` in `pantry/__init__.py` and push to `main`. That's all.
- **What happens:** GitHub Actions sees a version with no release yet. It runs the tests, publishes `ghcr.io/ashishkhatri/pantry:<version>` and `:latest`, and creates the `v<version>` tag and release. The installers then pick up the newest release.
- **Pushes that don't change the version** only run the tests.

## Project layout

```
start.bat               Run from a checkout on Windows
Dockerfile, docker-compose.yml
install/proxmox.sh      Proxmox host: create an LXC and install
install/linux.sh        Debian/Ubuntu: install or update (also `pantry-update`)
install/windows.ps1     Windows: install, update, uninstall
install/pantry.service  systemd unit (filled in by linux.sh)
.github/workflows/      Tests on every push; image + release on version tags
pantry/
  __main__.py           `python -m pantry`: starts waitress on --host/--port
  app.py                Flask routes (JSON API under /api), sign-in, job handlers
  db.py                 SQLite schema + migrations, settings, backups
  inventory.py          Products, items, expiry dates, scan-out, history
  shelf.py              Categories, shelf-life table, category guessing
  lookup.py             Open Food Facts barcode lookup
  ai.py                 AI providers (Ollama / OpenAI-compatible / Anthropic) and AI tasks
  jobs.py               Background job runner (one at a time)
  recipes.py            TheMealDB download, ingredient matching
  notify.py             Daily/weekly expiry reminders
  webpush.py            Web Push (VAPID + RFC 8291 encryption), no extra packages
  static/index.html     The app (one file), sw.js, manifest, icons
tests/                  python -m unittest discover tests
```

To change the database schema, add a new numbered entry to `MIGRATIONS` in `db.py`. Don't edit the existing ones.

## Ideas for later

- **Shopping list:** a list built from "Ran out recently" plus missing recipe ingredients, shared by the household.
- **Printed dates:** read the "best by" date with the camera when adding.
- **Native wrapper:** wrap the PWA as an APK with Capacitor, if Chrome's PWA support ever falls short.
- **Grocery delivery:** match receipts against order history (similar to the Amazon idea in Budget).
