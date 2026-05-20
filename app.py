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
from models import db, Product, Tag, Purchase, Settings, Currency, Publication, Brand, ImageHash, product_tags, product_related


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

    @app.route("/surprise")
    def surprise():
        """Redirect to a random product's detail page. SQLite RANDOM() is
        fine at this scale (a few hundred rows); for a much larger DB
        we'd swap to OFFSET random_int."""
        p = Product.query.order_by(func.random()).limit(1).first()
        if not p:
            flash("No products to surprise you with yet.", "info")
            return redirect(url_for("shopping_list"))
        return redirect(url_for("product_detail", product_id=p.id))

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
            like = f"%{escaped}%"
            # Match product name OR tracking URL — pasting a bare tracking
            # number finds the parcel even though the carrier-specific
            # querystring isn't part of the typed query.
            query = query.filter(or_(
                Product.name.ilike(like, escape="\\"),
                Product.purchase.has(Purchase.tracking_url.ilike(like, escape="\\")),
            ))
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
        currencies = Currency.query.order_by(Currency.code).all()

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
                like = f"%{escaped}%"
                val_query = val_query.filter(or_(
                    Product.name.ilike(like, escape="\\"),
                    Product.purchase.has(Purchase.tracking_url.ilike(like, escape="\\")),
                ))
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
            currencies=currencies,
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
        related = product.related_products()
        related_ids = {p.id for p in related}
        # Lightweight catalogue for the "Add related" picker — id + name +
        # store + thumbnail. Excludes self and already-linked products so
        # the picker is pre-filtered.
        pickable = [
            {"id": p.id, "name": p.name, "store": p.store or "",
             "image": p.image_url or (p.images[0] if p.images else "")}
            for p in Product.query.order_by(Product.name).all()
            if p.id != product.id and p.id not in related_ids
        ]
        return render_template(
            "product_detail.html",
            product=product,
            tags=tags,
            currencies=currencies,
            related=related,
            pickable=pickable,
        )

    @app.route("/products/<int:product_id>/related/add", methods=["POST"])
    def product_related_add(product_id):
        product = Product.query.get_or_404(product_id)
        raw_ids = request.form.getlist("other_ids") or request.form.getlist("other_ids[]")
        added = 0
        for raw in raw_ids:
            try:
                oid = int(raw)
            except (TypeError, ValueError):
                continue
            if oid == product.id:
                continue
            other = Product.query.get(oid)
            if other:
                product.link_related(oid)
                added += 1
        db.session.commit()
        if added:
            flash(f"Linked {added} related item{'s' if added != 1 else ''}.", "success")
        return redirect(url_for("product_detail", product_id=product.id))

    @app.route("/products/<int:product_id>/related/remove/<int:other_id>", methods=["POST"])
    def product_related_remove(product_id, other_id):
        product = Product.query.get_or_404(product_id)
        product.unlink_related(other_id)
        db.session.commit()
        return redirect(url_for("product_detail", product_id=product.id))

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

    @app.route("/products/create", methods=["POST"])
    def product_create():
        """Manual create from the homepage FAB modal. Roger remains the
        primary path; this is the fallback for one-offs and anything the
        scraper missed. Mandatory: name + url. Everything else optional."""
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        if not name or not url:
            flash("Name and URL are required.", "error")
            return redirect(url_for("shopping_list"))

        # Reject obvious dupes by URL — the same guardrail import uses.
        if Product.query.filter_by(url=url).first():
            flash("A listing with that URL already exists.", "warning")
            return redirect(url_for("shopping_list"))

        store = (request.form.get("store") or "").strip()
        notes = request.form.get("notes", "")
        try:
            quantity = max(1, int(request.form.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1

        price_raw = (request.form.get("price") or "").strip()
        price = _parse_price(price_raw) if price_raw else None
        try:
            currency_id = int(request.form.get("currency_id") or 0) or None
        except (TypeError, ValueError):
            currency_id = None

        # Pasted/dropped photos arrive as a JSON list of data: URIs in the
        # hidden field. Persist them after we have a product id, matching
        # the review-form pattern.
        try:
            incoming_images = json.loads(request.form.get("images") or "[]")
        except (json.JSONDecodeError, TypeError):
            incoming_images = []
        if not isinstance(incoming_images, list):
            incoming_images = []

        product = Product(
            url=url,
            name=name,
            store=store,
            current_price=price,
            original_price=price,
            image_url="",
            images=[],
            notes=notes,
            quantity=quantity,
            currency_id=currency_id,
            status="added",
        )
        db.session.add(product)
        db.session.flush()  # need product.id before saving images

        if incoming_images:
            from image_store import save_new_images_for_product
            saved = save_new_images_for_product(incoming_images, product.id, app)
            product.images = saved
            product.image_url = saved[0] if saved else ""

        for tid in request.form.getlist("tag_ids"):
            try:
                tag = Tag.query.get(int(tid))
            except (TypeError, ValueError):
                continue
            if tag:
                product.tags.append(tag)
        db.session.commit()
        flash(f"Added \"{name}\".", "success")
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

        # Photos arrive as a JSON list mixing existing filenames (inherited
        # from the source and not removed by the user) with new data: URIs
        # from paste/drop/upload. save_new_images_for_product handles both.
        try:
            incoming_images = json.loads(request.form.get("images") or "[]")
        except (json.JSONDecodeError, TypeError):
            incoming_images = []
        if not isinstance(incoming_images, list):
            incoming_images = []

        clone = Product(
            url=url,
            name=name,
            store=store,
            current_price=price,
            original_price=price,  # treat the listed price at clone-time as the new "original"
            image_url="",
            images=[],
            variants=dict(src.variants or {}),
            notes=notes,
            quantity=quantity,
            currency_id=currency_id,
            status="added",
        )
        db.session.add(clone)
        db.session.flush()

        if incoming_images:
            from image_store import save_new_images_for_product
            saved = save_new_images_for_product(incoming_images, clone.id, app)
            clone.images = saved
            clone.image_url = saved[0] if saved else ""

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

        # Existing publications grouped by repo, so the "Update existing"
        # picker can show only the pages that live on the currently-chosen
        # repo. Newest first matches how /settings/github lists them.
        pubs_by_repo = {}
        for p in (Publication.query
                  .filter(Publication.repo.in_(repos))
                  .order_by(Publication.updated_at.desc())
                  .all()):
            pubs_by_repo.setdefault(p.repo, []).append({
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "item_count": p.item_count,
                "has_item_ids": bool(p.item_ids),
            })

        return render_template(
            "publish_form.html",
            products=products,
            repos=repos,
            settings=s,
            pubs_by_repo=pubs_by_repo,
        )

    @app.route("/publish/create", methods=["POST"])
    def publish_create():
        """POST handler — render HTML, push to GitHub, record Publication."""
        import requests as _req
        import base64 as _b64
        s = Settings.get()
        if not s.github_token:
            flash("Connect GitHub first to publish.", "warning")
            return redirect(url_for("settings_github"))

        # Mode: "new" (default) | "add" (merge into existing) | "replace"
        # (rewrite existing). For "add"/"replace", target_pub_id picks
        # the Publication we're operating on; page_name + slug come from
        # there, not the form.
        mode = (request.form.get("mode") or "new").strip()
        target_pub = None
        if mode in ("add", "replace"):
            try:
                pub_id = int(request.form.get("target_pub_id") or 0)
            except ValueError:
                pub_id = 0
            target_pub = Publication.query.get(pub_id) if pub_id else None
            if not target_pub:
                flash("Pick an existing page to update.", "error")
                return redirect(request.referrer or url_for("shopping_list"))

        if target_pub:
            page_name = target_pub.name
            slug = target_pub.slug
            repo = target_pub.repo
        else:
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
            new_ids = [int(x) for x in (request.form.get("ids") or "").split(",") if x.strip()]
        except ValueError:
            new_ids = []
        if not new_ids:
            flash("No items to publish.", "warning")
            return redirect(url_for("shopping_list"))

        # Final id list depends on mode. For "add", the new selection goes
        # to the TOP of the page, followed by the previously-published items
        # that weren't re-selected. Re-selecting an existing item bubbles it
        # up to the front. For everything else, the form's selection is
        # authoritative.
        recovered_count = 0
        if mode == "add" and target_pub:
            existing_ids = list(target_pub.item_ids or [])
            # Legacy publications (created before item_ids existed) have an
            # empty list stored. Recover the content by scraping the live
            # HTML — URL is the stable identifier across builds.
            if not existing_ids:
                existing_ids = _recover_publication_ids(
                    s.github_token, target_pub.repo,
                    s.github_branch or "main", target_pub.slug,
                )
                recovered_count = len(existing_ids)
            ids = new_ids + [i for i in existing_ids if i not in new_ids]
        else:
            ids = new_ids

        rows = Product.query.filter(Product.id.in_(ids)).all()
        by_id = {p.id: p for p in rows}
        products = [by_id[i] for i in ids if i in by_id]
        final_ids = [p.id for p in products]

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
            existing.item_ids = final_ids
            existing.updated_at = datetime.now(timezone.utc)
            existing.url = _public_url(repo, slug)
        else:
            db.session.add(Publication(
                repo=repo, slug=slug, name=page_name,
                item_count=len(products),
                item_ids=final_ids,
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

        # Surface recovery so the user knows the merge actually merged.
        if mode == "add" and recovered_count:
            flash(
                f"Recovered {recovered_count} item{'s' if recovered_count != 1 else ''} "
                f"from the live page (legacy publication) and merged with the new selection.",
                "info",
            )

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

    def _recover_publication_ids(token, repo, branch, slug):
        """Best-effort recovery of the product ids that compose an already-
        published page. Used as a fallback for legacy Publication rows
        created before item_ids existed (commit 4edc96d): fetch the live
        HTML, regex out each card's product href, and look up by URL.

        Returns a list of ids in source-page order, or [] if the fetch /
        parse fails or no urls match local products. Silently drops cards
        whose URL no longer matches any Product (deleted / URL-changed)."""
        import requests as _req
        import re as _re
        try:
            r = _req.get(
                f"https://api.github.com/repos/{repo}/contents/{slug}.html",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.raw",
                },
                params={"ref": branch},
                timeout=15,
            )
        except _req.RequestException:
            return []
        if r.status_code != 200:
            return []
        urls = _re.findall(r'<a\s+class="card-main"\s+href="([^"]+)"', r.text)
        ids = []
        seen = set()
        for u in urls:
            if u in seen or u == "#":
                continue
            seen.add(u)
            p = Product.query.filter_by(url=u).first()
            if p:
                ids.append(p.id)
        return ids

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

    def _github_delete_file(token, repo, branch, path, commit_message):
        """DELETE a file from a GitHub repo via Contents API. Two-step like
        PUT — fetch SHA, then DELETE with it. Returns (ok, err)."""
        import requests as _req
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            g = _req.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers, params={"ref": branch}, timeout=15,
            )
        except _req.RequestException as e:
            return False, f"GET {path} network: {e.__class__.__name__}"
        if g.status_code == 404:
            # File already gone on the repo — treat as success.
            return True, None
        if g.status_code != 200:
            return False, f"GET {path} → {g.status_code}"
        sha = g.json().get("sha")
        try:
            d = _req.delete(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers,
                json={"message": commit_message, "sha": sha, "branch": branch},
                timeout=20,
            )
        except _req.RequestException as e:
            return False, f"DELETE {path} network: {e.__class__.__name__}"
        if d.status_code in (200, 204):
            return True, None
        try:
            err = d.json().get("message", f"HTTP {d.status_code}")
        except Exception:
            err = f"HTTP {d.status_code}"
        return False, f"DELETE {path}: {err}"

    @app.route("/publications/<int:pub_id>/delete", methods=["POST"])
    def publication_delete(pub_id):
        """Delete a published page from its repo + the Publication row.

        Two GitHub calls: one DELETE on `<slug>.html`, one PUT to regenerate
        index.html from the remaining Publications for that repo (so the
        deleted entry stops appearing in the index).
        """
        s = Settings.get()
        if not s.github_token:
            flash("Connect GitHub first.", "warning")
            return redirect(url_for("settings_github"))
        pub = Publication.query.get_or_404(pub_id)

        ok, err = _github_delete_file(
            token=s.github_token,
            repo=pub.repo,
            branch=s.github_branch or "main",
            path=f"{pub.slug}.html",
            commit_message=f"Delete: {pub.name}",
        )
        if not ok:
            flash(f"Couldn't delete from GitHub: {err}", "error")
            return redirect(url_for("settings_github"))

        repo = pub.repo
        db.session.delete(pub)
        db.session.commit()

        # Rewrite index.html for this repo from whatever's left.
        remaining = (
            Publication.query
            .filter_by(repo=repo)
            .order_by(Publication.updated_at.desc())
            .all()
        )
        if remaining:
            index_html = render_template(
                "_published_index.html",
                repo=repo,
                publications=remaining,
                published_at=datetime.now(timezone.utc),
            )
            ok2, err2 = _github_put_file(
                token=s.github_token,
                repo=repo,
                branch=s.github_branch or "main",
                path="index.html",
                content_str=index_html,
                commit_message="Update index after delete",
            )
            if not ok2:
                flash(f"Page deleted but index update failed: {err2}", "warning")
            else:
                flash(f'Deleted "{pub.name}".', "success")
        else:
            # Last publication on this repo gone — leave index.html as-is
            # rather than blanking it. User can manually clean if desired.
            flash(f'Deleted "{pub.name}". Index left in place (no remaining publications).', "info")

        return redirect(url_for("settings_github"))

    # ── Purchases page removed — /products now lists everything by default. ──

    def _has_review(p):
        """A product 'has a review' if any of the review fields carry
        content. Used by /reviews and could be reused elsewhere later."""
        return bool(
            (p.review_text and p.review_text.strip())
            or (p.review_video_url and p.review_video_url.strip())
            or p.review_photos
        )

    @app.route("/reviews")
    def reviews_list():
        """Listings that have a written review, photos, or a video link.
        Filtering happens in Python — the JSON review_photos column is
        awkward to predicate on in SQL and the table is small enough
        that the cost is negligible."""
        candidates = Product.query.order_by(Product.updated_at.desc()).all()
        products = [p for p in candidates if _has_review(p)]
        return render_template("reviews.html", products=products)

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

    def _tag_redirect():
        """Where to bounce after a tag CRUD action. Honours an optional
        `next` form field so /settings/tags and /tags can share the same
        write endpoints without forking. Only same-app paths are allowed."""
        nxt = (request.form.get("next") or "").strip()
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("tags"))

    @app.route("/tags/create", methods=["POST"])
    def tag_create():
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "")
        if not name:
            flash("Tag name is required.", "error")
            return _tag_redirect()
        name = name[:1].upper() + name[1:]
        if Tag.query.filter_by(name=name).first():
            flash(f"Tag \"{name}\" already exists.", "error")
            return _tag_redirect()
        colour = request.form.get("colour", "").strip() or _pick_random_tag_colour()
        tag = Tag(name=name, colour=colour, description=description)
        db.session.add(tag)
        db.session.commit()
        flash(f"Tag \"{name}\" created.", "success")
        return _tag_redirect()

    @app.route("/tags/<int:tag_id>/edit", methods=["POST"])
    def tag_edit(tag_id):
        tag = Tag.query.get_or_404(tag_id)
        tag.name = request.form.get("name", tag.name).strip()
        tag.colour = request.form.get("colour", tag.colour)
        tag.description = request.form.get("description", tag.description)
        db.session.commit()
        flash(f"Tag \"{tag.name}\" updated.", "success")
        return _tag_redirect()

    @app.route("/tags/<int:tag_id>/delete", methods=["POST"])
    def tag_delete(tag_id):
        tag = Tag.query.get_or_404(tag_id)
        db.session.delete(tag)
        db.session.commit()
        flash(f"Tag deleted.", "success")
        return _tag_redirect()

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.route("/settings")
    def settings():
        # Settings is now a tabbed surface — bounce to GitHub as the default.
        return redirect(url_for("settings_github"), code=302)

    @app.route("/settings/github")
    def settings_github():
        pubs = Publication.query.order_by(Publication.updated_at.desc()).all()
        return render_template(
            "settings.html",
            settings=Settings.get(),
            tab="github",
            publications=pubs,
        )

    @app.route("/settings/about")
    def settings_about():
        # About moved to a footer on all /settings pages; preserve the old URL.
        return redirect(url_for("settings_github"), code=302)

    @app.route("/settings/brands")
    def settings_brands():
        """Manage Brand rows — free-text notes keyed to brand/store names.
        Surfaces every distinct Product.store value that doesn't yet have
        a Brand row so the user can claim them in one click."""
        brands = Brand.query.order_by(func.lower(Brand.name)).all()
        # Per-brand product counts via case-insensitive match on Product.store.
        # One query per brand is fine — Brand counts are tiny.
        rows = []
        for b in brands:
            count = Product.query.filter(
                func.lower(Product.store) == b.name.lower()
            ).count()
            rows.append({"brand": b, "count": count})

        # Distinct stores not yet claimed by any Brand row.
        all_stores = {
            (s[0] or "").strip()
            for s in db.session.query(Product.store).distinct().all()
            if s[0] and s[0].strip()
        }
        claimed = {b.name.lower() for b in brands}
        unclaimed = sorted(
            (s for s in all_stores if s.lower() not in claimed),
            key=str.lower,
        )

        return render_template(
            "settings.html",
            settings=Settings.get(),
            tab="brands",
            brand_rows=rows,
            unclaimed_stores=unclaimed,
        )

    @app.route("/brands/<int:brand_id>")
    def brand_detail(brand_id):
        """All listings carrying this brand's name in Product.store
        (case-insensitive). Brand notes shown at the top so they're
        the first thing the user sees before scanning items."""
        brand = Brand.query.get_or_404(brand_id)
        products = (Product.query
                    .filter(func.lower(Product.store) == brand.name.lower())
                    .order_by(Product.updated_at.desc())
                    .all())
        return render_template("brand_detail.html", brand=brand, products=products)

    def _brand_redirect():
        nxt = (request.form.get("next") or "").strip()
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("settings_brands"))

    @app.route("/brands/create", methods=["POST"])
    def brand_create():
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Brand name is required.", "error")
            return _brand_redirect()
        if Brand.query.filter(func.lower(Brand.name) == name.lower()).first():
            flash(f"Brand \"{name}\" already exists.", "warning")
            return _brand_redirect()
        db.session.add(Brand(name=name, notes=request.form.get("notes", "")))
        db.session.commit()
        flash(f"Brand \"{name}\" added.", "success")
        return _brand_redirect()

    @app.route("/brands/<int:brand_id>/edit", methods=["POST"])
    def brand_edit(brand_id):
        brand = Brand.query.get_or_404(brand_id)
        new_name = (request.form.get("name") or "").strip()
        if new_name and new_name.lower() != brand.name.lower():
            # Reject name clashes (case-insensitive).
            clash = Brand.query.filter(
                func.lower(Brand.name) == new_name.lower(),
                Brand.id != brand.id,
            ).first()
            if clash:
                flash(f"Brand \"{new_name}\" already exists.", "error")
                return _brand_redirect()
            brand.name = new_name
        brand.notes = request.form.get("notes", brand.notes)
        brand.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash(f"Brand \"{brand.name}\" updated.", "success")
        return _brand_redirect()

    @app.route("/brands/<int:brand_id>/delete", methods=["POST"])
    def brand_delete(brand_id):
        brand = Brand.query.get_or_404(brand_id)
        name = brand.name
        db.session.delete(brand)
        db.session.commit()
        flash(f"Brand \"{name}\" removed.", "info")
        return _brand_redirect()

    @app.route("/settings/tags")
    def settings_tags():
        tags = Tag.query.order_by(Tag.name).all()
        # Single grouped query for product counts — avoids the N+1 that
        # `len(t.products)` would trigger in the loop below.
        counts = dict(
            db.session.query(
                product_tags.c.tag_id, func.count(product_tags.c.product_id)
            ).group_by(product_tags.c.tag_id).all()
        )
        rows = [{"tag": t, "count": counts.get(t.id, 0)} for t in tags]
        return render_template(
            "settings.html", settings=Settings.get(), tab="tags", tag_rows=rows,
        )

    # ── Data export / import ──────────────────────────────────────────────
    # Portable cross-instance backup format. A ZIP bundle containing:
    #   data.json   — products, tags, currencies, purchases (no tokens/pubs)
    #   images/     — every image file referenced by exported products
    # On import, image filenames are prefixed with a per-bundle UUID so they
    # never clash with what the receiving instance already has on disk; the
    # corresponding references in product.images / image_url / review_photos
    # are rewritten before insert. Products are deduped by URL — re-importing
    # is therefore safe.

    # v1 → original (products, tags, currencies, purchases)
    # v2 → adds brands + related_pairs (See Also backlinks). v1 imports still
    #      accepted; the new fields are read defensively with .get(..., []).
    EXPORT_SCHEMA_VERSION = 2

    def _is_local_filename(value):
        """True if `value` looks like a local image filename rather than a
        remote URL or empty string."""
        if not value:
            return False
        v = str(value)
        return not (v.startswith("http://") or v.startswith("https://") or v.startswith("data:"))

    @app.route("/settings/data")
    def settings_data():
        images_dir = os.path.join(app.instance_path, "images")
        try:
            image_count = sum(1 for n in os.listdir(images_dir) if not n.startswith("."))
        except FileNotFoundError:
            image_count = 0
        stats = {
            "products": Product.query.count(),
            "tags": Tag.query.count(),
            "purchases": Purchase.query.count(),
            "images": image_count,
        }
        return render_template(
            "settings.html", settings=Settings.get(), tab="data", stats=stats,
        )

    @app.route("/settings/export")
    def settings_export():
        import io
        import zipfile
        from flask import send_file

        images_dir = os.path.join(app.instance_path, "images")

        def _iso(dt):
            return dt.isoformat() if dt else None

        currencies = [
            {"code": c.code, "symbol": c.symbol, "name": c.name}
            for c in Currency.query.all()
        ]
        tags = [
            {"name": t.name, "colour": t.colour, "description": t.description or ""}
            for t in Tag.query.all()
        ]

        products_out = []
        referenced_files = set()

        for p in Product.query.all():
            # Collect any local-filename references so we can bundle the files.
            for fname in [p.image_url] + list(p.images or []) + list(p.review_photos or []):
                if _is_local_filename(fname):
                    referenced_files.add(str(fname))

            purchase = None
            if p.purchase:
                pu = p.purchase
                purchase = {
                    "paid_amount": pu.paid_amount,
                    "purchased_at": _iso(pu.purchased_at),
                    "notes": pu.notes or "",
                    "order_details_url": pu.order_details_url or "",
                    "tracking_url": pu.tracking_url or "",
                    "expected_delivery_at": _iso(pu.expected_delivery_at),
                    "delivered_at": _iso(pu.delivered_at),
                }

            products_out.append({
                "url": p.url,
                "name": p.name,
                "store": p.store or "",
                "current_price": p.current_price,
                "original_price": p.original_price,
                "image_url": p.image_url or "",
                "images": list(p.images or []),
                "variants": dict(p.variants or {}),
                "notes": p.notes or "",
                "quantity": p.quantity,
                "currency_code": p.currency.code if p.currency else None,
                "status": p.status,
                "created_at": _iso(p.created_at),
                "updated_at": _iso(p.updated_at),
                "review_text": p.review_text or "",
                "review_video_url": p.review_video_url or "",
                "review_photos": list(p.review_photos or []),
                "tags": [t.name for t in p.tags],
                "purchase": purchase,
            })

        # Brands — free-text notes keyed by name. No products embedded;
        # association is by case-insensitive match on Product.store at read.
        brands_out = [
            {"name": b.name, "notes": b.notes or ""}
            for b in Brand.query.order_by(Brand.name).all()
        ]

        # See-Also relations — pairs of URLs (stable across instances) rather
        # than pairs of ids (which are renumbered on import).
        from models import product_related as _pr
        id_to_url = {p_id: p_url for p_id, p_url in db.session.query(Product.id, Product.url).all()}
        related_pairs_out = []
        for a_id, b_id in db.session.query(_pr.c.a_id, _pr.c.b_id).all():
            a_url = id_to_url.get(a_id)
            b_url = id_to_url.get(b_id)
            if a_url and b_url:
                related_pairs_out.append([a_url, b_url])

        payload = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "products": len(products_out),
                "tags": len(tags),
                "currencies": len(currencies),
                "brands": len(brands_out),
                "related_pairs": len(related_pairs_out),
            },
            "currencies": currencies,
            "tags": tags,
            "brands": brands_out,
            "products": products_out,
            "related_pairs": related_pairs_out,
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("data.json", json.dumps(payload, indent=2))
            for fname in sorted(referenced_files):
                src = os.path.join(images_dir, fname)
                if os.path.isfile(src):
                    zf.write(src, arcname=f"images/{fname}")
        buf.seek(0)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return send_file(
            buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"theo_export_{stamp}.zip",
        )

    # ── Human-readable exports ────────────────────────────────────────────
    # Distinct from the migration bundle above. Each format matches the data
    # shape: HTML for listings (visual catalogue), CSV for reports (rows of
    # numbers), TXT for brand/tag notes (free-form prose).

    @app.route("/settings/export/listings.html")
    def settings_export_listings():
        """Self-contained HTML of every product Theo knows about — reuses
        the publish-page template so images are embedded inline and the
        file works offline / anywhere."""
        from flask import Response
        products = Product.query.order_by(Product.created_at.desc()).all()
        html = render_template(
            "_published_page.html",
            page_title="Theo — full inventory",
            products=products,
            published_at=datetime.now(timezone.utc),
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            html,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="theo_listings_{stamp}.html"'},
        )

    @app.route("/settings/export/reports.csv")
    def settings_export_reports():
        """Per-day purchase rollup as CSV — one row per date with item count
        and per-currency totals exploded into named columns."""
        from flask import Response
        import csv
        import io

        days = _days_with_purchases_summary()
        # Collect every currency symbol that appears so columns are stable.
        all_syms = sorted({sym for d in days for sym, _ in d["totals"]})

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "weekday", "item_count"] + [f"total_{s}" for s in all_syms])
        for d in days:
            totals_map = dict(d["totals"])
            row = [
                d["date"].isoformat(),
                d["date"].strftime("%a"),
                d["count"],
            ] + [f"{totals_map.get(s, 0):.2f}" for s in all_syms]
            writer.writerow(row)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="theo_reports_{stamp}.csv"'},
        )

    @app.route("/settings/export/brands.txt")
    def settings_export_brands():
        """Plain-text dump of brand notes — one section per brand."""
        from flask import Response
        brands = Brand.query.order_by(func.lower(Brand.name)).all()
        lines = []
        lines.append(f"# Theo brands — exported {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
        lines.append("")
        for b in brands:
            lines.append(f"=== {b.name} ===")
            notes = (b.notes or "").strip()
            lines.append(notes if notes else "(no notes)")
            lines.append("")
        body = "\n".join(lines)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            body,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="theo_brands_{stamp}.txt"'},
        )

    @app.route("/settings/export/tags.txt")
    def settings_export_tags():
        """Plain-text dump of tags — name, colour swatch, description."""
        from flask import Response
        tags = Tag.query.order_by(func.lower(Tag.name)).all()
        lines = []
        lines.append(f"# Theo tags — exported {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
        lines.append("")
        for t in tags:
            desc = (t.description or "").strip()
            head = f"{t.name}  ({t.colour})"
            lines.append(head)
            if desc:
                # Indent descriptions for visual grouping under the tag name.
                for line in desc.splitlines():
                    lines.append(f"    {line}")
            lines.append("")
        body = "\n".join(lines)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return Response(
            body,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="theo_tags_{stamp}.txt"'},
        )

    @app.route("/settings/import", methods=["POST"])
    def settings_import():
        import io
        import zipfile
        import uuid

        upload = request.files.get("bundle")
        if not upload or not upload.filename:
            flash("Pick a .zip bundle to import.", "warning")
            return redirect(url_for("settings_data"))

        try:
            zf = zipfile.ZipFile(io.BytesIO(upload.read()))
        except zipfile.BadZipFile:
            flash("That file isn't a valid .zip bundle.", "error")
            return redirect(url_for("settings_data"))

        try:
            data = json.loads(zf.read("data.json"))
        except KeyError:
            flash("Bundle is missing data.json.", "error")
            return redirect(url_for("settings_data"))
        except json.JSONDecodeError as e:
            flash(f"data.json is malformed: {e}", "error")
            return redirect(url_for("settings_data"))

        bundle_version = data.get("schema_version")
        if bundle_version not in (1, EXPORT_SCHEMA_VERSION):
            flash(
                f"Bundle schema version {bundle_version!r} isn't supported by "
                f"this build (expects 1 or {EXPORT_SCHEMA_VERSION}). Aborting.",
                "error",
            )
            return redirect(url_for("settings_data"))

        images_dir = os.path.join(app.instance_path, "images")
        os.makedirs(images_dir, exist_ok=True)
        prefix = f"imp_{uuid.uuid4().hex[:8]}_"

        # 1. Currencies — lookup-or-create by code.
        for c in data.get("currencies", []):
            if not Currency.query.filter_by(code=c["code"]).first():
                db.session.add(Currency(code=c["code"], symbol=c["symbol"], name=c["name"]))
        db.session.flush()

        # 2. Tags — lookup-or-create by name; keep first-seen colour.
        tag_cache = {}
        for t in data.get("tags", []):
            existing = Tag.query.filter_by(name=t["name"]).first()
            if existing:
                tag_cache[t["name"]] = existing
            else:
                new_t = Tag(
                    name=t["name"],
                    colour=t.get("colour", "#3d6b8a"),
                    description=t.get("description", ""),
                )
                db.session.add(new_t)
                tag_cache[t["name"]] = new_t
        db.session.flush()

        # 2b. Brands — lookup-or-create by case-insensitive name. Notes from
        # the bundle only fill in *new* rows; existing brand notes on this
        # instance are preserved so an import doesn't overwrite local edits.
        for br in data.get("brands", []):
            bname = (br.get("name") or "").strip()
            if not bname:
                continue
            existing = Brand.query.filter(func.lower(Brand.name) == bname.lower()).first()
            if not existing:
                db.session.add(Brand(name=bname, notes=br.get("notes", "")))
        db.session.flush()

        # 3. Images — extract everything under images/ into instance/images/
        # with the unique prefix. Map old → new so product refs can be rewritten.
        rename_map = {}
        for member in zf.namelist():
            if not member.startswith("images/") or member.endswith("/"):
                continue
            old_name = member[len("images/"):]
            if not old_name:
                continue
            new_name = prefix + old_name
            with zf.open(member) as src, open(os.path.join(images_dir, new_name), "wb") as dst:
                dst.write(src.read())
            rename_map[old_name] = new_name

        def _rewrite(ref):
            if not _is_local_filename(ref):
                return ref
            return rename_map.get(str(ref), ref)

        # 4. Products — skip URL duplicates.
        added = skipped = 0
        for p in data.get("products", []):
            if Product.query.filter_by(url=p["url"]).first():
                skipped += 1
                continue

            currency = None
            if p.get("currency_code"):
                currency = Currency.query.filter_by(code=p["currency_code"]).first()

            new_p = Product(
                url=p["url"],
                name=p["name"],
                store=p.get("store", ""),
                current_price=p.get("current_price"),
                original_price=p.get("original_price"),
                image_url=_rewrite(p.get("image_url", "")),
                images=[_rewrite(x) for x in (p.get("images") or [])],
                variants=p.get("variants") or {},
                notes=p.get("notes", ""),
                quantity=p.get("quantity", 1) or 1,
                currency=currency,
                status=p.get("status", "added"),
                review_text=p.get("review_text", ""),
                review_video_url=p.get("review_video_url", ""),
                review_photos=[_rewrite(x) for x in (p.get("review_photos") or [])],
            )
            # Preserve timestamps if present.
            for fld in ("created_at", "updated_at"):
                raw = p.get(fld)
                if raw:
                    try:
                        setattr(new_p, fld, datetime.fromisoformat(raw))
                    except ValueError:
                        pass

            # Tags via join table — use the cache or look up.
            for tname in p.get("tags") or []:
                tag = tag_cache.get(tname) or Tag.query.filter_by(name=tname).first()
                if tag is None:
                    tag = Tag(name=tname)
                    db.session.add(tag)
                    tag_cache[tname] = tag
                new_p.tags.append(tag)

            db.session.add(new_p)
            db.session.flush()  # need product.id before attaching purchase

            pu = p.get("purchase")
            if pu:
                def _parse(s):
                    try:
                        return datetime.fromisoformat(s) if s else None
                    except ValueError:
                        return None
                db.session.add(Purchase(
                    product_id=new_p.id,
                    paid_amount=pu.get("paid_amount") or 0.0,
                    purchased_at=_parse(pu.get("purchased_at")) or datetime.now(timezone.utc),
                    notes=pu.get("notes", ""),
                    order_details_url=pu.get("order_details_url", ""),
                    tracking_url=pu.get("tracking_url", ""),
                    expected_delivery_at=_parse(pu.get("expected_delivery_at")),
                    delivered_at=_parse(pu.get("delivered_at")),
                ))
            added += 1

        # 5. See-Also relations — pairs of URLs. Look up the products on
        # this instance (whether just-imported or pre-existing) and call
        # link_related, which idempotently canonicalizes a < b and skips
        # duplicates.
        linked_pairs = 0
        for pair in data.get("related_pairs", []) or []:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                continue
            url_a, url_b = pair
            pa = Product.query.filter_by(url=url_a).first()
            pb = Product.query.filter_by(url=url_b).first()
            if pa and pb and pa.id != pb.id:
                pa.link_related(pb.id)
                linked_pairs += 1

        db.session.commit()
        flash(
            f"Imported {added} product{'s' if added != 1 else ''}"
            f"{f', skipped {skipped} duplicate' + ('s' if skipped != 1 else '') if skipped else ''}"
            f"{f', linked {linked_pairs} related pair' + ('s' if linked_pairs != 1 else '') if linked_pairs else ''}.",
            "success",
        )
        return redirect(url_for("settings_data"))

    # ── Destructive reset ─────────────────────────────────────────────────
    # Wipes every user-data table and the image cache. Settings (GitHub PAT,
    # schema migration flags) and Currency rows (reference data the new-
    # listing modal depends on) are intentionally preserved so the user
    # doesn't have to re-connect GitHub or re-seed currencies after a reset.

    RESET_CONFIRM_PHRASE = "RESET ALL DATA"

    @app.route("/settings/reset", methods=["POST"])
    def settings_reset():
        if request.form.get("confirm") != RESET_CONFIRM_PHRASE:
            flash(
                f"Type the exact phrase “{RESET_CONFIRM_PHRASE}” to confirm reset.",
                "error",
            )
            return redirect(url_for("settings_data"))

        # 1. Delete user-data tables in FK-safe order.
        db.session.execute(product_related.delete())
        db.session.execute(product_tags.delete())
        Purchase.query.delete()
        ImageHash.query.delete()
        Publication.query.delete()
        Product.query.delete()
        Tag.query.delete()
        Brand.query.delete()
        db.session.commit()

        # 2. Clear image files on disk. Honour dotfiles (e.g. .DS_Store).
        images_dir = os.path.join(app.instance_path, "images")
        cleared_files = 0
        if os.path.isdir(images_dir):
            for fname in os.listdir(images_dir):
                if fname.startswith("."):
                    continue
                try:
                    os.remove(os.path.join(images_dir, fname))
                    cleared_files += 1
                except OSError:
                    pass

        # 3. Clear the client cart in the current session — anything else
        #    is per-other-session and will rebuild from the empty DB.
        session.pop("cart", None)

        flash(
            f"App reset. Wiped products, tags, brands, purchases, "
            f"publications, See Also links, and {cleared_files} image file"
            f"{'s' if cleared_files != 1 else ''}. "
            f"Settings and currencies preserved.",
            "success",
        )
        return redirect(url_for("shopping_list"))

    @app.route("/settings/stats")
    def settings_stats():
        # Database size
        db_path = os.path.join(app.instance_path, "theo.db")
        db_bytes = os.path.getsize(db_path)
        if db_bytes >= 1_048_576:
            db_size = f"{db_bytes / 1_048_576:.1f} MB"
        else:
            db_size = f"{db_bytes / 1024:.1f} KB"

        # ── Per-route load-time sweep ────────────────────────────────────────
        # Walk the url_map, fill in sample values for any path parameters we
        # know how to satisfy, and time each GET handler. Routes we can't
        # synthesize a real path for are reported as "skipped" with a reason
        # so the page stays an honest catalogue of GET surfaces.

        # Sample IDs from the DB — one cheap fetch each.
        sample_product = Product.query.first()
        sample_tag = Tag.query.first()
        sample_brand = Brand.query.first()
        sample_pub = Publication.query.first()
        sample_period_row = (
            Purchase.query.filter(Purchase.purchased_at.isnot(None))
            .order_by(Purchase.purchased_at.desc()).first()
        )
        sample_period = (
            sample_period_row.purchased_at.strftime("%Y-%m")
            if sample_period_row and sample_period_row.purchased_at else None
        )

        samples = {
            "product_id": sample_product.id if sample_product else None,
            "other_id": sample_product.id if sample_product else None,
            "tag_id": sample_tag.id if sample_tag else None,
            "brand_id": sample_brand.id if sample_brand else None,
            "pub_id": sample_pub.id if sample_pub else None,
            "period": sample_period,
            "brand_id": sample_brand.id if sample_brand else None,
        }

        # Routes whose body is intentionally heavy (large file downloads).
        # We skip-and-mark rather than time, so the stats page itself stays fast.
        heavy_routes = {"/settings/export/listings.html", "/settings/export"}
        # Self-skip: measuring /settings/stats would call this function
        # recursively (each inner call re-times all 30+ routes), turning a
        # 30 ms page into a 100-second page. The real number is the page
        # render time the user just waited for.
        self_route = "/settings/stats"

        # Some path-parameter names we can't manufacture sample values for —
        # filename/path on /images/<...> would 404 without a real file.
        unfillable = {"filename"}

        route_rows = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            if rule.endpoint == "static":
                continue
            methods = rule.methods or set()
            if "GET" not in methods:
                continue

            # Build kwargs for the view from rule.arguments + samples.
            kwargs = {}
            skip_reason = None
            for arg in rule.arguments:
                if arg in unfillable:
                    skip_reason = f"needs <{arg}>"
                    break
                if arg not in samples:
                    skip_reason = f"unknown param <{arg}>"
                    break
                if samples[arg] is None:
                    skip_reason = f"no sample for <{arg}>"
                    break
                kwargs[arg] = samples[arg]
            if skip_reason:
                route_rows.append({"path": rule.rule, "ms": None, "note": skip_reason})
                continue

            if rule.rule in heavy_routes:
                route_rows.append({"path": rule.rule, "ms": None, "note": "skipped (large download)"})
                continue
            if rule.rule == self_route:
                route_rows.append({"path": rule.rule, "ms": None, "note": "self — see page load"})
                continue

            # Concrete path used for test_request_context.
            try:
                with app.test_request_context("/"):
                    concrete = url_for(rule.endpoint, **kwargs)
            except Exception as e:
                route_rows.append({"path": rule.rule, "ms": None, "note": f"url_for: {e.__class__.__name__}"})
                continue

            view_fn = app.view_functions.get(rule.endpoint)
            if view_fn is None:
                route_rows.append({"path": rule.rule, "ms": None, "note": "no view fn"})
                continue

            try:
                with app.test_request_context(concrete):
                    t0 = time.perf_counter()
                    view_fn(**kwargs)
                    ms = (time.perf_counter() - t0) * 1000
                route_rows.append({"path": rule.rule, "ms": int(ms), "note": None})
            except Exception as e:
                route_rows.append({"path": rule.rule, "ms": None, "note": f"err: {e.__class__.__name__}"})

        return render_template(
            "settings.html",
            settings=Settings.get(),
            tab="stats",
            db_size=db_size,
            route_rows=route_rows,
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

    def _days_with_purchases_summary():
        """Day-level rollup for the /reports index — one entry per date that
        carries any purchase, newest first. Bucketing happens in Python so we
        can read each product's currency_symbol (a derived attribute on the
        Currency relationship rather than a stored column)."""
        rows = (
            Purchase.query
            .join(Product, Purchase.product_id == Product.id)
            .all()
        )
        by_day = {}  # date → {count, totals: {sym: amt}}
        for p in rows:
            if not p.purchased_at:
                continue
            prod = p.product
            if not prod:
                continue
            d = p.purchased_at.date()
            sym = prod.currency_symbol
            entry = by_day.setdefault(d, {"count": 0, "totals": {}})
            entry["count"] += 1
            entry["totals"][sym] = entry["totals"].get(sym, 0) + (p.paid_amount or 0)

        out = []
        for d in sorted(by_day.keys(), reverse=True):
            info = by_day[d]
            out.append({
                "date": d,
                "label": d.strftime("%a, %b %d, %Y"),
                "month_slug": d.strftime("%Y-%m"),
                "count": info["count"],
                # Sort symbols by absolute spend so the dominant currency
                # leads the row.
                "totals": sorted(info["totals"].items(), key=lambda kv: -kv[1]),
            })
        return out

    @app.route("/reports")
    def reports():
        days = _days_with_purchases_summary()

        # Streak detection — mark every date that sits inside a run of
        # consecutive calendar days (length ≥ 2). Walk the dates ascending
        # so adjacency is just date_next - date == 1 day.
        sorted_asc = sorted(days, key=lambda d: d["date"])
        streak_dates = set()
        i = 0
        while i < len(sorted_asc):
            j = i
            while (j + 1 < len(sorted_asc)
                   and (sorted_asc[j + 1]["date"] - sorted_asc[j]["date"]).days == 1):
                j += 1
            if j > i:
                streak_len = j - i + 1
                for k in range(i, j + 1):
                    sorted_asc[k]["streak_len"] = streak_len
                    streak_dates.add(sorted_asc[k]["date"])
            i = j + 1

        for d in days:
            d["in_streak"] = d["date"] in streak_dates

        return render_template("reports_index.html", days=days)

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

        # ── Per-day item counts for the calendar grid ───────────────────────
        # Cheap second pass — we re-query rather than refactor
        # _compute_month_report so its other callers stay untouched.
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        day_counts = {}
        for purchased_at, in (
            db.session.query(Purchase.purchased_at)
            .filter(Purchase.purchased_at >= start)
            .filter(Purchase.purchased_at < end)
            .all()
        ):
            if not purchased_at:
                continue
            d = purchased_at.day
            day_counts[d] = day_counts.get(d, 0) + 1

        # calendar.Calendar(firstweekday=0) → Monday is column 0
        cal_weeks = stdlib_calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
        weekday_headers = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        return render_template(
            "report_month.html",
            label=label,
            year=year,
            month=month,
            totals=totals,
            count=count,
            by_tag=by_tag,
            cal_weeks=cal_weeks,
            day_counts=day_counts,
            weekday_headers=weekday_headers,
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
    if column_exists("publication", "id") and not column_exists("publication", "item_ids"):
        # JSON list of product ids per publication so we can merge new
        # selections into existing pages. Legacy rows get [] — see model.
        db.session.execute(text("ALTER TABLE publication ADD COLUMN item_ids TEXT DEFAULT '[]'"))
        db.session.commit()

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
