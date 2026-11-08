"""Exercise HTTP, verified identity, real native bytes and private PostgreSQL SQL."""

import json
import unittest
from dataclasses import replace
from typing import ClassVar
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import JsonValue, TypeAdapter

import engine
from app.accounts import Account
from app.authentication.service import AuthenticationService
from app.main import create_app
from app.schemas import DocumentReceipt, DocumentSummary
from app.services.documents import native
from app.services.documents.errors import Conflict
from app.services.documents.models import Native, SchemaName
from app.services.documents.service import DocumentService
from app.tests.document_database import FixtureDatabase
from app.tests.test_authentication import configuration, public_jwk, token


def model(text: str) -> dict[str, JsonValue]:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def document(text: str, schema: str = "wright-rich-text") -> engine.NativeDocument:
    return engine.native_create(
        schema, json.dumps({"version": 1, "schema": schema, "doc": model(text)})
    )


def projected(text: str, schema: str = "wright-rich-text") -> JsonValue:
    return TypeAdapter(dict[str, JsonValue]).validate_json(
        document(text, schema).projection
    )["doc"]


class DocumentsTests(unittest.IsolatedAsyncioTestCase):
    database: ClassVar[FixtureDatabase]
    key: ClassVar[rsa.RSAPrivateKey]

    @classmethod
    def setUpClass(cls) -> None:
        cls.database = FixtureDatabase()
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.close()

    async def asyncSetUp(self) -> None:
        self.owner, self.editor, self.viewer, self.stranger = [
            uuid4() for _ in range(4)
        ]
        self.names: dict[UUID, str] = {}
        for user in (self.owner, self.editor, self.viewer, self.stranger):
            name = "user_" + user.hex[:20]
            self.names[user] = name
            self.database.execute("insert into auth.users(id) values(%s)", (user,))
            self.database.execute(
                "insert into public.profiles(id,username) values(%s,%s)", (user, name)
            )

        def fetch(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/v1/rpc/account_access":
                return httpx.Response(200, json=True)
            return httpx.Response(200, json={"keys": [public_jwk(self.key)]})

        self.authentication = AuthenticationService(
            configuration(), httpx.AsyncClient(transport=httpx.MockTransport(fetch))
        )
        with (
            patch("app.main.get_settings", return_value=configuration()),
            patch(
                "app.main.build_authentication_service",
                return_value=self.authentication,
            ),
            patch("app.main.build_image_service", return_value=None),
        ):
            app = create_app()
        self.service = DocumentService(self.database)
        app.state.resources = replace(
            app.state.resources, document_service=self.service
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://arctic.example"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.authentication.close()

    def headers(self, actor: UUID | None = None) -> dict[str, str]:
        return {
            "Authorization": "Bearer "
            + token(self.key, claims={"sub": str(actor or self.owner)})
        }

    def account(self, actor: UUID) -> Account:
        return Account(id=str(actor), issuer="https://issuer.example/auth/v1")

    async def create(self, text: str = "Initial content") -> UUID:
        response = await self.client.post(
            "/documents",
            headers=self.headers(),
            json={"operation_id": str(uuid4()), "content": model(text)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        receipt = DocumentReceipt.model_validate_json(response.content)
        self.assertEqual(receipt.head_seq, 0)
        return receipt.document_id

    async def metadata(self, target: UUID) -> DocumentSummary:
        response = await self.client.get(f"/documents/{target}", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return DocumentSummary.model_validate_json(response.content)

    async def state(self, target: UUID, through: int | None = None) -> bytes:
        response = await self.client.get(
            f"/documents/{target}/state",
            headers=self.headers(),
            params={} if through is None else {"through_seq": through},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/octet-stream")
        return response.content

    async def append(
        self,
        target: UUID,
        updates: list[bytes],
        operations: list[UUID] | None = None,
        actor: UUID | None = None,
    ) -> httpx.Response:
        return await self.client.post(
            f"/documents/{target}/changes",
            headers=self.headers(actor),
            data={
                "metadata": json.dumps(
                    {
                        "operations": [
                            str(op) for op in operations or [uuid4() for _ in updates]
                        ]
                    }
                )
            },
            files=[
                ("updates", ("update.bin", update, "application/octet-stream"))
                for update in updates
            ],
        )

    async def grant(self, target: UUID, actor: UUID, role: str | None) -> None:
        response = await self.client.put(
            f"/documents/{target}/access/{actor}",
            headers=self.headers(),
            json={"operation_id": str(uuid4()), "role": role},
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_create_retry_and_private_library(self) -> None:
        command = {
            "operation_id": str(uuid4()),
            "content": model("  Héllo   world\nsecond line"),
        }
        first = await self.client.post(
            "/documents", headers=self.headers(), json=command
        )
        self.assertEqual(first.status_code, 201, first.text)
        receipt = DocumentReceipt.model_validate_json(first.content)
        self.assertEqual(receipt.document_id.version, 7)
        retry = await self.client.post(
            "/documents", headers=self.headers(), json=command
        )
        self.assertEqual(retry.json(), first.json())
        conflict = await self.client.post(
            "/documents", headers=self.headers(), json={**command, "title": "Changed"}
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        metadata = await self.metadata(receipt.document_id)
        self.assertEqual(metadata.title, "Héllo world")
        self.assertEqual(metadata.path, f"/d/{receipt.document_id}")
        self.assertEqual(metadata.role, "owner")
        rows = self.database.execute(
            "select (select count(*) from documents.slugs where document_id=%s) as slugs,(select count(*) from documents.snapshots where document_id=%s) as snapshots",
            (receipt.document_id, receipt.document_id),
        )
        self.assertEqual(rows, [{"slugs": 0, "snapshots": 0}])
        other = await self.client.get(
            f"/documents/{receipt.document_id}", headers=self.headers(self.stranger)
        )
        self.assertEqual(other.status_code, 404)
        listed = await self.client.get(
            "/documents", headers=self.headers(self.stranger)
        )
        self.assertEqual(listed.json(), [])

    async def test_native_upload_preserves_uuid_and_exact_initial_bytes(self) -> None:
        target, operation = uuid4(), uuid4()
        initial = document("écrire 🦉", "basic").state
        command = {
            "operation_id": str(operation),
            "title": "  Native title  ",
            "title_is_user_set": True,
            "schema_name": "basic",
        }

        async def upload(payload: bytes) -> httpx.Response:
            return await self.client.put(
                f"/documents/{target}",
                headers=self.headers(),
                data={"metadata": json.dumps(command)},
                files={"state": ("state.bin", payload, "application/octet-stream")},
            )

        first = await upload(initial)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((await upload(initial)).json(), first.json())
        self.assertEqual(
            (await upload(document("changed", "basic").state)).status_code, 409
        )
        self.assertEqual((await self.metadata(target)).title, "  Native title  ")
        raw = await self.client.get(
            f"/documents/{target}/changes/0", headers=self.headers()
        )
        self.assertEqual(raw.content, initial)
        reopened = engine.native_merge("basic", [await self.state(target)])
        self.assertEqual(
            json.loads(reopened.projection)["doc"], projected("écrire 🦉", "basic")
        )

    async def test_append_preserves_event_boundaries_and_retry_receipts(self) -> None:
        target = await self.create()
        initial = await self.state(target)
        middle = engine.native_restore(
            "wright-rich-text", initial, document("Second").state
        )
        latest = engine.native_restore(
            "wright-rich-text", middle.state, document("Third").state
        )
        operations = [uuid4(), uuid4()]
        response = await self.append(target, [middle.update, latest.update], operations)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([row["head_seq"] for row in response.json()], [1, 2])
        retry = await self.append(target, [middle.update, latest.update], operations)
        self.assertEqual(retry.json(), response.json())
        self.assertEqual((await self.metadata(target)).head_seq, 2)
        self.assertEqual((await self.metadata(target)).title, "Third")
        for seq, payload in enumerate([middle.update, latest.update], start=1):
            raw = await self.client.get(
                f"/documents/{target}/changes/{seq}", headers=self.headers()
            )
            self.assertEqual(raw.content, payload)
        self.assertEqual(
            (await self.append(target, [latest.update], [operations[0]])).status_code,
            409,
        )
        self.assertEqual(
            json.loads(
                engine.native_merge(
                    "wright-rich-text", [await self.state(target)]
                ).projection
            )["doc"],
            projected("Third"),
        )

    async def test_missing_causal_dependencies_and_invalid_batch_roll_back(
        self,
    ) -> None:
        target = await self.create()
        first = engine.native_restore(
            "wright-rich-text",
            await self.state(target),
            document("First dependency").state,
        )
        second = engine.native_restore(
            "wright-rich-text", first.state, document("After dependency").state
        )
        operations = [uuid4(), uuid4()]
        missing = await self.append(target, [second.update], [operations[1]])
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertEqual(missing.json()["code"], "missing_dependencies")
        corrupt = await self.append(
            target, [first.update, b"not a Yrs update"], operations
        )
        self.assertEqual(corrupt.status_code, 422, corrupt.text)
        self.assertEqual((await self.metadata(target)).head_seq, 0)
        valid = await self.append(target, [first.update, second.update], operations)
        self.assertEqual(valid.status_code, 200, valid.text)

    async def test_title_watermark_does_not_conflict_with_typing(self) -> None:
        target = await self.create("Start")
        changed = engine.native_restore(
            "wright-rich-text", await self.state(target), document("Typed").state
        )
        self.assertEqual((await self.append(target, [changed.update])).status_code, 200)
        command = {
            "operation_id": str(uuid4()),
            "expected_metadata_seq": 0,
            "title": " Explicit ",
        }
        renamed = await self.client.patch(
            f"/documents/{target}/title", headers=self.headers(), json=command
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual((await self.metadata(target)).metadata_seq, 2)
        self.assertEqual((await self.metadata(target)).title, "Explicit")
        again = engine.native_restore(
            "wright-rich-text", changed.state, document("Typing again").state
        )
        self.assertEqual((await self.append(target, [again.update])).status_code, 200)
        self.assertEqual((await self.metadata(target)).title, "Explicit")
        stale = await self.client.patch(
            f"/documents/{target}/title",
            headers=self.headers(),
            json={**command, "operation_id": str(uuid4())},
        )
        self.assertEqual(stale.status_code, 409)
        retry = await self.client.patch(
            f"/documents/{target}/title", headers=self.headers(), json=command
        )
        self.assertEqual(retry.json(), renamed.json())
        reset = await self.client.patch(
            f"/documents/{target}/title",
            headers=self.headers(),
            json={
                "operation_id": str(uuid4()),
                "expected_metadata_seq": 2,
                "title": None,
            },
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual((await self.metadata(target)).title, "Typing again")
        self.assertFalse((await self.metadata(target)).title_is_user_set)

    async def test_share_is_explicit_atomic_exact_and_does_not_grant_link_access(
        self,
    ) -> None:
        target = await self.create()
        command = {
            "operation_id": str(uuid4()),
            "user_id": str(self.editor),
            "role": "editor",
        }
        shared = await self.client.post(
            f"/documents/{target}/share", headers=self.headers(), json=command
        )
        self.assertEqual(shared.status_code, 200, shared.text)
        path = shared.json()["path"]
        self.assertTrue(path.startswith("/" + self.names[self.owner] + "/"))
        self.assertNotIn("@", path)
        self.assertEqual(
            (
                await self.client.post(
                    f"/documents/{target}/share", headers=self.headers(), json=command
                )
            ).json(),
            shared.json(),
        )
        self.assertEqual((await self.metadata(target)).access_version, 1)
        for actor, status in (
            (self.owner, 200),
            (self.editor, 200),
            (self.stranger, 404),
        ):
            resolved = await self.client.get(
                "/documents/resolve" + path, headers=self.headers(actor)
            )
            self.assertEqual(resolved.status_code, status, resolved.text)
        bad = await self.client.get(
            "/documents/resolve" + path + "x", headers=self.headers()
        )
        self.assertEqual(bad.status_code, 404)
        namespace, slug = path.strip("/").split("/")
        case = await self.client.get(
            f"/documents/resolve/{namespace}/{slug.swapcase()}", headers=self.headers()
        )
        self.assertEqual(case.status_code, 404)
        denied = await self.client.post(
            f"/documents/{target}/share",
            headers=self.headers(self.editor),
            json={"operation_id": str(uuid4())},
        )
        self.assertEqual(denied.status_code, 403)
        renamed = "renamed_" + self.owner.hex[:16]
        self.database.execute(
            "update public.profiles set username=%s where id=%s", (renamed, self.owner)
        )
        self.assertEqual(
            (
                await self.client.get(
                    "/documents/resolve" + path, headers=self.headers()
                )
            ).status_code,
            200,
        )
        second = await self.client.post(
            f"/documents/{target}/share",
            headers=self.headers(),
            json={"operation_id": str(uuid4())},
        )
        self.assertEqual(second.json()["path"], f"/{renamed}/{slug}")
        with self.assertRaises(Conflict):
            self.database.execute(
                "update public.profiles set username=%s where id=%s",
                (namespace, self.stranger),
            )

    async def test_failed_share_does_not_consume_alias_or_grant(self) -> None:
        target = await self.create()
        response = await self.client.post(
            f"/documents/{target}/share",
            headers=self.headers(),
            json={
                "operation_id": str(uuid4()),
                "user_id": str(uuid4()),
                "role": "viewer",
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        counts = self.database.execute(
            "select next_slug,(select count(*) from documents.slugs where document_id=%s) as slugs from documents.spaces where owner_user_id=%s",
            (target, self.owner),
        )
        self.assertEqual(counts, [{"next_slug": 1, "slugs": 0}])
        self.assertEqual((await self.metadata(target)).head_seq, 0)

    async def test_viewers_only_receive_current_projection_and_revoked_retries_fail(
        self,
    ) -> None:
        target = await self.create("Private deleted words")
        await self.grant(target, self.viewer, "viewer")
        await self.grant(target, self.editor, "editor")
        edit = engine.native_restore(
            "wright-rich-text",
            await self.state(target),
            document("Visible words").state,
        )
        operation = uuid4()
        accepted = await self.append(target, [edit.update], [operation], self.editor)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        projection = await self.client.get(
            f"/documents/{target}/content", headers=self.headers(self.viewer)
        )
        self.assertEqual(projection.status_code, 200)
        self.assertIn("Visible words", projection.text)
        self.assertNotIn("Private deleted words", projection.text)
        for suffix in (
            "state",
            "state?through_seq=0",
            "changes",
            "changes/0",
            "access",
        ):
            denied = await self.client.get(
                f"/documents/{target}/{suffix}", headers=self.headers(self.viewer)
            )
            self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            (await self.append(target, [edit.update], actor=self.viewer)).status_code,
            403,
        )
        denied_restore = await self.client.post(
            f"/documents/{target}/restore-content",
            headers=self.headers(self.viewer),
            json={"operation_id": str(uuid4()), "through_seq": 0},
        )
        self.assertEqual(denied_restore.status_code, 403)
        await self.grant(target, self.editor, None)
        self.assertEqual(
            (
                await self.append(target, [edit.update], [operation], self.editor)
            ).status_code,
            404,
        )
        self.assertEqual(
            (
                await self.client.get("/documents", headers=self.headers(self.editor))
            ).json(),
            [],
        )

    async def test_trash_restore_never_resurrects_via_upload_or_edit(self) -> None:
        target = await self.create()
        await self.grant(target, self.editor, "editor")
        edit = engine.native_restore(
            "wright-rich-text", await self.state(target), document("Later").state
        )
        operation = str(uuid4())
        trashed = await self.client.post(
            f"/documents/{target}/trash",
            headers=self.headers(),
            json={"operation_id": operation},
        )
        self.assertEqual(trashed.status_code, 200, trashed.text)
        self.assertEqual(
            (
                await self.client.post(
                    f"/documents/{target}/trash",
                    headers=self.headers(),
                    json={"operation_id": operation},
                )
            ).json(),
            trashed.json(),
        )
        self.assertEqual((await self.append(target, [edit.update])).status_code, 404)
        upload = await self.client.put(
            f"/documents/{target}",
            headers=self.headers(),
            data={
                "metadata": json.dumps(
                    {
                        "operation_id": str(uuid4()),
                        "title": "",
                        "title_is_user_set": False,
                    }
                )
            },
            files={"state": ("state.bin", document("Overwrite").state)},
        )
        self.assertEqual(upload.status_code, 404)
        self.assertEqual(
            (await self.client.get("/documents", headers=self.headers())).json(), []
        )
        trash = await self.client.get("/documents?trashed=true", headers=self.headers())
        self.assertEqual([item["id"] for item in trash.json()], [str(target)])
        self.assertEqual(
            (
                await self.client.get(
                    "/documents?trashed=true", headers=self.headers(self.editor)
                )
            ).json(),
            [],
        )
        denied = await self.client.post(
            f"/documents/{target}/restore",
            headers=self.headers(self.editor),
            json={"operation_id": str(uuid4())},
        )
        self.assertEqual(denied.status_code, 404)
        restored = await self.client.post(
            f"/documents/{target}/restore",
            headers=self.headers(),
            json={"operation_id": str(uuid4())},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(
            (await self.append(target, [edit.update], actor=self.editor)).status_code,
            200,
        )

    async def test_snapshot_and_historical_restore_keep_all_original_events(
        self,
    ) -> None:
        target = await self.create("Original")
        initial = await self.state(target)
        edited = engine.native_restore(
            "wright-rich-text", initial, document("Replacement").state
        )
        self.assertEqual((await self.append(target, [edited.update])).status_code, 200)
        for through in (0, 1, 1):
            response = await self.client.post(
                f"/documents/{target}/snapshots?through_seq={through}",
                headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)
        rows = self.database.execute(
            "select through_seq,document_metadata from documents.snapshots where document_id=%s",
            (target,),
        )
        self.assertEqual(
            rows,
            [
                {
                    "through_seq": 1,
                    "document_metadata": {
                        "title": "Replacement",
                        "title_is_user_set": False,
                    },
                }
            ],
        )
        self.assertEqual(
            json.loads(
                engine.native_merge(
                    "wright-rich-text", [await self.state(target, 0)]
                ).projection
            )["doc"],
            projected("Original"),
        )
        command = {"operation_id": str(uuid4()), "through_seq": 0}
        restored = await self.client.post(
            f"/documents/{target}/restore-content", headers=self.headers(), json=command
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["head_seq"], 2)
        self.assertEqual(
            (
                await self.client.post(
                    f"/documents/{target}/restore-content",
                    headers=self.headers(),
                    json=command,
                )
            ).json(),
            restored.json(),
        )
        restore_bytes = await self.client.get(
            f"/documents/{target}/changes/2", headers=self.headers()
        )
        reopened = engine.native_merge(
            "wright-rich-text", [edited.state, restore_bytes.content]
        )
        self.assertEqual(json.loads(reopened.projection)["doc"], projected("Original"))
        self.assertNotEqual(restore_bytes.content, initial)
        self.assertEqual(
            json.loads(
                engine.native_merge(
                    "wright-rich-text", [await self.state(target)]
                ).projection
            )["doc"],
            projected("Original"),
        )
        history = await self.client.get(
            f"/documents/{target}/changes", headers=self.headers()
        )
        self.assertEqual(
            [item["kind"] for item in history.json()],
            ["create", "content", "restore_content"],
        )

    async def test_commit_failure_returns_no_receipt_and_can_retry_unchanged(
        self,
    ) -> None:
        target = await self.create()
        command = {"operation_id": str(uuid4())}
        self.database.fail_commit = True
        failed = await self.client.post(
            f"/documents/{target}/share", headers=self.headers(), json=command
        )
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual((await self.metadata(target)).head_seq, 0)
        retry = await self.client.post(
            f"/documents/{target}/share", headers=self.headers(), json=command
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["head_seq"], 1)
        rows = self.database.execute(
            "select count(*) as count from documents.slugs where document_id=%s",
            (target,),
        )
        self.assertEqual(rows, [{"count": 1}])

    async def test_authorization_is_rechecked_after_native_preparation(self) -> None:
        target = await self.create()
        await self.grant(target, self.editor, "editor")
        edit = engine.native_restore(
            "wright-rich-text",
            await self.state(target),
            document("Should not commit").state,
        )
        original = native.merge
        revoked = False

        def merge(schema: SchemaName, updates: list[bytes]) -> Native:
            nonlocal revoked
            result = original(schema, updates)
            if not revoked:
                revoked = True
                self.service.grant(
                    self.account(self.owner), target, uuid4(), self.editor, None
                )
            return result

        with patch("app.services.documents.native.merge", side_effect=merge):
            response = await self.append(target, [edit.update], actor=self.editor)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual((await self.metadata(target)).head_seq, 2)

    async def test_automatic_checkpoint_retains_history_and_accepts_later_native_edits(
        self,
    ) -> None:
        target = await self.create("Revision 0")
        current = await self.state(target)
        updates: list[bytes] = []
        for revision in range(1, 129):
            edited = engine.native_restore(
                "wright-rich-text", current, document(f"Revision {revision}").state
            )
            updates.append(edited.update)
            current = edited.state
        accepted = await self.append(target, updates)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()[-1]["head_seq"], 128)
        self.assertEqual(
            self.database.execute(
                "select through_seq from documents.snapshots where document_id=%s",
                (target,),
            ),
            [{"through_seq": 128}],
        )
        self.assertEqual(
            self.database.execute(
                "select count(*) as count from documents.changes where document_id=%s",
                (target,),
            ),
            [{"count": 129}],
        )
        next_edit = engine.native_restore(
            "wright-rich-text", current, document("Revision 129").state
        )
        self.assertEqual(
            (await self.append(target, [next_edit.update])).status_code, 200
        )
        self.assertEqual(
            json.loads(
                engine.native_merge(
                    "wright-rich-text", [await self.state(target)]
                ).projection
            )["doc"],
            projected("Revision 129"),
        )
        self.assertEqual(
            json.loads(
                engine.native_merge(
                    "wright-rich-text", [await self.state(target, 1)]
                ).projection
            )["doc"],
            projected("Revision 1"),
        )

    async def test_rich_attributes_marks_and_native_empty_documents_round_trip(
        self,
    ) -> None:
        rich: dict[str, JsonValue] = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2, "text_align": "center"},
                    "content": [
                        {
                            "type": "text",
                            "text": "Styled 🦉",
                            "marks": [
                                {"type": "strong"},
                                {
                                    "type": "link",
                                    "attrs": {"href": "https://example.com"},
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        created = await self.client.post(
            "/documents",
            headers=self.headers(),
            json={"operation_id": str(uuid4()), "content": rich},
        )
        self.assertEqual(created.status_code, 201, created.text)
        target = UUID(created.json()["document_id"])
        expected = engine.native_create(
            "wright-rich-text",
            json.dumps({"version": 1, "schema": "wright-rich-text", "doc": rich}),
        )
        actual = engine.native_merge("wright-rich-text", [await self.state(target)])
        self.assertEqual(json.loads(actual.projection), json.loads(expected.projection))
        self.assertEqual((await self.metadata(target)).title, "Styled 🦉")
        for number in (1, 2):
            empty = await self.client.post(
                "/documents",
                headers=self.headers(),
                json={"operation_id": str(uuid4())},
            )
            self.assertEqual(empty.status_code, 201, empty.text)
            self.assertEqual(
                (await self.metadata(UUID(empty.json()["document_id"]))).title,
                f"Untitled {number}",
            )

    async def test_optimistic_append_recaptures_a_changed_head(self) -> None:
        target = await self.create()
        edit = engine.native_restore(
            "wright-rich-text", await self.state(target), document("New body").state
        )
        original = native.merge
        advanced = False

        def merge(schema: SchemaName, updates: list[bytes]) -> Native:
            nonlocal advanced
            result = original(schema, updates)
            if not advanced:
                advanced = True
                self.service.lifecycle(self.account(self.owner), target, uuid4(), False)
            return result

        with patch("app.services.documents.native.merge", side_effect=merge):
            response = await self.append(target, [edit.update])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["head_seq"], 2)

    async def test_private_storage_without_rls_and_auth_before_body(self) -> None:
        rows = self.database.execute(
            "select relname,relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='documents' and relkind='r'"
        )
        self.assertEqual(len(rows), 7)
        self.assertFalse(any(row["relrowsecurity"] for row in rows))
        for role in ("anon", "authenticated"):
            privileges = self.database.execute(
                "select has_schema_privilege(%s,'documents','usage') as allowed",
                (role,),
            )
            self.assertEqual(privileges, [{"allowed": False}])
        self.assertEqual((await self.client.get("/documents")).status_code, 401)
        unauthenticated = await self.client.post(
            "/documents", content=b"", headers={"Content-Length": str(66 * 1024 * 1024)}
        )
        self.assertEqual(unauthenticated.status_code, 401)
        oversized = await self.client.post(
            "/documents",
            content=b"",
            headers={**self.headers(), "Content-Length": str(66 * 1024 * 1024)},
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.headers["cache-control"], "private, no-store")
        spoof = await self.client.post(
            "/documents",
            headers=self.headers(),
            json={"operation_id": str(uuid4()), "actor_user_id": str(self.stranger)},
        )
        self.assertEqual(spoof.status_code, 422)

    async def test_invalid_models_and_nonfinite_json_fail_without_creating_documents(
        self,
    ) -> None:
        for content in (
            {"type": "unknown"},
            {"type": "doc", "attrs": {"value": float("nan")}},
            {"type": "doc", "attrs": {"value": float("inf")}},
        ):
            response = await self.client.post(
                "/documents",
                headers={**self.headers(), "Content-Type": "application/json"},
                content=json.dumps({"operation_id": str(uuid4()), "content": content}),
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("code", response.json())
        self.assertEqual(
            (await self.client.get("/documents", headers=self.headers())).json(), []
        )
