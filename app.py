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

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from config import Config
from models import db, Product, Tag, PriceHistory, Purchase, Settings, product_tags


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
        tag_filter = request.args.get("tag")
        query = Product.query.filter_by(status="watching")
        if tag_filter:
            query = query.filter(Product.tags.any(Tag.id == int(tag_filter)))
        products = query.order_by(Product.created_at.desc()).all()
        tags = Tag.query.order_by(Tag.name).all()

        # Stats
        total_value = sum((p.current_price or 0) * (p.quantity or 1) for p in products)
        week_ago = datetime.utcnow() - timedelta(days=7)
        price_drops = 0
        price_rises = 0
        for p in products:
            recent = [ph for ph in p.price_history if ph.checked_at >= week_ago]
            if len(recent) >= 2 and recent[0].price < recent[-1].price:
                price_drops += 1
            elif len(recent) >= 2 and recent[0].price > recent[-1].price:
                price_rises += 1

        return render_template(
            "shopping_list.html",
            products=products,
            tags=tags,
            active_tag=tag_filter,
            total_value=total_value,
            price_drops=price_drops,
            price_rises=price_rises,
        )

    # ── Add Item ──────────────────────────────────────────────────────────────

    @app.route("/add-item", methods=["GET"])
    def add_item():
        tags = Tag.query.order_by(Tag.name).all()
        return render_template("add_item.html", tags=tags, scraped=None)

    @app.route("/add-item", methods=["POST"])
    def add_item_scrape():
        from scraper import scrape_product

        url = request.form.get("url", "").strip()
        if not url:
            flash("Please enter a URL.", "error")
            return redirect(url_for("add_item"))

        settings = Settings.get()
        try:
            scraped = scrape_product(url, use_browser=settings.use_browser_rendering)
        except Exception as e:
            flash(f"Failed to scrape: {e}", "error")
            return redirect(url_for("add_item"))

        tags = Tag.query.order_by(Tag.name).all()
        return render_template("add_item.html", tags=tags, scraped=scraped, url=url)

    @app.route("/add-item/save", methods=["POST"])
    def add_item_save():
        url = request.form.get("url", "")
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
        lowest_price = min((h.price for h in history), default=None) if history else None
        return render_template(
            "product_detail.html",
            product=product,
            history=history,
            tags=tags,
            lowest_price=lowest_price,
        )

    @app.route("/product/<int:product_id>/edit", methods=["POST"])
    def product_edit(product_id):
        product = Product.query.get_or_404(product_id)
        product.name = request.form.get("name", product.name)
        product.notes = request.form.get("notes", product.notes)
        product.url = request.form.get("url", product.url)
        try:
            product.quantity = max(1, int(request.form.get("quantity", product.quantity) or 1))
        except (TypeError, ValueError):
            pass

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

        purchase = Purchase(
            product_id=product.id,
            paid_amount=paid,
            purchased_at=purchased_at,
            notes=notes,
        )
        product.status = "purchased"
        db.session.add(purchase)
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
        grouped = {}
        if group_by == "tag":
            for purchase in all_purchases:
                product = purchase.product
                if product and product.tags:
                    for tag in product.tags:
                        grouped.setdefault(tag.name, {"tag": tag, "items": [], "total": 0})
                        grouped[tag.name]["items"].append(purchase)
                        grouped[tag.name]["total"] += purchase.paid_amount
                else:
                    grouped.setdefault("Uncategorised", {"tag": None, "items": [], "total": 0})
                    grouped["Uncategorised"]["items"].append(purchase)
                    grouped["Uncategorised"]["total"] += purchase.paid_amount
        else:
            for purchase in all_purchases:
                month_key = purchase.purchased_at.strftime("%B %Y")
                grouped.setdefault(month_key, {"tag": None, "items": [], "total": 0})
                grouped[month_key]["items"].append(purchase)
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
        colour = request.form.get("colour", "#3d6b8a")
        description = request.form.get("description", "")
        if not name:
            flash("Tag name is required.", "error")
            return redirect(url_for("tags"))
        if Tag.query.filter_by(name=name).first():
            flash(f"Tag \"{name}\" already exists.", "error")
            return redirect(url_for("tags"))
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

    def column_exists(table, column):
        rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)

    # Product.quantity added 2026-04
    if not column_exists("product", "quantity"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"))
        db.session.commit()


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
