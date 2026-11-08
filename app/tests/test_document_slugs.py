"""Compatibility of the pinned library/configuration used by sharing commands."""

import hashlib
import json
import unittest
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from app.services.documents import encode_sharing_number


class _Vector(BaseModel):
    number: str
    value: str


class DocumentSlugsTests(unittest.TestCase):
    def test_official_library_vectors_and_longer_than_minimum_ids(self) -> None:
        vectors = TypeAdapter(list[_Vector]).validate_json(
            Path(__file__).with_name("fixtures").joinpath("sqids.json").read_text()
        )
        for vector in vectors:
            with self.subTest(number=vector.number):
                self.assertEqual(
                    encode_sharing_number(int(vector.number)), vector.value
                )
        self.assertGreater(len(encode_sharing_number(2**63 - 1)), 6)

    def test_positive_bigint_bounds(self) -> None:
        for number in (True, False, -1, 0, 2**63):
            with self.subTest(number=number), self.assertRaises(ValueError):
                encode_sharing_number(number)

    def test_configuration_is_explicit_and_blocklist_does_not_drift(self) -> None:
        config_path = (
            Path(__file__).parents[1] / "services" / "documents" / "sqids.json"
        )

        # Check the resource independently from the encoder's parsed config.
        class Configuration(BaseModel):
            library_version: str
            alphabet: str
            min_length: int
            blocklist: list[str]

        config = Configuration.model_validate_json(config_path.read_text())
        self.assertEqual(config.library_version, "0.5.2")
        self.assertEqual(
            config.alphabet,
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        )
        self.assertEqual(config.min_length, 6)
        self.assertEqual(len(config.blocklist), 560)
        digest = hashlib.sha256(
            json.dumps(sorted(config.blocklist), separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(
            digest, "757018526eaa1af57b9ccc9439fe672345b3ae85cf8162b74af45c0d6f5a9b1a"
        )
