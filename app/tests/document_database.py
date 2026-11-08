"""Existing Postgres WASM fixture adapter for actual repository/HTTP tests.

The production service uses psycopg. This adapter only translates protocol values
and placeholders; all authorization, native processing and SQL remain production code.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, cast
from uuid import UUID

from app.services.documents.database import Row, Session
from app.services.documents.errors import Conflict, Unavailable
from app.services.documents.values import JsonParameter


def _encode(value: object) -> object:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, JsonParameter):
        return value.obj
    return value


def _decode(value: dict[str, object]) -> object:
    if "$bytes" in value:
        return base64.b64decode(str(value["$bytes"]))
    return value


class FixtureDatabase:
    def __init__(self, *, initialize: bool = True) -> None:
        module = Path(
            os.environ.get(
                "WRIGHT_ARCTIC_PGLITE_MODULE",
                str(
                    Path(__file__).with_name("node_modules")
                    / "@electric-sql/pglite/dist/index.js"
                ),
            )
        )
        bun = shutil.which("bun")
        if bun is None or not module.exists():
            raise RuntimeError(
                "Run make test to install the pinned SQL fixture; Bun is required"
            )
        self.process = subprocess.Popen(
            [
                bun,
                str(Path(__file__).with_name("document_pg.mjs")),
                *([] if initialize else ["--empty"]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.input = cast(IO[str], self.process.stdin)
        self.output = cast(IO[str], self.process.stdout)
        self.lock = threading.RLock()
        self.closed = False
        self.fail_commit = False
        if self.output.readline().strip() != '{"ready":true}':
            diagnostics = self.process.communicate(timeout=5)[1]
            raise RuntimeError("Existing fixture failed: " + diagnostics[-1500:])

    def open(self) -> None:
        pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.input.close()
            self.process.wait(timeout=10)
            self.output.close()
            if self.process.stderr is not None:
                self.process.stderr.close()

    def execute(self, query: str, parameters: Sequence[object] = ()) -> list[Row]:
        counter = 0

        def placeholder(match: re.Match[str]) -> str:
            nonlocal counter
            if match[1] == "%":
                return "%"
            if match[1] != "s":
                raise RuntimeError("Unsupported or unescaped psycopg placeholder")
            counter += 1
            return f"${counter}"

        if parameters:
            query = re.sub(r"%(.)", placeholder, query)
            if counter != len(parameters):
                raise RuntimeError("SQL parameter count mismatch")
        self.input.write(
            json.dumps({"sql": query, "parameters": list(parameters)}, default=_encode)
            + "\n"
        )
        self.input.flush()
        result = cast(
            dict[str, object], json.loads(self.output.readline(), object_hook=_decode)
        )
        if "error" in result:
            error = cast(dict[str, object], result["error"])
            if error.get("code") in {"23505", "23503", "23514", "23001"}:
                raise Conflict()
            raise RuntimeError(str(error))
        return cast(list[Row], result["rows"])

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.admin_transaction():
            self.execute("set local role arctic_documents")
            yield self

    @contextmanager
    def admin_transaction(self) -> Iterator[Session]:
        with self.lock:
            self.execute("begin")
            try:
                yield self
                if self.fail_commit:
                    self.fail_commit = False
                    raise Unavailable()
                self.execute("commit")
            except BaseException:
                self.execute("rollback")
                raise
