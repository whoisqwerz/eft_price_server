# Tarkov Price Server

Python API-сервер, который загружает предметы и цены из экосистемы
[tarkov.dev](https://tarkov.dev/), локализует данные, сохраняет их в SQLite и
отдаёт стабильный REST-контракт.

На момент создания проекта старый GraphQL endpoint
`https://api.tarkov.dev/graphql` отвечает `GraphQL server unavailable`.
Поэтому сервер использует актуальный статический feed
`https://json.tarkov.dev`, публикуемый той же экосистемой. URL источника можно
заменить через `.env`.

## Что хранится

- предметы, категории, характеристики и ссылки на изображения;
- агрегированные цены барахолки за 24/48 часов;
- предложения покупки и продажи у торговцев;
- снимок цены только при её изменении (история не раздувается дублями);
- полная локализованная запись upstream в `raw_data`;
- журнал успешных и неуспешных синхронизаций.

Успешные JSON-ответы предметов, истории, торговцев и экспорта дополнительно
хранятся в Redis один час. Повторный запрос с тем же URL возвращается до входа
в FastAPI-роут и не обращается к SQLite. Заголовок `X-Cache` показывает `HIT`,
`MISS` или `BYPASS` (Redis недоступен).

По умолчанию используются режим `regular`, русский язык и синхронизация раз в
час. Данные лежат в `data/tarkov.db`.

## Быстрый запуск

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Предварительно запустите Redis на localhost:6379.
python -m app
```

После запуска:

- Swagger UI: <http://127.0.0.1:5302/docs>
- состояние: <http://127.0.0.1:5302/health>
- готовность данных: <http://127.0.0.1:5302/ready>

Первая загрузка запускается в фоне и обычно занимает несколько секунд.

## Docker

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Каталог `data` монтируется с хоста, поэтому база сохраняется при пересоздании
контейнера. Compose также запускает отдельный Redis с лимитом памяти 256 МБ и
политикой вытеснения `allkeys-lru`. API доступен на порту `5302`.

## Как работает кэш

Кэшируются только успешные `GET`-ответы:

- `/api/v1/items` со всеми комбинациями фильтров;
- `/api/v1/items/{id}` и история цен;
- `/api/v1/traders`;
- `/api/v1/export/items`.

TTL составляет 3600 секунд. После успешной ручной или фоновой синхронизации все
ключи API удаляются, поэтому устаревшие данные не остаются до конца TTL. Первый
уникальный запрос после очистки читает SQLite и наполняет Redis; последующие
запросы обслуживаются Redis. Если Redis временно недоступен, используется
SQLite без ошибки для клиента.

## REST API

### Поиск предметов

```http
GET /api/v1/items?q=болт&type=barter&sort_by=price&order=desc&limit=50
```

Поддерживаются параметры:

- `q` — поиск по ID, локализованному названию и английскому slug;
- `type` — точный тип предмета (`ammo`, `barter`, `gun` и т. п.);
- `min_price`, `max_price` — фильтр по текущей цене;
- `sort_by=name|price|updated`, `order=asc|desc`;
- `limit`, `offset` — пагинация.

### Один предмет и история

```http
GET /api/v1/items/{item_id}
GET /api/v1/items/{item_id}?include_raw=true
GET /api/v1/items/{item_id}/history?limit=500
```

### Компактный экспорт

```http
GET /api/v1/export/items
GET /api/v1/export/items?format=list
```

Для быстрого экспорта только ID, типов и цен используйте:

```http
GET /api/v1/export/prices
```

Ответ — обычный JSON-массив без метаданных и лишних полей:

```json
[
  {
    "id": "5447a9cd4bdc2dbd208b4567",
    "types": ["gun", "wearable"],
    "prices": {
      "base": 18397,
      "avg24": 79935,
      "best_trader_price": 8278
    }
  }
]
```

`best_trader_price` — максимальная цена продажи предмета торговцу. Этот endpoint
делает SQL-запрос только к пяти необходимым колонкам и не читает тяжёлые
`raw_data`, описания или характеристики предметов.

По умолчанию `items` — объект с ID в качестве ключа. Пример:

```json
{
  "meta": {
    "game_mode": "regular",
    "language": "ru",
    "count": 5312
  },
  "items": {
    "5447a9cd4bdc2dbd208b4567": {
      "id": "5447a9cd4bdc2dbd208b4567",
      "name": "Colt M4A1 5.56x45",
      "types": ["gun", "wearable"],
      "prices": {
        "base": 18397,
        "flea": {
          "last_low": 29875,
          "average_24h": 79935
        },
        "best_trader_sell": {
          "trader_id": "54cb50c76803fa8b248b4571",
          "trader_name": "Прапор",
          "price_rub": 7358
        }
      }
    }
  }
}
```

OpenAPI-схема является точным источником полного формата ответа.

### Синхронизация

```http
GET  /api/v1/sync
POST /api/v1/sync
X-API-Key: value-from-ADMIN_API_KEY
```

Если `ADMIN_API_KEY` пуст, ручная синхронизация не требует ключа. Для сервера,
доступного из сети, ключ следует задать обязательно.

## Настройки `.env`

| Переменная | Значение по умолчанию | Назначение |
|---|---:|---|
| `HOST` | `0.0.0.0` | Адрес HTTP-сервера |
| `PORT` | `5302` | Порт HTTP-сервера |
| `DATABASE_URL` | `sqlite:///./data/tarkov.db` | Файл базы |
| `TARKOV_SOURCE_BASE_URL` | `https://json.tarkov.dev` | Источник данных |
| `TARKOV_GAME_MODE` | `regular` | `regular`, `pve`, `pvp-season` |
| `TARKOV_LANGUAGE` | `ru` | Язык перевода |
| `SYNC_INTERVAL_SECONDS` | `3600` | Интервал синхронизации |
| `SYNC_ON_STARTUP` | `true` | Запуск загрузки при старте |
| `REQUEST_TIMEOUT_SECONDS` | `90` | Таймаут источника |
| `REDIS_CACHE_ENABLED` | `true` | Включить Redis-кэш ответов |
| `REDIS_URL` | `redis://localhost:6379/0` | Подключение к Redis |
| `REDIS_CACHE_TTL_SECONDS` | `3600` | TTL ответа в Redis |
| `REDIS_CACHE_MAX_RESPONSE_BYTES` | `25000000` | Максимальный размер ответа |
| `ADMIN_API_KEY` | пусто | Ключ ручной синхронизации |

## Проверка

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```
