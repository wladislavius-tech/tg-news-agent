# -*- coding: utf-8 -*-
"""Одноразовий обмін OAuth-коду авторизації на access/refresh token.

Код беремо з callback-сторінки (tiktok-publisher.html -> Sign in with TikTok
-> tiktok-callback.html показує ?code=...). Client key/secret — ЛИШЕ зі
змінних середовища (ніколи не хардкодимо продакшн-секрет у файл).

Запуск:
    set TIKTOK_CLIENT_KEY=...
    set TIKTOK_CLIENT_SECRET=...
    python tiktok/oauth_exchange.py <code>

Виводить refresh_token — його треба зберегти як GitHub Actions secret
TIKTOK_REFRESH_TOKEN (тоді publish.py оновлюватиме access_token сам).
"""
from __future__ import annotations

import os
import sys

import requests

REDIRECT_URI = "https://wladislavius-tech.github.io/news-ukraine-mirror/tiktok-callback.html"


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) != 2:
        sys.exit("Використання: python tiktok/oauth_exchange.py <authorization_code>")
    code = sys.argv[1]

    client_key = os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not client_key or not client_secret:
        sys.exit("Задай TIKTOK_CLIENT_KEY і TIKTOK_CLIENT_SECRET у змінних середовища.")

    r = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Помилка обміну коду: {r.status_code} {r.text}")

    data = r.json()
    if "access_token" not in data:
        sys.exit(f"TikTok повернув 200, але без access_token. Повна відповідь:\n{data}")
    print("open_id:", data.get("open_id"))
    print("scope:", data.get("scope"))
    print("access_token (діє", data.get("expires_in"), "с):", data.get("access_token"))
    print()
    print("refresh_token (зберегти як GitHub secret TIKTOK_REFRESH_TOKEN):")
    print(data.get("refresh_token"))


if __name__ == "__main__":
    main()
