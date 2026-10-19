"""API 身份认证：Bearer key → 用户与角色。"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from .config import ApiKeyConfig


@dataclass(frozen=True, slots=True)
class User:
    user: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class KeyStore:
    def __init__(self, keys: tuple[ApiKeyConfig, ...]) -> None:
        self._keys = keys

    def authenticate(self, authorization: str | None) -> User:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError(401, "UNAUTHENTICATED")
        token = authorization[len("Bearer ") :].strip()
        if not token:
            raise AuthError(401, "UNAUTHENTICATED")
        for item in self._keys:
            if hmac.compare_digest(token, item.key):
                return User(user=item.user, role=item.role)
        raise AuthError(401, "INVALID_CREDENTIALS")
