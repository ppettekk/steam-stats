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
# скопировать сюда app/ и worker/
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx pillow

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

## Что осталось

- Фронт (дизайн отдельно)
- Рендер карточки в JPEG 1200×630 через Pillow + `og:image` на `/u/{steamid64}`
- Страница `/u/{steamid64}` с постоянным адресом, чтобы ссылку кидали в чат

## Агрегированная статистика

`snapshots` — по одной строке на профиль, Steam ID хранится хэшем с солью
(`STEAMSTATS_SALT` в `.env`), сырой id не пишется. Повторный расчёт того же
аккаунта счётчики не двигает: иначе один человек, обновивший страницу,
перекосил бы выборку.

`game_stats` — сколько людей владеет игрой, сколько запускали, сколько часов
суммарно. Отсюда берётся «у скольких лежит и никто не играл».

`GET /api/aggregate` отдаёт медианы, средние, топ-15 по владению и топ-15
самых пылящихся. Пока профилей меньше 50, отдаёт только счётчик и
`ready: false` — на маленькой выборке медианы скачут и публиковать их нечестно.

Медиана считается через `LIMIT 1 OFFSET count/2`: перцентилей в SQLite нет.
Смотреть на медиану, а не на среднее: один человек с 20 000 часов
вытягивает среднее вверх так, что цифра перестаёт что-либо значить.

## Про место на диске

Кэш картинок держать в JPEG q85 (~100 КБ), не в PNG. Чистка по `atime +7` уже
в таймере. При 1.3 ГБ свободного венв с Pillow займёт ~180 МБ, база цен ~20 МБ.
