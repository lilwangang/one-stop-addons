"""One Stop Sign-in Helper: drives Netflix and Disney+ website sign-ins in a hidden browser for Home Assistant."""
import asyncio, json, os, re, secrets, time, logging
from aiohttp import web
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("signin")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
TTL = 15 * 60
PW = None
BROWSER = None
SESSIONS = {}


async def browser():
    global PW, BROWSER
    if BROWSER is None or not BROWSER.is_connected():
        PW = PW or await async_playwright().start()
        BROWSER = await PW.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    return BROWSER


async def reap():
    while True:
        await asyncio.sleep(60)
        for k, s in list(SESSIONS.items()):
            if time.time() - s["t"] > TTL:
                await close(k)


async def close(sid):
    s = SESSIONS.pop(sid, None)
    if s:
        try:
            await s["ctx"].close()
        except Exception:
            pass


def body_text(t):
    return " ".join((t or "").split())[:300]


async def screen(pg):
    """Work out which Netflix screen we're on."""
    url = pg.url
    if await pg.locator('input[name="challengeOtp"]').count():
        return "code"
    if await pg.locator('input[name="password"]').count() and await pg.locator('input[name="password"]').first.is_visible():
        return "password"
    if "/browse" in url or "/profiles" in url or await pg.locator('[data-uia*="profile-link"], .profile-link, .list-profiles').count():
        return "signed_in"
    return "unknown"


async def wait_screen(pg, want, timeout=30):
    end = time.time() + timeout
    last = "unknown"
    while time.time() < end:
        try:
            last = await screen(pg)
        except Exception:
            last = "unknown"
        if last in want:
            return last
        await asyncio.sleep(0.5)
    return last


async def error_text(pg):
    for sel in ['[data-uia*="error"]', '[role="alert"]', '.ui-message-error', '[data-uia="text"]']:
        loc = pg.locator(sel)
        if await loc.count():
            t = body_text(await loc.first.inner_text())
            if t:
                return t
    return ""


PROFILE_LINKS = ['[data-uia^="profile-selector+tile-"]', '[data-uia^="profile-link"]', 'a.profile-link', '.choose-profile .profile', 'li.profile a']


async def profile_links(pg):
    for sel in PROFILE_LINKS:
        loc = pg.locator(sel)
        if await loc.count():
            return loc
    return None


async def profile_list(pg):
    """[{guid, name}] from Netflix's 'Who's watching?' screen."""
    if "/browse" not in pg.url and "/profiles" not in pg.url:
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
    await pg.wait_for_timeout(3000)
    return await pg.evaluate("""() => [...document.querySelectorAll('[data-uia^="profile-selector+tile-"]')].map(e => ({
        guid: e.getAttribute('data-uia').split('tile-')[1], name: (e.innerText || '').trim().split('\\n')[0] }))
        .filter(p => p.guid && p.name)""")


def meta_path(acct):
    return os.path.join(DATA, f"netflix_{acct}.meta.json")


async def profiles(pg):
    if "/browse" not in pg.url and "/profiles" not in pg.url:
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
    await pg.wait_for_timeout(3000)
    loc = await profile_links(pg)
    if loc is None:
        uia = await pg.evaluate("[...new Set([...document.querySelectorAll('[data-uia]')].map(e=>e.getAttribute('data-uia')))].slice(0,40)")
        log.info("no profile gate at %s; data-uia seen: %s", pg.url, uia)
        return []
    names = []
    for i in range(await loc.count()):
        t = body_text(await loc.nth(i).inner_text())
        if t and t.lower() not in ("add profile", "manage profiles", "edit"):
            names.append(t)
    return names


ACCOUNTS = {}          # account token -> open browser context (logged in)
DATA = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))


def state_path(acct):
    return os.path.join(DATA, f"netflix_{acct}.json")


async def done(sid, profile=None, plist=None):
    s = SESSIONS[sid]
    acct = secrets.token_urlsafe(12)
    await s["ctx"].storage_state(path=state_path(acct))
    with open(meta_path(acct), "w") as fh:
        json.dump({"profiles": plist or [], "active": (plist or [{}])[0].get("guid")}, fh)
    hist = await read_history(s["pg"], 5)
    await close(sid)
    if hist.get("error"):
        return {"step": "error", "message": "Signed in, but Netflix's history page didn't load: " + hist["error"]}
    return {"step": "done", "account": acct, "profile": profile, "profiles": plist or [], "sample": len(hist.get("items", []))}


async def read_history(pg, size=40):
    """Open netflix.com/viewingactivity in the signed-in browser and read what it shows.

    Uses the page's own data request when it can see it, otherwise reads the list on screen."""
    caught = []

    async def on_resp(resp):
        if "viewingactivity" in resp.url and "json" in (resp.headers.get("content-type") or ""):
            try:
                caught.append(await resp.json())
            except Exception:
                pass

    pg.on("response", on_resp)
    try:
        await pg.goto("https://www.netflix.com/viewingactivity", wait_until="domcontentloaded")
        url = pg.url
        log.info("viewingactivity page -> %s", url)
        if "/login" in url:
            return {"error": "signed_out"}
        if "/profiles" in url or await pg.locator('[data-uia^="profile-selector+tile-"]').count():
            return {"error": "no_profile"}
        try:
            await pg.wait_for_selector("li.retableRow, .retableRow, [data-uia*='activity'] li", timeout=15000)
        except Exception:
            pass
        await pg.wait_for_timeout(1500)
        for d in caught:
            items = d.get("viewedItems") if isinstance(d, dict) else None
            if items is not None:
                return {"items": items[:size], "profile": d.get("profileName")}
        rows = await pg.evaluate("""() => [...document.querySelectorAll('li.retableRow, .retableRow')].map(r => {
            const a = r.querySelector('a[href*="/title/"]'), d = r.querySelector('.date, .col.date');
            const id = a ? (a.getAttribute('href').match(/title\\/(\\d+)/) || [])[1] : null;
            return { title: a ? a.textContent.trim() : r.textContent.trim(), movieID: id, dateStr: d ? d.textContent.trim() : '' };
        })""")
        if rows:
            out = []
            for r in rows[:size]:
                t = r["title"]
                ts = None
                try:
                    from datetime import datetime
                    for fmt in ("%m/%d/%y", "%d/%m/%y", "%Y-%m-%d", "%m/%d/%Y"):
                        try:
                            ts = int(datetime.strptime(r["dateStr"], fmt).timestamp() * 1000)
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass
                series, _, ep = t.partition(": ")
                item = {"title": t, "movieID": r["movieID"], "date": ts}
                if ep:
                    item.update({"seriesTitle": series, "episodeTitle": ep})
                out.append(item)
            return {"items": out}
        body = " ".join((await pg.inner_text("body"))[:300].split())
        log.info("viewingactivity: nothing readable. Page says: %s", body)
        return {"error": "Netflix's viewing activity page showed nothing readable"}
    finally:
        pg.remove_listener("response", on_resp)


async def read_continue(pg, size=40):
    """Netflix's 'Continue Watching' row for the active profile (with progress)."""
    await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
    if "/login" in pg.url:
        return {"error": "signed_out"}
    await pg.wait_for_timeout(4000)
    items = []
    for _ in range(14):
        items = await pg.evaluate("""() => {
          const row = document.querySelector('[data-list-context="continueWatching"]') ||
            [...document.querySelectorAll('.lolomoRow, [data-list-context]')].find(r => /continue watching/i.test((r.querySelector('h2, .rowHeader, .row-header-title') || {}).innerText || ''));
          if (!row) return [];
          const seen = new Set(), out = [];
          for (const card of row.querySelectorAll('.title-card, .slider-item')) {
            const a = card.querySelector('a[href*="/watch/"], a[href*="/title/"]'); if (!a) continue;
            const img = card.querySelector('img');
            const t = ((a.getAttribute('aria-label') || (img && img.alt) || '').trim());
            const id = (a.getAttribute('href').match(/(?:watch|title)\/(\d+)/) || [])[1];
            if (!t || seen.has(id || t)) continue; seen.add(id || t);
            const bar = card.querySelector('.progress-completed, [class*="progress"] span[style*="width"]');
            const pct = bar ? parseFloat((bar.style.width || '').replace('%', '')) : null;
            out.push({ title: t, movieID: id, image: img ? (img.currentSrc || img.src) : null, progress: isNaN(pct) ? null : Math.round(pct) });
          }
          return out;
        }""")
        if items:
            break
        await pg.mouse.wheel(0, 700)
        await pg.wait_for_timeout(1000)
    now = int(time.time() * 1000)
    return {"items": [{**x, "date": now - i * 60000, "nodate": True, "kind": "continue"} for i, x in enumerate(items[:size])]}


async def account_page(acct):
    c = ACCOUNTS.get(acct)
    if c and not c["pg"].is_closed():
        return c["pg"]
    path = state_path(acct)
    if not os.path.exists(path):
        return None
    b = await browser()
    ctx = await b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900}, storage_state=path)
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    pg = await ctx.new_page()
    ACCOUNTS[acct] = {"ctx": ctx, "pg": pg}
    return pg


def read_meta(acct):
    try:
        with open(meta_path(acct)) as fh:
            return json.load(fh)
    except Exception:
        return {"profiles": [], "active": None}


async def list_profiles(req):
    data = await req.json()
    return web.json_response({"profiles": read_meta(data.get("account")).get("profiles", [])})


LOCKS = {}


async def history(req):
    data = await req.json()
    acct, want = data.get("account"), data.get("profile")
    pg = await account_page(acct)
    if pg is None:
        return web.json_response({"error": "unknown_account"}, status=404)
    lock = LOCKS.setdefault(acct, asyncio.Lock())
    async with lock:
        return await _history(acct, pg, want, int(data.get("size", 40)), data.get("kind", "history"))


async def _history(acct, pg, want, size, kind="history"):
    meta = read_meta(acct)
    if not meta.get("profiles"):          # older sign-ins didn't record the profiles; look them up once
        try:
            await pg.goto("https://www.netflix.com/profiles", wait_until="domcontentloaded")
            meta["profiles"] = await profile_list(pg)
            log.info("found %d profiles", len(meta["profiles"]))
            with open(meta_path(acct), "w") as fh:
                json.dump(meta, fh)
        except Exception:
            log.exception("profile lookup failed")
    if want and not any(p["guid"] == want for p in meta.get("profiles", [])):
        match = [p for p in meta.get("profiles", []) if p["name"].lower() == str(want).lower()]
        want = match[0]["guid"] if match else None
    try:
        if want and want != meta.get("active"):
            await pg.goto(f"https://www.netflix.com/SwitchProfile?tkn={want}", wait_until="domcontentloaded")
            await pg.wait_for_timeout(2500)
            if await pg.locator('input[type="password"], [data-uia*="pin"]').count():
                return web.json_response({"error": "That profile is locked with a PIN"})
            meta["active"] = want
            with open(meta_path(acct), "w") as fh:
                json.dump(meta, fh)
        res = await (read_continue(pg, size) if kind == "continue" else read_history(pg, size))
        res["profile_guid"] = meta.get("active")
        res["profiles"] = meta.get("profiles", [])
        if not res.get("error"):
            await ACCOUNTS[acct]["ctx"].storage_state(path=state_path(acct))   # keep refreshed sign-in
        return web.json_response(res, status=401 if res.get("error") == "signed_out" else 200)
    except Exception as err:
        log.exception("history failed")
        return web.json_response({"error": str(err)}, status=500)


async def latest(req):
    """Diagnostics: read history with the most recently saved sign-in."""
    files = sorted((f for f in os.listdir(DATA) if f.startswith("netflix_") and f.endswith(".json")), key=lambda f: os.path.getmtime(os.path.join(DATA, f)))
    if not files:
        return web.json_response({"error": "no saved sign-in"})
    acct = files[-1][len("netflix_"):-len(".json")]
    pg = await account_page(acct)
    want = req.query.get("profile")
    async with LOCKS.setdefault(acct, asyncio.Lock()):
        r = await _history(acct, pg, want, 10, req.query.get("kind", "history"))
    res = json.loads(r.text)
    return web.json_response({"profiles": [p["name"] for p in res.get("profiles", [])], "active": res.get("profile_guid"), "account_age_s": int(time.time() - os.path.getmtime(os.path.join(DATA, files[-1]))), "url": pg.url,
                              "count": len(res.get("items") or []), "error": res.get("error"),
                              "titles": [(x.get("seriesTitle") or x.get("title")) for x in (res.get("items") or [])][:10]})


async def debug_rows(req):
    files = sorted((f for f in os.listdir(DATA) if f.startswith("netflix_") and f.endswith(".json") and not f.endswith(".meta.json")), key=lambda f: os.path.getmtime(os.path.join(DATA, f)))
    acct = files[-1][len("netflix_"):-len(".json")]
    pg = await account_page(acct)
    async with LOCKS.setdefault(acct, asyncio.Lock()):
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
        await pg.wait_for_timeout(6000)
        info = await pg.evaluate("""() => {
          const h = [...document.querySelectorAll('h2, .rowHeader, .row-header-title, [class*="rowTitle"]')].find(e => /continue watching/i.test(e.textContent || ''));
          if (!h) return {err: 'no heading', url: location.href, heads: [...document.querySelectorAll('h2')].map(e => e.textContent).slice(0, 5)};
          let r = h, lvl = 0;
          for (; lvl < 8 && r; lvl++) { r = r.parentElement; if (r && r.querySelectorAll('img').length >= 2) break; }
          const tiles = [...r.querySelectorAll('img')].slice(0, 6).map(img => { let c = img; for (let i = 0; i < 6 && c && !(c.tagName === 'A' || c.getAttribute('role') || c.getAttribute('data-uia')); i++) c = c.parentElement;
             return { alt: img.alt, tag: c && c.tagName, role: c && c.getAttribute('role'), uia: c && c.getAttribute('data-uia'), href: c && c.getAttribute('href'), aria: c && c.getAttribute('aria-label'), outer: c ? c.outerHTML.slice(0, 600) : '' }; });
          return { hTag: h.tagName, hCls: h.className, lvl, rowUia: r.getAttribute('data-uia'), rowCls: r.className, tiles };
        }""")
    return web.json_response(info)


async def forget(req):
    data = await req.json()
    acct = data.get("account")
    c = ACCOUNTS.pop(acct, None)
    if c:
        await c["ctx"].close()
    try:
        os.remove(state_path(acct))
    except OSError:
        pass
    return web.json_response({"ok": True})


async def start(req):
    data = await req.json()
    email = (data.get("email") or "").strip()
    if not email:
        return web.json_response({"step": "error", "message": "Enter the email or phone number on the Netflix account."})
    b = await browser()
    ctx = await b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    pg = await ctx.new_page()
    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = {"ctx": ctx, "pg": pg, "t": time.time()}
    try:
        await pg.goto("https://www.netflix.com/login", wait_until="domcontentloaded")
        await pg.wait_for_load_state("networkidle", timeout=20000)
        await pg.wait_for_timeout(1500)
        f = pg.locator('input[name="userLoginId"]')
        await f.click()
        await f.press_sequentially(email, delay=60)
        await pg.wait_for_timeout(800)
        await pg.get_by_role("button", name="Continue").first.click()
        step = await wait_screen(pg, {"code", "signed_in"}, 30)
        if step == "unknown" and await pg.locator('input[name="password"]').count():
            step = "password"
        if step == "signed_in":
            return web.json_response({"session": sid, "step": "profiles", "profiles": await profiles(pg)})
        if step in ("code", "password"):
            return web.json_response({"session": sid, "step": step})
        msg = await error_text(pg)
        await close(sid)
        return web.json_response({"step": "error", "message": msg or "Netflix didn't move past the email step. Check the email and try again."})
    except Exception as err:
        log.exception("start failed")
        await close(sid)
        return web.json_response({"step": "error", "message": f"Netflix sign-in page failed: {err}"})


async def answer(req):
    data = await req.json()
    sid, kind, value = data.get("session"), data.get("kind"), (data.get("value") or "").strip()
    s = SESSIONS.get(sid)
    if not s:
        return web.json_response({"step": "error", "message": "That sign-in expired. Start again."})
    s["t"] = time.time()
    pg = s["pg"]
    try:
        if kind == "code":
            f = pg.locator('input[name="challengeOtp"]')
        else:
            f = pg.locator('input[name="password"]')
        if not await f.count():
            step = await screen(pg)
            if step != "signed_in":
                return web.json_response({"session": sid, "step": step if step in ("code", "password") else kind, "message": "Netflix moved on to a different screen. Start again."})
        else:
            await f.click(timeout=10000)
            await f.fill("", timeout=10000)
            await f.press_sequentially(value, delay=50, timeout=15000)
            # Netflix submits a full code by itself; only press the button if we're still on the same screen.
            step = await wait_screen(pg, {"signed_in"}, 4)
            if step != "signed_in":
                try:
                    btn = pg.locator('button[type="submit"], button[data-uia*="submit"]')
                    if await btn.count() and await btn.first.is_visible():
                        await btn.first.click(timeout=5000)
                    elif await f.count():
                        await f.press("Enter", timeout=5000)
                except Exception:
                    pass  # the page already moved on
                step = await wait_screen(pg, {"signed_in"}, 30)
        if step != "signed_in":
            return web.json_response({"session": sid, "step": kind, "message": await error_text(pg) or "Netflix didn't accept that. Check it and try again."})
        plist = await profile_list(pg)
        if plist:
            # Open a profile without a PIN; history for each profile is chosen later on the dashboard.
            await pg.goto(f"https://www.netflix.com/SwitchProfile?tkn={plist[0]['guid']}", wait_until="domcontentloaded")
            await pg.wait_for_timeout(3000)
        return web.json_response(await done(sid, None, plist))
    except Exception as err:
        log.exception("answer failed")
        return web.json_response({"session": sid, "step": kind, "message": f"Something went wrong: {err}"})


async def pick(req):
    data = await req.json()
    sid, idx = data.get("session"), int(data.get("index", 0))
    s = SESSIONS.get(sid)
    if not s:
        return web.json_response({"step": "error", "message": "That sign-in expired. Start again."})
    pg = s["pg"]
    try:
        names = await profiles(pg)
        links = await profile_links(pg)
        if links is None:
            return web.json_response(await done(sid, names[idx] if idx < len(names) else None))
        await links.nth(idx).click()
        await pg.wait_for_timeout(4000)
        if await pg.locator('input[type="password"], input[data-uia*="pin"]').count():
            await close(sid)
            return web.json_response({"step": "error", "message": "That profile has a PIN lock. Pick a profile without a PIN, or remove the lock in Netflix."})
        return web.json_response(await done(sid, names[idx] if idx < len(names) else None))
    except Exception as err:
        log.exception("pick failed")
        return web.json_response({"step": "error", "message": f"Couldn't pick that profile: {err}"})


# =====================================================================
# Disney+
# =====================================================================
D_HOME = "https://www.disneyplus.com"


def d_state(acct):
    return os.path.join(DATA, f"disney_{acct}.json")


def d_meta(acct):
    return os.path.join(DATA, f"disney_{acct}.meta.json")


async def d_dump(pg, why):
    try:
        info = await pg.evaluate("""() => ({url: location.href, h: [...document.querySelectorAll('h1,h2,h3')].map(e=>e.innerText.trim()).filter(Boolean).slice(0,8),
          inputs: [...document.querySelectorAll('input')].map(e=>[e.type,e.name,e.id,e.getAttribute('data-testid'),e.getAttribute('autocomplete'),e.maxLength]).slice(0,12),
          buttons: [...document.querySelectorAll('button,[role=button],a[role=link]')].map(e=>[(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,30),e.getAttribute('data-testid')]).filter(x=>x[0]||x[1]).slice(0,25)})""")
        log.info("disney %s: %s", why, json.dumps(info)[:2500])
    except Exception:
        pass


async def d_screen(pg):
    url = pg.url
    if await pg.locator('[data-testid="review-email-btn"]').count() or "auth_error=" in url:
        return "not_found"
    if re.search(r"disneyplus\.com/([a-z]{2}-[a-z]{2}/)?(home|select-profile|profiles?/?|browse)(\b|/|\?|$)", url):
        return "signed_in"
    if re.search(r"disneyplus\.com/([a-z]{2}-[a-z]{2}/)?(sign-?up|commerce|welcome|subscribe|plans)", url):
        return "not_found"
    for sel in ['input[autocomplete="one-time-code"]', 'input[data-testid*="otp"]', 'input[name*="otp" i]', 'input[name*="code" i]', 'input[inputmode="numeric"][maxlength="1"]']:
        loc = pg.locator(sel)
        if await loc.count() and await loc.first.is_visible():
            return "code"
    loc = pg.locator('input[type="password"]')
    if await loc.count() and await loc.first.is_visible():
        return "password"
    return "unknown"


async def d_wait(pg, want, timeout=30):
    end, last = time.time() + timeout, "unknown"
    while time.time() < end:
        try:
            last = await d_screen(pg)
        except Exception:
            last = "unknown"
        if last in want:
            return last
        await asyncio.sleep(0.5)
    return last


async def d_profiles(pg):
    """[{guid, name}] from Disney+'s 'Who's watching?' screen."""
    if "select-profile" not in pg.url:
        await pg.goto(D_HOME + "/select-profile", wait_until="domcontentloaded")
    await pg.wait_for_timeout(5000)
    found = await pg.evaluate("""() => {
      const skip = /^(add|edit|manage|done|kids|cancel)\\b|profile$/i, out = [], seen = new Set();
      const cands = [...document.querySelectorAll('[data-testid*="profile" i] , button, [role=button], a')];
      for (const e of cands) {
        const name = (e.getAttribute('aria-label') || e.innerText || '').trim().split('\\n')[0].replace(/^(select|edit) profile[:\\s]*/i, '');
        if (!name || name.length > 30 || skip.test(name) || seen.has(name)) continue;
        if (!e.querySelector('img') && !/profile|avatar/i.test(e.getAttribute('data-testid') || '')) continue;
        seen.add(name); out.push({ guid: name, name });
      }
      return out;
    }""")
    if not found:
        await d_dump(pg, "no profiles found")
    return found


async def d_pick(pg, name):
    if "select-profile" not in pg.url:
        await pg.goto(D_HOME + "/select-profile", wait_until="domcontentloaded")
        await pg.wait_for_timeout(4000)
    loc = pg.locator(f'[aria-label*="{name}"]').first
    if not await loc.count():
        loc = pg.get_by_text(name, exact=True).first
    await loc.click(timeout=10000)
    await pg.wait_for_timeout(5000)
    if await pg.locator('input[type="password"], input[inputmode="numeric"]').count() and "select-profile" in pg.url:
        return False
    return True


async def d_read(pg, size=40):
    """Disney+ has no viewing-history page, so read the 'Continue Watching' row on the home screen."""
    await pg.goto(D_HOME + "/home", wait_until="domcontentloaded")
    if "login.disney" in pg.url or "/identity" in pg.url:
        return {"error": "signed_out"}
    if "select-profile" in pg.url:
        return {"error": "no_profile"}
    await pg.wait_for_timeout(5000)
    items = []
    for _ in range(12):
        items = await pg.evaluate("""() => {
          const heads = [...document.querySelectorAll('h2,h3,h4,[role=heading]')].filter(h => /continue watching/i.test(h.innerText || ''));
          for (const h of heads) {
            let box = h.parentElement;
            for (let i = 0; i < 5 && box && !box.querySelector('a[href*="/play/"], a[href*="/browse/"], a[href*="/video/"]'); i++) box = box.parentElement;
            if (!box) continue;
            const seen = new Set(), out = [];
            for (const a of box.querySelectorAll('a[href]')) {
              const img = a.querySelector('img');
              let t = (a.getAttribute('aria-label') || (img && img.alt) || a.innerText || '').trim().split('\\n')[0];
              t = t.replace(/[,.]?\\s*(select|play|resume|continue watching).*$/i, '').trim();
              if (!t || seen.has(t)) continue; seen.add(t);
              out.push({ title: t, href: a.getAttribute('href'), image: img ? (img.currentSrc || img.src) : null });
            }
            if (out.length) return out;
          }
          return [];
        }""")
        if items:
            break
        await pg.mouse.wheel(0, 900)
        await pg.wait_for_timeout(1200)
    if not items:
        await d_dump(pg, "no continue watching row")
        return {"items": []}
    now = int(time.time() * 1000)
    return {"items": [{"title": x["title"], "date": now - i * 60000, "nodate": True, "image": x.get("image"),
                       "url": (D_HOME + x["href"]) if x.get("href", "").startswith("/") else x.get("href")} for i, x in enumerate(items[:size])]}


async def d_start(req):
    data = await req.json()
    email = (data.get("email") or "").strip()
    if not email:
        return web.json_response({"step": "error", "message": "Enter the email on the Disney+ account."})
    b = await browser()
    ctx = await b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900})
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    pg = await ctx.new_page()
    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = {"ctx": ctx, "pg": pg, "t": time.time(), "svc": "disney"}
    try:
        await pg.goto(D_HOME + "/identity/login", wait_until="domcontentloaded")
        f = pg.locator('input[name="lookupValue"], input[type="email"]').first
        await f.wait_for(timeout=30000)
        await pg.wait_for_timeout(1000)
        await f.click()
        await f.press_sequentially(email, delay=60)
        await pg.wait_for_timeout(600)
        await pg.locator('[data-testid="id-flow-submit"], button[type="submit"]').first.click()
        step = await d_wait(pg, {"code", "password", "signed_in", "not_found"}, 30)
        if step == "not_found":
            await close(sid)
            return web.json_response({"step": "error", "message": "Disney+ didn't find an account for that email. Check it (it may be different from your Netflix email) and try again."})
        if step in ("code", "password"):
            return web.json_response({"session": sid, "step": step})
        if step == "signed_in":
            return web.json_response(await d_finish(sid))
        await d_dump(pg, "after email")
        log.info("disney after email url=%s", pg.url)
        await close(sid)
        return web.json_response({"step": "error", "message": "Disney+ showed a screen this doesn't know yet. Try again in a minute."})
    except Exception as err:
        log.exception("disney start failed")
        await close(sid)
        return web.json_response({"step": "error", "message": f"Disney+ sign-in page failed: {err}"})


async def d_answer(req):
    data = await req.json()
    sid, kind, value = data.get("session"), data.get("kind"), (data.get("value") or "").strip()
    s = SESSIONS.get(sid)
    if not s:
        return web.json_response({"step": "error", "message": "That sign-in expired. Start again."})
    s["t"] = time.time()
    pg = s["pg"]
    try:
        cur = await d_screen(pg)
        if cur == "code":
            boxes = pg.locator('input[inputmode="numeric"][maxlength="1"]')
            f = boxes.first if await boxes.count() > 1 else pg.locator('input[autocomplete="one-time-code"], input[data-testid*="otp"], input[name*="otp" i], input[name*="code" i]').first
        elif cur == "password":
            f = pg.locator('input[type="password"]').first
        else:
            f = None
        if f is not None:
            await f.click(timeout=10000)
            await f.press_sequentially(value, delay=60, timeout=20000)
            nxt = await d_wait(pg, {"signed_in", "password", "code"} - {cur}, 4)
            if nxt == cur or nxt == "unknown":
                try:
                    btn = pg.locator('button[type="submit"]:visible, [data-testid*="continue"]:visible, [data-testid*="login"]:visible').first
                    if await btn.count():
                        await btn.click(timeout=5000)
                    else:
                        await f.press("Enter", timeout=5000)
                except Exception:
                    pass
        step = await d_wait(pg, {"signed_in", "password", "code"} - {cur}, 30)
        if step in ("password", "code"):
            return web.json_response({"session": sid, "step": step})
        if step != "signed_in":
            await d_dump(pg, "after " + kind)
            return web.json_response({"session": sid, "step": kind, "message": (await error_text(pg)) or "Disney+ didn't accept that. Check it and try again."})
        return web.json_response(await d_finish(sid))
    except Exception as err:
        log.exception("disney answer failed")
        return web.json_response({"session": sid, "step": kind, "message": f"Something went wrong: {err}"})


async def d_finish(sid):
    s = SESSIONS[sid]
    pg = s["pg"]
    await pg.wait_for_timeout(3000)
    plist = await d_profiles(pg)
    if plist:
        await d_pick(pg, plist[0]["name"])
    acct = secrets.token_urlsafe(12)
    await s["ctx"].storage_state(path=d_state(acct))
    with open(d_meta(acct), "w") as fh:
        json.dump({"profiles": plist, "active": plist[0]["guid"] if plist else None}, fh)
    hist = await d_read(pg, 5)
    await close(sid)
    if hist.get("error"):
        return {"step": "error", "message": "Signed in, but Disney+ didn't show the home screen: " + hist["error"]}
    return {"step": "done", "account": acct, "profiles": plist, "sample": len(hist.get("items", []))}


D_ACCOUNTS = {}


async def d_page(acct):
    c = D_ACCOUNTS.get(acct)
    if c and not c["pg"].is_closed():
        return c["pg"]
    if not os.path.exists(d_state(acct)):
        return None
    b = await browser()
    ctx = await b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900}, storage_state=d_state(acct))
    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    pg = await ctx.new_page()
    D_ACCOUNTS[acct] = {"ctx": ctx, "pg": pg}
    return pg


async def d_history(req):
    data = await req.json()
    acct, want = data.get("account"), data.get("profile")
    pg = await d_page(acct)
    if pg is None:
        return web.json_response({"error": "unknown_account"}, status=404)
    async with LOCKS.setdefault("d" + acct, asyncio.Lock()):
        try:
            with open(d_meta(acct)) as fh:
                meta = json.load(fh)
        except Exception:
            meta = {"profiles": [], "active": None}
        try:
            if want and want != meta.get("active") and any(p["guid"] == want for p in meta.get("profiles", [])):
                if not await d_pick(pg, want):
                    return web.json_response({"error": "That profile is locked with a PIN"})
                meta["active"] = want
                with open(d_meta(acct), "w") as fh:
                    json.dump(meta, fh)
            res = await d_read(pg, int(data.get("size", 40)))
            res["profile_guid"], res["profiles"] = meta.get("active"), meta.get("profiles", [])
            if not res.get("error"):
                await D_ACCOUNTS[acct]["ctx"].storage_state(path=d_state(acct))
            return web.json_response(res, status=401 if res.get("error") == "signed_out" else 200)
        except Exception as err:
            log.exception("disney history failed")
            return web.json_response({"error": str(err)}, status=500)



async def health(req):
    return web.json_response({"ok": True, "sessions": len(SESSIONS), "version": "1.4.0"})


async def startup(app):
    app["reaper"] = asyncio.create_task(reap())


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/netflix/start", start)
app.router.add_post("/netflix/answer", answer)
app.router.add_post("/netflix/profile", pick)
app.router.add_post("/netflix/history", history)
app.router.add_post("/netflix/forget", forget)
app.router.add_get("/netflix/latest", latest)
app.router.add_get("/netflix/debug_rows", debug_rows)
app.router.add_post("/netflix/profiles", list_profiles)
app.router.add_post("/disney/start", d_start)
app.router.add_post("/disney/answer", d_answer)
app.router.add_post("/disney/history", d_history)
app.on_startup.append(startup)
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8099")))
