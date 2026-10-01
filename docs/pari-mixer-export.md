# Что нужно predicatorr от pari-mixer

**Коротко.** Нужна одна новая ручка в pari-mixer: она отдаёт тот же JSON, что `/api/backup`,
но только по токену на чтение и прямо на сервере (`127.0.0.1:8000`). predicatorr будет
спрашивать её раз в 2 минуты вместо ветки `data-backup` на GitHub. Тогда сыгранная игра
попадёт в прогнозы через 10–15 минут, а не через 3–6 часов.

## Почему сейчас поздно

- predicatorr (`mixer-predicts.duckdns.org`, тот же сервер, порт 8100) строит модель на
  `backup.json` из ветки `data-backup`.
- Ветку пишет `.github/workflows/keep-alive.yml` по расписанию `*/10`, но GitHub запускает
  расписания с большими задержками. Последние коммиты: 30.09 в 13:20, 18:29, 22:29 и 01.10
  в 01:25, 07:25 UTC — то есть раз в 3–6 часов.
- Кубок #31 начался 01.10 в 09:00 UTC. К 12:35 закончились две игры и шла третья, а в
  бэкапе нет ни одной игры кубка. На сайте прогнозов из-за этого написано «кубок ещё не
  начался», и форма команд не учитывается.
- В базе pari-mixer эти игры уже есть: сбор идёт каждые `COLLECT_INTERVAL_SECONDS=600`,
  состав и героев даёт Steam, результат — mixer-cup, и всё это публикуется в живую базу
  ещё до OpenDota (`publish_core()` в `collect.py`). Не хватает только способа это отдать.

Сколько теряет модель из-за опоздания результатов (проверка на 7 прошлых кубках, 2097 игр):

| Результаты кубка приходят | Угадано | Logloss |
|---|---|---|
| сразу | 67.2% | 0.603 |
| через 3 часа | 66.5% | 0.610 |
| через сутки | 66.0% | 0.618 |
| формы нет совсем | 63.9% | 0.634 |

## Что сделать (обязательно)

### 1. Ручка `GET /api/export/backup`

- **Содержимое** — тот же JSON, что собирает `api_backup()` в `app.py`: те же ключи, тот же
  порядок полей в кортежах. `access_bindings` и `player_notes` можно не отдавать —
  predicatorr их не читает.
- **Доступ** — по отдельному токену только на чтение. Новая переменная `EXPORT_TOKEN` в
  `/etc/pari-mixer/env`, заголовок `X-Export-Token`, сравнение через `hmac.compare_digest`.
  Без токена или с неверным — 403. Этот токен не должен открывать `/api/collect`,
  `/api/backup` и `/api/archive`, а `OPS_TOKEN` predicatorr не нужен.
- Путь `/api/export/...` не попадает в `_is_ops_path()`, поэтому токен проверяет сама
  ручка (или нужно расширить `_gate()`). Проверка «запрос пришёл с 127.0.0.1» **не
  защищает**: nginx проксирует публичный домен на `127.0.0.1:8000`, и любой запрос из
  интернета выглядит локальным. Для надёжности путь можно ещё закрыть в публичном
  server-блоке nginx: `location /api/export/ { return 404; }`. predicatorr ходит на
  `127.0.0.1:8000` напрямую, мимо nginx.
- **ETag**: `resp.set_etag(<sha256 тела, или mtime+size файла БД>)` и
  `return resp.make_conditional(request)`. Тогда на повторный запрос с `If-None-Match`
  уйдёт `304` без тела, и частые опросы почти ничего не стоят.
- gzip — по желанию: файл около 2.3 МБ, но это localhost.

### 2. Что должно быть в выгрузке для идущего кубка

Сыгранная игра текущего кубка появляется в `/api/export/backup` не позже чем через
~10 минут после того, как mixer-cup отметил её `COMPLETE`:

- `matches`: `[match_id, league_id, start_time, duration, radiant_team_id, dire_team_id,
  radiant_win, mixer_tournament_id]` — с `radiant_win` (из mixer-cup) и
  `mixer_tournament_id`, равным глобальному номеру кубка (31 для текущего PARI Mixer Cup).
- `match_players[match_id]`: все 10 игроков, `[account_id, hero_id, team_id, is_radiant,
  k, d, a, gpm, xpm, net_worth]`. Состав и героев Steam даёт сразу. Убийства, GPM и золото
  от OpenDota могут прийти позже — до тех пор `null`. predicatorr такие матчи принимает и
  подхватит цифры со следующей выгрузкой.
- `match_drafts` — когда появятся, не срочно.
- `teams`, `players` (текущие составы, `roster_confirmed`, `mmr`), `queued_players`,
  `substitution_events` — как сейчас.

Судя по коду, всё это уже лежит в живой базе после каждого прохода сбора, поэтому
достаточно отдать это через новую ручку.

## Желательно (сильно поможет)

### 3. Чаще собирать во время игр

Пока у активного кубка есть игра `ACTIVE` или только что `COMPLETE`, снизить
`COLLECT_INTERVAL_SECONDS` с 600 до 180–300. Либо запускать сбор сразу, как mixer-cup
перевёл игру в `COMPLETE`. Нужна только дешёвая часть (Steam + mixer-cup), квоту OpenDota
это не тратит.

### 4. Живой драфт: `GET /api/export/live`

Тот же токен. Для лиг 19924, 20165 и 19965 — текущие игры из Steam
`IDOTA2Match_570/GetLiveLeagueGames/v1` (ключ Steam остаётся в pari-mixer). Опрос раз в
15–20 секунд, пока в mixer-cup есть игра `ACTIVE`, и ответ из памяти без обращения к Steam
на каждый запрос.

```json
{
  "fetched_at": 1790859800,
  "games": [{
    "league_id": 19924, "match_id": 9024301243, "lobby_id": 27110000000000000,
    "radiant_team_id": 10186635, "dire_team_id": 10186541,
    "game_time": 412, "radiant_score": 5, "dire_score": 3,
    "players": [{"account_id": 161864559, "hero_id": 29, "is_radiant": true}],
    "picks_bans": [{"order": 0, "hero_id": 90, "is_pick": false, "is_radiant": true}]
  }]
}
```

`game_time` — секунды игры (отрицательные или 0 до горна), `players` и `picks_bans` —
сколько уже известно. Начать стоит с проверки, видит ли `GetLiveLeagueGames` эти лобби
вообще (`GetMatchDetails` для лиги отвечает 500, с live может быть иначе), и написать
результат.

Зачем это нужно: драфт в Captains Mode идёт около 10 минут, после него PARI быстро двигает
линию. С этой ручкой герои подставятся в прогноз сами, без ручного ввода 10 героев.

## Чего не надо

- Не передавать predicatorr `OPS_TOKEN`, `AUTH_SECRET`, `STEAM_API_KEY`, `ACCESS_KEYS`.
- Не открывать новые ручки наружу без токена.
- Не менять формат `/api/backup` и ветку `data-backup`: бэкап на GitHub остаётся запасным
  источником для predicatorr.

## Что сделает predicatorr

- В `/opt/predicatorr/.env`:
  ```
  PREDICATOR_BACKUP_URL=http://127.0.0.1:8000/api/export/backup
  PREDICATOR_BACKUP_TOKEN=<тот же EXPORT_TOKEN>
  ```
- Раз в 2 минуты — запрос с `If-None-Match`. На `304` ничего не делаем, на новые данные —
  импорт и переобучение модели (пара секунд).
- Если локальная ручка не отвечает, бэкап берётся из GitHub, как сейчас.
- Матчи без статистики (`null`) учитываются в результатах и форме, но не портят признак
  «доля золота».

## Как проверить на сервере

```bash
TOKEN=$(sudo grep '^EXPORT_TOKEN=' /etc/pari-mixer/env | cut -d= -f2-)
URL=http://127.0.0.1:8000/api/export/backup

# 1. снаружи закрыто: ждём 403 или 404
curl -s -o /dev/null -w '%{http_code}\n' "https://<домен pari-mixer>/api/export/backup"

# 2. локально с токеном: сколько сыгранных игр кубка 31 в выгрузке
curl -s -H "X-Export-Token: $TOKEN" "$URL" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(sum(1 for m in d['matches'] if m[7] == 31 and m[6] is not None))"

# 3. ETag работает: ждём 304
ETAG=$(curl -s -D - -o /dev/null -H "X-Export-Token: $TOKEN" "$URL" | grep -i '^etag:' | cut -d' ' -f2- | tr -d '\r')
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Export-Token: $TOKEN" -H "If-None-Match: $ETAG" "$URL"
```

Число из пункта 2 должно совпадать с числом законченных игр кубка на mixer-cup и расти
не позже чем через ~10 минут после конца каждой игры.

Подключить predicatorr:

```bash
printf 'PREDICATOR_BACKUP_URL=%s\nPREDICATOR_BACKUP_TOKEN=%s\n' "$URL" "$TOKEN" | sudo tee -a /opt/predicatorr/.env >/dev/null
sudo systemctl restart predicatorr
```

После этого на сайте прогнозов на странице «Модель» → «Источники данных» будет видно,
откуда пришла история и когда она обновлялась.
