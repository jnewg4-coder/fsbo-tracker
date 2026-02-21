"""
Deal Pipeline — Database layer.

Reuses the FSBO tracker's Postgres connection (FSBO_DATABASE_URL).
All functions use psycopg2 with RealDictCursor for consistency.
"""

import json
import os
from contextlib import contextmanager
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor


def get_conn():
    url = os.environ.get("FSBO_DATABASE_URL")
    if not url:
        raise RuntimeError("FSBO_DATABASE_URL is not set.")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


@contextmanager
def db_cursor(commit=True):
    conn = get_conn()
    cur = conn.cursor()
    try:
        yield conn, cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Migration runner (deal_pipeline/migrations/ directory)
# ---------------------------------------------------------------------------
def run_migration():
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    migration_files = sorted(
        f for f in os.listdir(migrations_dir) if f.endswith(".sql")
    )
    with db_cursor() as (conn, cur):
        for mf in migration_files:
            path = os.path.join(migrations_dir, mf)
            with open(path) as f:
                sql = f.read()
            cur.execute(sql)
            print(f"[deal_pipeline] Migration {mf} applied")


# ---------------------------------------------------------------------------
# Activity log helper
# ---------------------------------------------------------------------------
def _log_activity(cur, deal_id: str, action: str, detail: str = None,
                  old_value: str = None, new_value: str = None):
    cur.execute("""
        INSERT INTO deal_activity_log (deal_id, action, detail, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s)
    """, (deal_id, action, detail, old_value, new_value))


# ---------------------------------------------------------------------------
# Deal CRUD
# ---------------------------------------------------------------------------
_DEAL_INSERT_FIELDS = [
    "listing_id", "side", "stage_profile",
    "address", "city", "state", "zip_code", "latitude", "longitude",
    "beds", "baths", "sqft", "year_built", "property_type",
    "list_price", "assessed_value", "zestimate", "redfin_estimate",
    "flood_zone", "photo_urls", "photo_analysis_json", "geo_risk_json",
    "source_links", "seller_name", "seller_phone", "seller_email", "seller_broker",
    "stage", "offer_price", "offer_date", "notes", "tags", "workflow_state",
]

# All fields that can be updated via PATCH
_DEAL_PATCH_FIELDS = [
    "address", "city", "state", "zip_code", "latitude", "longitude",
    "beds", "baths", "sqft", "year_built", "property_type",
    "list_price", "assessed_value", "zestimate", "redfin_estimate",
    "flood_zone", "photo_urls", "photo_analysis_json", "geo_risk_json",
    "source_links", "seller_name", "seller_phone", "seller_email", "seller_broker",
    "offer_price", "offer_date", "offer_expiration_date", "contingencies",
    "emd_amount", "emd_due_date", "acceptance_date",
    "contract_signed_date", "binding_date", "bind_notice_sent", "bind_notice_date",
    "disclosures_received", "disclosures_date",
    "title_company", "title_officer_name", "title_officer_phone", "title_officer_email",
    "title_ordered_date", "title_received_date", "survey_ordered_date", "survey_received_date",
    "dd_period_days", "dd_start_date", "dd_end_date", "dd_status", "ccr_review_status",
    "retrade_requested", "retrade_date", "original_price", "retrade_price",
    "credit_requested", "retrade_items", "retrade_status", "retrade_counter_price",
    "clear_to_close_date", "final_walkthrough_date", "final_walkthrough_status",
    "hud_review_status", "deed_review_status", "wire_instructions_received", "cash_due_at_close",
    "closing_date", "final_purchase_price", "total_closing_costs",
    "deed_recorded", "deed_recorded_date", "alta_received",
    "final_hud_received", "all_docs_clear",
    "notes", "tags", "workflow_state",
]


def create_deal(data: dict) -> dict:
    """Insert a new deal. Returns the full deal row."""
    from deal_pipeline.config import get_stage_config

    # Validate and normalize side + stage_profile
    side = data.get("side", "BUY").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {side}")
    data["side"] = side

    profile_map = {"BUY": "buy_v1", "SELL": "sell_v1"}
    data["stage_profile"] = profile_map[side]
    profile = data["stage_profile"]

    # Always force the first stage — never accept client-supplied stage
    stages, _, _ = get_stage_config(profile)
    data["stage"] = stages[0]["id"]

    fields = [f for f in _DEAL_INSERT_FIELDS if f in data and data[f] is not None]
    if "address" not in fields:
        raise ValueError("address is required")

    cols = ", ".join(fields)
    placeholders = ", ".join(["%s"] * len(fields))
    values = [data[f] for f in fields]

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            INSERT INTO deals ({cols})
            VALUES ({placeholders})
            RETURNING *
        """, values)
        deal = cur.fetchone()
        _log_activity(cur, str(deal["id"]), "deal_created",
                      f"Deal created: {data.get('side', 'BUY')} — {data['address']}")
        return deal


def create_deal_from_listing(listing_id: str) -> dict:
    """Promote an FSBO listing to a BUY deal. Auto-fills from listing data."""
    with db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM fsbo_listings WHERE id = %s", (listing_id,))
        listing = cur.fetchone()
        if not listing:
            raise ValueError(f"Listing {listing_id} not found")

        # Check for existing deal (lock row to prevent race condition)
        cur.execute("SELECT id FROM deals WHERE listing_id = %s AND archived = FALSE FOR UPDATE", (listing_id,))
        existing = cur.fetchone()
        if existing:
            raise ValueError(f"Active deal already exists for listing {listing_id}")

        # Build source_links JSON
        source_links = {}
        if listing.get("redfin_url"):
            source_links["redfin_url"] = listing["redfin_url"]
        if listing.get("zillow_url"):
            source_links["zillow_url"] = listing["zillow_url"]

        cur.execute("""
            INSERT INTO deals (
                listing_id, side, stage_profile, address, city, state, zip_code,
                latitude, longitude, beds, baths, sqft, year_built, property_type,
                list_price, assessed_value, zestimate, redfin_estimate,
                flood_zone, photo_urls, photo_analysis_json,
                source_links, seller_name, seller_phone, seller_email, seller_broker,
                stage, offer_price
            ) VALUES (
                %s, 'BUY', 'buy_v1', %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s,
                'offer', %s
            )
            RETURNING *
        """, (
            listing_id,
            listing.get("address"), listing.get("city"), listing.get("state"), listing.get("zip_code"),
            listing.get("latitude"), listing.get("longitude"),
            listing.get("beds"), listing.get("baths"), listing.get("sqft"),
            listing.get("year_built"), listing.get("property_type"),
            listing.get("price"), listing.get("assessed_value"),
            listing.get("zestimate"), listing.get("redfin_estimate"),
            listing.get("flood_zone"),
            listing.get("photo_urls"),
            listing.get("photo_analysis_json"),
            json.dumps(source_links) if source_links else None,
            listing.get("seller_name"), listing.get("seller_phone"),
            listing.get("seller_email"), listing.get("seller_broker"),
            listing.get("price"),
        ))
        deal = cur.fetchone()
        _log_activity(cur, str(deal["id"]), "deal_created",
                      f"Promoted from listing {listing_id}")
        return deal


def get_deal(deal_id: str) -> dict:
    """Get a single deal with all related data."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT * FROM deals WHERE id = %s", (deal_id,))
        deal = cur.fetchone()
        if not deal:
            return None

        cur.execute("SELECT * FROM deal_contacts WHERE deal_id = %s ORDER BY created_at", (deal_id,))
        deal["contacts"] = cur.fetchall()

        cur.execute("""
            SELECT id, deal_id, stage, doc_type, filename, mime_type, file_size, uploaded_at, ai_analysis_json
            FROM deal_documents WHERE deal_id = %s ORDER BY uploaded_at
        """, (deal_id,))
        deal["documents"] = cur.fetchall()

        cur.execute("SELECT * FROM deal_inspections WHERE deal_id = %s ORDER BY created_at", (deal_id,))
        deal["inspections"] = cur.fetchall()

        cur.execute("""
            SELECT * FROM deal_activity_log WHERE deal_id = %s ORDER BY created_at DESC LIMIT 50
        """, (deal_id,))
        deal["activity"] = cur.fetchall()

        cur.execute("SELECT * FROM offer_drafts WHERE deal_id = %s ORDER BY created_at DESC", (deal_id,))
        deal["offer_drafts"] = cur.fetchall()

        return deal


def list_deals(stage: str = None, side: str = None, archived: bool = False) -> list:
    """List deals with optional filters."""
    with db_cursor(commit=False) as (conn, cur):
        clauses = ["archived = %s"]
        params = [archived]
        if stage:
            clauses.append("stage = %s")
            params.append(stage)
        if side:
            clauses.append("side = %s")
            params.append(side)
        where = " AND ".join(clauses)
        cur.execute(f"""
            SELECT id, listing_id, side, stage_profile, address, city, state, zip_code,
                   stage, stage_changed_at, list_price, offer_price, final_purchase_price,
                   offer_expiration_date, emd_due_date, dd_end_date, closing_date,
                   emd_amount, notes, tags, created_at, updated_at
            FROM deals
            WHERE {where}
            ORDER BY updated_at DESC
        """, params)
        return cur.fetchall()


def update_deal(deal_id: str, data: dict) -> dict:
    """Partial update of deal fields. Returns updated deal.

    Special handling for workflow_state: merges incoming keys into existing JSON
    instead of overwriting (allows partial sub-task status updates).
    """
    # Handle workflow_state merge: read existing, merge, store full JSON
    if "workflow_state" in data and isinstance(data["workflow_state"], dict):
        with db_cursor(commit=False) as (conn, cur):
            cur.execute("SELECT workflow_state FROM deals WHERE id = %s", (deal_id,))
            row = cur.fetchone()
            if row:
                existing = {}
                if row.get("workflow_state"):
                    try:
                        existing = json.loads(row["workflow_state"])
                    except (json.JSONDecodeError, TypeError):
                        existing = {}
                existing.update(data["workflow_state"])
                data["workflow_state"] = json.dumps(existing)
            else:
                data["workflow_state"] = json.dumps(data["workflow_state"])

    fields = [f for f in _DEAL_PATCH_FIELDS if f in data]
    if not fields:
        raise ValueError("No valid fields to update")

    set_clauses = [f"{f} = %s" for f in fields]
    set_clauses.append("updated_at = %s")
    values = [data[f] for f in fields]
    values.append(datetime.utcnow())
    values.append(deal_id)

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            UPDATE deals SET {', '.join(set_clauses)}
            WHERE id = %s
            RETURNING *
        """, values)
        deal = cur.fetchone()
        if not deal:
            return None

        # Log changed fields
        for f in fields:
            _log_activity(cur, deal_id, "field_update", f"Updated {f}",
                          new_value=str(data[f]) if data[f] is not None else None)

        return deal


def archive_deal(deal_id: str) -> bool:
    """Soft-delete a deal by archiving it."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE deals SET archived = TRUE, updated_at = %s WHERE id = %s RETURNING id
        """, (datetime.utcnow(), deal_id))
        row = cur.fetchone()
        if row:
            _log_activity(cur, deal_id, "deal_archived", "Deal archived")
        return row is not None


# ---------------------------------------------------------------------------
# Stage transitions
# ---------------------------------------------------------------------------
def advance_deal(deal_id: str, target_stage: str) -> dict:
    """Advance a deal to the next stage with validation. Returns (deal, warnings)."""
    from deal_pipeline.config import get_stage_config, ADVANCE_WARNINGS

    with db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM deals WHERE id = %s FOR UPDATE", (deal_id,))
        deal = cur.fetchone()
        if not deal:
            raise ValueError("Deal not found")

        current = deal["stage"]
        profile = deal["stage_profile"]
        stages, transitions, requirements = get_stage_config(profile)

        # Validate transition
        valid_next = transitions.get(current, [])
        if target_stage not in valid_next:
            raise ValueError(
                f"Cannot move from '{current}' to '{target_stage}'. "
                f"Valid transitions: {valid_next}"
            )

        # Check hard requirements
        required = requirements.get(target_stage, [])
        missing = [f for f in required if not deal.get(f)]
        if missing:
            raise ValueError(
                f"Cannot advance to '{target_stage}': missing required fields: {missing}"
            )

        # Status-based logic gates (buy_v1 specific)
        if profile == "buy_v1":
            if current == "due_diligence" and target_stage == "retrade":
                dd = deal.get("dd_status")
                if dd not in ("issue", "retrade_needed"):
                    raise ValueError(
                        f"Cannot retrade: dd_status must be 'issue' or 'retrade_needed', got '{dd}'"
                    )
            elif current == "due_diligence" and target_stage == "clear_to_close":
                dd = deal.get("dd_status")
                if dd != "clear":
                    raise ValueError(
                        f"Cannot clear to close from DD: dd_status must be 'clear', got '{dd}'"
                    )
            elif current == "retrade" and target_stage == "clear_to_close":
                rs = deal.get("retrade_status")
                if rs not in ("accepted", "countered"):
                    raise ValueError(
                        f"Cannot clear to close from retrade: retrade_status must be 'accepted' or 'countered', got '{rs}'"
                    )

        # Check soft warnings (non-blocking)
        warnings = []
        warn_fields = ADVANCE_WARNINGS.get(target_stage, [])
        for f in warn_fields:
            if not deal.get(f):
                warnings.append(f"Recommended but missing: {f}")

        # Do the transition
        now = datetime.utcnow()
        cur.execute("""
            UPDATE deals SET stage = %s, stage_changed_at = %s, updated_at = %s
            WHERE id = %s RETURNING *
        """, (target_stage, now, now, deal_id))
        updated = cur.fetchone()

        _log_activity(cur, deal_id, "stage_change",
                      f"Stage: {current} → {target_stage}",
                      old_value=current, new_value=target_stage)

        return {"deal": updated, "warnings": warnings}


def terminate_deal(deal_id: str, reason: str = None) -> dict:
    """Kill a deal from any stage."""
    with db_cursor() as (conn, cur):
        cur.execute("SELECT stage FROM deals WHERE id = %s", (deal_id,))
        deal = cur.fetchone()
        if not deal:
            raise ValueError("Deal not found")

        old_stage = deal["stage"]
        if old_stage in ("closed", "terminated"):
            raise ValueError(f"Cannot terminate a deal in '{old_stage}' stage")

        now = datetime.utcnow()
        cur.execute("""
            UPDATE deals SET stage = 'terminated', stage_changed_at = %s, updated_at = %s,
                             notes = CASE WHEN %s IS NOT NULL
                                          THEN COALESCE(notes || E'\n', '') || 'Terminated: ' || %s
                                          ELSE notes END
            WHERE id = %s RETURNING *
        """, (now, now, reason, reason, deal_id))
        updated = cur.fetchone()

        _log_activity(cur, deal_id, "stage_change",
                      f"Terminated from {old_stage}" + (f": {reason}" if reason else ""),
                      old_value=old_stage, new_value="terminated")

        return updated


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
def add_contact(deal_id: str, data: dict) -> dict:
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO deal_contacts (deal_id, role, name, phone, email, company, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (deal_id, data["role"], data.get("name"), data.get("phone"),
              data.get("email"), data.get("company"), data.get("notes")))
        contact = cur.fetchone()
        _log_activity(cur, deal_id, "contact_add",
                      f"Added {data['role']}: {data.get('name', 'unnamed')}")
        return contact


def update_contact(deal_id: str, contact_id: str, data: dict) -> dict:
    fields = [f for f in ("role", "name", "phone", "email", "company", "notes") if f in data]
    if not fields:
        raise ValueError("No valid fields to update")
    set_clauses = [f"{f} = %s" for f in fields]
    values = [data[f] for f in fields]
    values.extend([contact_id, deal_id])

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            UPDATE deal_contacts SET {', '.join(set_clauses)}
            WHERE id = %s AND deal_id = %s RETURNING *
        """, values)
        return cur.fetchone()


def delete_contact(deal_id: str, contact_id: str) -> bool:
    with db_cursor() as (conn, cur):
        cur.execute(
            "DELETE FROM deal_contacts WHERE id = %s AND deal_id = %s RETURNING deal_id, role, name",
            (contact_id, deal_id),
        )
        row = cur.fetchone()
        if row:
            _log_activity(cur, str(row["deal_id"]), "contact_delete",
                          f"Removed {row['role']}: {row.get('name', 'unnamed')}")
        return row is not None


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def upload_document(deal_id: str, stage: str, doc_type: str,
                    filename: str, mime_type: str, file_size: int,
                    file_data: bytes) -> dict:
    from deal_pipeline.config import MAX_FILE_SIZE, MAX_DEAL_STORAGE, ALLOWED_MIME_TYPES

    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File too large: {file_size} bytes (max {MAX_FILE_SIZE})")
    if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"File type not allowed: {mime_type}")

    with db_cursor() as (conn, cur):
        # Check total storage for this deal
        cur.execute("SELECT COALESCE(SUM(file_size), 0) FROM deal_documents WHERE deal_id = %s", (deal_id,))
        current_usage = cur.fetchone()["coalesce"]
        if current_usage + file_size > MAX_DEAL_STORAGE:
            raise ValueError(
                f"Deal storage limit exceeded: {current_usage + file_size} > {MAX_DEAL_STORAGE} bytes"
            )

        cur.execute("""
            INSERT INTO deal_documents (deal_id, stage, doc_type, filename, mime_type, file_size, file_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, deal_id, stage, doc_type, filename, mime_type, file_size, uploaded_at
        """, (deal_id, stage, doc_type, filename, mime_type, file_size,
              psycopg2.Binary(file_data)))
        doc = cur.fetchone()
        _log_activity(cur, deal_id, "doc_upload", f"Uploaded {doc_type}: {filename}")
        return doc


def get_document(deal_id: str, doc_id: str) -> dict:
    """Get document including file_data for download."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("SELECT * FROM deal_documents WHERE id = %s AND deal_id = %s", (doc_id, deal_id))
        return cur.fetchone()


def delete_document(deal_id: str, doc_id: str) -> bool:
    with db_cursor() as (conn, cur):
        cur.execute("""
            DELETE FROM deal_documents WHERE id = %s AND deal_id = %s
            RETURNING deal_id, doc_type, filename
        """, (doc_id, deal_id))
        row = cur.fetchone()
        if row:
            _log_activity(cur, str(row["deal_id"]), "doc_delete",
                          f"Deleted {row['doc_type']}: {row['filename']}")
        return row is not None


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------
def add_inspection(deal_id: str, data: dict) -> dict:
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO deal_inspections (
                deal_id, inspection_type, inspector_name, inspector_phone,
                inspector_email, inspector_company, ordered_date, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            deal_id, data["inspection_type"],
            data.get("inspector_name"), data.get("inspector_phone"),
            data.get("inspector_email"), data.get("inspector_company"),
            data.get("ordered_date"), data.get("status", "pending"),
        ))
        insp = cur.fetchone()
        _log_activity(cur, deal_id, "inspection_add",
                      f"Added {data['inspection_type']} inspection")
        return insp


def update_inspection(deal_id: str, inspection_id: str, data: dict) -> dict:
    fields = [f for f in (
        "inspection_type", "inspector_name", "inspector_phone",
        "inspector_email", "inspector_company", "ordered_date", "completed_date",
        "status", "report_doc_id", "findings_json"
    ) if f in data]
    if not fields:
        raise ValueError("No valid fields to update")
    set_clauses = [f"{f} = %s" for f in fields]
    values = [data[f] for f in fields]
    values.extend([inspection_id, deal_id])

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            UPDATE deal_inspections SET {', '.join(set_clauses)}
            WHERE id = %s AND deal_id = %s RETURNING *
        """, values)
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Offer Drafts
# ---------------------------------------------------------------------------
def create_offer_draft(deal_id: str, draft_type: str = "purchase_offer") -> dict:
    """Create an empty shell draft record. AI generation is Phase 3."""
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO offer_drafts (deal_id, draft_type, status, generated_by)
            VALUES (%s, %s, 'draft', 'manual')
            RETURNING *
        """, (deal_id, draft_type))
        draft = cur.fetchone()
        _log_activity(cur, deal_id, "offer_draft_created",
                      f"Created {draft_type} draft (shell)")
        return draft


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def get_pipeline_stats() -> dict:
    """Pipeline summary counts by side and stage."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT side, stage, COUNT(*) as count
            FROM deals
            WHERE archived = FALSE
            GROUP BY side, stage
            ORDER BY side, stage
        """)
        rows = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE stage = 'closed') as closed,
                   COUNT(*) FILTER (WHERE stage = 'terminated') as terminated
            FROM deals WHERE archived = FALSE
        """)
        totals = cur.fetchone()

        return {"by_side_stage": rows, "totals": totals}
