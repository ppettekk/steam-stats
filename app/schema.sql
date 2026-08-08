PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

-- Цены. state отделён от initial намеренно:
--   priced    - цена есть
--   no_price  - продаётся, но price_overview отсутствует (F2P, предзаказ, DLC-пустышка)
--   not_sold  - success:false, в этом регионе не продаётся
CREATE TABLE IF NOT EXISTS prices (
  appid      INTEGER NOT NULL,
  cc         TEXT    NOT NULL,
  state      TEXT    NOT NULL,
  initial    INTEGER,
  currency   TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (appid, cc)
);

-- Очередь на опрос. priority: 10 - встречено у живого юзера, 100 - хвост из SteamSpy.
CREATE TABLE IF NOT EXISTS queue (
  appid    INTEGER NOT NULL,
  cc       TEXT    NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  PRIMARY KEY (appid, cc)
);
CREATE INDEX IF NOT EXISTS idx_queue_prio ON queue(priority, appid);

-- Курсы ЦБ, снимок раз в сутки.
CREATE TABLE IF NOT EXISTS fx (
  currency TEXT PRIMARY KEY,
  rate_rub REAL NOT NULL,
  date     TEXT NOT NULL
);

-- Готовые карточки. payload - посчитанный JSON, отдаётся как есть.
CREATE TABLE IF NOT EXISTS results (
  steamid64  TEXT    NOT NULL,
  cc         TEXT    NOT NULL,
  payload    TEXT    NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (steamid64, cc)
);
CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at);

-- Счётчик для главной. Одна строка, обновляется при каждом новом расчёте.
CREATE TABLE IF NOT EXISTS meta (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  profiles INTEGER NOT NULL DEFAULT 0,
  hours    INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta(id, profiles, hours) VALUES (1, 0, 0);
