import time
from datetime import datetime

from .db import HOME_CC, MIN_MINUTES_FOR_RATE, REGIONS

VERDICT_GOOD = 20      # руб/час и ниже - окупилась
VERDICT_BAD = 300      # руб/час и выше - переплата


def _money(minor, rate=1.0):
    """Steam отдаёт цену в минорных единицах: 190000 = 1900 руб."""
    return minor / 100.0 * rate


def build(summary, games, prices_by_cc, fx):
    """prices_by_cc: {cc: {appid: row}}. fx: {currency: рублей за единицу}."""
    now = int(time.time())
    home = prices_by_cc.get(HOME_CC, {})

    total_min = sum(g.get("playtime_forever", 0) for g in games)
    total_hours = total_min / 60.0

    created = summary.get("timecreated")
    account_days = max(1, (now - created) // 86400) if created else None

    played = [g for g in games if g.get("playtime_forever", 0) > 0]
    never = [g for g in games if g.get("playtime_forever", 0) == 0]

    # ── топ-5 ────────────────────────────────────────────────
    ranked = sorted(played, key=lambda g: -g["playtime_forever"])
    top = [
        {
            "appid": g["appid"],
            "name": g.get("name", f"App {g['appid']}"),
            "hours": round(g["playtime_forever"] / 60.0),
            "share": round(g["playtime_forever"] / total_min * 100, 1) if total_min else 0,
        }
        for g in ranked[:5]
    ]
    top_min = sum(g["playtime_forever"] for g in ranked[:5])
    rest_min = total_min - top_min

    # ── распределение библиотеки по наигранному ──────────────
    # Изначально тут был год последнего запуска, но Valve перестала отдавать
    # rtime_last_played: поле пустое и с include_extended_appinfo, и через
    # input_json, и на чужих аккаунтах с тысячей игр. Считаем из playtime.
    RANGES = [("0 ч", 0, 1), ("до 1 ч", 1, 60), ("1–10 ч", 60, 600),
              ("10–50 ч", 600, 3000), ("50–100 ч", 3000, 6000), ("100 ч+", 6000, None)]
    buckets = []
    for label, lo, hi in RANGES:
        n = sum(1 for g in games
                if lo <= g.get("playtime_forever", 0) and (hi is None or g.get("playtime_forever", 0) < hi))
        buckets.append({"label": label, "count": n})
    peak = max(buckets, key=lambda x: x["count"]) if any(b["count"] for b in buckets) else None
    # Запущенные и брошенные в первый же час. Ноль часов сюда не входит -
    # для них есть отдельный блок "кладбище".
    dropped = buckets[1]["count"]

    # ── деньги по домашнему региону ──────────────────────────
    wasted = None          # самая дорогая из тех, что не дожили до часа
    library_value = dead_value = 0.0
    priced_minutes = 0
    # Знаменатель покрытия: только те игры, у которых цена в принципе бывает.
    # F2P (no_price) и не продающиеся в регионе (not_sold) исключаются -
    # иначе у любого игрока в CS2 покрытие будет 10% и деньги скроются.
    priceable_minutes = 0
    rows, rates, graveyard = [], [], []

    for g in games:
        appid, minutes = g["appid"], g.get("playtime_forever", 0)
        r = home.get(appid)
        priced = r and r["state"] == "priced" and r["initial"]

        if minutes == 0:
            graveyard.append({
                "appid": appid,
                "name": g.get("name"),
                "price": round(_money(r["initial"])) if priced else None,
            })

        if r and r["state"] in ("no_price", "not_sold"):
            continue
        priceable_minutes += minutes

        if not priced:
            continue

        price = _money(r["initial"])
        library_value += price
        priced_minutes += minutes

        if minutes == 0:
            dead_value += price
        elif minutes < 60:
            if not wasted or price > wasted["price"]:
                wasted = {"name": g.get("name"), "price": round(price),
                          "minutes": minutes, "appid": appid}

        if minutes >= MIN_MINUTES_FOR_RATE:
            per_hour = price / (minutes / 60.0)
            rates.append((per_hour, g, price))
            rows.append({
                "name": g.get("name"),
                "hours": round(minutes / 60.0),
                "price": round(price),
                "per_hour": round(per_hour, 1) if per_hour < 10 else round(per_hour),
                "verdict": "окупилась" if per_hour <= VERDICT_GOOD
                else ("переплата" if per_hour >= VERDICT_BAD else "нормально"),
            })

    rows.sort(key=lambda x: -x["hours"])
    graveyard.sort(key=lambda x: -(x["price"] or 0))

    best = worst = None
    if rates:
        p, g, price = min(rates, key=lambda x: x[0])
        best = {"name": g.get("name"), "per_hour": round(p, 1),
                "hours": round(g["playtime_forever"] / 60.0), "price": round(price)}
        p, g, price = max(rates, key=lambda x: x[0])
        worst = {"name": g.get("name"), "per_hour": round(p),
                 "hours": round(g["playtime_forever"] / 60.0, 1), "price": round(price)}

    coverage = round(priced_minutes / priceable_minutes * 100) if priceable_minutes else 100

    # ── регионы ──────────────────────────────────────────────
    # Считаем только по играм, у которых цена есть во ВСЕХ регионах сразу.
    # Иначе регион с урезанным каталогом выглядит дешевле просто потому,
    # что части игр в нём нет.
    common = None
    for cc in REGIONS:
        have = {a for a, r in prices_by_cc.get(cc, {}).items()
                if r["state"] == "priced" and r["initial"]}
        common = have if common is None else (common & have)
    common = common or set()

    regions = []
    for cc, name in REGIONS.items():
        total, cur = 0.0, None
        for appid in common:
            r = prices_by_cc[cc][appid]
            cur = r["currency"]
            rate = 1.0 if cur == "RUB" else fx.get(cur, 0)
            total += _money(r["initial"], rate)
        regions.append({"cc": cc, "name": name, "currency": cur, "total": round(total)})

    base = next((r["total"] for r in regions if r["cc"] == HOME_CC), 0)
    for r in regions:
        r["diff"] = round((r["total"] - base) / base * 100) if base else 0
    regions.sort(key=lambda r: r["total"])

    # ── недоступно в России ──────────────────────────────────
    blocked = []
    us = prices_by_cc.get("us", {})
    blocked_min = 0
    for g in games:
        r = home.get(g["appid"])
        if not r or r["state"] != "not_sold":
            continue
        fb = us.get(g["appid"])
        # RU not_sold + US not_sold - снято с продажи везде, это не блокировка
        if not fb or fb["state"] != "priced" or not fb["initial"]:
            continue
        blocked_min += g.get("playtime_forever", 0)
        blocked.append({
            "appid": g["appid"],
            "name": g.get("name", f"App {g['appid']}"),
            "usd": round(fb["initial"] / 100.0, 2),
            "rub": round(_money(fb["initial"], fx.get("USD", 0))),
            "hours": round(g.get("playtime_forever", 0) / 60.0),
        })
    blocked.sort(key=lambda x: -x["rub"])

    return {
        "steamid64": summary.get("steamid"),
        "persona": summary.get("personaname"),
        "avatar": summary.get("avatarfull"),
        "created": created,
        "created_str": datetime.utcfromtimestamp(created).strftime("%d.%m.%Y") if created else None,
        "account_days": account_days,
        "generated_at": now,
        "time": {
            "total_hours": round(total_hours),
            "days": round(total_hours / 24),
            "weeks": round(total_hours / 168, 1),
            "years": round(total_hours / 8766, 2),
            "per_day_min": round(total_hours * 60 / account_days) if account_days else None,
            "games_owned": len(games),
            "games_played": len(played),
            "games_never": len(never),
            "never_share": round(len(never) / len(games) * 100) if games else 0,
            "top": top,
            "top5_share": round(top_min / total_min * 100, 1) if total_min else 0,
            "rest_count": max(0, len(games) - 5),
            "rest_hours": round(rest_min / 60.0),
            "rest_share": round(rest_min / total_min * 100, 1) if total_min else 0,
            "buckets": buckets,
            "peak": peak,
            "dropped": dropped,
            "wasted": wasted,
        },
        "money": {
            "currency": "RUB",
            "library_value": round(library_value),
            "avg_per_hour": round(library_value / total_hours, 1) if total_hours else 0,
            "dead_value": round(dead_value),
            "best": best,
            "worst": worst,
            "rows": rows[:8],
            "graveyard": graveyard,
            "coverage": coverage,
            # Ниже 50% покрытия по времени цифры врут - фронт прячет блок
            # целиком, а не показывает заниженную сумму без оговорок.
            "reliable": coverage >= 50,
        },
        "regions": {"list": regions, "common_games": len(common), "usd_rate": fx.get("USD")},
        "blocked": {
            "count": len(blocked),
            "value_rub": round(sum(b["rub"] for b in blocked)),
            "hours": round(blocked_min / 60.0),
            "share_of_playtime": round(blocked_min / total_min * 100, 1) if total_min else 0,
            "top": blocked[:12],
        },
    }
