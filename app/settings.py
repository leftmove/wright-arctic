"""Validated application configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read settings from the environment, local dotenv, and optional secret files."""

    model_config = SettingsConfigDict(
        env_prefix="WRIGHT_ARCTIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="wright-arctic", min_length=1)
    debug: bool = False
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
