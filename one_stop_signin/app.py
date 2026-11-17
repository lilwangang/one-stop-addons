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


async def profiles(pg):
    if "/browse" not in pg.url and "/profiles" not in pg.url:
        await pg.goto("https://www.netflix.com/browse", wait_until="domcontentloaded")
    await pg.wait_for_timeout(2500)
    names = []
    for sel in ['[data-uia*="profile-link"] .profile-name', '.profile-link .profile-name', 'span.profile-name']:
        loc = pg.locator(sel)
        n = await loc.count()
        if n:
            names = [body_text(await loc.nth(i).inner_text()) for i in range(n)]
            break
    return names


async def cookies(s):
    jar = {c["name"]: c["value"] for c in await s["ctx"].cookies("https://www.netflix.com")}
    return jar.get("NetflixId"), jar.get("SecureNetflixId")


async def done(sid, profile=None):
    s = SESSIONS[sid]
    nid, sec = await cookies(s)
    if not (nid and sec):
        return {"step": "error", "message": "Signed in, but Netflix didn't hand over the sign-in. Try again."}
    await close(sid)
    return {"step": "done", "netflix_id": nid, "secure_netflix_id": sec, "profile": profile}


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
        await f.click()
        await f.fill("")
        await f.press_sequentially(value, delay=50)
        await pg.wait_for_timeout(500)
        btn = pg.locator('button[type="submit"], button[data-uia*="submit"]')
        if await btn.count():
            await btn.first.click()
        else:
            await f.press("Enter")
        step = await wait_screen(pg, {"signed_in"}, 30)
        if step != "signed_in":
            return web.json_response({"session": sid, "step": kind, "message": await error_text(pg) or "Netflix didn't accept that. Check it and try again."})
        names = await profiles(pg)
        if len(names) > 1:
            return web.json_response({"session": sid, "step": "profiles", "profiles": names})
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
        loc = None
        for sel in ['[data-uia*="profile-link"]', '.profile-link']:
            if await pg.locator(sel).count():
                loc = pg.locator(sel).nth(idx)
                break
        if loc is None:
            return web.json_response(await done(sid, names[idx] if idx < len(names) else None))
        await loc.click()
        await pg.wait_for_timeout(4000)
        if await pg.locator('input[type="password"], input[data-uia*="pin"]').count():
            await close(sid)
            return web.json_response({"step": "error", "message": "That profile has a PIN lock. Pick a profile without a PIN, or remove the lock in Netflix."})
        return web.json_response(await done(sid, names[idx] if idx < len(names) else None))
    except Exception as err:
        log.exception("pick failed")
        return web.json_response({"step": "error", "message": f"Couldn't pick that profile: {err}"})


async def health(req):
    return web.json_response({"ok": True, "sessions": len(SESSIONS), "version": "1.0.0"})


async def startup(app):
    app["reaper"] = asyncio.create_task(reap())


app = web.Application()
app.router.add_get("/health", health)
app.router.add_post("/netflix/start", start)
app.router.add_post("/netflix/answer", answer)
app.router.add_post("/netflix/profile", pick)
app.on_startup.append(startup)
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8099")))
