# Первый запуск Matchbot

Для проверки подойдёт Windows. Для постоянной работы используй отдельный Ubuntu VPS: бот останется запущен, когда твой компьютер выключен. Бот получает обновления из Telegram сам, поэтому домен, веб-сервер и открытый входящий порт для приложения не нужны.

## Проверка на Windows без Telegram

В PowerShell из папки проекта выполни команды ниже. Они проверят конфигурацию, создание базы и сборку обработчиков без настоящего токена. Тестовая база лежит в `data/smoke.sqlite3`; рабочая база при этом не используется.

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
$savedBotToken = $env:BOT_TOKEN
$savedDatabasePath = $env:DATABASE_PATH
try {
    $env:BOT_TOKEN = '123456:offline_smoke'
    $env:DATABASE_PATH = 'data/smoke.sqlite3'
    .\.venv\Scripts\python.exe bot.py --check
} finally {
    $env:BOT_TOKEN = $savedBotToken
    $env:DATABASE_PATH = $savedDatabasePath
}
```

Ожидаемый результат: `Configuration, database and handlers OK. No Telegram request made.` Для живой проверки добавь токен в `.env`, запусти `start.bat` и пройди сценарий с двумя аккаунтами ниже. Останови локальный бот перед запуском того же токена на VPS.

## Подключение к Ubuntu VPS

Ниже — команды для чистого Ubuntu 24.04 LTS и пользователя с `sudo`. Из PowerShell на своём компьютере подключись по SSH. Подставь имя пользователя и IP из панели VPS, а при первом подключении сверь отпечаток ключа сервера с панелью.

```powershell
ssh USER@SERVER_IP
```

Дальше команды выполняются **на сервере**. Установи Docker Engine и плагин Compose из официального репозитория Docker:

```sh
sudo apt update
sudo apt install ca-certificates curl git nano
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSLo /etc/apt/keyrings/docker.asc https://download.docker.com/linux/ubuntu/gpg
sudo chmod 0644 /etc/apt/keyrings/docker.asc
printf '%s\n' \
  'Types: deb' \
  'URIs: https://download.docker.com/linux/ubuntu' \
  'Suites: noble' \
  'Components: stable' \
  "Architectures: $(dpkg --print-architecture)" \
  'Signed-By: /etc/apt/keyrings/docker.asc' \
  | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

Если Docker и Compose уже установлены, проверь `sudo docker compose version` и пропусти настройку репозитория. Для другой версии Ubuntu или образа с конфликтующими пакетами смотри [установку Docker Engine на Ubuntu](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository) и [установку плагина Compose](https://docs.docker.com/compose/install/linux/#install-using-the-repository).

## Настройка и запуск

1. В Telegram создай бота у **@BotFather** через `/newbot` и сохрани выданный токен.
2. Склонируй проект и открой `.env` на сервере:

   ```sh
   git clone https://github.com/IliyasNU/matchbot.git
   cd matchbot
   test -e .env || install -m 0600 .env.example .env
   chmod 0600 .env
   nano .env
   ```

   Вставь токен после `BOT_TOKEN=`. Оставь `DATABASE_PATH=data/bot.sqlite3`, чтобы база попала в том. На первом запуске `ADMIN_IDS` можно оставить пустым. Compose передаёт настройки из `.env` при запуске; сам файл исключён из Git и [контекста сборки Docker](https://docs.docker.com/build/concepts/context/#dockerignore-files).
3. Собери образ, проверь конфигурацию и запусти один контейнер:

   ```sh
   sudo docker compose build --pull bot
   sudo docker compose run --rm --no-deps bot python bot.py --check
   sudo docker compose up -d bot
   sudo docker compose ps bot
   sudo docker compose logs --tail 50 bot
   ```

   `--check` проверяет формат токена и доступ к SQLite. Подлинность токена и соединение с Telegram проверяются при запуске контейнера. В журнале после подключения появится `Bot initialized. Polling for private-chat updates.` Команда `up -d` оставляет бот работать после закрытия SSH.
4. Отправь боту `/start`, затем `/id`. Впиши номер из ответа в `ADMIN_IDS` в `.env`; несколько ID указываются через запятую. После изменения `.env` выполни `sudo docker compose up -d --force-recreate bot`, чтобы контейнер получил новое окружение. Проверь `/admin`.

Для этого токена запускай только один экземпляр: один контейнер на VPS или один локальный процесс. Сообщение `Conflict` при получении обновлений обычно означает, что такой же токен уже используется другим процессом.

## Проверка в Telegram

1. Открой бота из двух разных Telegram-аккаунтов. На каждом отправь `/start` и опубликуй анкету. Выбери совместимые возрастные, половые и городские предпочтения.
2. На первом аккаунте открой `/browse` и поставь лайк второму. На втором открой `/likes` и ответь лайком.
3. Убедись, что оба аккаунта получили уведомление о взаимной симпатии и видят контакт в `/matches`.
4. Выполни `sudo docker compose restart bot` и снова проверь `/matches`.

CI проверяет сборку, создание базы, сохранение тестовой записи и резервной копии после пересоздания контейнера без подключения к Telegram. Этот сценарий с реальными аккаунтами нужно пройти отдельно после настройки токена.

## Данные и резервные копии

Compose хранит рабочую SQLite-базу в томе `bot-data`, а резервные копии — в томе `bot-backups`. Оба сохраняются при пересоздании контейнера. Запускай команды из той же папки проекта: Compose привязывает тома к имени проекта. Обычный `docker compose down` оставляет тома; `docker compose down --volumes` удаляет их. [Документация Docker о томах](https://docs.docker.com/engine/storage/volumes/) и [команде down](https://docs.docker.com/reference/cli/docker/compose/down/).

Чтобы снять резервную копию работающего бота и выгрузить её на сервер:

```sh
backup="matchbot-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
sudo docker compose exec -T bot python scripts/backup.py --output "/app/backups/$backup"
install -d -m 0700 backups
(umask 077; sudo docker compose run --rm --no-deps -T bot cat "/app/backups/$backup" > "backups/$backup")
test -s "backups/$backup" && printf 'Backup: backups/%s\n' "$backup"
```

Скрипт использует SQLite backup API, чтобы получить согласованную копию, даже если бот пишет в базу. Из PowerShell на своём компьютере скачай файл с VPS, подставив выведенное имя:

```powershell
scp USER@SERVER_IP:matchbot/backups/ИМЯ_КОПИИ.sqlite3 .
```

Держи хотя бы одну свежую копию вне VPS: тома и копия на сервере могут пропасть вместе. В файле есть данные анкет, поэтому храни его в закрытом месте. Если проект склонирован не в домашний каталог пользователя, в команде `scp` укажи полный путь на сервере.

## Обновление и откат

Перед обновлением запиши текущий коммит (`git rev-parse HEAD`) и сделай свежую копию. Чтобы сохранить все данные до момента остановки, сначала выполни `sudo docker compose stop bot`, затем используй команды из предыдущего раздела, заменив `exec -T bot` на `run --rm --no-deps -T bot` в строке со скриптом резервирования. Сохрани имя копии рядом с SHA коммита.

На ветке `main` выполни:

```sh
git pull --ff-only origin main
sudo docker compose build --pull bot
sudo docker compose run --rm --no-deps bot python bot.py --check
sudo docker compose up -d bot
sudo docker compose logs --tail 50 bot
```

Если сборка, проверка или запуск завершились ошибкой, остановись на этом шаге. Для отката останови бот. Если новая версия изменила схему базы, восстанови копию **до** переключения кода. Восстановление заменит текущее состояние базы, поэтому сначала сохрани отдельную копию текущей базы, если она читается. Подставь имя сохранённой копии перед запуском:

```sh
sudo docker compose stop bot
backup=ИМЯ_КОПИИ.sqlite3
sudo docker compose run --rm --no-deps -T bot python - "$backup" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

backup = (Path('/app/backups') / Path(sys.argv[1]).name).resolve(strict=True)
with closing(sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True)) as source:
    if source.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise SystemExit('Backup integrity check failed')
    with closing(sqlite3.connect('/app/data/bot.sqlite3')) as database:
        source.backup(database)
PY
```

Затем переключись на сохранённый рабочий SHA и проверь запуск:

```sh
git switch --detach ПРЕДЫДУЩИЙ_SHA
sudo docker compose build bot
sudo docker compose run --rm --no-deps bot python bot.py --check
sudo docker compose up -d bot
```

Проверь `/start` и `/matches`. Для следующего обновления сначала вернись на `main` через `git switch main`. [Справка по разовым командам Compose](https://docs.docker.com/reference/cli/docker/compose/run/) и [пересозданию контейнера](https://docs.docker.com/reference/cli/docker/compose/up/).
