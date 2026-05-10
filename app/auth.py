from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import config

_basic = HTTPBasic()


def require_basic_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> str:
    user_ok = secrets.compare_digest(credentials.username, config.basic_auth_user)
    pass_ok = secrets.compare_digest(credentials.password, config.basic_auth_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Boat Pulse"'},
        )
    return credentials.username
