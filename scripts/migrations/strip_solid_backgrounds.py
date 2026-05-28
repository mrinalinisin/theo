#!/usr/bin/env python3
"""One-time: strip solid-colour backgrounds from all existing product images.

For every locally-stored product image, samples the four corners to detect
a plain studio background. If found, makes those pixels transparent and
resaves as PNG. Updates the product row in the DB if the filename changed.

Safe to re-run -- images that already have a transparent background (alpha
channel with zeros present) are skipped.

Usage:
    cd /Users/sindhus/Desktop/ss_life/Theo
    source venv/bin/activate
    python scripts/migrations/strip_solid_backgrounds.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import numpy as np
from PIL import Image

from app import create_app
from image_store import strip_solid_background
from models import db, Product

app = create_app()

IMAGES_DIR = os.path.join(app.instance_path, "images")


def already_transparent(filepath):
    """Return True if the image already has fully-transparent pixels."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".svg", ".gif", ".avif"):
        return True  # Skip unsupported formats
    try:
        with Image.open(filepath) as img:
            if img.mode != "RGBA":
                return False
            arr = np.array(img)
            return bool((arr[:, :, 3] == 0).any())
    except Exception:
        return False


def is_local_filename(value):
    """True if value is a bare filename (not a URL) stored on disk."""
    if not value or "/" in value or ":" in value:
        return False
    return os.path.isfile(os.path.join(IMAGES_DIR, value))


def process_product(product):
    """Strip backgrounds for all images of a product.

    Returns (changed, old_to_new) where old_to_new maps original
    filenames to their new names (only entries where name changed).
    """
    old_to_new = {}

    for filename in list(product.images or []):
        if not is_local_filename(filename):
            continue

        filepath = os.path.join(IMAGES_DIR, filename)

        if already_transparent(filepath):
            continue

        new_name = strip_solid_background(filepath)

        if new_name != filename:
            old_to_new[filename] = new_name

    return old_to_new


def apply_renames(product, old_to_new):
    """Update product.images and product.image_url with renamed files."""
    if not old_to_new:
        return

    product.images = [
        old_to_new.get(f, f) for f in (product.images or [])
    ]

    if product.image_url and product.image_url in old_to_new:
        product.image_url = old_to_new[product.image_url]


def run():
    with app.app_context():
        products = Product.query.order_by(Product.id).all()
        total = len(products)
        stripped = 0
        skipped = 0
        failed = 0

        print(f"Processing {total} products...\n")

        for idx, product in enumerate(products, 1):
            try:
                old_to_new = process_product(product)
                if old_to_new:
                    apply_renames(product, old_to_new)
                    db.session.add(product)
                    stripped += len(old_to_new)
                    print(
                        f"  [{idx:>4}/{total}] product {product.id:>5} — "
                        f"stripped {len(old_to_new)} image(s): "
                        + ", ".join(f"{k} → {v}" for k, v in old_to_new.items())
                    )
                else:
                    skipped += 1

            except Exception as exc:
                failed += 1
                print(
                    f"  [{idx:>4}/{total}] product {product.id:>5} — ERROR: {exc}",
                    file=sys.stderr,
                )

            # Commit every 50 products to avoid a huge single transaction
            if idx % 50 == 0 or idx == total:
                db.session.commit()

        print(f"\nDone.")
        print(f"  Images stripped : {stripped}")
        print(f"  Products skipped: {skipped}")
        print(f"  Errors          : {failed}")


if __name__ == "__main__":
    run()
