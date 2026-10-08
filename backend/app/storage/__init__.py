"""Storage helpers for ReviewLens AI.

The ``keys`` sub-module is the single source of truth for S3 object keys.
Import from there directly::

    from app.storage import keys

    key = keys.dataset_reviews(dataset_id, version)

The ``s3`` sub-module reads and writes object bytes for those keys::

    from app.storage import keys, s3

    s3.put_bytes(keys.check_page(check_id, item_id), html, content_type="text/html")
"""

from app.storage import keys, s3

__all__ = ["keys", "s3"]
