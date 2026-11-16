"""Bucket-scoped clients and credentials, provisioned once by each host."""

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from app.services.storage.objects import CheckpointObjects, ImageObjects
from app.services.storage.s3 import S3Objects
from app.services.storage.signing import S3Signer
from app.settings import Settings


@dataclass(frozen=True)
class Storage:
    checkpoints: CheckpointObjects
    checkpoint_signer: S3Signer
    images: ImageObjects
    image_signer: S3Signer

    def close(self) -> None:
        try:
            self.checkpoints.close()
        finally:
            self.images.close()


@lru_cache(maxsize=4)
def signer(
    settings: Settings, domain: Literal["documents", "images"]
) -> S3Signer | None:
    access = (
        settings.documents_access_key_id
        if domain == "documents"
        else settings.images_access_key_id
    )
    secret = (
        settings.documents_secret_access_key
        if domain == "documents"
        else settings.images_secret_access_key
    )
    if settings.storage_endpoint_url is None or access is None or secret is None:
        return None
    return S3Signer(
        str(settings.storage_endpoint_url),
        settings.documents_bucket if domain == "documents" else settings.images_bucket,
        settings.storage_region,
        access.get_secret_value(),
        secret.get_secret_value(),
        settings.storage_addressing_style,
    )


def build_storage(settings: Settings) -> Storage | None:
    if settings.storage_endpoint_url is None:
        return None
    import boto3
    from botocore.config import Config

    documents = signer(settings, "documents")
    images = signer(settings, "images")
    if documents is None or images is None:
        return None

    def objects(value: S3Signer) -> S3Objects:
        client = boto3.client(
            "s3",
            endpoint_url=value.endpoint,
            region_name=value.region,
            aws_access_key_id=value.access_key,
            aws_secret_access_key=value.secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": value.addressing_style},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                connect_timeout=3,
                read_timeout=20,
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        )
        return S3Objects(client, value.bucket)

    return Storage(objects(documents), documents, objects(images), images)
