"""One Stop Sign-in Helper: drives Netflix's website sign-in in a hidden browser for Home Assistant."""
import asyncio, os, secrets, time, logging
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


PROFILE_LINKS = ['[data-uia^="profile-link"]', 'a.profile-link', '.choose-profile .profile', 'li.profile a']


async def profile_links(pg):
    for sel in PROFILE_LINKS:
        loc = pg.locator(sel)
        if await loc.count():
            return loc
    return None


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


async def done(sid, profile=None):
    s = SESSIONS[sid]
    acct = secrets.token_urlsafe(12)
    await s["ctx"].storage_state(path=state_path(acct))
    hist = await read_history(s["pg"], 5)
    await close(sid)
    if hist.get("error"):
        return {"step": "error", "message": "Signed in, but Netflix's history page didn't load: " + hist["error"]}
    return {"step": "done", "account": acct, "profile": profile, "sample": len(hist.get("items", []))}


async def read_history(pg, size=40):
    """Read viewing activity from inside the signed-in browser (same as netflix.com/viewingactivity)."""
    if "netflix.com/browse" not in pg.url:
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
        await pg.wait_for_timeout(1500)
    if "/login" in pg.url:
        return {"error": "signed_out"}
    return await pg.evaluate("""async (size) => {
      const html = document.documentElement.innerHTML;
      let build = (window.netflix && netflix.reactContext && netflix.reactContext.models.serverDefs && netflix.reactContext.models.serverDefs.data.BUILD_IDENTIFIER) || (html.match(/"BUILD_IDENTIFIER":"([^"]+)"/) || [])[1];
      const tries = [build && `/api/shakti/${build}/viewingactivity`, '/api/shakti/mre/viewingactivity'].filter(Boolean);
      let last = 'no build id';
      for (const u of tries) {
        try {
          const r = await fetch(`${u}?pg=0&pgSize=${size}`, { credentials: 'include', headers: { Accept: 'application/json' } });
          if (r.status === 401 || r.status === 403) return { error: 'signed_out' };
          if (!r.ok) { last = 'HTTP ' + r.status; continue; }
          const d = await r.json();
          return { items: d.viewedItems || [], profile: d.profileName || null };
        } catch (e) { last = String(e); }
      }
      return { error: last };
    }""", size)


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


async def history(req):
    data = await req.json()
    acct = data.get("account")
    pg = await account_page(acct)
    if pg is None:
        return web.json_response({"error": "unknown_account"}, status=404)
    try:
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
        res = await read_history(pg, int(data.get("size", 40)))
        if not res.get("error"):
            await ACCOUNTS[acct]["ctx"].storage_state(path=state_path(acct))   # keep refreshed sign-in
        return web.json_response(res, status=401 if res.get("error") == "signed_out" else 200)
    except Exception as err:
        log.exception("history failed")
        return web.json_response({"error": str(err)}, status=500)


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
        names = await profiles(pg)
        if len(names) > 1:
            return web.json_response({"session": sid, "step": "profiles", "profiles": names})
        if len(names) == 1:
            links = await profile_links(pg)
            await links.nth(0).click()
            await pg.wait_for_timeout(4000)
        return web.json_response(await done(sid, names[0] if names else None))
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


async def health(req):
    return web.json_response({"ok": True, "sessions": len(SESSIONS), "version": "1.1.0"})


async def startup(app):
    app["reaper"] = asyncio.create_task(reap())


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/netflix/start", start)
app.router.add_post("/netflix/answer", answer)
app.router.add_post("/netflix/profile", pick)
app.router.add_post("/netflix/history", history)
app.router.add_post("/netflix/forget", forget)
app.on_startup.append(startup)
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8099")))
