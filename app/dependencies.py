"""Typed dependency providers shared by API routers."""

import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.settings import Settings


@lru_cache
def get_settings() -> Settings:
    """Load validated settings once per process."""
    return Settings(_secrets_dir=os.getenv("WRIGHT_ARCTIC_SECRETS_DIR"))


type SettingsDep = Annotated[Settings, Depends(get_settings)]
