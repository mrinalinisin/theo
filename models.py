from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# ── Many-to-many join table ──────────────────────────────────────────────────
product_tags = db.Table(
    "product_tags",
    db.Column("product_id", db.Integer, db.ForeignKey("product.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)


class Currency(db.Model):
    __tablename__ = "currency"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(3), nullable=False, unique=True)  # ISO 4217 e.g. INR, USD
    symbol = db.Column(db.String(4), nullable=False)  # e.g. ₹, $, €
    name = db.Column(db.String(64), nullable=False)  # e.g. Indian Rupee

    def __repr__(self):
        return f"<Currency {self.code}>"


class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    colour = db.Column(db.String(7), nullable=False, default="#3d6b8a")  # hex
    description = db.Column(db.Text, default="")

    products = db.relationship("Product", secondary=product_tags, back_populates="tags")

    def __repr__(self):
        return f"<Tag {self.name}>"


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.Text, nullable=False)
    name = db.Column(db.String(256), nullable=False)
    store = db.Column(db.String(128), default="")
    current_price = db.Column(db.Float, nullable=True)
    original_price = db.Column(db.Float, nullable=True)  # price when first added
    image_url = db.Column(db.Text, default="")  # main image
    images = db.Column(db.JSON, default=list)  # list of image URLs
    variants = db.Column(db.JSON, default=dict)  # {sizes: [...], colours: [...]}
    notes = db.Column(db.Text, default="")
    quantity = db.Column(db.Integer, default=1, nullable=False)
    currency_id = db.Column(db.Integer, db.ForeignKey("currency.id"), nullable=True)
    status = db.Column(db.String(20), default="watching")  # watching | awaiting_delivery | purchased
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # Bumped explicitly in the user edit route — sorting on "last modified"
    # only reflects user-initiated edits.
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Review (only meaningful once item has been bought / received).
    review_text = db.Column(db.Text, default="")
    review_video_url = db.Column(db.Text, default="")
    review_photos = db.Column(db.JSON, default=list)  # list of filenames in instance/images/

    tags = db.relationship("Tag", secondary=product_tags, back_populates="products")
    currency = db.relationship("Currency", foreign_keys=[currency_id])
    purchase = db.relationship(
        "Purchase", backref="product", uselist=False, cascade="all, delete-orphan"
    )
    image_hashes = db.relationship(
        "ImageHash", backref="product", cascade="all, delete-orphan"
    )

    @property
    def currency_symbol(self):
        return self.currency.symbol if self.currency else "₹"

    def fmt_price(self, value):
        """Format a numeric value using this product's currency symbol."""
        if value is None:
            return "—"
        return f"{self.currency_symbol}{value:,.0f}"

    @property
    def price_change_pct(self):
        """Percentage change from original price to current price."""
        if not self.original_price or not self.current_price:
            return None
        if self.original_price == 0:
            return None
        return ((self.current_price - self.original_price) / self.original_price) * 100

    def __repr__(self):
        return f"<Product {self.name}>"


class ImageHash(db.Model):
    """Perceptual hash of a product image for duplicate detection."""

    __tablename__ = "image_hash"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    filename = db.Column(db.String(256), nullable=False)
    phash = db.Column(db.String(16), nullable=False)


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, unique=True)
    paid_amount = db.Column(db.Float, nullable=False)
    purchased_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    notes = db.Column(db.Text, default="")
    # At least one of these must be set when a product is marked as purchased.
    # The route-level validator enforces that invariant — the DB allows NULL
    # for backfill compatibility with pre-existing purchase rows.
    order_details_url = db.Column(db.Text, default="")
    tracking_url = db.Column(db.Text, default="")
    # Delivery tracking. expected_delivery_at is what the user said it'd
    # arrive by (paired with tracking_url in the form). delivered_at is set
    # when the user clicks ✓ Arrived on the calendar — at which point status
    # auto-flips from awaiting_delivery to purchased so the row drops off
    # the calendar.
    expected_delivery_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)


class Publication(db.Model):
    """A static HTML page Theo has published to a GitHub repo.

    Theo is the source of truth for which pages exist per repo, so the
    repo's index.html can be regenerated from this list on every publish.
    Manually deleting a file from the repo leaves the row stale until
    the user removes it via Theo (or it gets auto-cleaned by a future
    repair routine).
    """

    id = db.Column(db.Integer, primary_key=True)
    repo = db.Column(db.Text, nullable=False)        # "owner/name"
    slug = db.Column(db.Text, nullable=False)        # filename without .html
    name = db.Column(db.Text, nullable=False)        # human title shown on index
    url = db.Column(db.Text, default="")             # full public URL
    item_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (db.UniqueConstraint("repo", "slug", name="uq_pub_repo_slug"),)


class Settings(db.Model):
    """Singleton settings row.

    Currently has no user-facing fields. monthly_income / shopping_budget
    columns added earlier are now orphaned in the SQLite schema but no
    longer referenced from code.
    """

    id = db.Column(db.Integer, primary_key=True)
    # One-shot marker for the listing-states refactor (Added/Purchased/
    # Shipped/Received). The migration block in create_app reads this and
    # sets it True after running the UPDATEs once. Subsequent app starts
    # see True and skip the work.
    state_refactor_done = db.Column(db.Boolean, default=False, nullable=False)

    # GitHub publishing — credentials for pushing static HTML to a repo.
    # Token is a Personal Access Token with `repo` scope (or fine-grained
    # equivalent). Stored plain — personal app on a private device.
    github_token = db.Column(db.Text, default="")
    # JSON list of "owner/name" strings. Empty list ⇒ use the default
    # "<username>.github.io" repo (constructed from github_username).
    github_repos = db.Column(db.JSON, default=list)
    github_branch = db.Column(db.Text, default="main")
    # Cached from GET /user on first successful save. Used to construct
    # the default repo name and the public Pages URL.
    github_username = db.Column(db.Text, default="")

    @classmethod
    def get(cls):
        """Return the singleton settings row, creating it if needed."""
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings
