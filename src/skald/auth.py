import secrets
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.requests import HTTPConnection

from skald.config import get_settings

security = HTTPBasic(auto_error=False)


async def get_credentials(connection: HTTPConnection) -> Optional[HTTPBasicCredentials]:
    return await security(connection)


def require_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(get_credentials),
) -> None:
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
