"""Object storage client (PRD sections 7, 18.1).

R2 in production, MinIO locally, one code path: R2 implements the S3 API, so
the boto3 calls and the `R2_*` variable names are identical in both places.

This lives in the shared library rather than in the API because both services
touch the store: the API writes an upload, the worker reads it back to parse.
It used to live in `services/api/storage.py` and the worker imported it from
there, which worked on a developer machine where the whole tree is on the
path and would have failed on the first parse in production, where the worker
image contains only `services/worker` and this package.
"""

from __future__ import annotations

import functools
import os
from typing import Any

import boto3
from botocore.config import Config


@functools.lru_cache(maxsize=1)
def client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT", "http://localhost:9100"),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "minioadmin"),
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def bucket() -> str:
    return os.environ.get("R2_BUCKET", "syllabi")


def put(key: str, data: bytes, content_type: str) -> None:
    client().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)


def get(key: str) -> bytes:
    body = client().get_object(Bucket=bucket(), Key=key)["Body"].read()
    return bytes(body)


def delete(key: str) -> None:
    client().delete_object(Bucket=bucket(), Key=key)
