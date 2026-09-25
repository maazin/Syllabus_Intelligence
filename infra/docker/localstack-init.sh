#!/bin/bash
# Runs once LocalStack is ready. `storage.py` does not create the bucket, which
# is correct for production and means every environment has to.
awslocal s3 mb s3://"${R2_BUCKET:-syllabi}" || true
echo "bucket ready: ${R2_BUCKET:-syllabi}"
