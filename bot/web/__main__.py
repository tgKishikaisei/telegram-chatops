"""Запуск приёмника вебхуков: python -m bot.web (порт 8000 только во внутренней сети, снаружи — Caddy)."""

import uvicorn

from bot.web.app import create_web_app

if __name__ == "__main__":
    uvicorn.run(create_web_app(), host="0.0.0.0", port=8000, proxy_headers=True,  # nosec B104 — внутри контейнера
                forwarded_allow_ips="*", server_header=False, log_config=None)
