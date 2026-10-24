#!/bin/sh
# Запуск Redis с ACL: пользователь default выключен, у бота свой пользователь,
# которому доступны только ключи с его префиксом и нет опасных команд (FLUSHALL, CONFIG, KEYS…).
# В файл ACL пишется SHA-256 пароля, а не сам пароль; файл лежит в tmpfs.
set -eu

PREFIX="${REDIS_KEY_PREFIX:?REDIS_KEY_PREFIX не задан}"
HASH="$(tr -d '\r\n' < /run/secrets/redis_password | sha256sum | cut -d' ' -f1)"

umask 077
printf 'user default off\nuser bot on #%s ~%s:* &* +@all -@dangerous -@admin\n' "$HASH" "$PREFIX" > /run/redis/users.acl

exec redis-server /deploy/redis.conf --aclfile /run/redis/users.acl
