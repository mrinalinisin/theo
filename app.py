"""Gummi — Shopping Price Tracker."""

# Suppress harmless multiprocessing semaphore leak warnings that occur when the
# server is killed abruptly while Playwright has Chromium subprocesses running.
# macOS cleans up the orphaned semaphores automatically.
import warnings
warnings.filterwarnings(
    "ignore",
    message=r".*resource_tracker.*leaked semaphore.*",
)

from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import json
import re

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
from config import Config
from models import db, Product, Tag, PriceHistory, Purchase, Settings, Currency, product_tags


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _run_lightweight_migrations()
        Settings.get()  # ensure singleton exists

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return redirect(url_for("shopping_list"))

    # ── Shopping List ─────────────────────────────────────────────────────────

    @app.route("/shopping-list")
    def shopping_list():
        PAGE_SIZE = 24
        tag_filter = request.args.get("tag")
        search_q = (request.args.get("q") or "").strip()
        try:
            page = max(1, int(request.args.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        is_partial = request.args.get("partial") == "1"

        query = Product.query.filter_by(status="watching")
        if tag_filter:
            query = query.filter(Product.tags.any(Tag.id == int(tag_filter)))
        if search_q:
            # SQLite LIKE is case-insensitive for ASCII; ilike for portability.
            # Escape LIKE wildcards so "50%" searches literally match "50%".
            escaped = search_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.filter(Product.name.ilike(f"%{escaped}%", escape="\\"))
        query = query.order_by(Product.created_at.desc())

        total_count = query.count()
        offset = (page - 1) * PAGE_SIZE
        page_products = query.offset(offset).limit(PAGE_SIZE).all()
        has_more = (offset + len(page_products)) < total_count

        # ── Partial response for infinite scroll: just the new rows + cards ──
        if is_partial:
            body = render_template("_shopping_list_items.html", products=page_products)
            resp = make_response(body)
            resp.headers["X-Has-More"] = "1" if has_more else "0"
            resp.headers["X-Next-Page"] = str(page + 1) if has_more else ""
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp

        # ── Full-page render: use ALL matches for header totals, first page for list ──
        all_products = query.all()  # for currency totals (aggregate over full set)
        currency_totals = {}  # code -> {"symbol": str, "code": str, "name": str, "total": float}
        for p in all_products:
            if not p.current_price:
                continue
            c = p.currency
            code = c.code if c else "INR"
            symbol = c.symbol if c else "\u20b9"
            name = c.name if c else "Indian Rupee"
            bucket = currency_totals.setdefault(
                code, {"symbol": symbol, "code": code, "name": name, "total": 0.0}
            )
            bucket["total"] += (p.current_price or 0) * (p.quantity or 1)
        currency_totals_list = sorted(currency_totals.values(), key=lambda b: b["code"])

        tags = Tag.query.order_by(Tag.name).all()

        # Active tag object (for header display)
        active_tag_obj = None
        if tag_filter:
            try:
                active_tag_obj = Tag.query.get(int(tag_filter))
            except (TypeError, ValueError):
                active_tag_obj = None

        return render_template(
            "shopping_list.html",
            products=page_products,
            tags=tags,
            active_tag=tag_filter,
            active_tag_obj=active_tag_obj,
            currency_totals=currency_totals_list,
            total_count=total_count,
            has_more=has_more,
            next_page=page + 1 if has_more else None,
            page_size=PAGE_SIZE,
            search_q=search_q,
        )

    # ── Add Item ──────────────────────────────────────────────────────────────

    @app.route("/add-item", methods=["GET"])
    def add_item():
        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        return render_template("add_item.html", tags=tags, currencies=currencies, scraped=None)

    @app.route("/add-item", methods=["POST"])
    def add_item_scrape():
        from scraper import scrape_product, sanitize_url
        from markupsafe import Markup, escape

        url = sanitize_url(request.form.get("url", ""))
        if not url:
            flash("Please enter a URL.", "error")
            return redirect(url_for("add_item"))

        # Duplicate check — match against the canonical (sanitized) URL
        existing = Product.query.filter_by(url=url).first()
        if existing:
            detail_url = url_for("product_detail", product_id=existing.id)
            flash(
                Markup(
                    f'This URL is already in your list as '
                    f'<a href="{detail_url}" style="text-decoration:underline;">'
                    f'{escape(existing.name)}</a>. Edit the existing listing instead of adding a duplicate.'
                ),
                "warning",
            )
            return redirect(url_for("add_item"))

        settings = Settings.get()
        try:
            scraped = scrape_product(url, use_browser=settings.use_browser_rendering)
        except Exception as e:
            flash(f"Failed to scrape: {e}", "error")
            return redirect(url_for("add_item"))

        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        return render_template("add_item.html", tags=tags, currencies=currencies, scraped=scraped, url=url)

    @app.route("/add-item/save", methods=["POST"])
    def add_item_save():
        from scraper import sanitize_url
        url = sanitize_url(request.form.get("url", ""))
        name = request.form.get("name", "Unknown Product")
        store = request.form.get("store", "")
        price_str = request.form.get("price", "0")
        price = _parse_price(price_str)
        image_url = request.form.get("image_url", "")
        images_raw = request.form.get("images", "[]")
        variants_raw = request.form.get("variants", "{}")
        notes = request.form.get("notes", "")
        try:
            quantity = max(1, int(request.form.get("quantity", "1") or 1))
        except (TypeError, ValueError):
            quantity = 1
        track_price = bool(request.form.get("track_price"))
        currency_id = None
        try:
            raw_cid = request.form.get("currency_id")
            if raw_cid:
                cid = int(raw_cid)
                if Currency.query.get(cid):
                    currency_id = cid
        except (TypeError, ValueError):
            currency_id = None
        if currency_id is None:
            inr = Currency.query.filter_by(code="INR").first()
            currency_id = inr.id if inr else None
        tag_ids = request.form.getlist("tag_ids")

        try:
            images = json.loads(images_raw)
        except (json.JSONDecodeError, TypeError):
            images = []
        try:
            variants = json.loads(variants_raw)
        except (json.JSONDecodeError, TypeError):
            variants = {}

        product = Product(
            url=url,
            name=name,
            store=store,
            current_price=price,
            original_price=price,
            image_url=image_url,
            images=images,
            variants=variants,
            notes=notes,
            quantity=quantity,
            track_price=track_price,
            currency_id=currency_id,
            status="watching",
        )

        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                product.tags.append(tag)

        db.session.add(product)
        db.session.flush()

        # Record initial price history
        if price:
            ph = PriceHistory(product_id=product.id, price=price)
            db.session.add(ph)

        db.session.commit()
        flash(f"Added \"{name}\" to your shopping list!", "success")
        return redirect(url_for("shopping_list"))

    # ── Product Detail ────────────────────────────────────────────────────────

    @app.route("/product/<int:product_id>")
    def product_detail(product_id):
        product = Product.query.get_or_404(product_id)
        history = sorted(product.price_history, key=lambda h: h.checked_at, reverse=True)
        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        lowest_price = min((h.price for h in history), default=None) if history else None
        return render_template(
            "product_detail.html",
            product=product,
            history=history,
            tags=tags,
            currencies=currencies,
            lowest_price=lowest_price,
        )

    @app.route("/product/<int:product_id>/edit", methods=["POST"])
    def product_edit(product_id):
        from scraper import sanitize_url
        product = Product.query.get_or_404(product_id)
        product.name = request.form.get("name", product.name)
        product.notes = request.form.get("notes", product.notes)
        product.url = sanitize_url(request.form.get("url", product.url)) or product.url
        try:
            product.quantity = max(1, int(request.form.get("quantity", product.quantity) or 1))
        except (TypeError, ValueError):
            pass

        # Price edit: parse, record history if it actually changed
        price_raw = request.form.get("price")
        if price_raw is not None and price_raw.strip() != "":
            new_price = _parse_price(price_raw)
            if new_price > 0 and new_price != product.current_price:
                product.current_price = new_price
                if product.original_price is None:
                    product.original_price = new_price
                ph = PriceHistory(product_id=product.id, price=new_price)
                db.session.add(ph)

        currency_id = request.form.get("currency_id")
        if currency_id:
            try:
                cid = int(currency_id)
                if Currency.query.get(cid):
                    product.currency_id = cid
            except (TypeError, ValueError):
                pass

        # Checkbox: present → True, absent → False
        product.track_price = bool(request.form.get("track_price"))

        # Image list + main image can be trimmed by the X buttons on the detail page
        images_raw = request.form.get("images")
        if images_raw is not None:
            try:
                parsed_images = json.loads(images_raw)
                if isinstance(parsed_images, list):
                    product.images = parsed_images
            except (json.JSONDecodeError, TypeError):
                pass
        if "image_url" in request.form:
            product.image_url = request.form.get("image_url", "") or ""

        tag_ids = request.form.getlist("tag_ids")
        product.tags.clear()
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                product.tags.append(tag)

        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("product_detail", product_id=product_id))

    @app.route("/product/<int:product_id>/purchase", methods=["POST"])
    def product_purchase(product_id):
        product = Product.query.get_or_404(product_id)
        price_mode = request.form.get("price_mode", "listed")
        if price_mode == "custom":
            paid = _parse_price(request.form.get("paid_amount", "0"))
        else:
            paid = product.current_price or 0

        purchased_at_str = request.form.get("purchased_at")
        if purchased_at_str:
            purchased_at = datetime.strptime(purchased_at_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        else:
            purchased_at = datetime.now(timezone.utc)

        notes = request.form.get("notes", "")

        # Idempotent: if a Purchase row already exists for this product
        # (unique constraint on product_id), update it rather than inserting.
        # This makes form re-submits / URL replays safe.
        purchase = Purchase.query.filter_by(product_id=product.id).first()
        if purchase:
            purchase.paid_amount = paid
            purchase.purchased_at = purchased_at
            purchase.notes = notes
        else:
            purchase = Purchase(
                product_id=product.id,
                paid_amount=paid,
                purchased_at=purchased_at,
                notes=notes,
            )
            db.session.add(purchase)
        product.status = "purchased"
        db.session.commit()

        # Check budget warning
        _check_budget_warning()

        flash(f"Marked \"{product.name}\" as purchased for {_fmt_price(paid)}!", "success")
        return redirect(url_for("purchases"))

    @app.route("/product/<int:product_id>/delete", methods=["POST"])
    def product_delete(product_id):
        product = Product.query.get_or_404(product_id)
        db.session.delete(product)
        db.session.commit()
        flash(f"Removed \"{product.name}\".", "success")
        return redirect(url_for("shopping_list"))

    @app.route("/product/<int:product_id>/rescrape", methods=["POST"])
    def product_rescrape(product_id):
        from scraper import check_price

        product = Product.query.get_or_404(product_id)
        settings = Settings.get()
        try:
            new_price = check_price(product.url, use_browser=settings.use_browser_rendering)
            if new_price is not None:
                old_price = product.current_price
                product.current_price = new_price
                product.last_checked_at = datetime.now(timezone.utc)
                ph = PriceHistory(product_id=product.id, price=new_price)
                db.session.add(ph)
                db.session.commit()
                if old_price and new_price < old_price:
                    flash(f"Price dropped: {_fmt_price(old_price)} → {_fmt_price(new_price)}", "success")
                elif old_price and new_price > old_price:
                    flash(f"Price rose: {_fmt_price(old_price)} → {_fmt_price(new_price)}", "warning")
                else:
                    flash(f"Price unchanged: {_fmt_price(new_price)}", "info")
            else:
                flash("Could not extract price from page.", "warning")
        except Exception as e:
            flash(f"Scraping failed: {e}", "error")

        return redirect(url_for("product_detail", product_id=product_id))

    # ── Analytics ─────────────────────────────────────────────────────────────

    @app.route("/analytics")
    def analytics():
        settings = Settings.get()
        now = datetime.utcnow()
        current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # This month's purchases
        month_purchases = (
            Purchase.query.filter(Purchase.purchased_at >= current_month_start).all()
        )
        month_spent = sum(p.paid_amount for p in month_purchases)

        # Spending by tag for current month
        tag_spending = {}
        tag_item_count = {}
        for purchase in month_purchases:
            product = purchase.product
            if product and product.tags:
                for tag in product.tags:
                    tag_spending[tag.name] = tag_spending.get(tag.name, 0) + purchase.paid_amount
                    tag_item_count[tag.name] = tag_item_count.get(tag.name, 0) + 1
            else:
                tag_spending["Uncategorised"] = tag_spending.get("Uncategorised", 0) + purchase.paid_amount
                tag_item_count["Uncategorised"] = tag_item_count.get("Uncategorised", 0) + 1

        # Get tag colours
        tags = Tag.query.all()
        tag_colours = {t.name: t.colour for t in tags}
        tag_colours["Uncategorised"] = "#a8a29e"

        # Monthly trend (last 6 months)
        monthly_trend = []
        for i in range(5, -1, -1):
            month_date = now - timedelta(days=30 * i)
            m_start = month_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if m_start.month == 12:
                m_end = m_start.replace(year=m_start.year + 1, month=1)
            else:
                m_end = m_start.replace(month=m_start.month + 1)
            m_purchases = Purchase.query.filter(
                Purchase.purchased_at >= m_start, Purchase.purchased_at < m_end
            ).all()
            m_total = sum(p.paid_amount for p in m_purchases)
            monthly_trend.append({
                "month": m_start.strftime("%b"),
                "year": m_start.year,
                "total": m_total,
                "is_current": i == 0,
            })

        max_trend = max((m["total"] for m in monthly_trend), default=1) or 1

        return render_template(
            "analytics.html",
            settings=settings,
            month_spent=month_spent,
            month_name=now.strftime("%B %Y"),
            tag_spending=tag_spending,
            tag_item_count=tag_item_count,
            tag_colours=tag_colours,
            monthly_trend=monthly_trend,
            max_trend=max_trend,
        )

    # ── Purchases ─────────────────────────────────────────────────────────────

    @app.route("/purchases")
    def purchases():
        period = request.args.get("period", "all")
        group_by = request.args.get("group_by", "tag")
        now = datetime.utcnow()

        query = Purchase.query
        if period == "1m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=30))
        elif period == "3m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=90))
        elif period == "6m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=180))
        elif period == "1y":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=365))

        all_purchases = query.order_by(Purchase.purchased_at.desc()).all()
        total_spent = sum(p.paid_amount for p in all_purchases)
        total_items = len(all_purchases)
        avg_per_item = total_spent / total_items if total_items else 0

        # Savings vs original listed price
        total_savings = 0
        for p in all_purchases:
            if p.product and p.product.original_price:
                total_savings += p.product.original_price - p.paid_amount

        # Group
        # NOTE: the inner dict key is "purchases" (not "items") because Jinja's
        # attribute lookup on a dict finds the builtin `dict.items` method
        # first, shadowing the key and breaking `{{ data.items|length }}`.
        grouped = {}
        if group_by == "tag":
            for purchase in all_purchases:
                product = purchase.product
                if product and product.tags:
                    for tag in product.tags:
                        grouped.setdefault(tag.name, {"tag": tag, "purchases": [], "total": 0})
                        grouped[tag.name]["purchases"].append(purchase)
                        grouped[tag.name]["total"] += purchase.paid_amount
                else:
                    grouped.setdefault("Uncategorised", {"tag": None, "purchases": [], "total": 0})
                    grouped["Uncategorised"]["purchases"].append(purchase)
                    grouped["Uncategorised"]["total"] += purchase.paid_amount
        else:
            for purchase in all_purchases:
                month_key = purchase.purchased_at.strftime("%B %Y")
                grouped.setdefault(month_key, {"tag": None, "purchases": [], "total": 0})
                grouped[month_key]["purchases"].append(purchase)
                grouped[month_key]["total"] += purchase.paid_amount

        return render_template(
            "purchases.html",
            grouped=grouped,
            period=period,
            group_by=group_by,
            total_spent=total_spent,
            total_items=total_items,
            avg_per_item=avg_per_item,
            total_savings=total_savings,
        )

    # ── Tags ──────────────────────────────────────────────────────────────────

    @app.route("/tags")
    def tags():
        all_tags = Tag.query.order_by(Tag.name).all()
        return render_template("tags.html", tags=all_tags)

    @app.route("/tags/create", methods=["POST"])
    def tag_create():
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "")
        if not name:
            flash("Tag name is required.", "error")
            return redirect(url_for("tags"))
        name = name[:1].upper() + name[1:]
        if Tag.query.filter_by(name=name).first():
            flash(f"Tag \"{name}\" already exists.", "error")
            return redirect(url_for("tags"))
        colour = _pick_random_tag_colour()
        tag = Tag(name=name, colour=colour, description=description)
        db.session.add(tag)
        db.session.commit()
        flash(f"Tag \"{name}\" created.", "success")
        return redirect(url_for("tags"))

    @app.route("/tags/<int:tag_id>/edit", methods=["POST"])
    def tag_edit(tag_id):
        tag = Tag.query.get_or_404(tag_id)
        tag.name = request.form.get("name", tag.name).strip()
        tag.colour = request.form.get("colour", tag.colour)
        tag.description = request.form.get("description", tag.description)
        db.session.commit()
        flash(f"Tag \"{tag.name}\" updated.", "success")
        return redirect(url_for("tags"))

    @app.route("/tags/<int:tag_id>/delete", methods=["POST"])
    def tag_delete(tag_id):
        tag = Tag.query.get_or_404(tag_id)
        db.session.delete(tag)
        db.session.commit()
        flash(f"Tag deleted.", "success")
        return redirect(url_for("tags"))

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.route("/settings")
    def settings():
        s = Settings.get()
        products = Product.query.filter_by(status="watching").order_by(Product.name).all()
        return render_template("settings.html", settings=s, products=products)

    @app.route("/settings", methods=["POST"])
    def settings_save():
        s = Settings.get()
        s.default_check_interval = int(request.form.get("default_check_interval", 240))
        s.monthly_income = _parse_price(request.form.get("monthly_income", "0"))
        s.shopping_budget = _parse_price(request.form.get("shopping_budget", "0"))
        s.use_browser_rendering = request.form.get("use_browser_rendering") == "on"
        s.auto_extract_variants = request.form.get("auto_extract_variants") == "on"
        s.notify_price_drop = request.form.get("notify_price_drop") == "on"
        s.notify_price_rise = request.form.get("notify_price_rise") == "on"
        s.notify_back_in_stock = request.form.get("notify_back_in_stock") == "on"
        s.notify_budget_warning = request.form.get("notify_budget_warning") == "on"

        # Per-item intervals
        for key, val in request.form.items():
            if key.startswith("interval_"):
                pid = int(key.replace("interval_", ""))
                product = Product.query.get(pid)
                if product:
                    v = int(val)
                    product.check_interval = v if v != s.default_check_interval else None

        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

    # ── WhatsApp Webhook ──────────────────────────────────────────────────────

    @app.route("/webhook/whatsapp", methods=["POST"])
    def whatsapp_webhook():
        from whatsapp import handle_incoming

        body = request.form.get("Body", "").strip()
        from_number = request.form.get("From", "")
        response_text = handle_incoming(body, from_number)

        from twilio.twiml.messaging_response import MessagingResponse

        resp = MessagingResponse()
        resp.message(response_text)
        return str(resp), 200, {"Content-Type": "text/xml"}

    # ── Template helpers ──────────────────────────────────────────────────────

    @app.template_filter("fmt_price")
    def fmt_price_filter(value):
        return _fmt_price(value)

    @app.template_filter("time_ago")
    def time_ago_filter(dt):
        if not dt:
            return "Never"
        now = datetime.utcnow()
        diff = now - dt
        seconds = diff.total_seconds()
        if seconds < 60:
            return "Just now"
        elif seconds < 3600:
            m = int(seconds // 60)
            return f"{m} min ago"
        elif seconds < 86400:
            h = int(seconds // 3600)
            return f"{h}h ago"
        else:
            d = int(seconds // 86400)
            return f"{d}d ago"

    @app.template_filter("fmt_date")
    def fmt_date_filter(dt):
        if not dt:
            return ""
        return dt.strftime("%b %d, %Y")

    @app.template_filter("tojson_safe")
    def tojson_safe_filter(value):
        return json.dumps(value) if value else "[]"

    @app.context_processor
    def inject_globals():
        settings = Settings.get()
        # Budget usage for sidebar
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_purchases = Purchase.query.filter(Purchase.purchased_at >= month_start).all()
        month_spent = sum(p.paid_amount for p in month_purchases)
        budget = settings.shopping_budget or 0
        budget_pct = (month_spent / budget * 100) if budget > 0 else 0
        budget_remaining = budget - month_spent

        watching_count = Product.query.filter_by(status="watching").count()

        # Sidebar tag list with per-tag watching counts
        sidebar_tags = []
        for tag in Tag.query.order_by(Tag.name).all():
            count = (
                Product.query.filter_by(status="watching")
                .filter(Product.tags.any(Tag.id == tag.id))
                .count()
            )
            sidebar_tags.append({"id": tag.id, "name": tag.name, "colour": tag.colour, "count": count})

        return {
            "g_settings": settings,
            "g_month_spent": month_spent,
            "g_budget_pct": min(budget_pct, 100),
            "g_budget_remaining": budget_remaining,
            "g_month_name": now.strftime("%B %Y"),
            "g_watching_count": watching_count,
            "g_sidebar_tags": sidebar_tags,
            "g_active_tag": request.args.get("tag") if request.endpoint == "shopping_list" else None,
        }

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run_lightweight_migrations():
    """Best-effort schema upgrades for columns added after initial DB creation.

    SQLAlchemy's create_all() never alters existing tables, so for a zero-ops
    local app we issue idempotent ALTER TABLE statements. Each block checks
    PRAGMA table_info first so it's safe to re-run on every startup.
    """
    from sqlalchemy import text
    from models import Currency

    def column_exists(table, column):
        rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)

    # Product.quantity added 2026-04
    if not column_exists("product", "quantity"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"))
        db.session.commit()

    # Product.track_price added 2026-04 — default OFF for all existing items
    if not column_exists("product", "track_price"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN track_price BOOLEAN NOT NULL DEFAULT 0"))
        db.session.commit()

    # Seed supported currencies (idempotent — only inserts missing codes).
    # Whitelist: anything outside this list gets pruned below.
    seed_currencies = [
        ("INR", "\u20b9", "Indian Rupee"),
        ("USD", "$", "US Dollar"),
        ("SGD", "S$", "Singapore Dollar"),
    ]
    for code, symbol, name in seed_currencies:
        if not Currency.query.filter_by(code=code).first():
            db.session.add(Currency(code=code, symbol=symbol, name=name))
    db.session.commit()

    # Prune any legacy currencies outside the whitelist. Reassign products
    # still pointing at a pruned currency to INR first so the FK stays valid.
    allowed_codes = {c[0] for c in seed_currencies}
    inr_row = Currency.query.filter_by(code="INR").first()
    stale = Currency.query.filter(~Currency.code.in_(allowed_codes)).all()
    if stale and inr_row:
        stale_ids = [c.id for c in stale]
        db.session.execute(
            text("UPDATE product SET currency_id = :inr WHERE currency_id IN :ids")
                .bindparams(db.bindparam("ids", expanding=True)),
            {"inr": inr_row.id, "ids": stale_ids},
        )
        for c in stale:
            db.session.delete(c)
        db.session.commit()
        print(f"[Migration] Pruned {len(stale)} currency row(s): {[c.code for c in stale]}")

    # Product.currency_id added 2026-04 — backfill to INR
    if not column_exists("product", "currency_id"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN currency_id INTEGER REFERENCES currency(id)"))
        db.session.commit()

    inr = Currency.query.filter_by(code="INR").first()
    if inr:
        db.session.execute(
            text("UPDATE product SET currency_id = :cid WHERE currency_id IS NULL"),
            {"cid": inr.id},
        )
        db.session.commit()

    # Backfill Product.url through sanitize_url so legacy rows with query
    # params / whitespace match the canonical form used by duplicate detection.
    # Idempotent: rows that are already canonical produce no UPDATE.
    from scraper import sanitize_url
    rows = db.session.execute(text("SELECT id, url FROM product")).fetchall()
    changed = 0
    for row_id, raw_url in rows:
        canonical = sanitize_url(raw_url)
        if canonical and canonical != raw_url:
            db.session.execute(
                text("UPDATE product SET url = :u WHERE id = :id"),
                {"u": canonical, "id": row_id},
            )
            changed += 1
    if changed:
        db.session.commit()
        print(f"[Migration] Canonicalised {changed} product URL(s)")


# Curated palette of 16 visually-distinct tag colours.
# Ordered so the first N tags get a nice spread even without randomisation.
TAG_COLOUR_PALETTE = [
    "#3d6b8a",  # slate blue
    "#7a4a9e",  # purple
    "#a05c1e",  # brown
    "#2d7a5a",  # green
    "#8a3a3a",  # dark red
    "#c2781e",  # orange
    "#1e8a9e",  # teal
    "#c94f7c",  # pink
    "#5a3a8a",  # deep purple
    "#2d5a27",  # forest green
    "#9e8d4a",  # olive
    "#4a7a9e",  # steel blue
    "#c94f4f",  # bright red
    "#4a9e4a",  # lime green
    "#8a4a1e",  # rust
    "#6a6a6a",  # gray
]


def _pick_random_tag_colour():
    """Return a hex colour not already assigned to any existing tag.

    Strategy: shuffle the curated palette and return the first unused entry.
    If every palette colour is taken, generate random HSL-based hex until we
    find one that's not in the used set (bounded by 20 tries to stay O(1)).
    """
    import random as _random
    import colorsys

    used = {
        (t.colour or "").lower()
        for t in Tag.query.with_entities(Tag.colour).all()
    }

    palette = list(TAG_COLOUR_PALETTE)
    _random.shuffle(palette)
    for colour in palette:
        if colour.lower() not in used:
            return colour

    # Palette exhausted — generate HSL-based colours in the same pleasant range.
    for _ in range(20):
        h = _random.random()
        s = _random.uniform(0.45, 0.70)
        l = _random.uniform(0.35, 0.55)
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        candidate = "#{:02x}{:02x}{:02x}".format(
            int(r * 255), int(g * 255), int(b * 255)
        )
        if candidate.lower() not in used:
            return candidate

    # Last-resort fallback (astronomically unlikely) — return any palette entry
    return TAG_COLOUR_PALETTE[0]


def _parse_price(s):
    """Extract a numeric price from a string like '₹2,490' or '2490.00'."""
    if not s:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    cleaned = re.sub(r"[^\d.]", "", str(s))
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


def _fmt_price(value):
    if value is None:
        return "—"
    return f"\u20b9{value:,.0f}"


def _check_budget_warning():
    """Send WhatsApp alert if spending exceeds 80% of budget."""
    try:
        settings = Settings.get()
        if not settings.notify_budget_warning or not settings.shopping_budget:
            return
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_purchases = Purchase.query.filter(Purchase.purchased_at >= month_start).all()
        month_spent = sum(p.paid_amount for p in month_purchases)
        pct = (month_spent / settings.shopping_budget) * 100
        if pct >= 80:
            from whatsapp import send_whatsapp

            send_whatsapp(
                f"\u26a0\ufe0f Budget Alert: You've spent {_fmt_price(month_spent)} "
                f"({pct:.0f}%) of your {_fmt_price(settings.shopping_budget)} budget for "
                f"{now.strftime('%B')}."
            )
    except Exception:
        pass  # WhatsApp is optional


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()

    # Start scheduler
    from scheduler import start_scheduler

    start_scheduler(app)

    # Register graceful shutdown so APScheduler (and any in-flight Playwright
    # subprocesses) release their resources cleanly on SIGTERM/SIGINT. Without
    # this, kill-then-restart cycles can orphan named semaphores on macOS.
    import atexit
    import signal
    from scheduler import scheduler

    def _graceful_shutdown(*_args):
        if scheduler.running:
            scheduler.shutdown(wait=False)

    atexit.register(_graceful_shutdown)
    signal.signal(signal.SIGTERM, lambda *a: (_graceful_shutdown(), exit(0)))

    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port, use_reloader=False)
