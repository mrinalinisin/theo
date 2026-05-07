#!/usr/bin/env python3
"""One-time: mark items with an expected_delivery_at before today as received.

For every Purchase (in `awaiting_delivery` OR `purchased` status) whose
expected_delivery_at falls before today's date and whose delivered_at is
still NULL, this script:

  1. Stamps purchase.delivered_at = purchase.expected_delivery_at
     (best estimate — the item was *expected* to arrive on that day).
  2. If the product is in 'awaiting_delivery', flips it to 'purchased'.
     Already-purchased items keep their status and just get the stamp.

Items with delivered_at already set are skipped — re-running is a no-op
for them.

Usage:

    python scripts/mark_overdue_as_received.py            # dry-run, prints what it'd do
    python scripts/mark_overdue_as_received.py --apply    # commits the changes
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app
from models import db, Product, Purchase

app = create_app()


def main(apply_changes: bool):
    today = date.today()
    print(f"Today is {today.isoformat()}.")

    rows = (
        Purchase.query
        .join(Product, Purchase.product_id == Product.id)
        .filter(Product.status.in_(("purchased", "shipped", "received")))
        .filter(Purchase.delivered_at.is_(None))
        .filter(Purchase.expected_delivery_at.isnot(None))
        .filter(Purchase.expected_delivery_at < today)
        .order_by(Purchase.expected_delivery_at.asc())
        .all()
    )

    if not rows:
        print("Nothing to migrate. No items with an overdue expected date and a missing delivered_at.")
        return

    n_status_flip = sum(1 for p in rows if p.product and p.product.status in ("purchased", "shipped"))
    n_stamp_only = len(rows) - n_status_flip
    print(f"\n{len(rows)} item{'s' if len(rows) != 1 else ''} qualify:")
    print(f"  - {n_stamp_only} purchased items will get delivered_at stamped only")
    print(f"  - {n_status_flip} awaiting items will be flipped to purchased + stamped\n")
    for p in rows:
        edate = p.expected_delivery_at.date() if p.expected_delivery_at else None
        name = p.product.name if p.product else "(missing product)"
        action = "flip + stamp" if (p.product and p.product.status in ("purchased", "shipped")) else "stamp only"
        print(f"  [{p.id:5}] expected {edate}  →  {action}, delivered_at = {edate}")
        print(f"           {name[:80]}")

    if not apply_changes:
        print("\nDry-run only. Re-run with --apply to commit.")
        return

    print("\nApplying...")
    for p in rows:
        # Stamp delivered_at to the expected date — best estimate; the user can
        # always edit per-item later if a more accurate date is known.
        p.delivered_at = p.expected_delivery_at
        if p.product and p.product.status in ("purchased", "shipped"):
            p.product.status = "received"
    db.session.commit()
    print(f"Done. Updated {len(rows)} purchase{'s' if len(rows) != 1 else ''}.")


if __name__ == "__main__":
    apply_changes = "--apply" in sys.argv
    with app.app_context():
        main(apply_changes)
