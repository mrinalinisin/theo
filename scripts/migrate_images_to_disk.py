#!/usr/bin/env python3
"""One-time migration: download/decode all product images to instance/images/.

Safe to re-run — already-migrated images (bare filenames on disk) are skipped.

Usage:
    python scripts/migrate_images_to_disk.py
"""

import sys
import os

# Ensure project root is on sys.path so imports work when run from any CWD.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app
from models import db, Product
from image_store import ensure_image_dir, save_image

app = create_app()


def migrate():
    ensure_image_dir(app)

    products = Product.query.all()
    total_products = len(products)
    total_saved = 0
    total_failed = 0
    total_skipped = 0

    for idx, product in enumerate(products, 1):
        old_images = list(product.images or [])
        old_main = product.image_url or ""

        # Build a mapping from old value → new filename
        old_to_new = {}

        # --- Migrate images list ---
        new_images = []
        for i, img in enumerate(old_images):
            fname = save_image(img, product.id, i, app)
            if fname:
                old_to_new[img] = fname
                new_images.append(fname)
                if fname != img:
                    total_saved += 1
                else:
                    total_skipped += 1
            else:
                total_failed += 1
                print(f"  FAIL: product {product.id} image[{i}]", file=sys.stderr)

        # --- Migrate main image_url ---
        if old_main:
            if old_main in old_to_new:
                # Already saved as part of the images list
                new_main = old_to_new[old_main]
            else:
                # Standalone main image not in the list — save separately
                fname = save_image(old_main, product.id, "main", app)
                if fname:
                    new_main = fname
                    if fname != old_main:
                        total_saved += 1
                    else:
                        total_skipped += 1
                else:
                    total_failed += 1
                    new_main = ""
                    print(f"  FAIL: product {product.id} image_url", file=sys.stderr)
        else:
            new_main = new_images[0] if new_images else ""

        # Apply changes
        if new_images != old_images:
            product.images = new_images
        if new_main != old_main:
            product.image_url = new_main
        db.session.commit()

        if idx % 10 == 0 or idx == total_products:
            print(f"  [{idx}/{total_products}] processed")

    print(f"\nDone! {total_products} products processed.")
    print(f"  Saved to disk : {total_saved}")
    print(f"  Already on disk: {total_skipped}")
    print(f"  Failed         : {total_failed}")


if __name__ == "__main__":
    with app.app_context():
        migrate()
