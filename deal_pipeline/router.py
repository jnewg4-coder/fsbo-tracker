"""
Deal Pipeline — API Router

All endpoints under /deals, gated by verify_fsbo_admin (X-Admin-Password header).
"""

import hmac
import io
import json
import logging
import os
import re
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse

logger = logging.getLogger("deal_pipeline")

router = APIRouter(tags=["deals"])


# ---------------------------------------------------------------------------
# Auth (same pattern as fsbo_tracker — checks X-Admin-Password header)
# ---------------------------------------------------------------------------
async def verify_fsbo_admin(x_admin_password: str = Header(...)):
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected or not hmac.compare_digest(x_admin_password, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


# ---------------------------------------------------------------------------
# JSON serialization helper
# ---------------------------------------------------------------------------
def _serialize(obj):
    """Make psycopg2 RealDictRow JSON-safe."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_serialize(r) for r in obj]
    if hasattr(obj, "keys"):
        d = dict(obj)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif isinstance(v, Decimal):
                d[k] = float(v)
            elif isinstance(v, bytes):
                d[k] = None  # Never send file_data in list responses
            elif isinstance(v, list):
                d[k] = _serialize(v)
            elif hasattr(v, "keys"):
                d[k] = _serialize(v)
        return d
    return obj


# ---------------------------------------------------------------------------
# Deal CRUD
# ---------------------------------------------------------------------------
@router.get("/deals")
async def list_deals(
    stage: Optional[str] = Query(None),
    side: Optional[str] = Query(None),
    archived: bool = Query(False),
    _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import list_deals as db_list
    try:
        deals = db_list(stage=stage, side=side, archived=archived)
        return {"deals": _serialize(deals), "count": len(deals)}
    except Exception as e:
        logger.exception("list_deals failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/deals")
async def create_deal(
    body: dict,
    _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import create_deal as db_create
    try:
        deal = db_create(body)
        return {"deal": _serialize(deal)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("create_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/deals/from-listing/{listing_id}")
async def create_deal_from_listing(
    listing_id: str,
    _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import create_deal_from_listing as db_promote
    try:
        deal = db_promote(listing_id)
        return {"deal": _serialize(deal)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Catch Postgres unique constraint violation → 409 Conflict
        err_type = type(e).__name__
        if err_type == "UniqueViolation" or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Deal already exists for this listing")
        logger.exception("create_deal_from_listing failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/deals/stats")
async def deal_stats(_auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import get_pipeline_stats
    try:
        stats = get_pipeline_stats()
        return _serialize(stats)
    except Exception as e:
        logger.exception("deal_stats failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/deals/{deal_id}")
async def get_deal(deal_id: str, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import get_deal as db_get
    try:
        deal = db_get(deal_id)
        if not deal:
            raise HTTPException(status_code=404, detail="Deal not found")
        return {"deal": _serialize(deal)}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/deals/{deal_id}")
async def update_deal(deal_id: str, body: dict, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import update_deal as db_update
    try:
        deal = db_update(deal_id, body)
        if not deal:
            raise HTTPException(status_code=404, detail="Deal not found")
        return {"deal": _serialize(deal)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("update_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: str, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import archive_deal
    try:
        ok = archive_deal(deal_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Deal not found")
        return {"status": "archived"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Stage transitions
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/advance")
async def advance_deal(deal_id: str, body: dict, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import advance_deal as db_advance
    target = body.get("target_stage")
    if not target:
        raise HTTPException(status_code=400, detail="target_stage is required")
    try:
        result = db_advance(deal_id, target)
        return {
            "deal": _serialize(result["deal"]),
            "warnings": result["warnings"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("advance_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/deals/{deal_id}/terminate")
async def terminate_deal(deal_id: str, body: dict = None, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import terminate_deal as db_terminate
    reason = (body or {}).get("reason")
    try:
        deal = db_terminate(deal_id, reason)
        return {"deal": _serialize(deal)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("terminate_deal failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/contacts")
async def add_contact(deal_id: str, body: dict, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import add_contact as db_add
    if "role" not in body:
        raise HTTPException(status_code=400, detail="role is required")
    try:
        contact = db_add(deal_id, body)
        return {"contact": _serialize(contact)}
    except Exception as e:
        logger.exception("add_contact failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/deals/{deal_id}/contacts/{contact_id}")
async def update_contact(deal_id: str, contact_id: str, body: dict, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import update_contact as db_update
    try:
        contact = db_update(deal_id, contact_id, body)
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        return {"contact": _serialize(contact)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("update_contact failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/deals/{deal_id}/contacts/{contact_id}")
async def delete_contact(deal_id: str, contact_id: str, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import delete_contact as db_delete
    try:
        ok = db_delete(deal_id, contact_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Contact not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete_contact failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/documents")
async def upload_document(
    deal_id: str,
    file: UploadFile = File(...),
    stage: str = Form(None),
    doc_type: str = Form("other"),
    _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import upload_document as db_upload
    from deal_pipeline.config import MAX_FILE_SIZE
    try:
        # Read in chunks to enforce size limit before full memory allocation
        chunks = []
        total = 0
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                raise ValueError(f"File too large (>{MAX_FILE_SIZE // (1024*1024)}MB)")
            chunks.append(chunk)
        file_data = b"".join(chunks)
        # Sanitize filename: strip path components, null bytes, limit length
        safe_filename = os.path.basename(file.filename or "upload")
        safe_filename = safe_filename.replace("\x00", "")
        safe_filename = re.sub(r'[<>:"/\\|?*]', '_', safe_filename)
        safe_filename = safe_filename[:255] or "upload"
        doc = db_upload(
            deal_id=deal_id,
            stage=stage,
            doc_type=doc_type,
            filename=safe_filename,
            mime_type=file.content_type,
            file_size=len(file_data),
            file_data=file_data,
        )
        return {"document": _serialize(doc)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("upload_document failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/deals/{deal_id}/documents/{doc_id}/download")
async def download_document(deal_id: str, doc_id: str, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import get_document
    try:
        doc = get_document(deal_id, doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        # Sanitize filename to prevent header injection
        safe_name = (doc.get("filename") or "download").replace('"', '_').replace('\n', '_').replace('\r', '_')
        return StreamingResponse(
            io.BytesIO(doc["file_data"]),
            media_type=doc.get("mime_type", "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("download_document failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/deals/{deal_id}/documents/{doc_id}")
async def delete_document(deal_id: str, doc_id: str, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import delete_document as db_delete
    try:
        ok = db_delete(deal_id, doc_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete_document failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/inspections")
async def add_inspection(deal_id: str, body: dict, _auth=Depends(verify_fsbo_admin)):
    from deal_pipeline.db import add_inspection as db_add
    if "inspection_type" not in body:
        raise HTTPException(status_code=400, detail="inspection_type is required")
    try:
        insp = db_add(deal_id, body)
        return {"inspection": _serialize(insp)}
    except Exception as e:
        logger.exception("add_inspection failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/deals/{deal_id}/inspections/{inspection_id}")
async def update_inspection(
    deal_id: str, inspection_id: str, body: dict, _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import update_inspection as db_update
    try:
        insp = db_update(deal_id, inspection_id, body)
        if not insp:
            raise HTTPException(status_code=404, detail="Inspection not found")
        return {"inspection": _serialize(insp)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("update_inspection failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# AI Inspection Analysis (placeholder — Phase 3)
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/analyze-inspection/{doc_id}")
async def analyze_inspection(deal_id: str, doc_id: str, _auth=Depends(verify_fsbo_admin)):
    raise HTTPException(
        status_code=501,
        detail="AI Inspection Analysis is not yet implemented. Coming in Phase 3.",
    )


# ---------------------------------------------------------------------------
# Offer Drafts (shell only — AI generation is Phase 3)
# ---------------------------------------------------------------------------
@router.post("/deals/{deal_id}/offer-draft")
async def create_offer_draft(
    deal_id: str,
    body: dict = None,
    _auth=Depends(verify_fsbo_admin),
):
    from deal_pipeline.db import create_offer_draft as db_create
    draft_type = (body or {}).get("draft_type", "purchase_offer")
    try:
        draft = db_create(deal_id, draft_type)
        return {"draft": _serialize(draft)}
    except Exception as e:
        logger.exception("create_offer_draft failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/deals/{deal_id}/offer-draft/{draft_id}/generate")
async def generate_offer_draft(
    deal_id: str, draft_id: str, _auth=Depends(verify_fsbo_admin),
):
    raise HTTPException(
        status_code=501,
        detail="AI Offer Writer is not yet implemented. Coming in Phase 3.",
    )
