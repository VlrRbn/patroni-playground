# Patroni Playground

Цель: разобраться, как PostgreSQL реплицирует данные и как Patroni управляет
кластером при отказах.

## Что мы строим

```text
                         HAProxy
                     localhost:55432 ──────► primary: чтение и запись
                     localhost:55433 ──────► replicas: только чтение
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
               pg1          pg2          pg3
           PostgreSQL   PostgreSQL   PostgreSQL
            + Patroni    + Patroni    + Patroni
                └────────────┼────────────┘
                             ▼
                    etcd1, etcd2, etcd3
                 выбор лидера и конфигурация
```

- **PostgreSQL** хранит данные. Primary принимает запись; replicas получают
  и воспроизводят WAL — журнал изменений. Это streaming replication.

- **Patroni** работает рядом с каждым PostgreSQL, управляет его запуском,
  остановкой и сменой роли. `pg1` — имя узла, его роль может меняться.

- **etcd** — распределённое хранилище координации, DCS. Здесь хранится аренда
  лидерства и общая конфигурация Patroni. Для кворума нужны два из трёх etcd.
  Строки таблиц хранятся в PostgreSQL.

- **HAProxy** проверяет REST API Patroni: `/primary` для записи, `/replica`
  для чтения. Новое соединение направляется на подходящий узел.

Версии: PostgreSQL 17.11, Patroni 4.1.5, etcd 3.6.5, HAProxy 3.2.
Образ PostgreSQL закреплён digest; etcd и HAProxy используют теги,
транзитивные Python-зависимости.

## Запуск

Docker Engine, Docker Compose **2.24.4+**, Python 3 и Make. Все семь контейнеров
работают на одной машине: моделируем отказы процессов, а отказ хоста остановит весь стенд.

Открой папку проекта:

```bash
cd /home/leprecha/patroni-playground
make up
make status
```

Ожидай одного `Leader` и две `Replica` в состоянии `streaming`.
Первым лидером может стать любой узел. Сборка в первый раз занимает несколько минут.
Если запуск завершился ошибкой: `docker compose logs --tail=80`.

`make setup` автоматически создаёт `.env` с двумя случайными паролями и правами
`0600`. Повторный запуск сохраняет файл. Он исключён из Git. Сохраняй `.env`,
пока сохраняешь данные кластера: изменение файла само по себе не меняет пароли в БД.

Это локальный стенд: внешние порты привязаны к `127.0.0.1`, TLS
и watchdog не настроены, административный REST API без пароля.
Репликация не заменяет резервные копии: `DELETE` тоже попадёт на реплики.

## 1. Находим primary и создаём данные

```bash
make sql
```

Откроется `psql`, подключённый через HAProxy к primary. Выполни:

```sql
SELECT inet_server_addr(), pg_is_in_recovery();

CREATE TABLE notes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO notes (body) VALUES ('Привет, Patroni!');
SELECT * FROM notes;
```

У primary `pg_is_in_recovery()` возвращает `false`. Запомни IP сервера:
позже он поможет заметить переключение. Выход из `psql`: `\q`.

## 2. Читаем с реплики

```bash
make sql PORT=5001
```

`5001` — порт HAProxy внутри Docker. Выполни:

```sql
SELECT inet_server_addr(), pg_is_in_recovery();
SELECT * FROM notes;
INSERT INTO notes (body) VALUES ('Запись с реплики?');
```

Ожидай `true`, строку из первого упражнения и ошибку `read-only transaction`
при вставке. Данные могут появиться с небольшой задержкой: репликация асинхронная.
HAProxy выбирает узел при установлении соединения. Один сеанс `psql` остаётся
на одной реплике; переподключение может привести на другую.

## 3. Смотрим репликацию

Подключись к primary через `make sql`:

```sql
SELECT application_name, state, sync_state,
       sent_lsn, write_lsn, flush_lsn, replay_lsn
FROM pg_stat_replication;
```

Ожидай две строки: `state = streaming`, `sync_state = async`.
LSN — позиция в WAL. Эти столбцы показывают этапы доставки:
отправка, запись, сброс на диск и воспроизведение на реплике.

На реплике через `make sql PORT=5001`:

```sql
SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();
```

Добавь несколько строк на primary и повтори запросы. После доставки изменений
позиции приёма и воспроизведения на реплике обычно совпадают.

## 4. Плановое переключение — switchover

```bash
make status
make ctl ARGS="switchover"
```

В диалоге выбери текущего лидера, другую здоровую реплику и немедленное
переключение, затем подтверди действие. `make status` покажет нового `Leader`.
Бывший лидер должен вернуться как `Replica` в состоянии `streaming`.

Открытое соединение `psql` при смене роли может оборваться. Подключись снова:

```bash
make sql
```

```sql
SELECT inet_server_addr(), pg_is_in_recovery();
INSERT INTO notes (body) VALUES ('После switchover');
SELECT * FROM notes;
```

Адрес подключения остался прежним; сервер за HAProxy изменился.
Приложению нужно уметь переподключаться. Если связь оборвалась во время `COMMIT`,
результат транзакции может быть неизвестен клиенту: перед повтором записи
проверь результат или используй уникальный ключ операции.

## 5. Аварийное переключение — failover

Выйди из `psql`. По `make status` найди текущего лидера. Ниже пример
для лидера **pg1**; замени имя, если лидер другой:

```bash
docker compose kill -s SIGKILL pg1
make status NODE=pg2
```

Это завершение контейнера без корректной остановки PostgreSQL.
Patroni на оставшихся узлах должен выбрать нового лидера. Повторяй просмотр
статуса: переключение обычно занимает десятки секунд. `ttl = 20`, `loop_wait = 5`
и `retry_timeout = 5` задают интервалы координации; фактическое время зависит
от обнаружения отказа, выборов и запуска PostgreSQL.

```bash
make sql NODE=pg2
```

```sql
INSERT INTO notes (body) VALUES ('После аварии');
SELECT * FROM notes;
```

`NODE` выбирает контейнер, в котором запускается клиент `psql` или `patronictl`.
Сам `psql` по-прежнему идёт к primary через HAProxy. Если остановлен `pg2`,
используй `NODE=pg1` или `NODE=pg3`.

**Почему возможна потеря данных:** в async-режиме primary подтверждает транзакцию,
не дожидаясь её доставки на реплику. Последние подтверждённые записи могут
отсутствовать на новом primary. `maximum_lag_on_failover = 1048576` ограничивает
отставание кандидата, но не гарантирует RPO в 1 МиБ: позиция лидера измеряется
периодически.

## 6. Возвращаем бывшего лидера

Для остановленного в прошлом `pg1`:

```bash
docker compose start pg1
make status NODE=pg2
```

Ожидай, что `pg1` станет репликой и получит строку «После аварии».
При расхождении истории WAL Patroni может использовать `pg_rewind`;
для этого включены `use_pg_rewind` и checksums при `initdb`.
Посмотри решение Patroni в `docker compose logs --tail=100 pg1`.
Если для возврата недостаёт WAL, ручная переинициализация — следующий шаг.

## 7. Зачем etcd нужен кворум

Начинай со всех работающих узлов:

```bash
docker compose stop etcd3
make status
```

Два etcd из трёх сохраняют кворум, кластер продолжает работать. Теперь:

```bash
docker compose stop etcd2
```

Кворум потерян. В этом стенде `failsafe_mode = false`: после неудачных попыток
продлить аренду лидер должен отказаться от роли primary. Подожди около 30 секунд:

```bash
docker compose logs --tail=40 pg1 pg2 pg3
curl -i http://127.0.0.1:8001/primary
curl -i http://127.0.0.1:8002/primary
curl -i http://127.0.0.1:8003/primary
```

Ожидай `503` у всех трёх. Запись через HAProxy станет недоступна.
`patronictl list` тоже может завершиться ошибкой: для него нужен DCS.
Работающие реплики PostgreSQL не заменяют потерянный кворум etcd.

Восстанови кворум и дождись возврата лидера:

```bash
docker compose start etcd2 etcd3
make status
make sql
```

## Команды и подключение с хоста

| Назначение | Адрес |
| --- | --- |
| SQL к primary | `127.0.0.1:55432` |
| SQL к репликам | `127.0.0.1:55433` |
| REST API pg1 / pg2 / pg3 | `127.0.0.1:8001` / `8002` / `8003` |

В DBeaver: пользователь `postgres`, база `postgres`, пароль — значение
`PATRONI_SUPERUSER_PASSWORD` из локального `.env`.
`make sql` подставляет пароль внутри контейнера.

```bash
make ctl ARGS="show-config"       # общая конфигурация из DCS
make ctl ARGS="edit-config"       # изменить её интерактивно
make logs                         # Ctrl+C завершает просмотр логов
make down                         # удалить контейнеры и сеть, сохранить volumes
make up                           # поднять кластер с сохранёнными данными
make acceptance                   # проверка в отдельном временном кластере
```

`bootstrap.dcs` из `patroni.yaml` применяется только при создании кластера.
Для существующего кластера общую конфигурацию меняй через `edit-config`.
Локальные настройки требуют отдельного reload/restart в зависимости от параметра.

Начать заново, **удалив все учебные данные и состояние etcd**:

```bash
docker compose down --volumes
make up
```

Автопроверка использует уникальное имя Compose-проекта, не публикует порты
и удаляет свои контейнеры и volumes в конце. Она проверяет две streaming-реплики,
маршрутизацию SQL, запрет записи на реплике, switchover, SIGKILL лидера,
возврат бывшего лидера и восстановление после потери кворума etcd.
Перед отказом тест ждёт доставки контрольной строки на все узлы.
Это проверка конкретного сценария, а не гарантия отсутствия потерь в async-режиме.

## Дальше

Следующий этап: `synchronous_mode` и `synchronous_mode_strict`, измерение времени
переключения и потерь, затем возврат реплики через `patronictl reinit`.

Официальные материалы:
[конфигурация Patroni](https://patroni.readthedocs.io/en/latest/yaml_configuration.html),
[patronictl](https://patroni.readthedocs.io/en/latest/patronictl.html),
[REST API](https://patroni.readthedocs.io/en/latest/rest_api.html),
[режимы репликации](https://patroni.readthedocs.io/en/latest/replication_modes.html).
