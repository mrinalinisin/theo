"""Theo — Shopping Price Tracker."""

# Suppress harmless multiprocessing semaphore leak warnings that occur when the
# server is killed abruptly while Playwright has Chromium subprocesses running.
# macOS cleans up the orphaned semaphores automatically.
#
# We set PYTHONWARNINGS so the filter propagates to child processes (the
# resource_tracker runs as a separate subprocess with its own warning state,
# so warnings.filterwarnings() in the main process alone has no effect).
import os
os.environ.setdefault(
    "PYTHONWARNINGS",
    "ignore::UserWarning:multiprocessing.resource_tracker",
)
import warnings
warnings.filterwarnings(
    "ignore",
    message=r".*resource_tracker.*leaked semaphore.*",
)

from datetime import datetime, timezone, timedelta, date
from urllib.parse import urlparse
import calendar as stdlib_calendar
import json
import os
import re
import time

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, session
from sqlalchemy import func
from config import Config
from models import db, Product, Tag, Purchase, Settings, Currency, product_tags


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

    @app.route("/favicon.ico")
    def favicon():
        # Browsers (esp. Safari) request /favicon.ico at the origin root in
        # addition to honoring <link rel="icon"> tags. Without this route the
        # request 404s and the favicon can disappear on deep routes like
        # /products/<id> when opened cold. Serve the PNG with the .ico path —
        # all modern browsers accept PNG content for favicon.ico.
        return send_from_directory(
            app.static_folder, "favicon-32.png", mimetype="image/png"
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

    @app.context_processor
    def inject_cart():
        cart = session.get("cart", [])
        return dict(cart_count=len(cart), cart_product_ids=set(cart))

    @app.context_processor
    def inject_compare():
        compare = session.get("compare", [])
        return dict(compare_count=len(compare), compare_product_ids=set(compare))

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

    # ── Browser Extension API ────────────────────────────────────────────────

    @app.route("/products/new_from_browser", methods=["POST", "OPTIONS"])
    def add_item_from_browser():
        """JSON endpoint for the Safari extension to send selected content."""
        if request.method == "OPTIONS":
            return "", 204

        from urls import sanitize_url
        from image_store import save_images_for_product, save_image, find_duplicate_by_image

        data = request.get_json(silent=True)
        if not data:
            return jsonify(ok=False, error="Invalid JSON"), 400

        url = sanitize_url(data.get("url", ""))
        name = (data.get("name") or data.get("selected_text", "")[:256] or "Unknown Product").strip()
        store = data.get("store", "")
        price = _parse_price(data.get("price", 0))
        image_url = data.get("image_url", "")
        images = data.get("images", [])
        notes = data.get("notes") or data.get("selected_text", "")

        # Resolve currency code to ID
        currency_code = data.get("currency", "INR")
        currency = Currency.query.filter_by(code=currency_code).first()
        if not currency:
            currency = Currency.query.filter_by(code="INR").first()
        currency_id = currency.id if currency else None

        # Resolve tag names to Tag objects, creating new ones as needed
        tag_names = data.get("tag_names", [])
        tags = []
        for tn in tag_names:
            tag = Tag.query.filter(func.lower(Tag.name) == tn.strip().lower()).first()
            if not tag:
                tag = Tag(name=tn.strip())
                db.session.add(tag)
            tags.append(tag)

        product = Product(
            url=url,
            name=name,
            store=store,
            current_price=price,
            original_price=price,
            image_url=image_url,
            images=images,
            variants={},
            notes=notes,
            quantity=1,
            currency_id=currency_id,
            status="watching",
        )
        for tag in tags:
            product.tags.append(tag)

        db.session.add(product)
        db.session.flush()

        # Save images to disk
        product.images = save_images_for_product(images, product.id, app)
        if image_url:
            saved_main = save_image(image_url, product.id, 0, app)
            product.image_url = saved_main or ""
            if saved_main and saved_main not in product.images:
                product.images.insert(0, saved_main)
        else:
            product.image_url = product.images[0] if product.images else ""

        # Check for image-based duplicates
        duplicate_name = None
        for ih in product.image_hashes:
            match = find_duplicate_by_image(ih.phash, exclude_product_id=product.id)
            if match:
                duplicate_name = match.name
                break

        db.session.commit()

        result = dict(ok=True, product_id=product.id, name=product.name)
        if duplicate_name:
            result["warning"] = f"Possible duplicate of \"{duplicate_name}\""
        return jsonify(result), 201

    @app.after_request
    def add_cors_for_browser_extension(response):
        if request.path == "/products/new_from_browser":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    # ── Product Detail ────────────────────────────────────────────────────────

    @app.route("/products/<int:product_id>")
    def product_detail(product_id):
        product = Product.query.get_or_404(product_id)
        tags = Tag.query.order_by(Tag.name).all()
        currencies = Currency.query.order_by(Currency.code).all()
        return render_template(
            "product_detail.html",
            product=product,
            tags=tags,
            currencies=currencies,
        )

    @app.route("/products/<int:product_id>/edit", methods=["POST"])
    def product_edit(product_id):
        from urls import sanitize_url
        product = Product.query.get_or_404(product_id)
        product.name = request.form.get("name", product.name)
        product.notes = request.form.get("notes", product.notes)
        product.url = sanitize_url(request.form.get("url", product.url)) or product.url
        try:
            product.quantity = max(1, int(request.form.get("quantity", product.quantity) or 1))
        except (TypeError, ValueError):
            pass

        # Price edit: parse and update if it actually changed
        price_raw = request.form.get("price")
        if price_raw is not None and price_raw.strip() != "":
            new_price = _parse_price(price_raw)
            if new_price > 0 and new_price != product.current_price:
                product.current_price = new_price
                if product.original_price is None:
                    product.original_price = new_price

        currency_id = request.form.get("currency_id")
        if currency_id:
            try:
                cid = int(currency_id)
                if Currency.query.get(cid):
                    product.currency_id = cid
            except (TypeError, ValueError):
                pass

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

            # Expected delivery date — optional. Empty string clears it.
            edate_raw = (request.form.get("expected_delivery_at") or "").strip()
            if edate_raw:
                try:
                    product.purchase.expected_delivery_at = datetime.strptime(
                        edate_raw, "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass  # Keep the existing value on parse failure
            else:
                product.purchase.expected_delivery_at = None

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

        # Let the user correct the quantity at purchase time — they may have
        # planned to buy 1 but ended up with a 3-pack. Update the product so
        # the source of truth stays consistent.
        try:
            quantity = max(1, int(request.form.get("quantity", product.quantity or 1)))
        except (TypeError, ValueError):
            quantity = product.quantity or 1
        product.quantity = quantity

        price_mode = request.form.get("price_mode", "listed")
        if price_mode == "custom":
            # Custom amount is the total paid, entered as-is.
            paid = _parse_price(request.form.get("paid_amount", "0"))
        else:
            # Listed price is per unit — multiply by the (possibly updated) quantity.
            paid = (product.current_price or 0) * quantity

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

        # Optional expected delivery date — paired with tracking_url in the form.
        # YYYY-MM-DD from <input type="date">; stored as midnight UTC for that day.
        expected_delivery_at = None
        edate_raw = (request.form.get("expected_delivery_at") or "").strip()
        if edate_raw:
            try:
                expected_delivery_at = datetime.strptime(edate_raw, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                expected_delivery_at = None

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
            purchase.expected_delivery_at = expected_delivery_at
        else:
            purchase = Purchase(
                product_id=product.id,
                paid_amount=paid,
                purchased_at=purchased_at,
                notes=notes,
                order_details_url=order_details_url,
                tracking_url=tracking_url,
                expected_delivery_at=expected_delivery_at,
            )
            db.session.add(purchase)
        target_status = request.form.get("target_status", "purchased")
        if target_status not in ("purchased", "awaiting_delivery"):
            target_status = "purchased"
        product.status = target_status
        db.session.commit()

        if target_status == "awaiting_delivery":
            flash(f"Marked \"{product.name}\" as awaiting delivery for {_fmt_price(paid)}!", "success")
            return redirect(url_for("product_detail", product_id=product.id))
        flash(f"Marked \"{product.name}\" as purchased for {_fmt_price(paid)}!", "success")
        return redirect(url_for("shopping_list"))

    @app.route("/products/<int:product_id>/arrived", methods=["POST"])
    def product_arrived(product_id):
        """Mark an awaiting-delivery item as arrived.

        Sets delivered_at = now() and auto-flips status to 'purchased'
        so the row drops off the Arriving Today calendar. The
        expected_delivery_at value is preserved as the *original*
        signal — useful later for "how often is my stuff late?" stats.
        """
        product = Product.query.get_or_404(product_id)
        if not product.purchase:
            flash(f"\"{product.name}\" has no purchase record yet.", "error")
            return redirect(url_for("purchases_calendar"))
        product.purchase.delivered_at = datetime.now(timezone.utc)
        if product.status == "awaiting_delivery":
            product.status = "purchased"
        db.session.commit()
        flash(f"Marked \"{product.name}\" as arrived.", "success")
        # Honour an optional ?next= so we can redirect back to wherever the
        # user clicked from (calendar, product detail, etc.).
        next_url = request.form.get("next") or url_for("purchases_calendar")
        return redirect(next_url)

    @app.route("/products/<int:product_id>/unpurchase", methods=["POST"])
    def product_unpurchase(product_id):
        product = Product.query.get_or_404(product_id)
        if product.purchase:
            db.session.delete(product.purchase)
        product.status = "watching"
        db.session.commit()
        flash(f"Moved \"{product.name}\" back to Watching.", "success")
        return redirect(url_for("shopping_list"))

    @app.route("/products/<int:product_id>/delete", methods=["POST"])
    def product_delete(product_id):
        product = Product.query.get_or_404(product_id)
        from image_store import delete_product_images
        delete_product_images(product.id, app)
        db.session.delete(product)
        db.session.commit()
        flash(f"Removed \"{product.name}\".", "success")
        return redirect(url_for("shopping_list"))

    # ── Cart ──────────────────────────────────────────────────────────────────

    @app.route("/cart/add/<int:product_id>", methods=["POST"])
    def cart_add(product_id):
        cart = session.get("cart", [])
        if product_id not in cart:
            cart.append(product_id)
            session["cart"] = cart
        return jsonify(ok=True, cart_count=len(cart), in_cart=True)

    @app.route("/cart/remove/<int:product_id>", methods=["POST"])
    def cart_remove(product_id):
        cart = session.get("cart", [])
        cart = [pid for pid in cart if pid != product_id]
        session["cart"] = cart
        return jsonify(ok=True, cart_count=len(cart), in_cart=False)

    @app.route("/cart/clear", methods=["POST"])
    def cart_clear():
        session["cart"] = []
        flash("Cart cleared.", "info")
        return redirect(url_for("cart"))

    @app.route("/cart")
    def cart():
        cart_ids = session.get("cart", [])
        items = []
        if cart_ids:
            items = Product.query.filter(
                Product.id.in_(cart_ids),
                Product.status == "watching",
            ).all()
            # Prune stale IDs (deleted or already-purchased products).
            valid_ids = [p.id for p in items]
            if set(valid_ids) != set(cart_ids):
                session["cart"] = valid_ids

        # Total value grouped by currency.
        value_by_currency = {}
        for p in items:
            sym = p.currency_symbol
            qty = p.quantity or 1
            price = (p.current_price or 0) * qty
            value_by_currency[sym] = value_by_currency.get(sym, 0) + price
        value_by_currency = [(sym, total) for sym, total in value_by_currency.items() if total]

        return render_template(
            "cart.html",
            items=items,
            value_by_currency=value_by_currency,
        )

    @app.route("/cart/checkout", methods=["GET", "POST"])
    def cart_checkout():
        cart_ids = session.get("cart", [])
        items = []
        if cart_ids:
            items = Product.query.filter(
                Product.id.in_(cart_ids),
                Product.status == "watching",
            ).all()

        if not items:
            flash("Your cart is empty.", "warning")
            return redirect(url_for("cart"))

        if request.method == "GET":
            return render_template("checkout.html", items=items)

        # ── POST: process checkout ──
        purchased_at_str = request.form.get("purchased_at")
        if purchased_at_str:
            purchased_at = datetime.strptime(purchased_at_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        else:
            purchased_at = datetime.now(timezone.utc)

        errors = []
        for p in items:
            order_url = (request.form.get(f"order_details_url_{p.id}") or "").strip()
            tracking = (request.form.get(f"tracking_url_{p.id}") or "").strip()
            if not order_url and not tracking:
                errors.append(p.name)

        if errors:
            flash(
                f"Please provide at least one URL for: {', '.join(errors)}",
                "error",
            )
            return render_template("checkout.html", items=items)

        count = 0
        for p in items:
            order_url = (request.form.get(f"order_details_url_{p.id}") or "").strip()
            tracking = (request.form.get(f"tracking_url_{p.id}") or "").strip()
            paid_str = request.form.get(f"paid_amount_{p.id}", "")
            paid = _parse_price(paid_str) if paid_str.strip() else (p.current_price or 0)

            purchase = Purchase.query.filter_by(product_id=p.id).first()
            if purchase:
                purchase.paid_amount = paid
                purchase.purchased_at = purchased_at
                purchase.order_details_url = order_url
                purchase.tracking_url = tracking
            else:
                purchase = Purchase(
                    product_id=p.id,
                    paid_amount=paid,
                    purchased_at=purchased_at,
                    order_details_url=order_url,
                    tracking_url=tracking,
                )
                db.session.add(purchase)
            p.status = "purchased"
            count += 1

        db.session.commit()
        session["cart"] = []
        flash(f"Checked out {count} item{'s' if count != 1 else ''} successfully!", "success")
        return redirect(url_for("purchases"))

    # ── Compare ───────────────────────────────────────────────────────────────
    # The compare basket mirrors the cart pattern: a session-stored list of
    # product IDs the user wants to view side-by-side. Hard-capped at 4 so
    # the comparison view stays readable.

    COMPARE_MAX = 4

    @app.route("/compare/add/<int:product_id>", methods=["POST"])
    def compare_add(product_id):
        compare = session.get("compare", [])
        if product_id in compare:
            return jsonify(ok=True, compare_count=len(compare), in_compare=True)
        if len(compare) >= COMPARE_MAX:
            return jsonify(
                ok=False,
                error=f"Compare holds {COMPARE_MAX} items max.",
                compare_count=len(compare),
                in_compare=False,
            ), 409
        compare.append(product_id)
        session["compare"] = compare
        return jsonify(ok=True, compare_count=len(compare), in_compare=True)

    @app.route("/compare/remove/<int:product_id>", methods=["POST"])
    def compare_remove(product_id):
        compare = session.get("compare", [])
        compare = [pid for pid in compare if pid != product_id]
        session["compare"] = compare
        return jsonify(ok=True, compare_count=len(compare), in_compare=False)

    @app.route("/compare/clear", methods=["POST"])
    def compare_clear():
        session["compare"] = []
        flash("Compare list cleared.", "info")
        return redirect(url_for("compare"))

    @app.route("/compare")
    def compare():
        # URL-supplied ?ids= wins over the session basket. This lets a user
        # share or revisit a specific comparison without disturbing whatever
        # they're currently building.
        ids_param = request.args.get("ids", "").strip()
        if ids_param:
            try:
                ids = [int(x) for x in ids_param.split(",") if x.strip()]
            except ValueError:
                ids = []
        else:
            ids = list(session.get("compare", []))

        # Truncate at the hard cap; preserve URL order.
        ids = ids[:COMPARE_MAX]

        # Fetch products and re-order to match the requested ID order. Skip
        # missing IDs silently — they may have been deleted since the compare
        # list was saved.
        if ids:
            rows = Product.query.filter(Product.id.in_(ids)).all()
            by_id = {p.id: p for p in rows}
            products = [by_id[i] for i in ids if i in by_id]
        else:
            products = []

        return render_template("compare.html", products=products, max_items=COMPARE_MAX)

    # ── Purchases ─────────────────────────────────────────────────────────────

    def _build_purchase_query():
        """Parse request args and return (ordered_query, period, tag_filter,
        sort_key, order_key, search_q, active_date)."""
        period = request.args.get("period", "all")
        tag_filter = request.args.get("tag", "")
        search_q = (request.args.get("q") or "").strip()
        sort_key = request.args.get("sort", "date")
        order_key = request.args.get("order", "desc")
        now = datetime.utcnow()

        # Optional single-day filter (YYYY-MM-DD). When set it wins over `period`
        # — a specific day is strictly narrower than any rolling window.
        active_date = None
        date_str = (request.args.get("date") or "").strip()
        if date_str:
            try:
                active_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                active_date = None

        query = Purchase.query.join(Product, Purchase.product_id == Product.id, isouter=True)
        if active_date:
            next_day = active_date + timedelta(days=1)
            query = query.filter(
                Purchase.purchased_at >= active_date,
                Purchase.purchased_at < next_day,
            )
        elif period == "1m":
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

        if search_q:
            escaped = search_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.filter(Product.name.ilike(f"%{escaped}%", escape="\\"))

        if sort_key == "amount":
            sort_col = Purchase.paid_amount
        elif sort_key == "modified":
            sort_col = Product.updated_at
        else:
            sort_col = Purchase.purchased_at
        query = query.order_by(sort_col.asc() if order_key == "asc" else sort_col.desc())

        return query, period, tag_filter, sort_key, order_key, search_q, active_date

    @app.route("/purchases")
    def purchases():
        query, period, tag_filter, sort_key, order_key, search_q, active_date = (
            _build_purchase_query()
        )
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
            search_q=search_q,
            has_more=has_more,
            total_count=total_count,
            active_date=active_date,
        )

    @app.route("/api/purchases")
    def purchases_api():
        """Return the next page of purchase cards as an HTML fragment."""
        query, period, tag_filter, sort_key, order_key, search_q, _active_date = (
            _build_purchase_query()
        )
        offset = request.args.get("offset", 0, type=int)
        limit = request.args.get("limit", _get_page_size(), type=int)

        total_count = query.count()
        purchases_page = query.offset(offset).limit(limit).all()
        has_more = (offset + len(purchases_page)) < total_count
        next_offset = offset + len(purchases_page)

        html = render_template("_purchase_cards.html", purchases=purchases_page)
        return jsonify(html=html, has_more=has_more, next_offset=next_offset, total_count=total_count)

    @app.route("/purchases/calendar")
    def purchases_calendar():
        today = datetime.utcnow().date()
        try:
            year = int(request.args.get("y", today.year))
            month = int(request.args.get("m", today.month))
            if not (1 <= month <= 12):
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month

        tag_id = request.args.get("tag", "")

        first_of_month = date(year, month, 1)
        next_month_first = (
            date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        )

        month_query = Purchase.query.join(
            Product, Purchase.product_id == Product.id
        ).filter(
            Purchase.purchased_at >= first_of_month,
            Purchase.purchased_at < next_month_first,
        )
        year_start = date(year, 1, 1)
        year_end = date(year + 1, 1, 1)
        year_query = Purchase.query.join(
            Product, Purchase.product_id == Product.id
        ).filter(
            Purchase.purchased_at >= year_start,
            Purchase.purchased_at < year_end,
        )
        if tag_id:
            try:
                tag_int = int(tag_id)
                month_query = month_query.filter(Product.tags.any(Tag.id == tag_int))
                year_query = year_query.filter(Product.tags.any(Tag.id == tag_int))
            except (TypeError, ValueError):
                tag_id = ""

        by_day = {}
        for p in month_query.all():
            by_day.setdefault(p.purchased_at.date(), []).append(p)

        yearly_totals = {}
        for p in year_query.all():
            yearly_totals[p.purchased_at.month] = (
                yearly_totals.get(p.purchased_at.month, 0) + p.paid_amount
            )

        # Sunday-first weeks to match Apple Calendar's default.
        cal = stdlib_calendar.Calendar(firstweekday=6)
        weeks = cal.monthdatescalendar(year, month)

        prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
        next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

        return render_template(
            "purchases_calendar.html",
            year=year,
            month=month,
            month_name=stdlib_calendar.month_name[month],
            month_abbrs=[stdlib_calendar.month_abbr[m] for m in range(1, 13)],
            weeks=weeks,
            by_day=by_day,
            today=today,
            prev_y=prev_y,
            prev_m=prev_m,
            next_y=next_y,
            next_m=next_m,
            yearly_totals=yearly_totals,
            tags=Tag.query.order_by(Tag.name).all(),
            active_tag=tag_id,
        )

    # ── Tags ──────────────────────────────────────────────────────────────────

    @app.route("/tags")
    def tags():
        all_tags = Tag.query.order_by(Tag.name).all()
        wants_json = (
            request.args.get("format") == "json"
            or request.accept_mimetypes.best_match(
                ["text/html", "application/json"]
            ) == "application/json"
        )
        if wants_json:
            resp = jsonify(tags=[
                {"id": t.id, "name": t.name, "colour": getattr(t, "colour", None)}
                for t in all_tags
            ])
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
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
        s.monthly_income = _parse_price(request.form.get("monthly_income", "0"))
        s.shopping_budget = _parse_price(request.form.get("shopping_budget", "0"))
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))

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
        db_path = os.path.join(app.instance_path, "theo.db")
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
            "/purchases": _measure("/purchases", purchases),
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

    # Purchase.order_details_url / tracking_url added 2026-04 — at least one is
    # required when marking a product purchased (enforced at the route layer).
    if not column_exists("purchase", "order_details_url"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN order_details_url TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("purchase", "tracking_url"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN tracking_url TEXT DEFAULT ''"))
        db.session.commit()

    # Purchase.expected_delivery_at / delivered_at added 2026-05 — feed the
    # "Arriving today" calendar. Both nullable; existing rows backfill to NULL
    # which means they don't appear on the calendar (only matters for items
    # currently in awaiting_delivery — purchased rows never appear regardless).
    if not column_exists("purchase", "expected_delivery_at"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN expected_delivery_at DATETIME"))
        db.session.commit()
    if not column_exists("purchase", "delivered_at"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN delivered_at DATETIME"))
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
    from urls import sanitize_url
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




# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()


    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port, use_reloader=False)
