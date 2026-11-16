"""Compare portable signing against AWS's independent SigV4 implementation."""

import hashlib
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from botocore.auth import S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.services.storage.signing import S3Signer


class R2QueryAuth(S3SigV4QueryAuth):
    """AWS's signer with the R2 payload marker verified by the live probe."""

    def canonical_request(self, request: AWSRequest) -> str:
        return (
            str(super().canonical_request(request)).rsplit("\n", 1)[0]
            + "\nUNSIGNED-PAYLOAD"
        )


class PermissionTests(unittest.TestCase):
    def test_cached_signing_keys_follow_date_region_and_credentials(self) -> None:
        for region, secret in (
            ("auto", "first"),
            ("other", "first"),
            ("auto", "second"),
        ):
            signer = S3Signer(
                "https://example.r2.cloudflarestorage.com",
                "wright-documents",
                region,
                "access",
                secret,
            )
            for day in (14, 15, 14):
                now = datetime(2026, 11, day, 12, tzinfo=UTC)
                permission = signer.permission(
                    "documents/a/b/c", 60, method="GET", now=now
                )
                request = AWSRequest(
                    method="GET",
                    url="https://example.r2.cloudflarestorage.com/wright-documents/documents/a/b/c",
                )
                with patch("botocore.auth.get_current_datetime", return_value=now):
                    S3SigV4QueryAuth(
                        Credentials("access", secret), "s3", region, expires=60
                    ).add_auth(request)
                self.assertEqual(
                    parse_qs(urlsplit(permission.url).query),
                    parse_qs(urlsplit(request.url or "").query),
                )

    def test_put_matches_aws_sigv4_and_binds_operation_key_length_type_and_hash(
        self,
    ) -> None:
        now = datetime(2026, 11, 15, 12, tzinfo=UTC)
        digest = hashlib.sha256(b"opaque-checkpoint").hexdigest()
        signer = S3Signer(
            "https://example.r2.cloudflarestorage.com",
            "wright-documents",
            "auto",
            "access",
            "secret",
        )
        permission = signer.permission(
            "documents/space/document/revision",
            300,
            method="PUT",
            size=17,
            sha256=digest,
            media_type="application/octet-stream",
            now=now,
        )
        request = AWSRequest(
            method="PUT",
            url="https://example.r2.cloudflarestorage.com/wright-documents/documents/space/document/revision",
            headers=permission.headers,
        )
        with patch("botocore.auth.get_current_datetime", return_value=now):
            R2QueryAuth(
                Credentials("access", "secret"), "s3", "auto", expires=300
            ).add_auth(request)
        self.assertEqual(
            parse_qs(urlsplit(permission.url).query),
            parse_qs(urlsplit(request.url or "").query),
        )
        query = parse_qs(urlsplit(permission.url).query)
        self.assertEqual(
            query["X-Amz-SignedHeaders"],
            ["content-length;content-type;host;x-amz-content-sha256;x-amz-meta-sha256"],
        )
        self.assertFalse(
            any(name.lower().startswith("if-") for name in permission.headers)
        )
        self.assertEqual(permission.expires_at.timestamp() - now.timestamp(), 300)
        other = signer.permission(
            "documents/space/document/other", 300, method="GET", now=now
        )
        self.assertNotEqual(
            query["X-Amz-Signature"],
            parse_qs(urlsplit(other.url).query)["X-Amz-Signature"],
        )

    def test_aws_endpoint_preserves_standard_payload_header_signing(self) -> None:
        now = datetime(2026, 11, 15, 12, tzinfo=UTC)
        signer = S3Signer(
            "https://s3.us-east-1.amazonaws.com",
            "bucket",
            "us-east-1",
            "access",
            "secret",
        )
        permission = signer.permission(
            "images/a/b",
            60,
            method="PUT",
            size=1,
            sha256="a" * 64,
            media_type="image/avif",
            now=now,
        )
        request = AWSRequest(
            method="PUT",
            url="https://s3.us-east-1.amazonaws.com/bucket/images/a/b",
            headers=permission.headers,
        )
        with patch("botocore.auth.get_current_datetime", return_value=now):
            S3SigV4QueryAuth(
                Credentials("access", "secret"), "s3", "us-east-1", expires=60
            ).add_auth(request)
        self.assertEqual(
            parse_qs(urlsplit(permission.url).query),
            parse_qs(urlsplit(request.url or "").query),
        )

    def test_get_matches_aws_s3_presigning_and_has_no_conditional_headers(self) -> None:
        now = datetime(2026, 11, 15, 12, tzinfo=UTC)
        signer = S3Signer(
            "https://example.r2.cloudflarestorage.com",
            "wright-documents",
            "auto",
            "access",
            "secret",
        )
        permission = signer.permission("documents/a/b/c", 60, method="GET", now=now)
        request = AWSRequest(
            method="GET",
            url="https://example.r2.cloudflarestorage.com/wright-documents/documents/a/b/c",
        )
        with patch("botocore.auth.get_current_datetime", return_value=now):
            S3SigV4QueryAuth(
                Credentials("access", "secret"), "s3", "auto", expires=60
            ).add_auth(request)
        self.assertEqual(
            parse_qs(urlsplit(permission.url).query),
            parse_qs(urlsplit(request.url or "").query),
        )
        self.assertEqual(permission.headers, {})
