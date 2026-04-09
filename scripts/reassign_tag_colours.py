"""One-shot: reassign every existing tag a distinct colour from the palette.

Run from the repo root:
    python scripts/reassign_tag_colours.py

Uses the same palette as app._pick_random_tag_colour; if there are more tags
than palette entries, falls back to HSL-generated colours.
"""

import os
import sys
import random
import colorsys

# Make the repo root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, TAG_COLOUR_PALETTE
from models import db, Tag


def generate_extra_colour(used):
    for _ in range(50):
        h = random.random()
        s = random.uniform(0.45, 0.70)
        l = random.uniform(0.35, 0.55)
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        candidate = "#{:02x}{:02x}{:02x}".format(
            int(r * 255), int(g * 255), int(b * 255)
        )
        if candidate.lower() not in used:
            return candidate
    return TAG_COLOUR_PALETTE[0]


def main():
    app = create_app()
    with app.app_context():
        tags = Tag.query.order_by(Tag.id).all()
        if not tags:
            print("No tags in DB. Nothing to do.")
            return

        palette = list(TAG_COLOUR_PALETTE)
        random.shuffle(palette)
        assigned = []
        used = set()
        for i, tag in enumerate(tags):
            if i < len(palette):
                colour = palette[i]
            else:
                colour = generate_extra_colour(used)
            used.add(colour.lower())
            old = tag.colour
            tag.colour = colour
            assigned.append((tag.name, old, colour))
        db.session.commit()

        print(f"Reassigned {len(assigned)} tag colour(s):")
        for name, old, new in assigned:
            print(f"  {name!r}: {old} -> {new}")


if __name__ == "__main__":
    main()
