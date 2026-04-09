"""Background scheduler for periodic price checking."""

from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()


def start_scheduler(app):
    """Start the background scheduler with the Flask app context."""

    def price_check_job():
        """Check prices for all watching products that are due."""
        with app.app_context():
            from models import db, Product, PriceHistory, Settings
            from scraper import check_price

            settings = Settings.get()
            default_interval = settings.default_check_interval  # minutes
            now = datetime.utcnow()

            products = Product.query.filter_by(status="watching").all()

            for product in products:
                interval = product.check_interval or default_interval
                cutoff = now - timedelta(minutes=interval)

                # Skip if checked recently
                if product.last_checked_at and product.last_checked_at > cutoff:
                    continue

                try:
                    new_price = check_price(product.url, use_browser=settings.use_browser_rendering)
                    if new_price is None:
                        continue

                    old_price = product.current_price
                    product.current_price = new_price
                    product.last_checked_at = now

                    # Record price history
                    ph = PriceHistory(product_id=product.id, price=new_price)
                    db.session.add(ph)
                    db.session.commit()

                    # Send WhatsApp notification if price changed
                    _notify_price_change(product, old_price, new_price, settings)

                except Exception as e:
                    print(f"[Scheduler] Error checking {product.name}: {e}")
                    db.session.rollback()

    # Run the check job every 60 seconds
    scheduler.add_job(price_check_job, "interval", seconds=60, id="price_checker", replace_existing=True)
    scheduler.start()
    print("[Scheduler] Price checker started (checks every 60s for due items)")


def _notify_price_change(product, old_price, new_price, settings):
    """Send WhatsApp notification for price changes."""
    if old_price is None or old_price == new_price:
        return

    try:
        from whatsapp import send_whatsapp

        if new_price < old_price and settings.notify_price_drop:
            pct = ((old_price - new_price) / old_price) * 100
            send_whatsapp(
                f"\U0001f4c9 Price Drop: {product.name}\n"
                f"\u20b9{old_price:,.0f} \u2192 \u20b9{new_price:,.0f} ({pct:.1f}% off)\n"
                f"Store: {product.store}"
            )
        elif new_price > old_price and settings.notify_price_rise:
            pct = ((new_price - old_price) / old_price) * 100
            send_whatsapp(
                f"\U0001f4c8 Price Rise: {product.name}\n"
                f"\u20b9{old_price:,.0f} \u2192 \u20b9{new_price:,.0f} (+{pct:.1f}%)\n"
                f"Store: {product.store}"
            )
    except Exception as e:
        print(f"[Scheduler] WhatsApp notification failed: {e}")
