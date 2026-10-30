"""Validated application configuration."""

import re
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_NAME = re.compile(r"^[a-z0-9-]{1,32}$")


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
    auth_issuer: str | None = None
    auth_jwks_url: AnyHttpUrl | None = None
    auth_audience: str = Field(default="convex", min_length=1, max_length=256)
    auth_algorithm: Literal["RS256", "ES256"] = "RS256"
    auth_max_token_lifetime_seconds: int = Field(default=3600, ge=60, le=3600)
    storage_endpoint_url: AnyHttpUrl | None = None
    storage_region: str | None = None
    storage_bucket: str | None = None
    storage_access_key_id: SecretStr | None = Field(
        default=None, exclude=True, repr=False
    )
    storage_secret_access_key: SecretStr | None = Field(
        default=None, exclude=True, repr=False
    )
    storage_public_base_url: AnyHttpUrl | None = None
    storage_addressing_style: Literal["path", "virtual"] = "path"
    storage_presign_seconds: int = Field(default=900, ge=60, le=604800)

    @field_validator("auth_issuer")
    @classmethod
    def _auth_issuer_is_secure(cls, value: str | None) -> str | None:
        if value is not None:
            if value != value.strip():
                raise ValueError("must not include surrounding whitespace")
            _secure_auth_url(TypeAdapter(AnyHttpUrl).validate_python(value))
        # JWT issuer comparison is exact: preserve paths and trailing slashes.
        return value

    @field_validator("auth_jwks_url")
    @classmethod
    def _auth_jwks_is_secure(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None:
            _secure_auth_url(value)
        return value

    @model_validator(mode="after")
    def _auth_settings_are_complete(self) -> Self:
        if (self.auth_issuer is None) != (self.auth_jwks_url is None):
            raise ValueError(
                "WRIGHT_ARCTIC_AUTH_ISSUER and WRIGHT_ARCTIC_AUTH_JWKS_URL "
                "must be set together"
            )
        return self

    @field_validator("storage_endpoint_url", "storage_public_base_url")
    @classmethod
    def _storage_url_has_no_secrets(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is None:
            return None
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("must not include credentials, a query, or a fragment")
        return value

    @field_validator("storage_bucket")
    @classmethod
    def _storage_bucket_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _valid_bucket_name(value):
            raise ValueError("must be a valid S3 bucket name")
        return value

    @field_validator("storage_region")
    @classmethod
    def _storage_region_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _REGION_NAME.fullmatch(value) is None:
            raise ValueError("must be an S3 region such as auto or us-east-1")
        return value

    @model_validator(mode="after")
    def _storage_settings_are_complete(self) -> Self:
        configured = {
            "WRIGHT_ARCTIC_STORAGE_ENDPOINT_URL": self.storage_endpoint_url is not None,
            "WRIGHT_ARCTIC_STORAGE_REGION": self.storage_region is not None,
            "WRIGHT_ARCTIC_STORAGE_BUCKET": self.storage_bucket is not None,
            "WRIGHT_ARCTIC_STORAGE_ACCESS_KEY_ID": _secret_configured(
                self.storage_access_key_id
            ),
            "WRIGHT_ARCTIC_STORAGE_SECRET_ACCESS_KEY": _secret_configured(
                self.storage_secret_access_key
            ),
        }
        missing = [name for name, present in configured.items() if not present]
        if self.storage_public_base_url is not None and missing:
            raise ValueError(
                "WRIGHT_ARCTIC_STORAGE_PUBLIC_BASE_URL requires " + ", ".join(missing)
            )
        if missing and len(missing) != len(configured):
            raise ValueError(
                "Image storage settings must be set together; missing "
                + ", ".join(missing)
            )
        return self


def _secret_configured(value: SecretStr | None) -> bool:
    return value is not None and value.get_secret_value() != ""


def _valid_bucket_name(value: str) -> bool:
    if _BUCKET_NAME.fullmatch(value) is None or ".." in value:
        return False
    labels = value.split(".")
    if all(label.isdigit() for label in labels):
        return False
    return all(
        label and not label.startswith("-") and not label.endswith("-")
        for label in labels
    )


def _secure_auth_url(value: AnyHttpUrl) -> None:
    if value.scheme != "https":
        raise ValueError("authentication URLs must use HTTPS")
    if value.username or value.password or value.query or value.fragment:
        raise ValueError("must not include credentials, a query, or a fragment")
