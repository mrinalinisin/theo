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
    status = db.Column(db.String(20), default="watching")  # watching | purchased
    check_interval = db.Column(db.Integer, nullable=True)  # per-item override in minutes
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_checked_at = db.Column(db.DateTime, nullable=True)

    tags = db.relationship("Tag", secondary=product_tags, back_populates="products")
    currency = db.relationship("Currency", foreign_keys=[currency_id])
    price_history = db.relationship(
        "PriceHistory", backref="product", lazy="select", cascade="all, delete-orphan",
        order_by="PriceHistory.checked_at.desc()"
    )
    purchase = db.relationship(
        "Purchase", backref="product", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def currency_symbol(self):
        return self.currency.symbol if self.currency else "\u20b9"

    def fmt_price(self, value):
        """Format a numeric value using this product's currency symbol."""
        if value is None:
            return "\u2014"
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


class PriceHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    price = db.Column(db.Float, nullable=False)
    checked_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, unique=True)
    paid_amount = db.Column(db.Float, nullable=False)
    purchased_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    notes = db.Column(db.Text, default="")


class Settings(db.Model):
    """Singleton settings row."""

    id = db.Column(db.Integer, primary_key=True)
    default_check_interval = db.Column(db.Integer, default=240)  # minutes
    monthly_income = db.Column(db.Float, default=0)
    shopping_budget = db.Column(db.Float, default=0)
    use_browser_rendering = db.Column(db.Boolean, default=False)
    auto_extract_variants = db.Column(db.Boolean, default=True)
    notify_price_drop = db.Column(db.Boolean, default=True)
    notify_price_rise = db.Column(db.Boolean, default=True)
    notify_back_in_stock = db.Column(db.Boolean, default=False)
    notify_budget_warning = db.Column(db.Boolean, default=True)

    @classmethod
    def get(cls):
        """Return the singleton settings row, creating it if needed."""
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings
