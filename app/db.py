import os
import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("STEAMSTATS_DB", str(BASE / "data" / "steam.db"))
STEAM_API_KEY = STEAM_KEY = os.getenv("STEAM_API_KEY", "")

HOME_CC = "ru"          # базовый регион
FALLBACK_CC = "us"      # чем оцениваем то, что в RU не продаётся

# Регионы для блока сравнения. Турция и Аргентина сидят на USD с тех пор,
# как Valve убрала там локальные валюты - региональный арбитраж в них мёртв,
# но в таблице они остаются, чтобы это было видно.
REGIONS = {
    "ru": "Россия",
    "kz": "Казахстан",
    "ua": "Украина",
    "tr": "Турция",
    "us": "США",
    "de": "Германия",
}
RESULT_TTL = 12 * 3600  # кэш карточки
PRICE_TTL = 7 * 24 * 3600

# Порог, ниже которого не считаем рубль за час: 5 минут запуска дают
# бессмысленные миллионы рублей в час и ломают номинацию "худшая покупка".
MIN_MINUTES_FOR_RATE = 180


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init():
    # Пустой ключ даёт 403 на первом же запросе и маскируется под
    # "Steam не отвечает". Лучше не стартовать вообще.
    if not STEAM_KEY:
        raise RuntimeError("STEAM_API_KEY пуст: проверь .env и EnvironmentFile в юните")

    schema = (Path(__file__).resolve().parent / "schema.sql").read_text()
    with connect() as conn:
        conn.executescript(schema)


def get_prices(conn, appids, cc):
    """{appid: sqlite3.Row} по списку appid. Бьём на пачки, чтобы не упереться
    в лимит переменных SQLite при библиотеке в тысячу игр."""
    out = {}
    appids = list(appids)
    for i in range(0, len(appids), 500):
        chunk = appids[i:i + 500]
        q = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM prices WHERE cc=? AND appid IN ({q})", [cc, *chunk]
        ).fetchall()
        for r in rows:
            out[r["appid"]] = r
    return out


def enqueue(conn, appids, cc, priority=100):
    now = int(time.time())
    conn.executemany(
        """INSERT INTO queue(appid, cc, priority) VALUES (?,?,?)
           ON CONFLICT(appid, cc) DO UPDATE SET priority=MIN(priority, excluded.priority)""",
        [(a, cc, priority) for a in appids],
    )
    conn.execute(
        "DELETE FROM queue WHERE cc=? AND appid IN (SELECT appid FROM prices WHERE cc=? AND updated_at > ?)",
        (cc, cc, now - PRICE_TTL),
    )
    conn.commit()


def get_fx(conn, currency=None):
    if currency:
        row = conn.execute("SELECT rate_rub FROM fx WHERE currency=?", (currency,)).fetchone()
        return row["rate_rub"] if row else None
    return {r["currency"]: r["rate_rub"] for r in conn.execute("SELECT currency, rate_rub FROM fx")}
