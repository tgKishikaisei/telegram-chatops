"""Безопасная загрузка страниц по ссылкам от пользователей (защита от SSRF).

Зачем: бот скачивает URL, который прислал человек. Без проверок можно прислать
http://127.0.0.1:6379 или http://169.254.169.254/ и заставить сервер ходить
во внутреннюю сеть — к базе, Redis, метаданным облака.

Что делаем:
- разрешены только http/https на портах 80/443, без логина и пароля в URL;
- IP-адрес проверяется в момент подключения (свой резолвер) — это закрывает и DNS-rebinding;
- редиректы проходим вручную и проверяем каждый новый адрес;
- ограничены размер ответа, время ожидания и типы содержимого.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
HTML_TYPES = ("text/html", "application/xhtml+xml")
_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)


class UnsafeUrlError(ValueError):
    """Ссылка ведёт туда, куда боту ходить нельзя (или вообще не похожа на ссылку)."""


class FetchError(Exception):
    """Страницу не удалось скачать: таймаут, ошибка сервера, слишком большой ответ и т.п.

    status — HTTP-код ответа, если сервер ответил (None — до ответа дело не дошло);
    retry_after — сколько секунд сервер просит подождать (заголовок Retry-After у 429/503).
    """

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    """Retry-After в секундах (дату вместо числа не разбираем — вызывающий подождёт по своему правилу)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 86400 else None


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 — это тот же 127.0.0.1
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> str:
    """Проверяет ссылку без обращения к сети. Возвращает её же или бросает UnsafeUrlError."""
    url = url.strip()
    if not url or len(url) > 2048:
        raise UnsafeUrlError("Пустая или слишком длинная ссылка")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("Поддерживаются только ссылки http:// и https://")
    if parts.username or parts.password:
        raise UnsafeUrlError("Ссылки с логином и паролем не поддерживаются")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("В ссылке нет адреса сайта")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("Некорректный порт") from exc
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("Разрешены только стандартные порты 80 и 443")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES) or "." not in host:
            raise UnsafeUrlError("Внутренние адреса недоступны") from None
    else:
        # IP-адрес в ссылке aiohttp не передаёт в резолвер — проверяем здесь
        if not is_public_ip(host):
            raise UnsafeUrlError("Внутренние адреса недоступны")
    return url


class PublicOnlyResolver(AbstractResolver):
    """DNS-резолвер, который отказывается подключаться к внутренним адресам."""

    def __init__(self) -> None:
        self._inner = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if not results or any(not is_public_ip(item["host"]) for item in results):
            raise UnsafeUrlError("Сайт указывает на внутренний адрес")
        return results

    async def close(self) -> None:
        await self._inner.close()


@dataclass(frozen=True)
class FetchedPage:
    url: str
    content_type: str
    body: bytes
    charset: str | None = None

    def text(self) -> str:
        for encoding in (self.charset, "utf-8"):
            if not encoding:
                continue
            try:
                return self.body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", errors="replace")


class SafeHttpClient:
    """HTTP-клиент для пользовательских ссылок. Один экземпляр на весь бот."""

    def __init__(
        self,
        *,
        max_bytes: int = 3_000_000,
        timeout_seconds: float = 20,
        max_redirects: int = 5,
        user_agent: str = "Mozilla/5.0 (compatible; TelegramBot/1.0)",
    ) -> None:
        self._max_bytes = max_bytes
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_redirects = max_redirects
        self._headers = {"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"}
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=10)
            # trust_env=False: не подхватывать прокси из окружения
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=self._timeout, headers=self._headers, trust_env=False
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def fetch(self, url: str, accept: tuple[str, ...] = HTML_TYPES,
                    headers: dict[str, str] | None = None) -> FetchedPage:
        """accept=() — принять любой тип содержимого; headers — дополнительные заголовки запроса."""
        current = validate_url(url)
        session = self._get_session()
        try:
            for _ in range(self._max_redirects + 1):
                async with session.get(current, allow_redirects=False, headers=headers) as response:
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchError("Сайт вернул редирект без адреса", response.status)
                        current = validate_url(urljoin(current, location))
                        continue
                    if response.status != 200:
                        raise FetchError(f"Сайт ответил кодом {response.status}", response.status,
                                         _retry_after(response.headers.get("Retry-After")))

                    raw_type = response.headers.get("Content-Type", "")
                    content_type = raw_type.split(";")[0].strip().lower()
                    if accept and content_type not in accept:
                        raise FetchError(f"Неподходящий тип содержимого: {content_type or 'неизвестен'}")
                    body = await self._read_limited(response)
                    match = _CHARSET_RE.search(raw_type)
                    return FetchedPage(current, content_type, body, match.group(1) if match else None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FetchError("Не удалось открыть страницу") from exc
        raise FetchError("Слишком много перенаправлений")

    async def _read_limited(self, response: aiohttp.ClientResponse) -> bytes:
        declared = response.content_length
        if declared is not None and declared > self._max_bytes:
            raise FetchError("Страница слишком большая")
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > self._max_bytes:
                raise FetchError("Страница слишком большая")
            chunks.append(chunk)
        return b"".join(chunks)
