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
from sqlalchemy import func, or_, and_
from config import Config
from models import db, Product, Tag, Purchase, Settings, Currency, Publication, product_tags


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
        status_filter, tag_filter, search_q, domain_filter)."""
        tag_filter = request.args.get("tag")
        search_q = (request.args.get("q") or "").strip()
        status_filter = request.args.get("status", "all")
        if status_filter not in ("added", "purchased", "shipped", "received", "all"):
            status_filter = "all"
        domain_filter = (request.args.get("domain") or "").strip().lower()

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
        if domain_filter:
            # Match both bare and www-prefixed forms; the `//` anchor avoids
            # matching "fakeamazon.com" when filtering "amazon.com".
            query = query.filter(or_(
                Product.url.like(f"%//{domain_filter}%"),
                Product.url.like(f"%//www.{domain_filter}%"),
            ))

        primary = sort_col.asc() if order_key == "asc" else sort_col.desc()
        tiebreak = Product.id.asc() if order_key == "asc" else Product.id.desc()
        query = query.order_by(primary, tiebreak)

        return query, sort_key, order_key, status_filter, tag_filter, search_q, domain_filter

    def _all_product_domains():
        """Return the sorted unique list of normalized hostnames used by
        any product. 'www.' prefix stripped so amazon.com and www.amazon.com
        collapse to one entry. Empty/invalid URLs skipped.
        """
        from urllib.parse import urlparse
        seen = set()
        rows = db.session.query(Product.url).filter(Product.url != "").distinct().all()
        for (url,) in rows:
            try:
                host = (urlparse(url).hostname or "").lower()
            except Exception:
                continue
            if not host:
                continue
            if host.startswith("www."):
                host = host[4:]
            seen.add(host)
        return sorted(seen)

    @app.route("/products")
    def shopping_list():
        query, sort_key, order_key, status_filter, tag_filter, search_q, domain_filter = (
            _build_product_query()
        )

        tags = Tag.query.order_by(Tag.name).all()
        domains = _all_product_domains()

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
            domains=domains,
            domain_filter=domain_filter,
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
        query, sort_key, order_key, status_filter, tag_filter, search_q, domain_filter = (
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
            status="added",
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

        # Order/tracking/date edits apply to any product with a Purchase row
        # (purchased, shipped, received). We re-enforce the "at least one link"
        # invariant so a user can't blank both fields.
        if product.status in ("purchased", "shipped", "received") and product.purchase and (
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
            had_tracking = bool(product.purchase.tracking_url)
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

            # Auto-flip Purchased → Shipped when tracking is first added.
            # Reverse direction (Shipped → Purchased on tracking removal)
            # is intentionally NOT done; we never demote.
            if (not had_tracking) and tracking_url and product.status == "purchased":
                product.status = "shipped"

            # Auto-stamp delivered_at when expected date is in the past — and
            # promote the item to Received. Mirrors the rule baked into the
            # one-shot mark_overdue_as_received script, now part of the live
            # save flow so new past-dated entries self-correct.
            exp = product.purchase.expected_delivery_at
            if exp and not product.purchase.delivered_at:
                exp_date = exp.date() if hasattr(exp, "date") else exp
                if exp_date < datetime.now(timezone.utc).date():
                    product.purchase.delivered_at = exp
                    if product.status in ("purchased", "shipped"):
                        product.status = "received"

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
        # New target_status set: 'purchased' (intermediate, just bought) or
        # 'received' (already in hand). Tracking-link presence implicitly
        # promotes the 'purchased' case to 'shipped' via the auto-flip in
        # product_edit, so we don't need a separate Shipped target here.
        target_status = request.form.get("target_status", "purchased")
        if target_status not in ("purchased", "received"):
            target_status = "purchased"
        # If user explicitly marked Received, also stamp delivered_at now (if
        # not already set during purchase form submission).
        if target_status == "received" and not purchase.delivered_at:
            purchase.delivered_at = datetime.now(timezone.utc)
        # Or, when user marked Purchased but tracking is provided, the item is
        # already known to be in motion → Shipped. Mirrors product_edit's
        # auto-flip; keeps state consistent regardless of entry path.
        if target_status == "purchased" and tracking_url:
            target_status = "shipped"
        product.status = target_status
        db.session.commit()

        if target_status == "received":
            flash(f"Marked \"{product.name}\" as received for {_fmt_price(paid)}.", "success")
            return redirect(url_for("product_detail", product_id=product.id))
        if target_status == "shipped":
            flash(f"Marked \"{product.name}\" as shipped for {_fmt_price(paid)}.", "success")
            return redirect(url_for("product_detail", product_id=product.id))
        flash(f"Marked \"{product.name}\" as purchased for {_fmt_price(paid)}.", "success")
        return redirect(url_for("shopping_list"))

    @app.route("/products/<int:product_id>/arrived", methods=["POST"])
    def product_arrived(product_id):
        """Mark a purchased / shipped item as received.

        Sets delivered_at = now() and flips status to 'received' so the row
        drops off the Deliveries page. expected_delivery_at is
        preserved as the *original* signal for late-arrival analytics.
        """
        product = Product.query.get_or_404(product_id)
        if not product.purchase:
            flash(f"\"{product.name}\" has no purchase record yet.", "error")
            return redirect(url_for("purchases_calendar"))
        product.purchase.delivered_at = datetime.now(timezone.utc)
        if product.status in ("purchased", "shipped"):
            product.status = "received"
        db.session.commit()
        flash(f"Marked \"{product.name}\" as received.", "success")
        # Honour an optional ?next= so we can redirect back to wherever the
        # user clicked from (calendar, product detail, etc.).
        next_url = request.form.get("next") or url_for("purchases_calendar")
        return redirect(next_url)

    @app.route("/products/<int:product_id>/unreceived", methods=["POST"])
    def product_unreceived(product_id):
        """Inverse of /arrived — flips a Received item back to in-flight.

        Clears delivered_at and reverts status to 'shipped' (if tracking is
        on file — the typical case) or 'purchased' (no tracking). Everything
        else on the Purchase row (paid amount, links, expected date, notes)
        stays intact. Useful when an item gets auto-stamped but was actually
        still in transit.
        """
        product = Product.query.get_or_404(product_id)
        if not product.purchase:
            flash(f"\"{product.name}\" has no purchase record.", "error")
            return redirect(url_for("product_detail", product_id=product.id))
        product.purchase.delivered_at = None
        if product.purchase.tracking_url:
            product.status = "shipped"
        else:
            product.status = "purchased"
        db.session.commit()
        flash(f"\"{product.name}\" moved back to {product.status.title()}.", "success")
        return redirect(url_for("product_detail", product_id=product.id))

    @app.route("/products/<int:product_id>/unpurchase", methods=["POST"])
    def product_unpurchase(product_id):
        product = Product.query.get_or_404(product_id)
        if product.purchase:
            db.session.delete(product.purchase)
        product.status = "added"
        db.session.commit()
        flash(f"Moved \"{product.name}\" back to Added.", "success")
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

    @app.route("/products/<int:product_id>/review", methods=["POST"])
    def product_review(product_id):
        """Save / update the review for a purchased item.

        Reads review_text, review_video_url, and a JSON list of pasted
        photos (existing filenames + base64 data URIs). New images get
        saved via image_store with timestamped filenames so they don't
        collide with the product's main photos.
        """
        product = Product.query.get_or_404(product_id)
        if product.status not in ("purchased", "shipped", "received"):
            flash("Reviews are only available on items you've bought.", "error")
            return redirect(url_for("product_detail", product_id=product.id))

        product.review_text = request.form.get("review_text", "").strip()
        product.review_video_url = (request.form.get("review_video_url") or "").strip()

        # Photos: JSON list of strings — mix of existing filenames and new
        # data URIs. save_new_images_for_product handles both cleanly.
        photos_raw = request.form.get("review_photos") or "[]"
        try:
            incoming = json.loads(photos_raw)
        except (json.JSONDecodeError, TypeError):
            incoming = []
        if isinstance(incoming, list):
            from image_store import save_new_images_for_product
            product.review_photos = save_new_images_for_product(
                incoming, product.id, app
            )

        product.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Review saved.", "success")
        return redirect(url_for("product_detail", product_id=product.id))

    @app.route("/products/<int:product_id>/clone", methods=["GET", "POST"])
    def product_clone(product_id):
        """Two-step clone: GET renders a form pre-filled from the source;
        POST creates a new 'added' product from the (possibly edited) form
        values. Images and variants carry over verbatim — user can edit
        those on the new product after creation.
        """
        src = Product.query.get_or_404(product_id)

        if request.method == "GET":
            tags = Tag.query.order_by(Tag.name).all()
            currencies = Currency.query.order_by(Currency.code).all()
            return render_template(
                "clone.html",
                source=src,
                tags=tags,
                currencies=currencies,
            )

        # POST — read form values, fall back to source for unsubmitted fields.
        name = (request.form.get("name") or src.name).strip()
        url = (request.form.get("url") or src.url).strip()
        store = (request.form.get("store") or "").strip()
        notes = request.form.get("notes", "")
        try:
            quantity = max(1, int(request.form.get("quantity") or src.quantity or 1))
        except (TypeError, ValueError):
            quantity = src.quantity or 1
        price_raw = request.form.get("price", "")
        price = _parse_price(price_raw) if price_raw.strip() else (src.current_price or 0)
        try:
            currency_id = int(request.form.get("currency_id") or src.currency_id or 0) or None
        except (TypeError, ValueError):
            currency_id = src.currency_id
        tag_ids = request.form.getlist("tag_ids")

        clone = Product(
            url=url,
            name=name,
            store=store,
            current_price=price,
            original_price=price,  # treat the listed price at clone-time as the new "original"
            image_url=src.image_url,
            images=list(src.images or []),  # shallow-copy — see INSIGHTS.md
            variants=dict(src.variants or {}),
            notes=notes,
            quantity=quantity,
            currency_id=currency_id,
            status="added",
        )
        db.session.add(clone)
        db.session.flush()
        for tid in tag_ids:
            tag = Tag.query.get(int(tid))
            if tag:
                clone.tags.append(tag)
        db.session.commit()
        flash(f"Cloned \"{src.name}\" — edit the new copy as needed.", "success")
        return redirect(url_for("product_detail", product_id=clone.id))

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

    @app.route("/cart/select-and-checkout", methods=["POST"])
    def cart_select_and_checkout():
        """Multi-select shortcut from the Browse page.

        Replaces the session cart with the IDs from the form (sent as
        repeated `ids` fields by the selection bar's hidden inputs)
        and redirects to /cart/checkout. Restricted to currently-
        watching products — silently drops anything else so the user
        can't accidentally bulk-purchase already-purchased items.
        """
        raw_ids = request.form.getlist("ids")
        try:
            ids = [int(x) for x in raw_ids if x]
        except (TypeError, ValueError):
            ids = []

        if ids:
            valid_ids = [
                pid for (pid,) in db.session.query(Product.id)
                .filter(Product.id.in_(ids), Product.status == "added")
                .all()
            ]
        else:
            valid_ids = []

        if not valid_ids:
            flash("Nothing to check out — only Added items can be marked Purchased.", "warning")
            return redirect(url_for("shopping_list"))

        # If selection mixed Added with non-Added, surface that we're only
        # processing the Added subset — prevents quiet "I selected 5, only 2
        # got marked" surprises.
        skipped = len(ids) - len(valid_ids)
        if skipped:
            flash(f"Checking out {len(valid_ids)} Added item{'s' if len(valid_ids) != 1 else ''}; {skipped} non-Added item{'s' if skipped != 1 else ''} were skipped.", "info")
        session["cart"] = valid_ids
        return redirect(url_for("cart_checkout"))

    @app.route("/cart")
    def cart():
        cart_ids = session.get("cart", [])
        items = []
        if cart_ids:
            items = Product.query.filter(
                Product.id.in_(cart_ids),
                Product.status == "added",
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
                Product.status == "added",
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
            # Tracking-link presence promotes to Shipped; otherwise the item
            # is just-bought / Purchased (intermediate). Same rule as
            # product_purchase and product_edit's auto-flip.
            p.status = "shipped" if tracking else "purchased"
            count += 1

        db.session.commit()
        session["cart"] = []
        flash(f"Checked out {count} item{'s' if count != 1 else ''} successfully!", "success")
        return redirect(url_for("shopping_list"))

    # ── Compare ───────────────────────────────────────────────────────────────
    # Read-only side-by-side view. The selection driving it lives client-side
    # in shopping_list.html (the same selectedIds Set the Cart uses); the
    # Compare nav link navigates here with ?ids=1,2,3. No server-side basket.
    COMPARE_MAX = 4

    @app.route("/compare")
    def compare():
        # IDs come from the URL query string only — there's no session basket.
        # The Compare nav link in shopping_list.html builds the URL from the
        # client-side selectedIds Set on click.
        ids_param = request.args.get("ids", "").strip()
        ids = []
        if ids_param:
            try:
                ids = [int(x) for x in ids_param.split(",") if x.strip()]
            except ValueError:
                ids = []

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

    # ── Publish (selection → static HTML on GitHub) ───────────────────────────
    # Flow: client selects cards → Publish nav → /publish?ids=… → form for
    # page name + (if multiple repos) repo picker → POST /publish/create →
    # render HTML → push to GitHub → regenerate index.html → redirect.

    def _resolve_publish_targets(s):
        """Return the list of usable repos in "owner/name" form. If user
        configured none but is connected, the default
        <username>/<username>.github.io is the implicit single target.
        Empty list ⇒ no token configured → caller redirects to /settings.
        """
        if not s.github_token or not s.github_username:
            return []
        repos = list(s.github_repos or [])
        if not repos:
            u = s.github_username
            repos = [f"{u}/{u}.github.io"]
        return repos

    def _slugify(name):
        """Web-safe lowercase slug. Falls back to "page" if input is empty."""
        import re as _re
        slug = _re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
        return slug or "page"

    def _public_url(repo, slug):
        """Public Pages URL for owner/name + filename slug. Both `<u>.github.io`
        and `<u>/<repo>` flavours. Theo doesn't *enable* Pages — user has to
        do that on github.com — but we can predict the URL regardless.
        """
        owner, _, name = repo.partition("/")
        if name == f"{owner}.github.io":
            return f"https://{owner}.github.io/{slug}.html"
        return f"https://{owner}.github.io/{name}/{slug}.html"

    @app.route("/publish")
    def publish():
        """GET → form. The selection comes via ?ids=1,2,3 (set by the nav
        click handler in shopping_list.html, mirroring the Compare flow).
        """
        s = Settings.get()
        if not s.github_token:
            flash("Connect GitHub first to publish.", "warning")
            return redirect(url_for("settings_github"))

        repos = _resolve_publish_targets(s)
        ids_param = request.args.get("ids", "").strip()
        try:
            ids = [int(x) for x in ids_param.split(",") if x.strip()]
        except ValueError:
            ids = []
        if not ids:
            flash("No items selected to publish.", "warning")
            return redirect(url_for("shopping_list"))

        rows = Product.query.filter(Product.id.in_(ids)).all()
        by_id = {p.id: p for p in rows}
        products = [by_id[i] for i in ids if i in by_id]
        return render_template("publish_form.html", products=products, repos=repos, settings=s)

    @app.route("/publish/create", methods=["POST"])
    def publish_create():
        """POST handler — render HTML, push to GitHub, record Publication."""
        import requests as _req
        import base64 as _b64
        s = Settings.get()
        if not s.github_token:
            flash("Connect GitHub first to publish.", "warning")
            return redirect(url_for("settings_github"))

        # Parse form
        page_name = (request.form.get("page_name") or "").strip()
        if not page_name:
            flash("Page name required.", "error")
            return redirect(request.referrer or url_for("shopping_list"))
        slug = _slugify(page_name)

        repos = _resolve_publish_targets(s)
        repo = (request.form.get("repo") or "").strip() or (repos[0] if repos else "")
        if not repo:
            flash("No publish target available.", "error")
            return redirect(url_for("settings_github"))

        try:
            ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip()]
        except ValueError:
            ids = []
        if not ids:
            flash("No items to publish.", "warning")
            return redirect(url_for("shopping_list"))
        rows = Product.query.filter(Product.id.in_(ids)).all()
        by_id = {p.id: p for p in rows}
        products = [by_id[i] for i in ids if i in by_id]

        # Render the static page. _published_page.html is a standalone
        # document (no {% extends %}). Image references get inlined to data
        # URIs by the template via a custom filter so the HTML is single-file.
        html = render_template(
            "_published_page.html",
            page_title=page_name,
            products=products,
            published_at=datetime.now(timezone.utc),
        )

        # Push the page
        ok, err = _github_put_file(
            token=s.github_token,
            repo=repo,
            branch=s.github_branch or "main",
            path=f"{slug}.html",
            content_str=html,
            commit_message=f"Publish: {page_name}",
        )
        if not ok:
            flash(f"Push failed: {err}", "error")
            return redirect(url_for("shopping_list"))

        # Record Publication (upsert on repo+slug)
        existing = Publication.query.filter_by(repo=repo, slug=slug).first()
        if existing:
            existing.name = page_name
            existing.item_count = len(products)
            existing.updated_at = datetime.now(timezone.utc)
            existing.url = _public_url(repo, slug)
        else:
            db.session.add(Publication(
                repo=repo, slug=slug, name=page_name,
                item_count=len(products),
                url=_public_url(repo, slug),
            ))
        db.session.commit()

        # Regenerate index.html for this repo from all known Publications
        pubs = (Publication.query
                .filter_by(repo=repo)
                .order_by(Publication.updated_at.desc())
                .all())
        index_html = render_template(
            "_published_index.html",
            repo=repo,
            publications=pubs,
            published_at=datetime.now(timezone.utc),
        )
        ok2, err2 = _github_put_file(
            token=s.github_token,
            repo=repo,
            branch=s.github_branch or "main",
            path="index.html",
            content_str=index_html,
            commit_message=f"Update index for {page_name}",
        )
        if not ok2:
            flash(f"Page published but index update failed: {err2}", "warning")

        # Render a success page instead of redirecting — it opens the
        # published URL in a new tab via JS (mirrors Roger's "open in
        # background" UX) and falls back to a manual link if popups
        # are blocked. After ~1.5s it sends the user to /products.
        return render_template(
            "publish_success.html",
            page_name=page_name,
            url=_public_url(repo, slug),
            repo=repo,
        )

    def _github_put_file(token, repo, branch, path, content_str, commit_message):
        """PUT a file to a GitHub repo via Contents API. Handles update-vs-create
        (needs SHA when updating). Returns (ok: bool, err: str|None).
        """
        import requests as _req
        import base64 as _b64
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        # Get current SHA if file exists (need it to update)
        sha = None
        try:
            g = _req.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers,
                params={"ref": branch},
                timeout=15,
            )
            if g.status_code == 200:
                sha = g.json().get("sha")
            elif g.status_code not in (404,):
                return False, f"GET {path} → {g.status_code}"
        except _req.RequestException as e:
            return False, f"GET {path} network: {e.__class__.__name__}"

        # Encode content
        if isinstance(content_str, str):
            content_b64 = _b64.b64encode(content_str.encode("utf-8")).decode("ascii")
        else:
            content_b64 = _b64.b64encode(content_str).decode("ascii")

        body = {
            "message": commit_message,
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        try:
            p = _req.put(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers,
                json=body,
                timeout=20,
            )
        except _req.RequestException as e:
            return False, f"PUT {path} network: {e.__class__.__name__}"
        if p.status_code in (200, 201):
            return True, None
        try:
            err = p.json().get("message", f"HTTP {p.status_code}")
        except Exception:
            err = f"HTTP {p.status_code}"
        return False, f"PUT {path}: {err}"

    # ── Purchases page removed — /products now lists everything by default. ──

    @app.route("/purchases/calendar")
    def purchases_calendar():
        """Deliveries — chronological list of items in flight.

        Shows Purchased + Shipped items that haven't yet been marked
        delivered. Each row is bucketed under
        `display_date = max(expected_delivery_at, today)` so items past
        their expected date silently roll forward to "today" rather
        than appearing in a separate Overdue section. Items without
        an expected_delivery_at fall to the "Date not set" footer
        where the user can fill in a date inline.
        """
        today = date.today()

        rows = (
            Purchase.query
            .join(Product, Purchase.product_id == Product.id)
            .filter(
                # Both Purchased (just bought, no tracking) and Shipped
                # (in transit) belong on the Deliveries page — anything
                # not yet Received with a known or unknown date.
                Product.status.in_(("purchased", "shipped")),
                Purchase.delivered_at.is_(None),
            )
            .all()
        )

        # Split: items with an expected date go into the chronological
        # buckets; items without go to the "Date not set" footer.
        by_day = {}
        undated = []
        for p in rows:
            if p.expected_delivery_at:
                edate = p.expected_delivery_at.date()
                display_date = edate if edate >= today else today
                by_day.setdefault(display_date, []).append(p)
            else:
                undated.append(p)

        # Within a day, prefer the longest-overdue items first (they've
        # been waiting the most). Falls back to purchase date for ties.
        for day_list in by_day.values():
            day_list.sort(
                key=lambda p: (p.expected_delivery_at, p.purchased_at)
            )

        # Newest unfulfilled-purchase undated rows first — feels right
        # for the "fill these in" CTA section.
        undated.sort(key=lambda p: p.purchased_at, reverse=True)

        days = sorted(by_day.keys())
        return render_template(
            "purchases_calendar.html",
            days=days,
            by_day=by_day,
            undated=undated,
            today=today,
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
        # Settings is now a tabbed surface — bounce to GitHub as the default.
        return redirect(url_for("settings_github"), code=302)

    @app.route("/settings/github")
    def settings_github():
        return render_template("settings.html", settings=Settings.get(), tab="github")

    @app.route("/settings/about")
    def settings_about():
        return render_template("settings.html", settings=Settings.get(), tab="about")

    @app.route("/settings/stats")
    def settings_stats():
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
            "/tags": _measure("/tags", tags),
            "/settings": _measure("/settings", settings),
        }
        sample = Product.query.first()
        if sample:
            load_times["/products/<id>"] = _measure(
                f"/products/{sample.id}", product_detail, product_id=sample.id
            )

        return render_template(
            "settings.html",
            settings=Settings.get(),
            tab="stats",
            db_size=db_size,
            load_times=load_times,
        )

    @app.route("/settings/github", methods=["POST"])
    def settings_save():
        """Save GitHub publishing settings + verify connection.

        Repos arrive as repeated `github_repos[]` form fields. Empty entries
        and duplicates are dropped. Token: empty submission keeps existing,
        "__clear__" wipes it.
        """
        import requests as _req
        s = Settings.get()

        # Branch
        s.github_branch = (request.form.get("github_branch") or "main").strip() or "main"

        # Repos — list of strings, dedupe + strip empty.
        raw_repos = request.form.getlist("github_repos[]")
        cleaned = []
        for r in raw_repos:
            r = (r or "").strip()
            if r and r not in cleaned:
                cleaned.append(r)
        s.github_repos = cleaned

        # Token sentinel handling
        token_raw = request.form.get("github_token", "")
        if token_raw == "__clear__":
            s.github_token = ""
            s.github_username = ""
        elif token_raw.strip():
            s.github_token = token_raw.strip()
        db.session.commit()

        # Verify token + cache username if set
        if s.github_token:
            try:
                u_resp = _req.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {s.github_token}",
                        "Accept": "application/vnd.github+json",
                    },
                    timeout=10,
                )
                if u_resp.status_code == 200:
                    s.github_username = u_resp.json().get("login", "") or ""
                    db.session.commit()
                elif u_resp.status_code == 401:
                    flash("GitHub rejected the token (401). Check it's valid.", "warning")
                    return redirect(url_for("settings_github"))
            except _req.RequestException as e:
                flash(f"Couldn't reach GitHub ({e.__class__.__name__}).", "warning")
                return redirect(url_for("settings_github"))

        # Validate each configured repo individually
        if s.github_token and s.github_repos:
            failures = []
            for repo in s.github_repos:
                try:
                    r = _req.get(
                        f"https://api.github.com/repos/{repo}",
                        headers={
                            "Authorization": f"Bearer {s.github_token}",
                            "Accept": "application/vnd.github+json",
                        },
                        timeout=10,
                    )
                    if r.status_code != 200:
                        failures.append(f"{repo} ({r.status_code})")
                except _req.RequestException:
                    failures.append(f"{repo} (network)")
            if failures:
                flash(f"Repo check failed for: {', '.join(failures)}.", "warning")
            else:
                user_label = f" as {s.github_username}" if s.github_username else ""
                if s.github_repos:
                    flash(f"Connected{user_label}. {len(s.github_repos)} repo{'s' if len(s.github_repos) != 1 else ''} verified.", "success")
                else:
                    flash(f"Connected{user_label}. Default target: {s.github_username}.github.io", "success")
        elif s.github_token:
            flash(f"Connected as {s.github_username}. Default target: {s.github_username}.github.io", "success")
        else:
            flash("Settings saved.", "info")
        return redirect(url_for("settings_github"))

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

    @app.template_filter("days_late")
    def days_late_filter(dt):
        """Days dt is in the past (positive int) or None if not late.

        Used to render the "(N days late)" badge on overdue purchase
        cards. Returns None for None input, future dates, and today —
        so templates can write `{% if exp|days_late %}` cleanly.
        """
        if not dt:
            return None
        today = datetime.now(timezone.utc).date()
        target = dt.date() if hasattr(dt, "date") else dt
        delta = (today - target).days
        return delta if delta > 0 else None

    @app.template_filter("inline_image")
    def inline_image_filter(path):
        """Read an image from instance/images/ and return a base64 data URI.
        Used by the publish templates so the static HTML is fully self-contained
        (no external image hosting needed). Returns "" for missing files.
        """
        import base64 as _b64
        import mimetypes as _mt
        if not path:
            return ""
        # Normalize: strip any 'images/' prefix; the filter expects raw filenames.
        fname = path.split("/")[-1]
        full = os.path.join(app.instance_path, "images", fname)
        if not os.path.exists(full):
            return ""
        mime = _mt.guess_type(fname)[0] or "image/jpeg"
        with open(full, "rb") as f:
            data = _b64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{data}"

    @app.template_filter("min_delivery_date")
    def min_delivery_date_filter(dt):
        """YYYY-MM-DD for the day *after* dt — used as the `min` attribute on
        delivery-date pickers so users can't pick a date on or before the
        order/purchase date. Returns "" for None so the attr is harmless.
        """
        if not dt:
            return ""
        target = dt.date() if hasattr(dt, "date") else dt
        return (target + timedelta(days=1)).isoformat()

    @app.template_filter("tojson_safe")
    def tojson_safe_filter(value):
        return json.dumps(value) if value else "[]"

    # ── About / Stats redirects (now tabs inside /settings) ──────────────────

    @app.route("/about")
    def about():
        return redirect(url_for("settings_about"), code=302)

    # ── Reports — monthly summaries computed on demand from Purchase rows ────

    def _compute_month_report(year, month):
        """Return (totals_by_currency, item_count, by_tag) for a given month.

        totals_by_currency: list of (symbol, total) tuples, summed across
                            every Purchase made that month.
        item_count:         number of Purchase rows in scope.
        by_tag:             list of dicts {tag, count, totals: {sym: amt}}
                            sorted by count desc. An untagged bucket appears
                            as tag=None when present.
        """
        from datetime import date as _date
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        purchases = (
            Purchase.query
            .join(Product, Purchase.product_id == Product.id)
            .filter(Purchase.purchased_at >= start)
            .filter(Purchase.purchased_at < end)
            .all()
        )

        totals = {}              # symbol → running total
        per_tag = {}             # tag.id (or None) → {tag, count, totals}
        for p in purchases:
            prod = p.product
            if not prod:
                continue
            sym = prod.currency_symbol  # respects per-product currency
            totals[sym] = totals.get(sym, 0) + (p.paid_amount or 0)
            tags = list(prod.tags) if prod.tags else [None]
            for t in tags:
                key = t.id if t else None
                if key not in per_tag:
                    per_tag[key] = {"tag": t, "count": 0, "totals": {}}
                per_tag[key]["count"] += 1
                per_tag[key]["totals"][sym] = per_tag[key]["totals"].get(sym, 0) + (p.paid_amount or 0)

        totals_list = sorted(totals.items(), key=lambda kv: -kv[1])
        by_tag = sorted(per_tag.values(), key=lambda d: -d["count"])
        return totals_list, len(purchases), by_tag

    def _months_with_purchases():
        """Distinct (year, month) of any Purchase, newest first."""
        rows = (
            db.session.query(
                func.strftime("%Y", Purchase.purchased_at).label("y"),
                func.strftime("%m", Purchase.purchased_at).label("m"),
            )
            .distinct()
            .all()
        )
        out = []
        for y, m in rows:
            try:
                out.append((int(y), int(m)))
            except (TypeError, ValueError):
                continue
        return sorted(out, reverse=True)

    @app.route("/reports")
    def reports():
        months = _months_with_purchases()
        # Pre-compute lightweight summary per month for the index card list.
        summaries = []
        for (y, m) in months:
            totals, count, _ = _compute_month_report(y, m)
            summaries.append({
                "year": y, "month": m,
                "label": datetime(y, m, 1).strftime("%B %Y"),
                "slug": f"{y:04d}-{m:02d}",
                "totals": totals,
                "count": count,
            })
        return render_template("reports_index.html", summaries=summaries)

    @app.route("/reports/<period>")
    def report_month(period):
        # period format: YYYY-MM
        try:
            y_str, m_str = period.split("-", 1)
            year, month = int(y_str), int(m_str)
            if not (1 <= month <= 12):
                raise ValueError
        except (TypeError, ValueError):
            flash("Invalid month.", "error")
            return redirect(url_for("reports"))
        totals, count, by_tag = _compute_month_report(year, month)
        label = datetime(year, month, 1).strftime("%B %Y")
        return render_template(
            "report_month.html",
            label=label,
            year=year,
            month=month,
            totals=totals,
            count=count,
            by_tag=by_tag,
        )

    # ── Stats ────────────────────────────────────────────────────────────────

    @app.route("/stats")
    def stats():
        return redirect(url_for("settings_stats"), code=302)

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
    # "Deliveries" page. Both nullable; existing rows backfill to NULL
    # which means they don't appear on the calendar (only matters for items
    # currently in awaiting_delivery — purchased rows never appear regardless).
    if not column_exists("purchase", "expected_delivery_at"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN expected_delivery_at DATETIME"))
        db.session.commit()
    if not column_exists("purchase", "delivered_at"):
        db.session.execute(text("ALTER TABLE purchase ADD COLUMN delivered_at DATETIME"))
        db.session.commit()

    # Review fields on Product added 2026-05 — text, photos, video link.
    if not column_exists("product", "review_text"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN review_text TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("product", "review_video_url"):
        db.session.execute(text("ALTER TABLE product ADD COLUMN review_video_url TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("product", "review_photos"):
        # SQLite stores JSON as TEXT; SQLAlchemy reads/writes it transparently.
        db.session.execute(text("ALTER TABLE product ADD COLUMN review_photos TEXT DEFAULT '[]'"))
        db.session.commit()

    # GitHub publishing credentials added 2026-05.
    if not column_exists("settings", "github_token"):
        db.session.execute(text("ALTER TABLE settings ADD COLUMN github_token TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("settings", "github_repo"):
        db.session.execute(text("ALTER TABLE settings ADD COLUMN github_repo TEXT DEFAULT ''"))
        db.session.commit()
    if not column_exists("settings", "github_branch"):
        db.session.execute(text("ALTER TABLE settings ADD COLUMN github_branch TEXT DEFAULT 'main'"))
        db.session.commit()

    # Multi-repo upgrade: github_repo (singular) → github_repos (JSON list).
    # Old column stays orphaned for safety; we just stop using it.
    if not column_exists("settings", "github_repos"):
        db.session.execute(text("ALTER TABLE settings ADD COLUMN github_repos TEXT DEFAULT '[]'"))
        # Backfill from the old singular column if it had a value.
        db.session.execute(text("""
            UPDATE settings
            SET github_repos = '["' || github_repo || '"]'
            WHERE github_repo IS NOT NULL AND github_repo != '' AND (github_repos IS NULL OR github_repos = '[]')
        """))
        db.session.commit()
    if not column_exists("settings", "github_username"):
        db.session.execute(text("ALTER TABLE settings ADD COLUMN github_username TEXT DEFAULT ''"))
        db.session.commit()

    # Publication table — see models.Publication. SQLAlchemy create_all
    # below will create it on a fresh DB; nothing to do here for an
    # upgrading DB beyond letting SQLAlchemy's metadata catch up.

    # Settings.state_refactor_done added 2026-05 — gates the one-shot
    # listing-states migration below (watching/awaiting_delivery/purchased
    # → added/purchased/shipped/received).
    if not column_exists("settings", "state_refactor_done"):
        db.session.execute(text(
            "ALTER TABLE settings ADD COLUMN state_refactor_done BOOLEAN NOT NULL DEFAULT 0"
        ))
        db.session.commit()

    # ── Listing states refactor (one-shot) ──
    # Maps the old three-state model (watching/awaiting_delivery/purchased)
    # to the new four-state model (added/purchased/shipped/received).
    # Order matters: rename the OLD 'purchased' rows before any new 'purchased'
    # rows are produced, so we don't double-process. Sentinel approach.
    settings_row = Settings.get()
    if not settings_row.state_refactor_done:
        from datetime import date as _date
        today_iso = _date.today().isoformat()

        # Step 1: park current 'purchased' rows under a sentinel so subsequent
        # UPDATEs producing new 'purchased' values don't re-touch them.
        db.session.execute(text(
            "UPDATE product SET status='__migrating_old_purchased__' WHERE status='purchased'"
        ))

        # Step 2a: items that already have evidence of arrival → received.
        db.session.execute(text("""
            UPDATE product SET status='received'
            WHERE status='__migrating_old_purchased__'
            AND id IN (
                SELECT product_id FROM purchase WHERE delivered_at IS NOT NULL
            )
        """))
        # Step 2b: items with a past expected date but no delivered_at — stamp
        # delivered_at = expected_delivery_at and call them received. Mirrors
        # mark_overdue_as_received.py's logic so the script becomes redundant.
        db.session.execute(text(f"""
            UPDATE purchase SET delivered_at = expected_delivery_at
            WHERE delivered_at IS NULL
            AND expected_delivery_at IS NOT NULL
            AND expected_delivery_at < '{today_iso}'
            AND product_id IN (
                SELECT id FROM product WHERE status='__migrating_old_purchased__'
            )
        """))
        db.session.execute(text("""
            UPDATE product SET status='received'
            WHERE status='__migrating_old_purchased__'
            AND id IN (
                SELECT product_id FROM purchase WHERE delivered_at IS NOT NULL
            )
        """))
        # Step 2c: remaining 'old purchased' rows have no arrival evidence.
        # Split by tracking_url presence: tracking → shipped, else stay
        # 'purchased' (intermediate).
        db.session.execute(text("""
            UPDATE product SET status='shipped'
            WHERE status='__migrating_old_purchased__'
            AND id IN (
                SELECT product_id FROM purchase
                WHERE tracking_url IS NOT NULL AND tracking_url != ''
            )
        """))
        db.session.execute(text(
            "UPDATE product SET status='purchased' WHERE status='__migrating_old_purchased__'"
        ))

        # Step 3: split awaiting_delivery → shipped (has tracking) or
        # purchased (no tracking).
        db.session.execute(text("""
            UPDATE product SET status='shipped'
            WHERE status='awaiting_delivery'
            AND id IN (
                SELECT product_id FROM purchase
                WHERE tracking_url IS NOT NULL AND tracking_url != ''
            )
        """))
        db.session.execute(text(
            "UPDATE product SET status='purchased' WHERE status='awaiting_delivery'"
        ))

        # Step 4: rename watching → added.
        db.session.execute(text("UPDATE product SET status='added' WHERE status='watching'"))

        # Done — set the marker so this block becomes a no-op on subsequent boots.
        settings_row.state_refactor_done = True
        db.session.commit()
        print("[Theo] Listing-states refactor migration applied.")

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
