import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import calc, db, render, steam

BASE = Path(__file__).resolve().parent.parent
app = FastAPI(title="Steam Life", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))

ERROR_TEXT = {
    "not_found": "Профиль не найден. Проверь ссылку.",
    "private": "Библиотека скрыта. Настройки приватности → «Мои данные об игре» → Открытый.",
    "no_games": "В библиотеке нет игр.",
    "upstream": "Steam не отвечает, попробуй через минуту.",
}


# ── фильтры шаблонов ─────────────────────────────────────────
def _ru(v):
    """Неразрывный пробел между разрядами, дробная часть через запятую."""
    if isinstance(v, float) and v != int(v):
        return f"{v:,.1f}".replace(",", "\u00a0").replace(".", ",")
    return f"{int(v):,}".replace(",", "\u00a0")


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _hm(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h} ч {m} мин" if h else f"{m} мин"


templates.env.filters.update(ru=_ru, plural=_plural, hm=_hm)


@app.on_event("startup")
def _startup():
    db.init()


def _host(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ── данные ───────────────────────────────────────────────────
async def _compute(q: str, refresh: bool = False, cc: str = db.HOME_CC):
    cc = cc if cc in db.REGIONS else db.HOME_CC
    conn = db.connect()
    try:
        kind, value = steam.parse_input(q)

        # Если на входе уже id64 - отдаём кэш, не трогая Steam.
        if kind == "id64" and not refresh:
            row = conn.execute(
                "SELECT payload, created_at FROM results WHERE steamid64=? AND cc=?",
                (value, cc),
            ).fetchone()
            if row and time.time() - row["created_at"] < db.RESULT_TTL:
                return json.loads(row["payload"])

        steamid64, summary, games = await steam.fetch_profile(q)

        row = conn.execute(
            "SELECT payload, created_at FROM results WHERE steamid64=? AND cc=?",
            (steamid64, cc),
        ).fetchone()
        if row and not refresh and time.time() - row["created_at"] < db.RESULT_TTL:
            return json.loads(row["payload"])

        appids = [g["appid"] for g in games]
        prices_by_cc = {cc: db.get_prices(conn, appids, cc) for cc in db.REGIONS}
        payload = calc.build(summary, games, prices_by_cc, db.get_fx(conn), home_cc=cc)

        conn.execute(
            "INSERT OR REPLACE INTO results(steamid64, cc, payload, created_at) VALUES (?,?,?,?)",
            (steamid64, cc, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.execute(
            "UPDATE meta SET profiles = profiles + 1, hours = hours + ? WHERE id = 1",
            (payload["time"]["total_hours"],),
        )

        # Всё, чего нет в кэше цен, уходит в очередь с высоким приоритетом:
        # это игры живых пользователей, они важнее хвоста из SteamSpy.
        for cc in db.REGIONS:
            missing = [a for a in appids if a not in prices_by_cc[cc]]
            if missing:
                db.enqueue(conn, missing, cc, priority=10)

        conn.commit()

        # Карточку рисуем сразу: ссылку кинут в чат в ближайшие секунды,
        # и мессенджер придёт за og:image раньше, чем человек обновит страницу.
        try:
            if cc == db.HOME_CC:
                render.render_to_file(payload, steamid64)
        except Exception:
            pass

        return payload
    finally:
        conn.close()


def _cached(steamid64: str, cc: str = db.HOME_CC):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM results WHERE steamid64=? AND cc=?", (steamid64, cc)
        ).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


# ── маршруты ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = db.connect()
    try:
        m = conn.execute("SELECT profiles, hours FROM meta WHERE id=1").fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "host": _host(request),
            "stats": {"profiles": m["profiles"], "years": round(m["hours"] / 8766)},
        },
    )


@app.get("/api/lookup")
async def lookup(q: str, refresh: bool = False, cc: str = db.HOME_CC):
    try:
        return JSONResponse(await _compute(q, refresh, cc))
    except steam.SteamError as e:
        raise HTTPException(400, {"code": e.code, "message": ERROR_TEXT.get(e.code, str(e))})
    except HTTPException:
        raise
    except Exception:
        logging.exception("lookup failed: %s", q)
        raise HTTPException(502, {"code": "upstream", "message": ERROR_TEXT["upstream"]})


@app.get("/u/{steamid64}", response_class=HTMLResponse)
async def result(request: Request, steamid64: str, cc: str = db.HOME_CC):
    cc = cc if cc in db.REGIONS else db.HOME_CC
    d = _cached(steamid64, cc)
    if not d:
        try:
            d = await _compute(steamid64, cc=cc)
        except Exception:
            return RedirectResponse("/", status_code=302)

    # Сетка «жизнь в днях»: 182 клетки (26 x 7), каждая — доля возраста аккаунта.
    days = max(1, d["account_days"])
    step = max(1, round(days / 182))
    played_cells = min(182, round(d["time"]["days"] / step))

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "d": d,
            "host": _host(request),
            "grid_step": step,
            "grid_played": played_cells,
            "cc": cc,
            "regions": db.REGIONS,
        },
    )


@app.get("/og/{steamid64}.jpg")
async def og(steamid64: str):
    path = render.CACHE / f"{steamid64}.jpg"
    if not path.exists():
        d = _cached(steamid64)
        if not d:
            raise HTTPException(404)
        path = render.render_to_file(d, steamid64)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=43200"})


@app.get("/api/health")
def health():
    conn = db.connect()
    try:
        return {
            "prices": {cc: conn.execute("SELECT COUNT(*) c FROM prices WHERE cc=?", (cc,)).fetchone()["c"]
                       for cc in db.REGIONS},
            "queue": conn.execute("SELECT COUNT(*) c FROM queue").fetchone()["c"],
            "results": conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"],
            "fx": conn.execute("SELECT COUNT(*) c FROM fx").fetchone()["c"],
        }
    finally:
        conn.close()
