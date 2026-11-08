"""Authenticated document lifecycle and short Postgres transactions.

Canonical reconstruction happens outside write locks. Every commit rechecks the
current role and captured head; failed or ambiguous requests keep their IDs.
"""

import hashlib
import json
import re
from typing import Literal
from uuid import UUID, uuid7

from pydantic import JsonValue, TypeAdapter

from app.accounts import Account
from app.schemas import (
    CreateDocument,
    DocumentChange,
    DocumentContent,
    DocumentGrant,
    DocumentReceipt,
    DocumentSummary,
    RenameDocument,
    ShareDocument,
    UploadDocument,
)
from app.services.documents import native
from app.services.documents.database import Database, Row, Session
from app.services.documents.errors import (
    Conflict,
    Forbidden,
    InvalidNative,
    NotFound,
    TooLarge,
    Unavailable,
)
from app.services.documents.models import Document, Native, Prefix
from app.services.documents.slugs import encode_sharing_number
from app.services.documents.values import JsonParameter

_SELECT = """select d.*,s.owner_user_id,s.canonical_name,
 case when s.owner_user_id=%s then 'owner' else a.role end as role
 from documents.documents d join documents.spaces s on s.id=d.space_id
 left join documents.access a on a.document_id=d.id and a.user_id=%s
 where d.id=%s and (s.owner_user_id=%s or a.user_id=%s)"""

# Compile boundary validators once, rather than for every SQL value/history row.
_NUMBER = TypeAdapter(int)
_BYTES = TypeAdapter(bytes)
_METADATA = TypeAdapter(dict[str, JsonValue])
_UUID = TypeAdapter(UUID)


def _number(row: Row, key: str) -> int:
    return _NUMBER.validate_python(row[key])


def _bytes(row: Row, key: str) -> bytes:
    return _BYTES.validate_python(row[key], strict=True)


def _json(row: Row, key: str) -> dict[str, JsonValue]:
    return _METADATA.validate_python(row[key])


def summary(document: Document) -> DocumentSummary:
    return DocumentSummary.model_validate(
        {
            **document.model_dump(include=set(DocumentSummary.model_fields)),
            "path": f"/d/{document.id}",
        }
    )


def digest(
    actor: UUID,
    target: UUID | None,
    kind: str,
    command: dict[str, JsonValue],
    payload: bytes = b"",
) -> bytes:
    try:
        normalized = json.dumps(
            {
                "version": 1,
                "actor": str(actor),
                "document": str(target) if target else None,
                "kind": kind,
                "command": command,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except ValueError:
        raise InvalidNative("invalid_document_command") from None
    return hashlib.sha256(
        len(normalized).to_bytes(8, "big") + normalized + payload
    ).digest()


class DocumentService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def open(self) -> None:
        self.database.open()

    def close(self) -> None:
        self.database.close()

    @staticmethod
    def actor(account: Account) -> UUID:
        try:
            return UUID(account.id)
        except ValueError:
            raise Forbidden() from None

    def _document(
        self,
        tx: Session,
        actor: UUID,
        target: UUID,
        *,
        lock: Literal["update", "share"] | None = None,
        owner: bool = False,
        canonical: bool = False,
        trashed: bool = False,
    ) -> Document:
        suffix = f" for {lock} of d" if lock else ""
        rows = tx.execute(_SELECT + suffix, (actor, actor, target, actor, actor))
        if not rows:
            raise NotFound()
        # Recheck after waiting for a document lock. Permissions and lifecycle
        # commands acquire that same row lock before changing anything.
        if lock:
            rows = tx.execute(_SELECT, (actor, actor, target, actor, actor))
        if not rows:
            raise NotFound()
        doc = Document.model_validate(rows[0])
        if doc.trashed_at is not None and not (trashed and doc.role == "owner"):
            raise NotFound()
        if (owner and doc.role != "owner") or (canonical and doc.role == "viewer"):
            raise Forbidden()
        return doc

    def _receipt(
        self, tx: Session, doc: Document, operation: UUID, fingerprint: bytes
    ) -> DocumentReceipt | None:
        rows = tx.execute(
            "select seq,request_sha256,metadata from documents.changes where document_id=%s and operation_id=%s",
            (doc.id, operation),
        )
        if not rows:
            return None
        if _bytes(rows[0], "request_sha256") != fingerprint:
            raise Conflict("operation_id_reused")
        path = _json(rows[0], "metadata").get("path")
        return DocumentReceipt(
            document_id=doc.id,
            space_id=doc.space_id,
            operation_id=operation,
            head_seq=_number(rows[0], "seq"),
            path=path if isinstance(path, str) else None,
        )

    def _automatic(
        self, tx: Session, doc: Document | None, space: UUID, inferred: str | None
    ) -> str:
        if inferred is not None:
            return inferred
        current = doc.title if doc else ""
        if current == "Untitled" or re.fullmatch(r"Untitled [1-9][0-9]*", current):
            return current
        rows = tx.execute(
            "select title from documents.documents where space_id=%s and title like 'Untitled %%'",
            (space,),
        )
        used = {str(row["title"]) for row in rows}
        number = 1
        while f"Untitled {number}" in used:
            number += 1
        return f"Untitled {number}"

    def _record(
        self,
        tx: Session,
        actor: UUID,
        doc: Document,
        operation: UUID,
        fingerprint: bytes,
        kind: str,
        *,
        payload: bytes | None = None,
        title: str | None = None,
        user_title: bool | None = None,
        details: dict[str, JsonValue] | None = None,
        access_changed: bool = False,
        source: str = "arctic",
    ) -> tuple[Document, DocumentReceipt]:
        title = doc.title if title is None else title
        user_title = doc.title_is_user_set if user_title is None else user_title
        metadata: dict[str, JsonValue] = {
            "title": title,
            "title_is_user_set": user_title,
            **(details or {}),
        }
        sequence = doc.head_seq + 1
        tx.execute(
            """insert into documents.changes(document_id,seq,operation_id,actor_user_id,source,kind,
          format_version,schema_name,schema_version,payload,metadata,request_sha256)
          values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                doc.id,
                sequence,
                operation,
                actor,
                source,
                kind,
                doc.format_version,
                doc.schema_name,
                doc.schema_version,
                payload,
                JsonParameter(metadata),
                fingerprint,
            ),
        )
        rows = tx.execute(
            """update documents.documents set head_seq=%s,title=%s,title_is_user_set=%s,
          metadata_seq=case when %s='title' then %s else metadata_seq end,
          access_version=access_version+%s,updated_at=clock_timestamp(),
          trashed_at=case when %s='trash' then clock_timestamp() when %s='restore' then null else trashed_at end
          where id=%s returning *""",
            (
                sequence,
                title,
                user_title,
                kind,
                sequence,
                int(access_changed or kind in {"trash", "restore"}),
                kind,
                kind,
                doc.id,
            ),
        )
        updated = Document.model_validate({**doc.model_dump(), **rows[0]})
        receipt = self._receipt(tx, updated, operation, fingerprint)
        assert receipt is not None
        return updated, receipt

    def metadata(self, account: Account, target: UUID) -> DocumentSummary:
        with self.database.transaction() as tx:
            return summary(self._document(tx, self.actor(account), target))

    def list_documents(
        self, account: Account, *, trashed: bool, limit: int, offset: int
    ) -> list[DocumentSummary]:
        actor = self.actor(account)
        with self.database.transaction() as tx:
            rows = tx.execute(
                """select d.*,s.owner_user_id,s.canonical_name,
              case when s.owner_user_id=%s then 'owner' else a.role end as role
              from documents.documents d join documents.spaces s on s.id=d.space_id
              left join documents.access a on a.document_id=d.id and a.user_id=%s
              where ((%s and d.trashed_at is not null and s.owner_user_id=%s)
                or (not %s and d.trashed_at is null and (s.owner_user_id=%s or a.user_id=%s)))
              order by d.updated_at desc,d.id limit %s offset %s""",
                (actor, actor, trashed, actor, trashed, actor, actor, limit, offset),
            )
            return [summary(Document.model_validate(row)) for row in rows]

    def resolve(self, account: Account, namespace: str, slug: str) -> DocumentSummary:
        with self.database.transaction() as tx:
            rows = tx.execute(
                """select s.document_id from documents.names n join documents.slugs s on s.space_id=n.space_id
              where n.name=%s and s.value=%s""",
                (namespace, slug),
            )
            if not rows:
                raise NotFound()
            target = _UUID.validate_python(rows[0]["document_id"])
            return summary(self._document(tx, self.actor(account), target))

    def _insert(
        self,
        tx: Session,
        actor: UUID,
        target: UUID,
        command: UploadDocument,
        prepared: Native,
        state: bytes,
        fingerprint: bytes,
        source: str = "native",
    ) -> DocumentReceipt:
        spaces = tx.execute(
            "select id from documents.spaces where owner_user_id=%s", (actor,)
        )
        if not spaces:
            raise Unavailable()
        space = _UUID.validate_python(spaces[0]["id"])
        title = (
            command.title
            if command.title_is_user_set
            else self._automatic(tx, None, space, prepared.inferred_title)
        )
        # Initial upload metadata is frozen along with bytes. Existing automatic
        # placeholders survive; nonblank inferred native content is authoritative.
        if (
            not command.title_is_user_set
            and prepared.inferred_title is None
            and (
                command.title == "Untitled"
                or re.fullmatch(r"Untitled [1-9][0-9]*", command.title)
            )
        ):
            title = command.title
        inserted = tx.execute(
            """insert into documents.documents(id,space_id,created_by,title,title_is_user_set,
          format_version,schema_name,schema_version) values(%s,%s,%s,%s,%s,%s,%s,%s)
          on conflict(id) do nothing returning id""",
            (
                target,
                space,
                actor,
                title,
                command.title_is_user_set,
                command.format_version,
                command.schema_name,
                command.schema_version,
            ),
        )
        if not inserted:
            doc = self._document(tx, actor, target, lock="update", owner=True)
            receipt = self._receipt(tx, doc, command.operation_id, fingerprint)
            if receipt is None:
                raise Conflict("document_id_exists")
            return receipt
        tx.execute(
            """insert into documents.changes(document_id,seq,operation_id,actor_user_id,source,kind,
          format_version,schema_name,schema_version,payload,metadata,request_sha256)
          values(%s,0,%s,%s,%s,'create',%s,%s,%s,%s,%s,%s)""",
            (
                target,
                command.operation_id,
                actor,
                source,
                command.format_version,
                command.schema_name,
                command.schema_version,
                state,
                JsonParameter(
                    {"title": title, "title_is_user_set": command.title_is_user_set}
                ),
                fingerprint,
            ),
        )
        return DocumentReceipt(
            document_id=target,
            space_id=space,
            operation_id=command.operation_id,
            head_seq=0,
        )

    def upload(
        self, account: Account, target: UUID, command: UploadDocument, state: bytes
    ) -> DocumentReceipt:
        if not state or len(state) > native.MAX_STATE:
            raise TooLarge()
        prepared = native.merge(command.schema_name, [state])
        actor = self.actor(account)
        fingerprint = digest(
            actor,
            target,
            "upload",
            command.model_dump(mode="json", exclude={"operation_id"}),
            state,
        )
        with self.database.transaction() as tx:
            return self._insert(
                tx, actor, target, command, prepared, state, fingerprint
            )

    def create(self, account: Account, command: CreateDocument) -> DocumentReceipt:
        actor = self.actor(account)
        fingerprint = digest(
            actor,
            None,
            "create",
            command.model_dump(mode="json", exclude={"operation_id"}),
        )
        prepared = native.create(command.schema_name, command.content)
        with self.database.transaction() as tx:
            tx.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"{actor}/{command.operation_id}",),
            )
            existing = tx.execute(
                """select document_id from documents.changes where actor_user_id=%s
              and operation_id=%s and kind='create' and source='arctic'""",
                (actor, command.operation_id),
            )
            if existing:
                target = _UUID.validate_python(existing[0]["document_id"])
                doc = self._document(tx, actor, target, lock="update", owner=True)
                receipt = self._receipt(tx, doc, command.operation_id, fingerprint)
                assert receipt is not None
                return receipt
            upload = UploadDocument(
                operation_id=command.operation_id,
                title=command.title or "",
                title_is_user_set=command.title is not None,
                schema_name=command.schema_name,
            )
            return self._insert(
                tx,
                actor,
                uuid7(),
                upload,
                prepared,
                prepared.state,
                fingerprint,
                "arctic",
            )

    def _prefix(self, tx: Session, doc: Document, through: int | None = None) -> Prefix:
        through = doc.head_seq if through is None else through
        if not 0 <= through <= doc.head_seq:
            raise Conflict("invalid_history_prefix")
        checkpoint = tx.execute(
            """select through_seq,state from documents.snapshots where document_id=%s
          and through_seq<=%s order by through_seq desc limit 1""",
            (doc.id, through),
        )
        base = _number(checkpoint[0], "through_seq") if checkpoint else -1
        updates = [_bytes(checkpoint[0], "state")] if checkpoint else []
        rows = tx.execute(
            """select payload from documents.changes where document_id=%s and seq>%s
          and seq<=%s and payload is not null order by seq""",
            (doc.id, base, through),
        )
        updates.extend(_bytes(row, "payload") for row in rows)
        metadata = tx.execute(
            "select metadata from documents.changes where document_id=%s and seq=%s",
            (doc.id, through),
        )
        if not metadata or not updates:
            raise Unavailable()
        values = _json(metadata[0], "metadata")
        return Prefix(
            doc,
            through,
            updates,
            {
                "title": values["title"],
                "title_is_user_set": values["title_is_user_set"],
            },
        )

    def capture(
        self,
        account: Account,
        target: UUID,
        through: int | None = None,
        *,
        viewer: bool = False,
    ) -> Prefix:
        with self.database.transaction() as tx:
            doc = self._document(
                tx, self.actor(account), target, lock="share", canonical=not viewer
            )
            return self._prefix(tx, doc, through)

    def state(
        self, account: Account, target: UUID, through: int | None = None
    ) -> tuple[Prefix, Native]:
        prefix = self.capture(account, target, through)
        return prefix, native.merge(prefix.document.schema_name, prefix.updates)

    def content(self, account: Account, target: UUID) -> DocumentContent:
        prefix = self.capture(account, target, viewer=True)
        prepared = native.merge(prefix.document.schema_name, prefix.updates)
        return DocumentContent(
            document=summary(prefix.document), content=prepared.projection
        )

    def append(
        self,
        account: Account,
        target: UUID,
        operations: list[UUID],
        updates: list[bytes],
    ) -> list[DocumentReceipt]:
        if (
            len(operations) != len(updates)
            or not operations
            or len(set(operations)) != len(operations)
            or len(operations) > 128
        ):
            raise InvalidNative("invalid_content_batch")
        if (
            any(not item or len(item) > native.MAX_UPDATE for item in updates)
            or sum(map(len, updates)) > native.MAX_BATCH
        ):
            raise TooLarge()
        actor = self.actor(account)
        fingerprints = [
            digest(actor, target, "content", {}, update) for update in updates
        ]
        for _ in range(4):
            with self.database.transaction() as tx:
                doc = self._document(tx, actor, target, lock="share", canonical=True)
                receipts = [
                    self._receipt(tx, doc, op, hashed)
                    for op, hashed in zip(operations, fingerprints, strict=True)
                ]
                if all(receipt is not None for receipt in receipts):
                    return [receipt for receipt in receipts if receipt is not None]
                prefix = self._prefix(tx, doc)
            candidate = native.merge(doc.schema_name, prefix.updates)
            titles: list[str | None] = []
            for index, update in enumerate(updates):
                if receipts[index] is None:
                    candidate = native.merge(doc.schema_name, [candidate.state, update])
                titles.append(candidate.inferred_title)
            with self.database.transaction() as tx:
                current = self._document(
                    tx, actor, target, lock="update", canonical=True
                )
                if current.head_seq != doc.head_seq:
                    continue
                accepted: list[DocumentReceipt] = []
                for op, hashed, update, inferred in zip(
                    operations, fingerprints, updates, titles, strict=True
                ):
                    receipt = self._receipt(tx, current, op, hashed)
                    if receipt is None:
                        title = (
                            current.title
                            if current.title_is_user_set
                            else self._automatic(
                                tx, current, current.space_id, inferred
                            )
                        )
                        current, receipt = self._record(
                            tx,
                            actor,
                            current,
                            op,
                            hashed,
                            "content",
                            payload=update,
                            title=title,
                            source="native",
                        )
                    accepted.append(receipt)
                self._checkpoint_if_needed(
                    tx,
                    current,
                    candidate.state,
                    sum(map(len, prefix.updates)) + sum(map(len, updates)),
                )
                return accepted
        raise Conflict("history_head_changed")

    def rename(
        self, account: Account, target: UUID, command: RenameDocument
    ) -> DocumentReceipt:
        actor = self.actor(account)
        hashed = digest(
            actor,
            target,
            "title",
            command.model_dump(mode="json", exclude={"operation_id"}),
        )
        for _ in range(4):
            prefix = self.capture(account, target)
            prepared = (
                native.merge(prefix.document.schema_name, prefix.updates)
                if command.title is None
                else None
            )
            with self.database.transaction() as tx:
                doc = self._document(tx, actor, target, lock="update", canonical=True)
                existing = self._receipt(tx, doc, command.operation_id, hashed)
                if existing:
                    return existing
                if doc.metadata_seq != command.expected_metadata_seq:
                    raise Conflict("metadata_conflict")
                if prepared is not None and doc.head_seq != prefix.document.head_seq:
                    continue
                title = (
                    command.title.strip()
                    if command.title is not None
                    else self._automatic(
                        tx,
                        doc,
                        doc.space_id,
                        prepared.inferred_title if prepared else None,
                    )
                )
                return self._record(
                    tx,
                    actor,
                    doc,
                    command.operation_id,
                    hashed,
                    "title",
                    title=title,
                    user_title=command.title is not None,
                )[1]
        raise Conflict("history_head_changed")

    def _grant(
        self,
        tx: Session,
        actor: UUID,
        doc: Document,
        recipient: UUID,
        role: Literal["editor", "viewer"] | None,
    ) -> bool:
        if recipient == doc.owner_user_id:
            raise Conflict("owner_role_is_derived")
        before = tx.execute(
            "select role from documents.access where document_id=%s and user_id=%s",
            (doc.id, recipient),
        )
        if (str(before[0]["role"]) if before else None) == role:
            return False
        if role is None:
            tx.execute(
                "delete from documents.access where document_id=%s and user_id=%s",
                (doc.id, recipient),
            )
        else:
            tx.execute(
                """insert into documents.access(document_id,user_id,role,granted_by) values(%s,%s,%s,%s)
              on conflict(document_id,user_id) do update set role=excluded.role,granted_by=excluded.granted_by,granted_at=clock_timestamp()""",
                (doc.id, recipient, role, actor),
            )
        return True

    def share(
        self, account: Account, target: UUID, command: ShareDocument
    ) -> DocumentReceipt:
        if (command.user_id is None) != (command.role is None):
            raise InvalidNative("invalid_sharing_grant")
        actor = self.actor(account)
        hashed = digest(
            actor,
            target,
            "share",
            command.model_dump(mode="json", exclude={"operation_id"}),
        )
        with self.database.transaction() as tx:
            initial = self._document(tx, actor, target, owner=True)
            spaces = tx.execute(
                "select id,canonical_name,next_slug from documents.spaces where id=%s for update",
                (initial.space_id,),
            )
            doc = self._document(tx, actor, target, lock="update", owner=True)
            if doc.space_id != initial.space_id:
                raise Conflict("document_moved")
            receipt = self._receipt(tx, doc, command.operation_id, hashed)
            if receipt:
                return receipt
            aliases = tx.execute(
                "select value from documents.slugs where space_id=%s and document_id=%s",
                (doc.space_id, target),
            )
            if aliases:
                value = str(aliases[0]["value"])
            else:
                number = _number(spaces[0], "next_slug")
                value = encode_sharing_number(number)
                tx.execute(
                    "insert into documents.slugs(space_id,number,value,document_id,created_by) values(%s,%s,%s,%s,%s)",
                    (doc.space_id, number, value, target, actor),
                )
                tx.execute(
                    "update documents.spaces set next_slug=next_slug+1 where id=%s",
                    (doc.space_id,),
                )
            changed = (
                self._grant(tx, actor, doc, command.user_id, command.role)
                if command.user_id is not None
                else False
            )
            path = f"/{spaces[0]['canonical_name']}/{value}"
            return self._record(
                tx,
                actor,
                doc,
                command.operation_id,
                hashed,
                "share",
                details={
                    "path": path,
                    "user_id": str(command.user_id) if command.user_id else None,
                    "role": command.role,
                },
                access_changed=changed,
            )[1]

    def grant(
        self,
        account: Account,
        target: UUID,
        operation: UUID,
        recipient: UUID,
        role: Literal["editor", "viewer"] | None,
    ) -> DocumentReceipt:
        actor = self.actor(account)
        hashed = digest(
            actor, target, "access", {"user_id": str(recipient), "role": role}
        )
        with self.database.transaction() as tx:
            doc = self._document(tx, actor, target, lock="update", owner=True)
            existing = self._receipt(tx, doc, operation, hashed)
            if existing:
                return existing
            changed = self._grant(tx, actor, doc, recipient, role)
            return self._record(
                tx,
                actor,
                doc,
                operation,
                hashed,
                "access",
                details={"user_id": str(recipient), "role": role},
                access_changed=changed,
            )[1]

    def grants(self, account: Account, target: UUID) -> list[DocumentGrant]:
        with self.database.transaction() as tx:
            self._document(tx, self.actor(account), target, lock="share", owner=True)
            rows = tx.execute(
                "select user_id,role,granted_by,granted_at from documents.access where document_id=%s order by user_id",
                (target,),
            )
            return [DocumentGrant.model_validate(row, strict=False) for row in rows]

    def lifecycle(
        self, account: Account, target: UUID, operation: UUID, trashed: bool
    ) -> DocumentReceipt:
        actor = self.actor(account)
        kind = "trash" if trashed else "restore"
        hashed = digest(actor, target, kind, {})
        with self.database.transaction() as tx:
            doc = self._document(
                tx, actor, target, lock="update", owner=True, trashed=True
            )
            existing = self._receipt(tx, doc, operation, hashed)
            return existing or self._record(tx, actor, doc, operation, hashed, kind)[1]

    def history(
        self, account: Account, target: UUID, after: int, limit: int
    ) -> list[DocumentChange]:
        with self.database.transaction() as tx:
            doc = self._document(
                tx, self.actor(account), target, lock="share", canonical=True
            )
            rows = tx.execute(
                """select seq,operation_id,actor_user_id,source,kind,metadata,recorded_at from documents.changes
              where document_id=%s and seq>%s and seq<=%s order by seq limit %s""",
                (target, after, doc.head_seq, limit),
            )
            return [DocumentChange.model_validate(row, strict=False) for row in rows]

    def change(self, account: Account, target: UUID, sequence: int) -> bytes | None:
        with self.database.transaction() as tx:
            doc = self._document(
                tx, self.actor(account), target, lock="share", canonical=True
            )
            if sequence > doc.head_seq:
                raise NotFound()
            rows = tx.execute(
                "select payload from documents.changes where document_id=%s and seq=%s",
                (target, sequence),
            )
            if not rows:
                raise NotFound()
            return (
                _bytes(rows[0], "payload") if rows[0]["payload"] is not None else None
            )

    @staticmethod
    def _checkpoint(
        tx: Session,
        doc: Document,
        through: int,
        state: bytes,
        metadata: dict[str, JsonValue],
    ) -> None:
        tx.execute(
            """insert into documents.snapshots(document_id,through_seq,format_version,schema_name,schema_version,state,document_metadata,state_sha256)
              values(%s,%s,%s,%s,%s,%s,%s,%s) on conflict(document_id,through_seq) do nothing""",
            (
                doc.id,
                through,
                doc.format_version,
                doc.schema_name,
                doc.schema_version,
                state,
                JsonParameter(metadata),
                hashlib.sha256(state).digest(),
            ),
        )

    def _checkpoint_if_needed(
        self, tx: Session, doc: Document, state: bytes, replay_bytes: int
    ) -> None:
        rows = tx.execute(
            "select coalesce(max(through_seq),0) as seq from documents.snapshots where document_id=%s",
            (doc.id,),
        )
        if (
            doc.head_seq - _number(rows[0], "seq") >= 128
            or replay_bytes >= 2 * 1024 * 1024
        ):
            self._checkpoint(
                tx,
                doc,
                doc.head_seq,
                state,
                {"title": doc.title, "title_is_user_set": doc.title_is_user_set},
            )

    def snapshot(self, account: Account, target: UUID, through: int | None) -> int:
        prefix, prepared = self.state(account, target, through)
        if prefix.through_seq == 0:
            return 0  # Creation bytes are already the initial checkpoint.
        with self.database.transaction() as tx:
            self._document(
                tx, self.actor(account), target, lock="share", canonical=True
            )
            self._checkpoint(
                tx, prefix.document, prefix.through_seq, prepared.state, prefix.metadata
            )
        return prefix.through_seq

    def restore(
        self, account: Account, target: UUID, operation: UUID, through: int
    ) -> DocumentReceipt:
        actor = self.actor(account)
        hashed = digest(actor, target, "restore_content", {"through_seq": through})
        for _ in range(4):
            with self.database.transaction() as tx:
                doc = self._document(tx, actor, target, lock="share", canonical=True)
                existing = self._receipt(tx, doc, operation, hashed)
                if existing:
                    return existing
                current, old = self._prefix(tx, doc), self._prefix(tx, doc, through)
            prepared = native.restore(
                doc.schema_name,
                native.merge(doc.schema_name, current.updates).state,
                native.merge(doc.schema_name, old.updates).state,
            )
            with self.database.transaction() as tx:
                latest = self._document(
                    tx, actor, target, lock="update", canonical=True
                )
                existing = self._receipt(tx, latest, operation, hashed)
                if existing:
                    return existing
                if latest.head_seq != doc.head_seq:
                    continue
                title = (
                    latest.title
                    if latest.title_is_user_set
                    else self._automatic(
                        tx, latest, latest.space_id, prepared.inferred_title
                    )
                )
                latest, receipt = self._record(
                    tx,
                    actor,
                    latest,
                    operation,
                    hashed,
                    "restore_content",
                    payload=prepared.update,
                    title=title,
                    details={"through_seq": through},
                )
                self._checkpoint_if_needed(
                    tx,
                    latest,
                    prepared.state,
                    sum(map(len, current.updates)) + len(prepared.update),
                )
                return receipt
        raise Conflict("history_head_changed")
