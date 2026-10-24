# Секреты

Здесь лежат файлы с секретами для `docker compose` — по одному значению в файле, без
перевода строки в конце. Папка в `.gitignore`: **в git эти файлы не попадают никогда**.

Docker монтирует их в контейнер как `/run/secrets/<имя>` (только для чтения).
Это надёжнее переменных окружения: те видны в `docker inspect` и в списке процессов.

Создание (Linux/macOS, в папке бота):

```bash
umask 077
mkdir -p secrets
printf '%s' 'ТОКЕН_ОТ_BOTFATHER' > secrets/bot_token
for name in db_superuser_password db_owner_password db_app_password redis_password; do
  openssl rand -base64 32 | tr -d '\n' > "secrets/$name"
done
```

Список файлов, нужных этому боту, — в разделе `secrets:` файла `docker-compose.yml`
и в README бота. Секреты, которые выдаёт внешний сервис (токены, ключи API), вводите сами —
**не вставляйте их в чат с ИИ**.
