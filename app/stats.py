"""Обезличенный сбор данных для агрегированного отчёта.

Пишется один раз на профиль: повторный расчёт того же аккаунта счётчики
не двигает, иначе один человек, обновивший страницу десять раз, перекосит
всю выборку. Steam ID не хранится, только хэш.
"""
import hashlib
import os

SALT = os.getenv("STEAMSTATS_SALT", "steamlife")


def _hash(steamid64: str) -> str:
    return hashlib.sha256(f"{SALT}:{steamid64}".encode()).hexdigest()[:32]


def record(conn, payload, games):
    sid = _hash(payload["steamid64"])
    if conn.execute("SELECT 1 FROM snapshots WHERE sid_hash=?", (sid,)).fetchone():
        return False

    t, m, b = payload["time"], payload["money"], payload["blocked"]
    conn.execute(
        """INSERT INTO snapshots(sid_hash, created_at, account_days, account_year,
             total_hours, games_owned, games_played, games_never, library_value,
             dead_value, per_hour, coverage, top5_share, blocked_count,
             blocked_value, blocked_hours)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sid, payload["generated_at"], payload["account_days"],
            int(payload["created_str"][-4:]) if payload.get("created_str") else None,
            t["total_hours"], t["games_owned"], t["games_played"], t["games_never"],
            m["library_value"] if m["reliable"] else None,
            m["dead_value"] if m["reliable"] else None,
            m["avg_per_hour"] if m["reliable"] else None,
            m["coverage"], t["top5_share"],
            b["count"], b["value_rub"], b["hours"],
        ),
    )
    conn.executemany(
        """INSERT INTO game_stats(appid, name, owners, played, never, total_minutes)
           VALUES (?,?,1,?,?,?)
           ON CONFLICT(appid) DO UPDATE SET
             name = COALESCE(excluded.name, name),
             owners = owners + 1,
             played = played + excluded.played,
             never = never + excluded.never,
             total_minutes = total_minutes + excluded.total_minutes""",
        [(g["appid"], g.get("name"),
          1 if g.get("playtime_forever", 0) > 0 else 0,
          1 if g.get("playtime_forever", 0) == 0 else 0,
          g.get("playtime_forever", 0)) for g in games],
    )
    return True


def _median(conn, col):
    """SQLite без перцентилей, считаем медиану через смещение."""
    row = conn.execute(
        f"""SELECT {col} FROM snapshots WHERE {col} IS NOT NULL
            ORDER BY {col} LIMIT 1
            OFFSET (SELECT COUNT(*) / 2 FROM snapshots WHERE {col} IS NOT NULL)"""
    ).fetchone()
    return row[0] if row else None


def aggregate(conn, min_profiles=50):
    n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if n < min_profiles:
        # На маленькой выборке медианы скачут, показывать их нечестно
        return {"profiles": n, "ready": False, "min_profiles": min_profiles}

    avg = conn.execute(
        """SELECT AVG(total_hours), AVG(games_owned), AVG(games_never),
                  AVG(library_value), AVG(per_hour), AVG(blocked_count)
           FROM snapshots"""
    ).fetchone()

    top = conn.execute(
        """SELECT name, owners, played, never, total_minutes
           FROM game_stats WHERE name IS NOT NULL
           ORDER BY owners DESC LIMIT 15"""
    ).fetchall()

    # Самые заброшенные: у многих есть, но почти никто не запускал
    dust = conn.execute(
        """SELECT name, owners, never, ROUND(never * 100.0 / owners, 1) AS share
           FROM game_stats WHERE owners >= 10 AND name IS NOT NULL
           ORDER BY share DESC, owners DESC LIMIT 15"""
    ).fetchall()

    return {
        "profiles": n,
        "ready": True,
        "median": {
            "hours": _median(conn, "total_hours"),
            "games": _median(conn, "games_owned"),
            "never": _median(conn, "games_never"),
            "library": _median(conn, "library_value"),
            "per_hour": _median(conn, "per_hour"),
        },
        "mean": {
            "hours": round(avg[0] or 0),
            "games": round(avg[1] or 0),
            "never": round(avg[2] or 0),
            "library": round(avg[3] or 0),
            "per_hour": round(avg[4] or 0, 1),
            "blocked": round(avg[5] or 0, 1),
        },
        "top_owned": [dict(r) for r in top],
        "most_dusty": [dict(r) for r in dust],
    }
