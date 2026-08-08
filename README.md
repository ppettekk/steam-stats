# Steam Stats

Сайт: по ссылке на профиль Steam показывает, сколько человек наиграл за всю жизнь,
во сколько это обошлось и сколько игр из его библиотеки сейчас в России не купить.

## Логика в двух словах

На пользователя уходит ровно два запроса в официальный Steam Web API: резолв профиля
и `GetOwnedGames`. Цен там нет, они собираются заранее воркером через
`store.steampowered.com/api/appdetails` и лежат в SQLite. Прод-запрос в магазин
не ходит никогда.

Три состояния цены (`prices.state`):

| ответ appdetails | state | что значит |
|---|---|---|
| `success: false` | `not_sold` | в этом регионе не продаётся |
| `success: true`, `data: []` или без `price_overview` | `no_price` | F2P, предзаказ, DLC |
| есть `price_overview` | `priced` | цена есть |

`not_sold` по RU + `priced` по US = региональная блокировка, идёт в блок «недоступно
в России». `not_sold` в обоих = снято с продажи везде, молчим.

Берём `initial`, а не `final`: иначе во время распродажи вся статистика проседает в разы.

## Установка

```bash
apt update && apt install -y python3-venv nginx
mkdir -p /opt/steamstats && cd /opt/steamstats
# скопировать сюда app/ worker/ static/ templates/ assets/
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx pillow jinja2

cat > /opt/steamstats/.env << 'EOF'
STEAM_API_KEY=сюда_ключ_с_steamcommunity.com/dev/apikey
STEAMSTATS_DB=/opt/steamstats/data/steam.db
EOF
mkdir -p data cache/og
```

Первый прогон, по порядку:

```bash
cd /opt/steamstats && set -a && . ./.env && set +a
.venv/bin/python -m worker.prices fx      # курсы ЦБ
.venv/bin/python -m worker.prices seed    # ~10 минут, SteamSpy режет до 1 req/min
.venv/bin/python -m worker.prices run     # RU-проход, ~7 минут
.venv/bin/python -m worker.prices run     # US-проход по not_sold
```

## systemd

```ini
# /etc/systemd/system/steamstats.service
[Unit]
Description=Steam Stats
After=network.target

[Service]
WorkingDirectory=/opt/steamstats
EnvironmentFile=/opt/steamstats/.env
ExecStart=/opt/steamstats/.venv/bin/uvicorn app.api:app --host 127.0.0.1 --port 8100 --workers 1
Restart=always
MemoryHigh=250M
MemoryMax=350M

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/steamstats-worker.service   (Type=oneshot)
[Service]
Type=oneshot
WorkingDirectory=/opt/steamstats
EnvironmentFile=/opt/steamstats/.env
ExecStart=/opt/steamstats/.venv/bin/python -m worker.prices fx
ExecStart=/opt/steamstats/.venv/bin/python -m worker.prices run
ExecStart=/opt/steamstats/.venv/bin/python -m worker.prices clean
ExecStart=/usr/bin/find /opt/steamstats/cache/og -type f -atime +7 -delete
MemoryMax=200M
```

```ini
# /etc/systemd/system/steamstats-worker.timer
[Timer]
OnCalendar=Sun 04:00
Persistent=true
[Install]
WantedBy=timers.target
```

**Обязательно** дописать Kwork Radar, чтобы OOM killer выбирал жертвой сайт, а не бота:

```bash
systemctl edit kwork-radar
# [Service]
# OOMScoreAdjust=-500
```

## nginx

```nginx
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/m;

server {
    listen 80;
    server_name ЧТО_ТО.ru;

    root /opt/steamstats/static;
    index index.html;

    location /og/ {
        alias /opt/steamstats/cache/og/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
    location /api/ {
        limit_req zone=api burst=5 nodelay;
        proxy_pass http://127.0.0.1:8100;
    }
    location / { try_files $uri $uri/ /index.html; }
}
```

Сертификат через acme.sh, не через certbot: тот притащит полсотни пакетов на диск,
которого и так 1.3 ГБ.

## API

`GET /api/lookup?q=<ссылка|id64|ник>` → JSON карточки. `&refresh=1` мимо кэша.

Ошибки приходят с кодом: `not_found`, `private`, `no_games`, `upstream`.
`private` - самый частый, под него нужен отдельный экран с инструкцией
(Профиль → Настройки приватности → «Игровые подробности» → Открытый),
иначе треть трафика уйдёт молча.

`GET /api/health` → сколько цен в базе и сколько осталось в очереди.

## Страницы

| Маршрут | Что |
|---|---|
| `/` | форма, экран загрузки и экран ошибки — один шаблон, переключается на JS |
| `/u/{steamid64}` | отчёт по постоянному адресу, с `og:image` |
| `/og/{steamid64}.jpg` | карточка 1200×630, рисуется Pillow при первом расчёте |
| `/api/lookup?q=` | JSON |
| `/api/health` | размер кэша цен, очереди и результатов |

## Дизайн

Дизайн-система Industry: тёмная тема, квадратные углы, хайрлайн-рамки
с регистрационными метками по углам (`.blueprint` + четыре `<i class="corner">`).

**Barlow заменён на Fira.** У Barlow и Barlow Condensed нет кириллицы —
только latin, latin-ext и vietnamese, поэтому в макете заголовки молча падали
на системный шрифт, а Pillow нарисовал бы пустые квадраты. Fira Sans Condensed
и Fira Sans лежат в `assets/fonts/` (нужны для рендера карточки) и подключаются
с Google Fonts на страницах.

## Чего в Steam API нет и не будет

- **Наигранных часов по годам.** Есть только `playtime_forever`, `playtime_2weeks`
  и `rtime_last_played`. Блок «часы по годам» из макета заменён на распределение
  библиотеки по году последнего запуска.
- **Цен покупки.** Ключи, бандлы, скидки — ничего этого в API нет. Везде пишем
  «по текущим ценам магазина».
- **Дат покупки.** В кладбище остаётся только название игры.

## Покрытие цен

`money.reliable` = покрытие ≥ 50%, где покрытие считается по времени,
но **только среди игр, у которых цена в принципе бывает**. F2P (`no_price`)
из знаменателя исключены: иначе у любого игрока в CS2 или Dota покрытие
было бы 10%, и денежный блок скрывался бы именно у тех, кто стал бы им делиться.

## Про место на диске

Кэш картинок держать в JPEG q85 (~100 КБ), не в PNG. Чистка по `atime +7` уже
в таймере. При 1.3 ГБ свободного венв с Pillow займёт ~180 МБ, база цен ~20 МБ.
