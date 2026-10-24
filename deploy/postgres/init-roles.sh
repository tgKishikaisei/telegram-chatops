#!/bin/sh
# Выполняется один раз при создании пустой БД (docker-entrypoint-initdb.d).
#
# Две роли вместо одной «всемогущей»:
#   bot_owner — владелец схемы app, под ним работают только миграции;
#   bot_app   — роль бота: читать и писать строки, но не менять структуру и не удалять таблицы.
# Если бот взломают через SQL-инъекцию или уязвимость, DROP TABLE ему недоступен.
# Пароли читаются из Docker secrets и в командную строку не попадают.
set -eu

OWNER_PASSWORD="$(cat /run/secrets/db_owner_password)"
APP_PASSWORD="$(cat /run/secrets/db_app_password)"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v db_name="$POSTGRES_DB" -v owner_password="$OWNER_PASSWORD" -v app_password="$APP_PASSWORD" <<'SQL'
CREATE ROLE bot_owner LOGIN PASSWORD :'owner_password' CONNECTION LIMIT 5;
CREATE ROLE bot_app LOGIN PASSWORD :'app_password' CONNECTION LIMIT 40;

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO bot_owner, bot_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA app AUTHORIZATION bot_owner;
GRANT USAGE ON SCHEMA app TO bot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE bot_owner IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE bot_owner IN SCHEMA app
    GRANT USAGE, SELECT ON SEQUENCES TO bot_app;

ALTER ROLE bot_owner SET search_path = app;
ALTER ROLE bot_app SET search_path = app;

-- Зависший запрос или забытая транзакция не держат блокировки вечно
ALTER ROLE bot_app SET statement_timeout = '15s';
ALTER ROLE bot_app SET lock_timeout = '5s';
ALTER ROLE bot_app SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE bot_owner SET lock_timeout = '10s';
SQL
