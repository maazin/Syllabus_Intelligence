"""Object storage and content-hash dedup — PRD sections 7, 18.1, 18.3.

R2 in production, MinIO locally, one code path: R2 implements the S3 API, so
the boto3 calls and the `R2_*` variable names are identical in both places.

Dedup is load-bearing rather than an optimization (18.3). Dozens of students in
a section upload the identical file; parsing once per section is what keeps
token spend, Neon compute-hours, and R2 operations inside the free tiers during
week 0 — and it means later uploaders get instant results.
"""

from __future__ import annotations

import functools
import logging
import os
import uuid

import boto3
from botocore.config import Config
from db.models import SyllabusDocument, User
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT", "http://localhost:9100"),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "minioadmin"),
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket() -> str:
    return os.environ.get("R2_BUCKET", "syllabi")


def content_hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def guess_mime(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def store_document(
    db: Session,
    *,
    user: User,
    filename: str,
    data: bytes,
    section_id: uuid.UUID | None = None,
) -> tuple[SyllabusDocument, bool]:
    """Persist an upload, reusing an existing parse when the bytes match.

    Returns (document, deduped). When `deduped` is True the caller should not
    enqueue a parse — the extraction already exists and can be reused for this
    student immediately.
    """
    sha = content_hash(data)

    existing = db.scalar(select(SyllabusDocument).where(SyllabusDocument.sha256 == sha).limit(1))
    if existing is not None:
        if existing.uploader_user_id == user.id:
            return existing, True

        # Same bytes, different student. Create a row so this student owns their
        # own provenance record and their own overrides, but point at the same
        # stored object and skip re-parsing. The uploaded file itself is still
        # never served to anyone (15.1).
        document = SyllabusDocument(
            uploader_user_id=user.id,
            section_id=section_id or existing.section_id,
            storage_key=existing.storage_key,
            sha256=sha,
            mime=existing.mime,
            page_count=existing.page_count,
            source="upload",
            is_scanned=existing.is_scanned,
            visibility="private",
        )
        db.add(document)
        db.flush()
        logger.info("Deduped %s to existing parse (sha %s)", filename, sha[:12])
        return document, True

    storage_key = f"documents/{sha[:2]}/{sha}"
    try:
        _s3().put_object(
            Bucket=_bucket(),
            Key=storage_key,
            Body=data,
            ContentType=guess_mime(filename),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not store the uploaded file: {exc}") from exc

    document = SyllabusDocument(
        uploader_user_id=user.id,
        section_id=section_id,
        storage_key=storage_key,
        sha256=sha,
        mime=guess_mime(filename),
        source="upload",
        visibility="private",
    )
    db.add(document)
    db.flush()
    return document, False


def fetch_document(storage_key: str) -> bytes:
    return _s3().get_object(Bucket=_bucket(), Key=storage_key)["Body"].read()


def delete_document(storage_key: str) -> None:
    """Used by account deletion (15.2), which removes documents outright."""
    _s3().delete_object(Bucket=_bucket(), Key=storage_key)
