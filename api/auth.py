"""Password login with a signed session cookie.

Fails closed: without DASHBOARD_PASSWORD nobody can log in, because this API can
move money. For plain-http local testing set TT_INSECURE_COOKIE=1.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections import deque

from fastapi import HTTPException, Request, Response

COOKIE_NAME = 'tt_session'
SESSION_SECONDS = 14 * 24 * 3600
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 300
_failures: deque[float] = deque()


def _password() -> str:
    return os.environ.get('DASHBOARD_PASSWORD', '')


def _signature(expires: str) -> str:
    key = hashlib.sha256(b'tt-session-v1:' + _password().encode()).digest()
    return hmac.new(key, expires.encode(), hashlib.sha256).hexdigest()


def _valid(token: str | None) -> bool:
    if not token or not _password() or '.' not in token:
        return False
    expires, signature = token.split('.', 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _signature(expires))


def is_authenticated(request: Request) -> bool:
    return _valid(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail='Not logged in')


def login(password: str, response: Response) -> None:
    if not _password():
        raise HTTPException(status_code=503, detail='DASHBOARD_PASSWORD is not configured')

    now = time.time()
    while _failures and _failures[0] < now - _FAILURE_WINDOW_SECONDS:
        _failures.popleft()
    if len(_failures) >= _MAX_FAILURES:
        raise HTTPException(status_code=429, detail='Too many attempts. Try again in a few minutes.')

    if not hmac.compare_digest(password.encode(), _password().encode()):
        _failures.append(now)
        raise HTTPException(status_code=401, detail='Wrong password')

    expires = str(int(now) + SESSION_SECONDS)
    response.set_cookie(
        COOKIE_NAME, f'{expires}.{_signature(expires)}', max_age=SESSION_SECONDS,
        httponly=True, samesite='strict', path='/',
        secure=os.environ.get('TT_INSECURE_COOKIE') != '1',
    )


def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path='/')
