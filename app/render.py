"""OG-карточка Steam Life, 1200x630, на Pillow.

Headless-браузер сюда не ставим намеренно: Playwright съедает полгига на
инстанс, а на машине 708 МБ и рядом живёт Kwork Radar.
"""
import os
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent.parent
FONTS = BASE / "assets" / "fonts"
CACHE = Path(os.getenv("STEAMSTATS_CACHE", str(BASE / "cache" / "og")))

W, H = 1200, 630
PAD_X, PAD_Y = 72, 62

BG = (29, 45, 61)            # --color-accent-900
FG = (238, 244, 250)
ACCENT = (148, 188, 227)     # --color-accent-400

_fonts = {}


def font(name, size):
    key = (name, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)
    return _fonts[key]


def cond(size):
    return font("FiraSansCondensed-SemiBold", size)


def body(size, medium=False):
    return font("FiraSans-Medium" if medium else "FiraSans-Regular", size)


def fade(alpha):
    """Прозрачность текста поверх известного фона - считаем цвет заранее,
    это дешевле, чем рисовать в RGBA-слой и композить."""
    return tuple(round(BG[i] + (FG[i] - BG[i]) * alpha) for i in range(3))


def num(n):
    return f"{int(n):,}".replace(",", "\u00a0")


def plural(n, one, few, many):
    """Русские числительные: 1 день, 2 дня, 5 дней."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def avatar(url, size):
    """Аватар грузится один раз на профиль, при рендере. Не достали - рисуем
    пустую рамку, как в макете, и не роняем карточку."""
    try:
        r = httpx.get(url, timeout=5.0)
        r.raise_for_status()
        from io import BytesIO
        im = Image.open(BytesIO(r.content)).convert("RGB")
        return im.resize((size, size), Image.LANCZOS)
    except Exception:
        return None


def tracked(draw, xy, text, fnt, fill, spacing=0):
    """Letter-spacing, которого в Pillow нет."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + spacing
    return x


def ellipsize(draw, text, fnt, max_w):
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return text + "…"


def _corners(draw, box, color, arm=13, off=7):
    """Регистрационные метки дизайн-системы: четыре плюса по углам."""
    x0, y0, x1, y1 = box
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        draw.line([(cx - arm + off, cy), (cx + arm - off, cy)], fill=color)
        draw.line([(cx, cy - arm + off), (cx, cy + arm - off)], fill=color)


def render(data) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Декоративные рамки в правом верхнем углу
    d.rectangle([W - 480, -80, W + 80, 480], outline=fade(0.14))
    d.rectangle([W - 380, -180, W + 180, 380], outline=fade(0.10))

    t, m, b = data["time"], data["money"], data["blocked"]

    # ── шапка ────────────────────────────────────────────────
    box = (PAD_X, PAD_Y, PAD_X + 68, PAD_Y + 68)
    av = avatar(data.get("avatar") or "", 68)
    if av:
        img.paste(av, (PAD_X, PAD_Y))
    d.rectangle(box, outline=fade(0.3))
    _corners(d, box, fade(0.5))

    nx = PAD_X + 92
    d.text((nx, PAD_Y + 2), ellipsize(d, data["persona"] or "—", cond(38), 620), font=cond(38), fill=FG)
    d.text((nx, PAD_Y + 46), f"в Steam с {data['created_str'] or '—'}", font=body(21), fill=fade(0.55))

    label = "STEAM LIFE"
    lw = sum(d.textlength(c, font=cond(24)) + 3.5 for c in label)
    tracked(d, (W - PAD_X - lw, PAD_Y + 12), label, cond(24), fade(0.55), 3.5)

    # ── главная цифра ────────────────────────────────────────
    hours = f"{num(t['total_hours'])} ч"
    d.text((PAD_X, 232), hours, font=cond(150), fill=FG)
    hw = d.textlength(hours, font=cond(150))
    days = t["days"]
    d.text((PAD_X + hw + 26, 330),
           f"= {num(days)} {plural(days, 'день', 'дня', 'дней')} жизни",
           font=cond(40), fill=fade(0.55))

    # Вторая строка подстраивается: если денег нет, показываем то, что есть.
    nv = t["games_never"]
    tail = (f"{num(nv)} {plural(nv, 'игра', 'игры', 'игр')} "
            f"{plural(nv, 'так и не запущена', 'так и не запущены', 'так и не запущены')}.")
    if m["reliable"] and m["library_value"]:
        line = f"и {num(m['library_value'])} ₽ — по {m['avg_per_hour']:g} ₽ за час. {tail}"
    else:
        line = f"{num(t['games_owned'])} {plural(t['games_owned'], 'игра', 'игры', 'игр')} в библиотеке. {tail}"
    d.text((PAD_X, 412), ellipsize(d, line, body(27), W - PAD_X * 2), font=body(27), fill=fade(0.82))

    # ── подвал ───────────────────────────────────────────────
    y = H - PAD_Y - 78
    d.line([(PAD_X, y), (W - PAD_X, y)], fill=fade(0.2))
    y += 22

    if t["top"]:
        g = t["top"][0]
        d.text((PAD_X, y), ellipsize(d, g["name"], cond(38), 560), font=cond(38), fill=FG)
        tracked(d, (PAD_X, y + 44), f"ГЛАВНАЯ ИГРА · {num(g['hours'])} Ч".upper(),
                body(19), fade(0.55), 1.6)

    # Справа - самая злая из доступных цифр
    if b["count"]:
        val = f"{num(b['count'])} {plural(b['count'], 'игра', 'игры', 'игр')}"
        cap = "НЕДОСТУПНО В РОССИИ"
    elif m["reliable"] and m["dead_value"]:
        val, cap = f"{num(m['dead_value'])} ₽", "В НЕЗАПУЩЕННОМ"
    else:
        val, cap = f"{t['weeks']:g}".replace(".", ",") + " нед", "НЕПРЕРЫВНОЙ ИГРЫ"

    vw = d.textlength(val, font=cond(38))
    d.text((W - PAD_X - vw, y), val, font=cond(38), fill=FG)
    cw = sum(d.textlength(c, font=body(19)) + 1.6 for c in cap)
    tracked(d, (W - PAD_X - cw, y + 44), cap, body(19), fade(0.55), 1.6)

    return img


def render_to_file(data, steamid64: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{steamid64}.jpg"
    # JPEG, не PNG: на карточку уходит ~100 КБ вместо 400, а диска 1.3 ГБ
    render(data).save(path, "JPEG", quality=85, optimize=True, progressive=True)
    return path
