"""S3 Signature Version 4 GET capabilities without constructing an SDK client."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True, repr=False)
class S3UrlSigner:
    endpoint: str
    bucket: str
    region: str
    access_key: str
    secret_key: str

    def sign(self, key: str, seconds: int, *, now: datetime | None = None) -> str:
        timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        date = timestamp[:8]
        scope = f"{date}/{self.region}/s3/aws4_request"
        endpoint = urlsplit(self.endpoint)
        path = quote(endpoint.path.rstrip("/") + f"/{self.bucket}/{key}", safe="/~")
        parameters = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": timestamp,
            "X-Amz-Expires": str(seconds),
            "X-Amz-SignedHeaders": "host",
        }
        query = urlencode(sorted(parameters.items()), quote_via=quote, safe="~")
        canonical = (
            f"GET\n{path}\n{query}\nhost:{endpoint.netloc}\n\nhost\nUNSIGNED-PAYLOAD"
        )
        to_sign = f"AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        signing_key = ("AWS4" + self.secret_key).encode()
        for value in (date, self.region, "s3", "aws4_request"):
            signing_key = hmac.digest(signing_key, value.encode(), "sha256")
        signature = hmac.new(signing_key, to_sign.encode(), "sha256").hexdigest()
        return urlunsplit(
            (
                endpoint.scheme,
                endpoint.netloc,
                path,
                query + "&X-Amz-Signature=" + signature,
                "",
            )
        )
