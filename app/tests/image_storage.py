"""In-memory S3 behavior for image lifecycle tests, including conditional writes."""

from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import Mock

from botocore.exceptions import ClientError


def memory_storage() -> Mock:
    objects: dict[str, tuple[bytes, dict[str, str], str]] = {}
    client = Mock()
    modified = datetime.now(UTC)

    def missing() -> ClientError:
        return ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

    def put(
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        Metadata: dict[str, str],
        ContentType: str,
        CacheControl: str,
        ContentDisposition: str,
        IfNoneMatch: str | None = None,
    ) -> None:
        if IfNoneMatch == "*" and Key in objects:
            raise ClientError(
                {
                    "ResponseMetadata": {
                        "HTTPStatusCode": 412,
                        "HTTPHeaders": {},
                        "HostId": "test",
                        "RequestId": "test",
                        "RetryAttempts": 0,
                    }
                },
                "PutObject",
            )
        objects[Key] = (Body, Metadata, ContentType)

    def head(*, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in objects:
            raise missing()
        body, metadata, media_type = objects[Key]
        return {
            "ContentLength": len(body),
            "Metadata": metadata,
            "ContentType": media_type,
            "LastModified": modified,
        }

    def get(*, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in objects:
            raise missing()
        return {"Body": BytesIO(objects[Key][0])}

    def delete(*, Bucket: str, Key: str) -> None:
        objects.pop(Key, None)

    def listing(*, Bucket: str, Prefix: str, MaxKeys: int) -> dict[str, object]:
        return {
            "Contents": [
                {"Key": key, "Size": len(value[0]), "LastModified": modified}
                for key, value in sorted(objects.items())
                if key.startswith(Prefix)
            ]
        }

    client.put_object.side_effect = put
    client.head_object.side_effect = head
    client.get_object.side_effect = get
    client.delete_object.side_effect = delete
    client.list_objects_v2.side_effect = listing
    client.generate_presigned_url.return_value = "https://images.example/signed"
    return client
