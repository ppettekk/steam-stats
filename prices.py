"""Сборщик цен. Гоняется по таймеру, в прод-запросы не лезет.

  python -m worker.prices seed    - залить топ-10к игр из SteamSpy в очередь (раз)
  python -m worker.prices fx      - обновить курс ЦБ (раз в сутки)
  python -m worker.prices run     - разгрести очередь (раз в неделю + после seed)

Порядок при первом запуске: fx, seed, run.
"""
import re
import sys
import time
import xml.etree.ElementTree as ET

import httpx

from app import db

STORE = "https://store.steampowered.com/api/appdetails"
SPY = "https://steamspy.com/api.php"
CBR = "https://www.cbr.ru/scripts/XML_daily.asp"

BATCH = 50           # appdetails принимает пачку только вместе с filters
PAUSE = 2.0
SPY_PAGES = 10       # 1000 игр на страницу
UA = {"User-Agent": "steamstats/1.0 (+hobby project)"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cmd_fx():
    r = httpx.get(CBR, timeout=20, headers=UA)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    date = root.attrib.get("Date", "")
    rows = []
    for v in root.findall("Valute"):
        code = v.findtext("CharCode")
        value = float(v.findtext("Value").replace(",", "."))
        nominal = float(v.findtext("Nominal") or 1)
        rows.append((code, value / nominal, date))
    conn = db.connect()
    conn.executemany(
        "INSERT OR REPLACE INTO fx(currency, rate_rub, date) VALUES (?,?,?)", rows
    )
    conn.commit()
    log(f"курсы на {date}: USD = {db.get_fx(conn, 'USD'):.2f} руб")
    conn.close()


def cmd_seed():
    """SteamSpy режет до одного запроса в минуту, торопиться нельзя."""
    conn = db.connect()
    total = 0
    for page in range(SPY_PAGES):
        r = httpx.get(SPY, params={"request": "all", "page": page}, timeout=60, headers=UA)
        if r.status_code != 200:
            log(f"страница {page}: HTTP {r.status_code}, стоп")
            break
        appids = [int(a) for a in r.json().keys()]
        if not appids:
            break
        db.enqueue(conn, appids, db.HOME_CC, priority=100)
        total += len(appids)
        log(f"страница {page}: +{len(appids)}, всего {total}")
        if page < SPY_PAGES - 1:
            time.sleep(61)
    conn.close()


def _parse(entry):
    """Три исхода, и not_sold - значимый результат, а не пропуск."""
    if not entry.get("success"):
        return ("not_sold", None, None)
    data = entry.get("data")
    if not isinstance(data, dict):
        return ("no_price", None, None)
    po = data.get("price_overview")
    if not po:
        return ("no_price", None, None)
    # initial, а не final: во время распродажи вся статистика поедет на 70%
    return ("priced", po.get("initial"), po.get("currency"))


def fetch_batch(client, appids, cc):
    params = {
        "appids": ",".join(str(a) for a in appids),
        "filters": "price_overview",
        "cc": cc,
        "l": "en",
    }
    for attempt in range(5):
        r = client.get(STORE, params=params, timeout=30, headers=UA)
        if r.status_code == 429:
            wait = 30 * (attempt + 1)
            log(f"429, ждём {wait}с")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("appdetails не отвечает, стоп")


def cmd_run(limit=None):
    conn = db.connect()
    now = int(time.time())
    processed = 0
    with httpx.Client(follow_redirects=True) as client:
        while True:
            rows = conn.execute(
                "SELECT appid, cc FROM queue ORDER BY priority, appid LIMIT ?", (BATCH,)
            ).fetchall()
            if not rows:
                break
            # В одном запросе только один cc
            cc = rows[0]["cc"]
            batch = [r["appid"] for r in rows if r["cc"] == cc]

            try:
                data = fetch_batch(client, batch, cc)
            except Exception as e:
                log(f"ошибка: {e}")
                break

            out = []
            for appid in batch:
                state, initial, currency = _parse(data.get(str(appid), {}))
                out.append((appid, cc, state, initial, currency, now))

            conn.executemany(
                """INSERT OR REPLACE INTO prices(appid, cc, state, initial, currency, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                out,
            )
            conn.executemany("DELETE FROM queue WHERE appid=? AND cc=?", [(a, cc) for a in batch])
            conn.commit()

            processed += len(batch)
            log(f"{cc}: обработано {processed}")
            if limit and processed >= limit:
                break
            time.sleep(PAUSE)

    # Второй проход: то, что не продаётся в RU, оцениваем по US.
    n = conn.execute(
        """INSERT INTO queue(appid, cc, priority)
           SELECT p.appid, ?, 50 FROM prices p
           WHERE p.cc=? AND p.state='not_sold'
             AND NOT EXISTS (SELECT 1 FROM prices q WHERE q.appid=p.appid AND q.cc=?)
           ON CONFLICT DO NOTHING""",
        (db.FALLBACK_CC, db.HOME_CC, db.FALLBACK_CC),
    ).rowcount
    conn.commit()
    if n > 0:
        log(f"в очередь на {db.FALLBACK_CC} добавлено {n}, запускай run ещё раз")
    conn.close()


def cmd_clean():
    """Кэш карточек и старые png."""
    conn = db.connect()
    n = conn.execute(
        "DELETE FROM results WHERE created_at < ?", (int(time.time()) - db.RESULT_TTL,)
    ).rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    log(f"удалено карточек: {n}")


if __name__ == "__main__":
    db.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    {"seed": cmd_seed, "fx": cmd_fx, "clean": cmd_clean}.get(cmd, lambda: cmd_run(limit))()
