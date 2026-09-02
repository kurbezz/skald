import secrets
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from skald.config import get_settings

security = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    settings = get_settings()
    if not settings.auth_username or not settings.auth_password:
        return

    valid = credentials is not None and (
        secrets.compare_digest(credentials.username, settings.auth_username)
        and secrets.compare_digest(credentials.password, settings.auth_password)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
