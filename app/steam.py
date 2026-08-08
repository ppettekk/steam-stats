import re
import httpx

from . import db

API = "https://api.steampowered.com"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class SteamError(Exception):
    """code: not_found | private | no_games | upstream"""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_input(raw: str):
    """Из чего угодно достаём либо steamid64, либо vanity-имя.
    Принимаем: /profiles/765..., /id/nickname, голый id, голый ник."""
    s = (raw or "").strip()
    if not s:
        raise SteamError("not_found", "Пустой запрос")

    m = re.search(r"/profiles/(\d{17})", s)
    if m:
        return ("id64", m.group(1))
    m = re.search(r"/id/([A-Za-z0-9_\-.]+)", s)
    if m:
        return ("vanity", m.group(1))
    if re.fullmatch(r"\d{17}", s):
        return ("id64", s)
    if re.fullmatch(r"[A-Za-z0-9_\-.]{2,64}", s):
        return ("vanity", s)
    raise SteamError("not_found", "Не похоже на профиль Steam")


async def resolve(client, raw: str) -> str:
    kind, value = parse_input(raw)
    if kind == "id64":
        return value
    r = await client.get(
        f"{API}/ISteamUser/ResolveVanityURL/v1/",
        params={"key": db.STEAM_KEY, "vanityurl": value},
    )
    r.raise_for_status()
    resp = r.json().get("response", {})
    if resp.get("success") != 1:
        raise SteamError("not_found", "Профиль с таким адресом не найден")
    return resp["steamid"]


async def get_summary(client, steamid64: str) -> dict:
    r = await client.get(
        f"{API}/ISteamUser/GetPlayerSummaries/v2/",
        params={"key": db.STEAM_KEY, "steamids": steamid64},
    )
    r.raise_for_status()
    players = r.json().get("response", {}).get("players", [])
    if not players:
        raise SteamError("not_found", "Профиль не найден")
    return players[0]


async def get_owned_games(client, steamid64: str) -> list:
    """Закрытый профиль не отдаёт ошибку - он отдаёт пустой response.
    Отличаем закрытый от пустого по наличию ключа game_count."""
    r = await client.get(
        f"{API}/IPlayerService/GetOwnedGames/v1/",
        params={
            "key": db.STEAM_KEY,
            "steamid": steamid64,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "skip_unvetted_apps": "false",
        },
    )
    r.raise_for_status()
    resp = r.json().get("response", {})
    if "game_count" not in resp:
        raise SteamError(
            "private",
            "Библиотека скрыта. Профиль → Настройки приватности → «Игровые подробности» → Открытый",
        )
    games = resp.get("games") or []
    if not games:
        raise SteamError("no_games", "В библиотеке нет игр")
    return games


async def fetch_profile(raw: str):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        steamid64 = await resolve(client, raw)
        summary = await get_summary(client, steamid64)
        games = await get_owned_games(client, steamid64)
        return steamid64, summary, games
