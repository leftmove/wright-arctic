"""Portable S3 SigV4 capabilities for exactly one key and HTTP operation."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from app.schemas import Permission


@lru_cache(maxsize=8)
def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = ("AWS4" + secret).encode()
    for value in (date, region, "s3", "aws4_request"):
        key = hmac.digest(key, value.encode(), "sha256")
    return key


@dataclass(frozen=True, repr=False)
class SignedRequest:
    method: Literal["GET", "PUT", "HEAD", "DELETE"]
    url: str
    headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True, repr=False)
class S3Signer:
    endpoint: str
    bucket: str
    region: str
    access_key: str
    secret_key: str
    addressing_style: Literal["path", "virtual"] = "path"

    def permission(
        self,
        key: str,
        seconds: int,
        *,
        method: Literal["GET", "PUT"],
        size: int | None = None,
        sha256: str | None = None,
        media_type: str | None = None,
        now: datetime | None = None,
    ) -> Permission:
        signed = self.request(
            key,
            seconds,
            method=method,
            size=size,
            sha256=sha256,
            media_type=media_type,
            now=now,
        )
        return Permission(
            method=method,
            url=signed.url,
            headers=signed.headers,
            expires_at=signed.expires_at,
        )

    def request(
        self,
        key: str,
        seconds: int,
        *,
        method: Literal["GET", "PUT", "HEAD", "DELETE"],
        size: int | None = None,
        sha256: str | None = None,
        media_type: str | None = None,
        now: datetime | None = None,
    ) -> SignedRequest:
        now = (now or datetime.now(UTC)).replace(microsecond=0)
        if not 1 <= seconds <= 900:
            raise ValueError("Capabilities must expire within fifteen minutes")
        if method == "PUT" and (size is None or sha256 is None or media_type is None):
            raise ValueError("Uploads require an exact size, SHA-256, and media type")
        endpoint = urlsplit(self.endpoint)
        host = endpoint.netloc
        root = endpoint.path.rstrip("/")
        if self.addressing_style == "virtual":
            host = self.bucket + "." + host
        else:
            root += "/" + self.bucket
        path = quote(root + "/" + key, safe="/~")
        headers = {"host": host}
        payload_hash = "UNSIGNED-PAYLOAD"
        if method == "PUT":
            assert size is not None and sha256 is not None and media_type is not None
            # R2 canonicalizes presigned PUTs with UNSIGNED-PAYLOAD even when
            # validating a signed SHA-256 body header. AWS includes that header's
            # digest in the canonical payload. Keep both endpoint contracts.
            if not (endpoint.hostname or "").endswith(".r2.cloudflarestorage.com"):
                payload_hash = sha256
            headers.update(
                {
                    "content-length": str(size),
                    "content-type": media_type,
                    "x-amz-content-sha256": sha256,
                    "x-amz-meta-sha256": sha256,
                }
            )
        signed_headers = ";".join(sorted(headers))
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        scope = f"{date}/{self.region}/s3/aws4_request"
        query = urlencode(
            sorted(
                {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": f"{self.access_key}/{scope}",
                    "X-Amz-Date": timestamp,
                    "X-Amz-Expires": str(seconds),
                    "X-Amz-SignedHeaders": signed_headers,
                }.items()
            ),
            quote_via=quote,
            safe="~",
        )
        canonical_headers = "".join(
            f"{name}:{headers[name]}\n" for name in sorted(headers)
        )
        canonical = f"{method}\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        to_sign = f"AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        signature = hmac.digest(
            _signing_key(self.secret_key, date, self.region), to_sign.encode(), "sha256"
        ).hex()
        return SignedRequest(
            method=method,
            url=urlunsplit(
                (
                    endpoint.scheme,
                    host,
                    path,
                    query + "&X-Amz-Signature=" + signature,
                    "",
                )
            ),
            headers={name: value for name, value in headers.items() if name != "host"},
            expires_at=now + timedelta(seconds=seconds),
        )
