#!/usr/bin/env python3
"""One-time: compute perceptual hashes for all existing product images.

Safe to re-run -- images that already have a hash row are skipped.

Usage:
    python scripts/backfill_image_hashes.py
"""

import sys
import os

# Ensure project root is on sys.path so imports work when run from any CWD.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app
from models import db, Product, ImageHash
from image_store import compute_image_hash

app = create_app()


def backfill():
    products = Product.query.all()
    total_products = len(products)
    total_hashed = 0
    total_skipped = 0
    total_failed = 0

    images_dir = os.path.join(app.instance_path, "images")

    for idx, product in enumerate(products, 1):
        for filename in (product.images or []):
            # Skip if already hashed
            existing = ImageHash.query.filter_by(
                product_id=product.id, filename=filename
            ).first()
            if existing:
                total_skipped += 1
                continue

            filepath = os.path.join(images_dir, filename)
            if not os.path.isfile(filepath):
                total_failed += 1
                print(f"  MISSING: product {product.id} — {filename}", file=sys.stderr)
                continue

            phash = compute_image_hash(filepath)
            if phash:
                db.session.add(ImageHash(
                    product_id=product.id,
                    filename=filename,
                    phash=phash,
                ))
                total_hashed += 1
            else:
                total_failed += 1
                print(f"  FAIL: product {product.id} — {filename}", file=sys.stderr)

        # Commit in batches
        if idx % 50 == 0 or idx == total_products:
            db.session.commit()
            print(f"  [{idx}/{total_products}] processed")

    # Final commit for any remaining
    db.session.commit()

    print(f"\nDone! {total_products} products processed.")
    print(f"  Hashed       : {total_hashed}")
    print(f"  Already hashed: {total_skipped}")
    print(f"  Failed       : {total_failed}")


if __name__ == "__main__":
    with app.app_context():
        backfill()
