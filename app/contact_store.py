import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from app.models import (
    CampaignContactPreview,
    CampaignDraftPayload,
    CampaignInfo,
    CampaignResultItem,
    ContactInfo,
    ContactListResult,
    ContactQueryParams,
    ContactSyncDiagnostics,
    ContactSyncRunInfo,
)
from app.contact_name_utils import normalize_contact_name

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONTACTS_DB_PATH = os.path.join(BASE_DIR, "contacts.sqlite3")


class ContactStore:
    def __init__(self, db_path: str = CONTACTS_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    identity_key TEXT PRIMARY KEY,
                    zid TEXT,
                    name TEXT NOT NULL,
                    phone TEXT,
                    avatar_url TEXT,
                    last_message TEXT,
                    unread INTEGER NOT NULL DEFAULT 0,
                    identity_source TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_sync_run_id INTEGER,
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS contact_sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sync_status TEXT NOT NULL,
                    contact_count INTEGER NOT NULL DEFAULT 0,
                    stored_contact_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    selected_contact_ids_json TEXT NOT NULL DEFAULT '[]',
                    matched_contacts_json TEXT NOT NULL DEFAULT '[]',
                    matched_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'draft',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    results_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    executed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_contacts_zid ON contacts(zid);
                CREATE INDEX IF NOT EXISTS idx_contacts_active ON contacts(is_active, name);
                CREATE INDEX IF NOT EXISTS idx_sync_runs_timestamp ON contact_sync_runs(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_campaigns_created_at ON campaigns(created_at DESC);
                """
            )

    def get_contacts_result(self, filters: Optional[ContactQueryParams] = None) -> ContactListResult:
        filters = filters or ContactQueryParams()
        with self._lock, self._connect() as conn:
            last_run = self._fetch_last_sync_run(conn)
            contacts = self._fetch_active_contacts(conn, filters)
            total_stored = conn.execute(
                "SELECT COUNT(*) AS count FROM contacts WHERE is_active = 1"
            ).fetchone()["count"]
            return ContactListResult(
                contacts=contacts,
                contact_count=len(contacts),
                stored_contact_count=total_stored,
                sync_status="stored" if total_stored else "idle",
                sync_run_id=last_run.sync_run_id if last_run else None,
                last_sync_at=last_run.timestamp if last_run else None,
                last_sync_status=last_run.sync_status if last_run else None,
                diagnostics=last_run.diagnostics if last_run else ContactSyncDiagnostics(),
                message=self._build_stored_message(last_run, len(contacts), filters),
            )

    def persist_sync_result(self, sync_result: dict) -> ContactListResult:
        timestamp = sync_result.get("timestamp") or datetime.utcnow().isoformat()
        diagnostics = self._coerce_diagnostics(sync_result.get("diagnostics"))
        raw_contacts = sync_result.get("contacts") or []
        prepared_contacts = [self._coerce_contact(contact, timestamp) for contact in raw_contacts]
        sync_status = sync_result.get("sync_status") or "unknown"
        message = sync_result.get("message") or ""

        with self._lock, self._connect() as conn:
            sync_run_id = self._create_sync_run(
                conn=conn,
                sync_status=sync_status,
                timestamp=timestamp,
                contact_count=len(prepared_contacts),
                stored_contact_count=0,
                message=message,
                diagnostics=diagnostics,
            )

            seen_keys = []
            for contact in prepared_contacts:
                seen_keys.append(contact.identity_key)
                conn.execute(
                    """
                    INSERT INTO contacts (
                        identity_key, zid, name, phone, avatar_url, last_message, unread,
                        identity_source, last_seen_at, last_seen_sync_run_id, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(identity_key) DO UPDATE SET
                        zid = COALESCE(excluded.zid, contacts.zid),
                        name = excluded.name,
                        phone = COALESCE(excluded.phone, contacts.phone),
                        avatar_url = COALESCE(excluded.avatar_url, contacts.avatar_url),
                        last_message = COALESCE(excluded.last_message, contacts.last_message),
                        unread = excluded.unread,
                        identity_source = excluded.identity_source,
                        last_seen_at = excluded.last_seen_at,
                        last_seen_sync_run_id = excluded.last_seen_sync_run_id,
                        is_active = 1
                    """,
                    (
                        contact.identity_key,
                        contact.zid,
                        contact.name,
                        contact.phone,
                        contact.avatar_url,
                        contact.last_message,
                        int(contact.unread),
                        contact.identity_source or "unknown",
                        contact.last_seen_at or timestamp,
                        sync_run_id,
                    ),
                )

            if sync_status == "success":
                if seen_keys:
                    placeholders = ",".join("?" for _ in seen_keys)
                    conn.execute(
                        f"UPDATE contacts SET is_active = 0 WHERE identity_key NOT IN ({placeholders})",
                        seen_keys,
                    )
                else:
                    conn.execute("UPDATE contacts SET is_active = 0")

            stored_count = conn.execute(
                "SELECT COUNT(*) AS count FROM contacts WHERE is_active = 1"
            ).fetchone()["count"]
            conn.execute(
                "UPDATE contact_sync_runs SET stored_contact_count = ? WHERE id = ?",
                (stored_count, sync_run_id),
            )
            conn.commit()

            persisted_contacts = self._fetch_active_contacts(conn, ContactQueryParams())
            return ContactListResult(
                contacts=persisted_contacts,
                contact_count=len(prepared_contacts),
                stored_contact_count=stored_count,
                sync_status=sync_status,
                sync_run_id=sync_run_id,
                last_sync_at=timestamp,
                last_sync_status=sync_status,
                diagnostics=diagnostics,
                message=message,
            )

    def list_sync_runs(self, limit: int = 20) -> list[ContactSyncRunInfo]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sync_status, contact_count, stored_contact_count, message, timestamp, diagnostics_json
                FROM contact_sync_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_sync_run(row) for row in rows]

    def create_campaign(self, payload: CampaignDraftPayload) -> CampaignInfo:
        filters = self._normalize_filters(payload.filters)
        selected_ids = list(dict.fromkeys(filters.selected_ids))
        created_at = datetime.utcnow().isoformat()
        with self._lock, self._connect() as conn:
            matched_contacts = self._resolve_campaign_selected_contacts(conn, selected_ids, filters)
            cursor = conn.execute(
                """
                INSERT INTO campaigns (
                    name, message, filters_json, selected_contact_ids_json, matched_contacts_json,
                    matched_count, status, sent_count, failed_count, results_json, created_at, executed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, 0, '[]', ?, NULL)
                """,
                (
                    payload.name.strip(),
                    payload.message,
                    json.dumps(filters.model_dump()),
                    json.dumps(selected_ids),
                    json.dumps([item.model_dump() for item in matched_contacts]),
                    len(matched_contacts),
                    created_at,
                ),
            )
            conn.commit()
            return self._fetch_campaign_by_id(conn, int(cursor.lastrowid))

    def list_campaigns(self, limit: int = 20) -> list[CampaignInfo]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM campaigns
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_campaign(row) for row in rows]

    def get_campaign(self, campaign_id: int) -> Optional[CampaignInfo]:
        with self._lock, self._connect() as conn:
            return self._fetch_campaign_by_id(conn, campaign_id)

    def prepare_campaign_execution(self, campaign_id: int) -> dict:
        with self._lock, self._connect() as conn:
            campaign = self._fetch_campaign_by_id(conn, campaign_id)
            if not campaign:
                raise ValueError("Campaign not found.")
            selected_contact_ids = campaign.selected_contact_ids or campaign.filters.selected_ids
            matched_contacts = self._resolve_campaign_selected_contacts(conn, selected_contact_ids, campaign.filters)
            selected_targets = self._build_campaign_targets(matched_contacts)
            return {
                "campaign": campaign.model_copy(
                    update={
                        "matched_contacts": matched_contacts,
                        "matched_count": len(matched_contacts),
                        "selected_contact_ids": selected_contact_ids,
                    }
                ),
                "targets": selected_targets,
            }

    def finalize_campaign_execution(self, campaign_id: int, matched_contacts: list[CampaignContactPreview], send_result: dict) -> CampaignInfo:
        executed_at = datetime.utcnow().isoformat()
        results = []
        result_lookup = {}
        for row in send_result.get("results", []):
            if hasattr(row, "model_dump"):
                row = row.model_dump()
            result_lookup[row["target"]] = row

        targets = self._build_campaign_targets(matched_contacts)
        for contact, target in zip(matched_contacts, targets):
            outcome = result_lookup.get(target, {})
            results.append(
                CampaignResultItem(
                    identity_key=contact.identity_key,
                    target=target,
                    name=contact.name,
                    success=bool(outcome.get("success")),
                    error=outcome.get("error"),
                )
            )

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE campaigns
                SET matched_contacts_json = ?, matched_count = ?, status = ?, sent_count = ?, failed_count = ?,
                    results_json = ?, executed_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps([item.model_dump() for item in matched_contacts]),
                    len(matched_contacts),
                    "completed" if send_result.get("failed", 0) == 0 else "completed_with_failures",
                    int(send_result.get("sent", 0)),
                    int(send_result.get("failed", 0)),
                    json.dumps([item.model_dump() for item in results]),
                    executed_at,
                    campaign_id,
                ),
            )
            conn.commit()
            return self._fetch_campaign_by_id(conn, campaign_id)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _fetch_active_contacts(self, conn: sqlite3.Connection, filters: ContactQueryParams) -> list[ContactInfo]:
        query = [
            """
            SELECT zid, name, phone, avatar_url, last_message, unread, identity_key, identity_source, last_seen_at
            FROM contacts
            WHERE is_active = 1
            """
        ]
        params: list[object] = []

        if filters.search:
            query.append("AND LOWER(name) LIKE ?")
            params.append(f"%{filters.search.strip().lower()}%")
        if filters.unread_only:
            query.append("AND unread = 1")
        if filters.identity_source in {"zid", "name_avatar"}:
            query.append("AND identity_source = ?")
            params.append(filters.identity_source)
        if filters.selected_ids:
            selected_ids = self._expand_selected_identity_keys(filters.selected_ids)
            placeholders = ",".join("?" for _ in selected_ids)
            query.append(f"AND identity_key IN ({placeholders})")
            params.extend(selected_ids)

        sort_column = "LOWER(name)"
        if filters.sort_by == "last_seen_at":
            sort_column = "last_seen_at"
        sort_order = "DESC" if filters.sort_order == "desc" else "ASC"
        query.append(f"ORDER BY {sort_column} {sort_order}, identity_key ASC")

        rows = conn.execute(" ".join(query), params).fetchall()
        return [
            ContactInfo(
                zid=row["zid"],
                name=normalize_contact_name(row["name"]) or row["name"],
                phone=row["phone"],
                avatar_url=row["avatar_url"],
                last_message=row["last_message"],
                unread=bool(row["unread"]),
                identity_key=row["identity_key"],
                identity_source=row["identity_source"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    def _fetch_last_sync_run(self, conn: sqlite3.Connection) -> Optional[ContactSyncRunInfo]:
        row = conn.execute(
            """
            SELECT id, sync_status, contact_count, stored_contact_count, message, timestamp, diagnostics_json
            FROM contact_sync_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return self._row_to_sync_run(row) if row else None

    def _row_to_sync_run(self, row: sqlite3.Row) -> ContactSyncRunInfo:
        return ContactSyncRunInfo(
            sync_run_id=row["id"],
            sync_status=row["sync_status"],
            contact_count=row["contact_count"],
            stored_contact_count=row["stored_contact_count"],
            message=row["message"],
            timestamp=row["timestamp"],
            diagnostics=self._coerce_diagnostics(row["diagnostics_json"]),
        )

    def _create_sync_run(
        self,
        conn: sqlite3.Connection,
        sync_status: str,
        timestamp: str,
        contact_count: int,
        stored_contact_count: int,
        message: str,
        diagnostics: ContactSyncDiagnostics,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO contact_sync_runs (
                sync_status, contact_count, stored_contact_count, message, timestamp, diagnostics_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sync_status,
                contact_count,
                stored_contact_count,
                message,
                timestamp,
                json.dumps(diagnostics.model_dump()),
            ),
        )
        return int(cursor.lastrowid)

    def _coerce_contact(self, contact: ContactInfo | dict, timestamp: str) -> ContactInfo:
        if not isinstance(contact, ContactInfo):
            contact = ContactInfo(**contact)
        normalized_name = normalize_contact_name(contact.name)
        identity_key = contact.identity_key or self._identity_key(contact.zid, normalized_name or contact.name, contact.avatar_url)
        identity_source = contact.identity_source or ("zid" if contact.zid else "name_avatar")
        return contact.model_copy(
            update={
                "name": normalized_name or contact.name,
                "identity_key": identity_key,
                "identity_source": identity_source,
                "last_seen_at": timestamp,
            }
        )

    def _coerce_diagnostics(self, diagnostics: ContactSyncDiagnostics | str | dict | None) -> ContactSyncDiagnostics:
        if isinstance(diagnostics, ContactSyncDiagnostics):
            return diagnostics
        if isinstance(diagnostics, str) and diagnostics.strip():
            return ContactSyncDiagnostics(**json.loads(diagnostics))
        if isinstance(diagnostics, dict):
            return ContactSyncDiagnostics(**diagnostics)
        return ContactSyncDiagnostics()

    def _identity_key(self, zid: Optional[str], name: str, avatar_url: Optional[str]) -> str:
        if zid:
            return f"id:{zid}"
        normalized_name = normalize_contact_name(name).lower()
        normalized_avatar = (avatar_url or "").strip().lower()
        return f"name_avatar:{normalized_name}|{normalized_avatar}"

    def _build_stored_message(self, last_run: Optional[ContactSyncRunInfo], count: int, filters: ContactQueryParams) -> str:
        if not last_run:
            return "No contacts have been stored yet. Run a sync to persist your Zalo friend list."
        prefix = f"Loaded {count} stored contact(s)."
        if filters.search or filters.unread_only or filters.identity_source != "all" or filters.selected_ids:
            prefix = f"Loaded {count} filtered contact(s)."
        return f"{prefix} Last sync: {last_run.sync_status} at {last_run.timestamp}."

    def _normalize_filters(self, filters: ContactQueryParams | dict | None) -> ContactQueryParams:
        if isinstance(filters, ContactQueryParams):
            return filters
        if isinstance(filters, dict):
            return ContactQueryParams(**filters)
        return ContactQueryParams()

    def _fetch_campaign_matches(self, conn: sqlite3.Connection, filters: ContactQueryParams) -> list[CampaignContactPreview]:
        contacts = self._fetch_active_contacts(conn, filters)
        return [
            CampaignContactPreview(
                identity_key=item.identity_key or "",
                name=item.name,
                avatar_url=item.avatar_url,
                unread=item.unread,
                identity_source=item.identity_source,
                last_seen_at=item.last_seen_at,
            )
            for item in contacts
        ]

    def _resolve_campaign_selected_contacts(
        self,
        conn: sqlite3.Connection,
        selected_ids: list[str],
        filters: Optional[ContactQueryParams] = None,
    ) -> list[CampaignContactPreview]:
        ordered_ids = [item for item in dict.fromkeys(selected_ids) if item]
        if not ordered_ids:
            return []

        expanded_ids = self._expand_selected_identity_keys(ordered_ids)
        contacts = self._fetch_active_contacts(
            conn,
            ContactQueryParams(
                selected_ids=expanded_ids,
                sort_by=(filters.sort_by if filters else "name"),
                sort_order=(filters.sort_order if filters else "asc"),
            ),
        )
        order_lookup: dict[str, int] = {}
        for index, identity_key in enumerate(ordered_ids):
            for alias in self._identity_key_aliases(identity_key):
                order_lookup.setdefault(alias, index)
        contacts.sort(key=lambda item: order_lookup.get(item.identity_key or "", len(order_lookup)))
        return [
            CampaignContactPreview(
                identity_key=item.identity_key or "",
                name=item.name,
                avatar_url=item.avatar_url,
                unread=item.unread,
                identity_source=item.identity_source,
                last_seen_at=item.last_seen_at,
            )
            for item in contacts
        ]

    def _expand_selected_identity_keys(self, selected_ids: list[str]) -> list[str]:
        expanded: list[str] = []
        seen: set[str] = set()
        for identity_key in selected_ids:
            for alias in self._identity_key_aliases(identity_key):
                if alias and alias not in seen:
                    seen.add(alias)
                    expanded.append(alias)
        return expanded

    def _identity_key_aliases(self, identity_key: str) -> list[str]:
        key = (identity_key or "").strip()
        if not key:
            return []

        aliases = [key]
        normalized_key = self._normalize_identity_key(key)
        if normalized_key != key:
            aliases.append(normalized_key)
        return aliases

    def _normalize_identity_key(self, identity_key: str) -> str:
        if not identity_key.startswith("name_avatar:"):
            return identity_key

        payload = identity_key[len("name_avatar:") :]
        name_part, separator, avatar_part = payload.partition("|")
        if not separator:
            return identity_key

        normalized_name = self._normalize_identity_name(name_part)
        normalized_avatar = avatar_part.strip().lower()
        return f"name_avatar:{normalized_name}|{normalized_avatar}"

    def _normalize_identity_name(self, value: str) -> str:
        normalized_value = " ".join((value or "").strip().lower().split())
        max_prefix = min(4, len(normalized_value) - 1)
        for prefix_len in range(max_prefix, 0, -1):
            prefix = normalized_value[:prefix_len]
            remainder = normalized_value[prefix_len:].lstrip()
            if not prefix.isalpha() or not remainder[:2].isalpha():
                continue
            words = [word for word in remainder.split(" ") if word]
            initials = [word[0] for word in words if word and word[0].isalpha()]
            if not initials:
                continue

            comparisons = {initials[0]}
            if len(initials) >= 2:
                comparisons.add(initials[0] + initials[1])
                comparisons.add(initials[0] + initials[-1])
            if len(initials) >= len(prefix):
                comparisons.add("".join(initials[: len(prefix)]))

            if prefix in comparisons:
                return remainder

        return normalized_value

    def _build_campaign_targets(self, matched_contacts: list[CampaignContactPreview]) -> list[str]:
        return [contact.name for contact in matched_contacts]

    def _fetch_campaign_by_id(self, conn: sqlite3.Connection, campaign_id: int) -> Optional[CampaignInfo]:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return self._row_to_campaign(row) if row else None

    def _row_to_campaign(self, row: sqlite3.Row) -> CampaignInfo:
        filters = self._normalize_filters(json.loads(row["filters_json"] or "{}"))
        matched_contacts = [
            CampaignContactPreview(**item) for item in json.loads(row["matched_contacts_json"] or "[]")
        ]
        results = [CampaignResultItem(**item) for item in json.loads(row["results_json"] or "[]")]
        return CampaignInfo(
            campaign_id=row["id"],
            name=row["name"],
            message=row["message"],
            filters=filters,
            selected_contact_ids=list(json.loads(row["selected_contact_ids_json"] or "[]")),
            matched_contacts=matched_contacts,
            matched_count=row["matched_count"],
            status=row["status"],
            sent_count=row["sent_count"],
            failed_count=row["failed_count"],
            results=results,
            created_at=row["created_at"],
            executed_at=row["executed_at"],
        )


contact_store = ContactStore()
