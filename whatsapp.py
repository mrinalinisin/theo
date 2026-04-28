"""WhatsApp integration via Twilio Sandbox."""

import re
from config import Config

config = Config()


def send_whatsapp(message):
    """Send a WhatsApp message to the configured number."""
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN or not config.TWILIO_WHATSAPP_TO:
        print(f"[WhatsApp] Twilio not configured. Message: {message}")
        return False

    try:
        from twilio.rest import Client

        client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=config.TWILIO_WHATSAPP_FROM,
            to=config.TWILIO_WHATSAPP_TO,
        )
        return True
    except Exception as e:
        print(f"[WhatsApp] Send failed: {e}")
        return False


def handle_incoming(body, from_number):
    """
    Handle an incoming WhatsApp message and return a response string.

    Supported commands:
      - A URL: scrapes and adds the product
      - 'budget' / 'left': current month budget status
      - 'list': watching items summary
      - 'drops': recent price drops
      - 'help': list commands
    """
    body = body.strip()

    # Check if it's a URL
    url_match = re.match(r"https?://\S+", body, re.IGNORECASE)
    if url_match:
        return _handle_add_url(url_match.group(0))

    # Normalise command
    cmd = body.lower().strip()

    if cmd in ("budget", "left", "remaining", "spent"):
        return _handle_budget()
    elif cmd in ("list", "items", "watching"):
        return _handle_list()
    elif cmd in ("drops", "deals", "price drops"):
        return _handle_drops()
    elif cmd in ("help", "?", "commands"):
        return _handle_help()
    else:
        return (
            "Unknown command. Send 'help' for available commands.\n\n"
            "Quick tip: paste a product URL to add it to your list!"
        )


def _handle_add_url(url):
    """Scrape a URL and add it to the shopping list."""
    try:
        from scraper import scrape_product
        from models import db, Product, PriceHistory, Tag

        scraped = scrape_product(url)

        # Find or create "Uncategorised" tag
        tag = Tag.query.filter_by(name="Uncategorised").first()
        if not tag:
            tag = Tag(name="Uncategorised", colour="#a8a29e", description="Items added via WhatsApp")
            db.session.add(tag)

        product = Product(
            url=url,
            name=scraped.get("name", "Unknown Product"),
            store=scraped.get("store", ""),
            current_price=scraped.get("price"),
            original_price=scraped.get("price"),
            image_url=scraped.get("image_url", ""),
            images=scraped.get("images", []),
            variants=scraped.get("variants", {}),
            status="watching",
        )
        product.tags.append(tag)
        db.session.add(product)
        db.session.flush()

        if scraped.get("price"):
            ph = PriceHistory(product_id=product.id, price=scraped["price"])
            db.session.add(ph)

        db.session.commit()

        price_str = f"\u20b9{scraped['price']:,.0f}" if scraped.get("price") else "Price unknown"
        return (
            f"\u2705 Added: {scraped.get('name', 'Product')}\n"
            f"Price: {price_str}\n"
            f"Store: {scraped.get('store', 'Unknown')}\n\n"
            f"Tagged as 'Uncategorised'. Use the web UI to change tags."
        )
    except Exception as e:
        return f"\u274c Failed to scrape URL: {str(e)[:100]}"


def _handle_budget():
    """Return current month budget status."""
    try:
        from datetime import datetime, timezone
        from models import Settings, Purchase

        settings = Settings.get()
        now = datetime.utcnow()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        purchases = Purchase.query.filter(Purchase.purchased_at >= month_start).all()
        spent = sum(p.paid_amount for p in purchases)
        budget = settings.shopping_budget or 0
        income = settings.monthly_income or 0
        remaining = budget - spent
        pct = (spent / budget * 100) if budget > 0 else 0

        month_name = now.strftime("%B")

        lines = [f"\U0001f4b0 {month_name} Budget"]
        if income:
            lines.append(f"Income: \u20b9{income:,.0f}")
        if budget:
            lines.append(f"Budget: \u20b9{budget:,.0f}")
            lines.append(f"Spent: \u20b9{spent:,.0f} ({pct:.0f}%)")
            lines.append(f"Remaining: \u20b9{remaining:,.0f}")
            if pct >= 90:
                lines.append("\n\u26a0\ufe0f Almost at your limit!")
        else:
            lines.append("No budget set. Configure in Settings.")

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching budget: {str(e)[:100]}"


def _handle_list():
    """Return summary of watching items."""
    try:
        from models import Product

        products = Product.query.filter(
            Product.status.in_(("watching", "awaiting_delivery"))
        ).order_by(Product.name).all()
        if not products:
            return "\U0001f6cd\ufe0f No items in your shopping list."

        lines = [f"\U0001f6cd\ufe0f {len(products)} items:\n"]
        for i, p in enumerate(products[:15], 1):
            price = f"\u20b9{p.current_price:,.0f}" if p.current_price else "?"
            change = ""
            if p.price_change_pct:
                if p.price_change_pct < 0:
                    change = f" \u2193{p.price_change_pct:.0f}%"
                elif p.price_change_pct > 0:
                    change = f" \u2191+{p.price_change_pct:.0f}%"
            lines.append(f"{i}. {p.name} \u2014 {price}{change}")

        if len(products) > 15:
            lines.append(f"\n...and {len(products) - 15} more")

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching list: {str(e)[:100]}"


def _handle_drops():
    """Return recent price drops."""
    try:
        from datetime import datetime, timezone, timedelta
        from models import Product, PriceHistory

        week_ago = datetime.utcnow() - timedelta(days=7)
        products = Product.query.filter(
            Product.status.in_(("watching", "awaiting_delivery"))
        ).all()

        drops = []
        for p in products:
            recent = [ph for ph in p.price_history if ph.checked_at >= week_ago]
            if len(recent) >= 2 and recent[0].price < recent[-1].price:
                pct = ((recent[-1].price - recent[0].price) / recent[-1].price) * 100
                drops.append((p.name, recent[0].price, pct))

        if not drops:
            return "\U0001f4c9 No price drops in the last 7 days."

        lines = [f"\U0001f4c9 {len(drops)} price drops this week:\n"]
        for name, price, pct in drops:
            lines.append(f"\u2022 {name} \u2014 \u20b9{price:,.0f} (\u2193{pct:.0f}%)")

        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching drops: {str(e)[:100]}"


def _handle_help():
    """Return help message."""
    return (
        "\U0001f9c1 *Hector* \u2014 Shopping Tracker\n\n"
        "Send me:\n"
        "\u2022 *A product URL* \u2014 adds it to your shopping list\n"
        "\u2022 *budget* \u2014 current month spending status\n"
        "\u2022 *list* \u2014 all items you're watching\n"
        "\u2022 *drops* \u2014 recent price drops\n"
        "\u2022 *help* \u2014 this message"
    )
