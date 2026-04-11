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
import os
import re
import time

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory
from sqlalchemy import func
from config import Config
from models import db, Product, Tag, PriceHistory, Purchase, Settings, Currency, DomainStrategy, product_tags


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # Allow large form posts (pasted images still arrive as base64 in form POSTs).
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB total body
    app.config["MAX_FORM_MEMORY_SIZE"] = 32 * 1024 * 1024  # urlencoded form fields
    app.config["MAX_FORM_PARTS"] = 2000
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _run_lightweight_migrations()
        Settings.get()  # ensure singleton exists

    # ── Image storage setup ──────────────────────────────────────────────────
    from image_store import ensure_image_dir
    ensure_image_dir(app)

    @app.route("/images/<path:filename>")
    def serve_image(filename):
        import os
        return send_from_directory(
            os.path.join(app.instance_path, "images"), filename
        )

    @app.context_processor
    def inject_image_src():
        def image_src(value):
            if not value:
                return ""
            if value.startswith(("http://", "https://", "data:")):
                return value
            return url_for("serve_image", filename=value)
        return dict(image_src=image_src)

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return redirect(url_for("shopping_list"))

    # ── Shopping List ─────────────────────────────────────────────────────────

    DEFAULT_PAGE_SIZE = 10

    def _get_page_size():
        """Return the number of items to load per page.

        Priority: ?limit query param > page_size cookie > DEFAULT_PAGE_SIZE.
        Clamped to 6..60 to avoid extremes.
        """
        raw = request.args.get("limit") or request.cookies.get("page_size")
        if raw:
            try:
                return max(6, min(60, int(raw)))
            except (TypeError, ValueError):
                pass
        return DEFAULT_PAGE_SIZE

    def _build_product_query():
        """Parse request args and return (ordered_query, sort_key, order_key,
        status_filter, tag_filter, search_q)."""
        tag_filter = request.args.get("tag")
        search_q = (request.args.get("q") or "").strip()
        status_filter = request.args.get("status", "watching")
        if status_filter not in ("watching", "awaiting_delivery", "purchased", "all"):
            status_filter = "watching"

        SORT_COLUMNS = {
            "created": Product.created_at,
            "modified": Product.updated_at,
        }
        sort_key = request.args.get("sort", "created")
        if sort_key not in SORT_COLUMNS:
            sort_key = "created"
        order_key = request.args.get("order", "desc")
        if order_key not in ("asc", "desc"):
            order_key = "desc"
        sort_col = SORT_COLUMNS[sort_key]

        query = Product.query
        if status_filter != "all":
            query = query.filter_by(status=status_filter)
        if tag_filter:
            query = query.filter(Product.tags.any(Tag.id == int(tag_filter)))
        if search_q:
            escaped = search_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.filter(Product.name.ilike(f"%{escaped}%", escape="\\"))

        primary = sort_col.asc() if order_key == "asc" else sort_col.desc()
        tiebreak = Product.id.asc() if order_key == "asc" else Product.id.desc()
        query = query.order_by(primary, tiebreak)

        return query, sort_key, order_key, status_filter, tag_filter, search_q

    @app.route("/products")
    def shopping_list():
        query, sort_key, order_key, status_filter, tag_filter, search_q = (
            _build_product_query()
        )

        tags = Tag.query.order_by(Tag.name).all()

        active_tag_obj = None
        if tag_filter:
            try:
                active_tag_obj = Tag.query.get(int(tag_filter))
            except (TypeError, ValueError):
                active_tag_obj = None

        page_size = _get_page_size()
        total_count = query.count()
        products_page = query.limit(page_size).all()
        has_more = len(products_page) < total_count

        # Total value by currency (only when filtering by tag).
        value_by_currency = []
        if tag_filter:
            val_query = db.session.query(
                Currency.symbol, func.sum(Product.current_price * Product.quantity)
            ).outerjoin(Currency, Product.currency_id == Currency.id)
            if status_filter != "all":
                val_query = val_query.filter(Product.status == status_filter)
            val_query = val_query.filter(Product.tags.any(Tag.id == int(tag_filter)))
            if search_q:
                escaped = search_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                val_query = val_query.filter(Product.name.ilike(f"%{escaped}%", escape="\\"))
            value_by_currency = [
                (symbol or "₹", total)
                for symbol, total in val_query.group_by(Currency.symbol).all()
                if total
            ]

        return render_template(
            "shopping_list.html",
            products=products_page,
            tags=tags,
            active_tag=tag_filter,
            active_tag_obj=active_tag_obj,
            has_more=has_more,
            total_count=total_count,
            search_q=search_q,
            sort_key=sort_key,
            order_key=order_key,
            status_filter=status_filter,
            value_by_currency=value_by_currency,
        )

    @app.route("/api/products")
    def shopping_list_api():
        """Return the next page of product cards as an HTML fragment."""
        query, sort_key, order_key, status_filter, tag_filter, search_q = (
            _build_product_query()
        )
        offset = request.args.get("offset", 0, type=int)
        limit = request.args.get("limit", _get_page_size(), type=int)

        total_count = query.count()
        products = query.offset(offset).limit(limit).all()
        has_more = (offset + len(products)) < total_count
        next_offset = offset + len(products)

        active_tag_obj = None
        if tag_filter:
            try:
                active_tag_obj = Tag.query.get(int(tag_filter))
            except (TypeError, ValueError):
                active_tag_obj = None

        html = render_template(
            "_shopping_list_cards.html",
            products=products,
            active_tag_obj=active_tag_obj,
        )
        return jsonify(html=html, has_more=has_more, next_offset=next_offset, total_count=total_count)

    # ── Add Item ──────────────────────────────────────────────────────────────

    @app.route("/products/new", methods=["GET"])
    def add_item():
        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        return render_template("add_item.html", tags=tags, currencies=currencies, scraped=None)

    @app.route("/products/new", methods=["POST"])
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

    @app.route("/products/new/save", methods=["POST"])
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

        # Save images to disk
        from image_store import save_images_for_product, save_image
        product.images = save_images_for_product(images, product.id, app)
        if image_url:
            saved_main = save_image(image_url, product.id, 0, app)
            product.image_url = saved_main or ""
            if saved_main and saved_main not in product.images:
                product.images.insert(0, saved_main)
        else:
            product.image_url = product.images[0] if product.images else ""

        # Record initial price history
        if price:
            ph = PriceHistory(product_id=product.id, price=price)
            db.session.add(ph)

        db.session.commit()
        flash(f"Added \"{name}\" to your shopping list!", "success")
        return redirect(url_for("shopping_list"))

    # ── Product Detail ────────────────────────────────────────────────────────

    @app.route("/products/<int:product_id>")
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

    @app.route("/products/<int:product_id>/edit", methods=["POST"])
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
                    from image_store import save_new_images_for_product
                    product.images = save_new_images_for_product(
                        parsed_images, product.id, app
                    )
            except (json.JSONDecodeError, TypeError):
                pass
        if "image_url" in request.form:
            product.image_url = request.form.get("image_url", "") or ""
        # If there's exactly one image, force it to be the main image —
        # no point letting a single-image listing have a blank main.
        if product.images and len(product.images) == 1:
            product.image_url = product.images[0]
        # And if the current main image was removed from the list, fall
        # back to the first remaining image (if any).
        elif product.images and product.image_url not in product.images:
            product.image_url = product.images[0]
        elif not product.images:
            product.image_url = ""

        tag_ids = request.form.getlist("tag_ids")
        product.tags.clear()
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                product.tags.append(tag)

        # Order details / tracking link edits — only meaningful for purchased
        # or awaiting_delivery products (the edit modal only renders those
        # inputs when a Purchase row exists). We re-enforce the "at least one
        # link" invariant so a user can't blank both fields.
        if product.status in ("purchased", "awaiting_delivery") and product.purchase and (
            "order_details_url" in request.form or "tracking_url" in request.form
        ):
            order_details_url = (request.form.get("order_details_url") or "").strip()
            tracking_url = (request.form.get("tracking_url") or "").strip()
            if not order_details_url and not tracking_url:
                flash(
                    "Items require an Order details URL or a Tracking link.",
                    "error",
                )
                return redirect(url_for("product_detail", product_id=product_id))
            product.purchase.order_details_url = order_details_url
            product.purchase.tracking_url = tracking_url

        product.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("product_detail", product_id=product_id))

    @app.route("/products/<int:product_id>/tags/<int:tag_id>", methods=["POST"])
    def product_toggle_tag(product_id, tag_id):
        """Add or remove a tag from a product (used by the quick tag selector)."""
        product = Product.query.get_or_404(product_id)
        tag = Tag.query.get_or_404(tag_id)
        if tag in product.tags:
            product.tags.remove(tag)
            active = False
        else:
            product.tags.append(tag)
            active = True
        db.session.commit()
        return jsonify({"active": active})

    @app.route("/products/<int:product_id>/purchase", methods=["POST"])
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
        order_details_url = (request.form.get("order_details_url") or "").strip()
        tracking_url = (request.form.get("tracking_url") or "").strip()

        # A purchased item must have either an order details link or a tracking
        # link (or both) so we always have a way back to the receipt/shipment.
        if not order_details_url and not tracking_url:
            flash(
                "Please provide an Order details URL or a Tracking link before marking as purchased.",
                "error",
            )
            return redirect(url_for("product_detail", product_id=product.id))

        # Idempotent: if a Purchase row already exists for this product
        # (unique constraint on product_id), update it rather than inserting.
        # This makes form re-submits / URL replays safe.
        purchase = Purchase.query.filter_by(product_id=product.id).first()
        if purchase:
            purchase.paid_amount = paid
            purchase.purchased_at = purchased_at
            purchase.notes = notes
            purchase.order_details_url = order_details_url
            purchase.tracking_url = tracking_url
        else:
            purchase = Purchase(
                product_id=product.id,
                paid_amount=paid,
                purchased_at=purchased_at,
                notes=notes,
                order_details_url=order_details_url,
                tracking_url=tracking_url,
            )
            db.session.add(purchase)
        target_status = request.form.get("target_status", "purchased")
        if target_status not in ("purchased", "awaiting_delivery"):
            target_status = "purchased"
        product.status = target_status
        db.session.commit()

        # Check budget warning
        _check_budget_warning()

        if target_status == "awaiting_delivery":
            flash(f"Marked \"{product.name}\" as awaiting delivery for {_fmt_price(paid)}!", "success")
            return redirect(url_for("product_detail", product_id=product.id))
        flash(f"Marked \"{product.name}\" as purchased for {_fmt_price(paid)}!", "success")
        return redirect(url_for("purchases"))

    @app.route("/products/<int:product_id>/delete", methods=["POST"])
    def product_delete(product_id):
        product = Product.query.get_or_404(product_id)
        from image_store import delete_product_images
        delete_product_images(product.id, app)
        db.session.delete(product)
        db.session.commit()
        flash(f"Removed \"{product.name}\".", "success")
        return redirect(url_for("shopping_list"))

    @app.route("/products/<int:product_id>/rescrape", methods=["POST"])
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

    # ── Purchases ─────────────────────────────────────────────────────────────

    def _build_purchase_query():
        """Parse request args and return (ordered_query, period, tag_filter,
        sort_key, order_key)."""
        period = request.args.get("period", "all")
        tag_filter = request.args.get("tag", "")
        sort_key = request.args.get("sort", "modified")
        order_key = request.args.get("order", "desc")
        now = datetime.utcnow()

        query = Purchase.query.join(Product, Purchase.product_id == Product.id, isouter=True)
        if period == "1m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=30))
        elif period == "3m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=90))
        elif period == "6m":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=180))
        elif period == "1y":
            query = query.filter(Purchase.purchased_at >= now - timedelta(days=365))

        if tag_filter:
            try:
                query = query.filter(Product.tags.any(Tag.id == int(tag_filter)))
            except (TypeError, ValueError):
                pass

        if sort_key == "amount":
            sort_col = Purchase.paid_amount
        elif sort_key == "modified":
            sort_col = Product.updated_at
        else:
            sort_col = Purchase.purchased_at
        query = query.order_by(sort_col.asc() if order_key == "asc" else sort_col.desc())

        return query, period, tag_filter, sort_key, order_key

    @app.route("/purchases")
    def purchases():
        query, period, tag_filter, sort_key, order_key = _build_purchase_query()
        tags = Tag.query.order_by(Tag.name).all()

        page_size = _get_page_size()
        total_count = query.count()
        purchases_page = query.limit(page_size).all()
        has_more = len(purchases_page) < total_count

        return render_template(
            "purchases.html",
            purchases=purchases_page,
            tags=tags,
            active_tag=tag_filter,
            period=period,
            sort_key=sort_key,
            order_key=order_key,
            has_more=has_more,
            total_count=total_count,
        )

    @app.route("/api/purchases")
    def purchases_api():
        """Return the next page of purchase cards as an HTML fragment."""
        query, period, tag_filter, sort_key, order_key = _build_purchase_query()
        offset = request.args.get("offset", 0, type=int)
        limit = request.args.get("limit", _get_page_size(), type=int)

        total_count = query.count()
        purchases_page = query.offset(offset).limit(limit).all()
        has_more = (offset + len(purchases_page)) < total_count
        next_offset = offset + len(purchases_page)

        html = render_template("_purchase_cards.html", purchases=purchases_page)
        return jsonify(html=html, has_more=has_more, next_offset=next_offset, total_count=total_count)

    # ── Add Purchase (dual-mode: existing item or new item) ─────────────────

    @app.route("/purchases/new")
    def add_purchase():
        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return render_template(
            "add_purchase.html",
            tags=tags,
            currencies=currencies,
            today=today,
        )

    @app.route("/purchases/new", methods=["POST"])
    def add_purchase_save():
        order_details_url = (request.form.get("order_details_url") or "").strip()
        tracking_url = (request.form.get("tracking_url") or "").strip()
        if not order_details_url and not tracking_url:
            flash(
                "Please provide an Order details URL or a Tracking link.",
                "error",
            )
            return redirect(url_for("add_purchase"))

        purchased_at_str = request.form.get("purchased_at")
        if purchased_at_str:
            purchased_at = datetime.strptime(purchased_at_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        else:
            purchased_at = datetime.now(timezone.utc)

        notes = request.form.get("notes", "")

        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Product name is required.", "error")
            return redirect(url_for("add_purchase"))

        paid = _parse_price(request.form.get("paid_amount", "0"))
        store = (request.form.get("store") or "").strip()
        url = (request.form.get("url") or "").strip() or "manual-entry"

        currency_id = None
        try:
            raw_cid = request.form.get("currency_id")
            if raw_cid:
                cid = int(raw_cid)
                if Currency.query.get(cid):
                    currency_id = cid
        except (TypeError, ValueError):
            pass
        if currency_id is None:
            inr = Currency.query.filter_by(code="INR").first()
            currency_id = inr.id if inr else None

        product = Product(
            url=url,
            name=name,
            store=store,
            current_price=paid,
            original_price=paid,
            currency_id=currency_id,
            status="purchased",
        )

        tag_ids = request.form.getlist("tag_ids")
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                product.tags.append(tag)

        db.session.add(product)
        db.session.flush()

        # Create or update Purchase row (idempotent)
        purchase = Purchase.query.filter_by(product_id=product.id).first()
        if purchase:
            purchase.paid_amount = paid
            purchase.purchased_at = purchased_at
            purchase.notes = notes
            purchase.order_details_url = order_details_url
            purchase.tracking_url = tracking_url
        else:
            purchase = Purchase(
                product_id=product.id,
                paid_amount=paid,
                purchased_at=purchased_at,
                notes=notes,
                order_details_url=order_details_url,
                tracking_url=tracking_url,
            )
            db.session.add(purchase)

        product.status = "purchased"
        db.session.commit()
        _check_budget_warning()

        flash(f"Marked \"{product.name}\" as purchased for {_fmt_price(paid)}!", "success")
        return redirect(url_for("purchases"))

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
        domain_strategies = DomainStrategy.query.order_by(DomainStrategy.domain).all()
        return render_template("settings.html", settings=s, products=products,
                               domain_strategies=domain_strategies)

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

        # ── Domain scraping strategies ─────────────────────────────────────
        # 1. Delete rows marked for removal
        for key in list(request.form.keys()):
            if key.startswith("ds_delete_"):
                ds_id = int(key.replace("ds_delete_", ""))
                ds = DomainStrategy.query.get(ds_id)
                if ds:
                    db.session.delete(ds)

        # 2. Update existing rows
        for key, val in request.form.items():
            if key.startswith("ds_domain_") and not key.startswith("ds_domain_new_"):
                ds_id = int(key.replace("ds_domain_", ""))
                if request.form.get(f"ds_delete_{ds_id}"):
                    continue
                ds = DomainStrategy.query.get(ds_id)
                if ds:
                    domain = val.strip().lower()
                    if domain:
                        ds.domain = domain
                        ds.strategy = request.form.get(f"ds_strategy_{ds_id}", "requests")

        # 3. Add new rows
        idx = 0
        while f"ds_domain_new_{idx}" in request.form:
            domain = request.form[f"ds_domain_new_{idx}"].strip().lower()
            strategy = request.form.get(f"ds_strategy_new_{idx}", "requests")
            if domain:
                existing = DomainStrategy.query.filter_by(domain=domain).first()
                if existing:
                    existing.strategy = strategy
                else:
                    db.session.add(DomainStrategy(domain=domain, strategy=strategy))
            idx += 1

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

        active_statuses = ("watching", "awaiting_delivery")
        watching_count = Product.query.filter(
            Product.status.in_(active_statuses)
        ).count()

        return {
            "g_settings": settings,
            "g_month_spent": month_spent,
            "g_budget_pct": min(budget_pct, 100),
            "g_budget_remaining": budget_remaining,
            "g_month_name": now.strftime("%B %Y"),
            "g_watching_count": watching_count,
        }

    # ── Stats ────────────────────────────────────────────────────────────────

    @app.route("/stats")
    def stats():
        # Database size
        db_path = os.path.join(app.instance_path, "gummi.db")
        db_bytes = os.path.getsize(db_path)
        if db_bytes >= 1_048_576:
            db_size = f"{db_bytes / 1_048_576:.1f} MB"
        else:
            db_size = f"{db_bytes / 1024:.1f} KB"

        def _measure(path, fn, **kwargs):
            with app.test_request_context(path):
                t0 = time.perf_counter()
                fn(**kwargs)
                return f"{(time.perf_counter() - t0) * 1000:.0f} ms"

        load_times = {
            "/products": _measure("/products", shopping_list),
            "/api/products": _measure("/api/products", shopping_list_api),
            "/products/new": _measure("/products/new", add_item),
            "/purchases": _measure("/purchases", purchases),
            "/purchases/new": _measure("/purchases/new", add_purchase),
            "/tags": _measure("/tags", tags),
            "/settings": _measure("/settings", settings),
        }

        # /products/<id> needs a real product; skip if DB is empty
        sample = Product.query.first()
        if sample:
            load_times["/products/<id>"] = _measure(
                f"/products/{sample.id}", product_detail, product_id=sample.id
            )

        return render_template("stats.html", db_size=db_size, load_times=load_times)

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

    # Purchase.order_details_url / tracking_url added 2026-04 — at least one is
    # required when marking a product purchased (enforced at the route layer).
    if not column_exists("purchase", "order_details_url"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN order_details_url TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("purchase", "tracking_url"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN tracking_url TEXT DEFAULT ''"))
        db.session.commit()

    # Product.updated_at added 2026-04 — backfill with created_at so existing
    # rows sort sensibly on "last modified" until they're edited.
    if not column_exists("product", "updated_at"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN updated_at DATETIME"))
        db.session.execute(text("UPDATE product SET updated_at = created_at WHERE updated_at IS NULL"))
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


    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port, use_reloader=False)
