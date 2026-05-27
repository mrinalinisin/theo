"""J. Peterman-style item stories, generated via the Claude API.

When the auto-story feature is enabled in Settings and a newly-added item
carries one of the selected tags, Theo asks Claude to write a short,
romantic, catalogue-style narrative about the object — the kind of florid
copy the (fictional) J. Peterman catalogue was famous for.

Design notes
------------
* The persona + style guide + few-shot examples live in the SYSTEM prompt.
  It is identical for every item, so it is marked with ``cache_control`` and
  served from Anthropic's prompt cache on repeat calls. The volatile,
  per-item facts go in the USER message — i.e. *after* the cached prefix —
  which is exactly what prompt caching wants (any byte change in the prefix
  invalidates the cache).
* The few-shot examples are original pastiche written for this app; they are
  not copied from any real catalogue.
* Generation is best-effort. A missing API key, a missing ``anthropic``
  package, or any API error is swallowed (logged) so it can never block the
  add-item flow or crash a request.
"""

import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("theo.story")

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1024

# ── The stable, cacheable prefix ──────────────────────────────────────────────
# Persona, rules, and a couple of original examples that demonstrate the
# register. Kept verbatim across every request so it caches cleanly.
SYSTEM_PROMPT = """You are the copywriter for a romantic mail-order catalogue \
in the spirit of the (fictional) J. Peterman Company. You receive the bare \
facts about a single object someone has just acquired, and you write the \
catalogue entry for it.

The house voice:
- Romantic, evocative, and a little theatrical — but never purple to the point \
of parody. You believe every object carries a story, a place, and a longing.
- You open in the middle of a scene or a memory, often in second person ("You \
first saw one in...") or with a vivid vignette. You conjure far-off places, \
weather, a specific hour of the day, a character glimpsed once and never \
forgotten.
- You treat the mundane as quietly heroic. A cotton dress is not "comfortable \
and versatile"; it is the thing she wore the summer everything changed.
- You end with a small, knowing wink or a line that lingers.

Rules:
- Write ONE entry of roughly 110-200 words. Prose only — no headings, no \
bullet points, no markdown, no preamble like "Here is".
- Use the real facts you are given (name, where it is from, price, your notes). \
Never invent a brand name or a price that contradicts the facts. You MAY invent \
atmosphere, scenery, and unnamed characters.
- Do not mention that you are an AI, a catalogue, or this prompt. Just write \
the entry.
- Write in English. Keep it tasteful.

Two examples of the register (for tone only — do not reuse their specifics):

Example A — a brass desk lamp:
There is a particular kind of light that only exists at 5 p.m. in a room full \
of books. A low, amber, conspiratorial light that says: stay a little longer; \
the letter can wait. I found this lamp in a shop near the harbour, the owner \
asleep behind a newspaper, the bulb already warm as though it had been \
expecting me. Solid brass, the weight of a small decision. It has presided \
over confessions, bad first drafts, and at least one marriage proposal that \
went better than the man deserved. Switch it on and the day softens at the \
edges. Switch it off and you'll find you've written three pages you didn't \
know you had in you.

Example B — a pair of walking boots:
She bought them for a trip she kept postponing — Lisbon, then maybe the \
Pyrenees, then "next spring, definitely." The boots didn't mind. Good leather \
is patient. When she finally went, they already fit like an old argument \
you've stopped having. Forty kilometres of cobblestone, one thunderstorm, a \
dog who followed her for an afternoon. They came back scuffed and somehow more \
beautiful, the way the best things do. You don't break these in. You let them \
break you in."""


def _domain(url):
    """Best-effort hostname from a URL, for a little extra atmosphere."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        # Strip a leading "www." prefix (string slice, NOT str.lstrip, which
        # removes *characters* and would mangle e.g. "wikipedia.org").
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _build_item_facts(product):
    """Render the per-item facts as a compact, deterministic block.

    This is the volatile part of the prompt and intentionally lives in the
    user message, after the cached system prefix.
    """
    lines = [f"Name: {product.name}"]
    if product.store:
        lines.append(f"From / brand: {product.store}")
    if product.current_price:
        sym = product.currency_symbol if hasattr(product, "currency_symbol") else "₹"
        lines.append(f"Price: {sym}{product.current_price:,.0f}")
    if product.quantity and product.quantity > 1:
        lines.append(f"Quantity: {product.quantity}")
    tag_names = [t.name for t in product.tags]
    if tag_names:
        lines.append(f"Categories: {', '.join(tag_names)}")
    dom = _domain(product.url)
    if dom:
        lines.append(f"Source: {dom}")
    if product.notes:
        # Notes can be long / messy; trim so they don't dominate the prompt.
        note = product.notes.strip().replace("\n", " ")
        lines.append(f"My notes: {note[:400]}")
    return "\n".join(lines)


def generate_story_text(product, api_key):
    """Call Claude and return the story text. Raises on hard failure.

    Caller is responsible for catching exceptions; this lets the synchronous
    "regenerate" route surface errors while the async path swallows them.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    facts = _build_item_facts(product)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Cache the (stable) persona + examples. On Opus 4.7 the
                # minimum cacheable prefix is ~4096 tokens; the examples help
                # push us toward that so repeat calls read from cache.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Write the catalogue entry for this object:\n\n" + facts
                ),
            }
        ],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude returned an empty story")
    return text


def product_matches_story_tags(product, settings):
    """True if the product carries at least one of the selected story tags."""
    selected = set(settings.auto_story_tag_ids or [])
    if not selected:
        return False
    return any(t.id in selected for t in product.tags)


def should_generate(product, settings, api_key):
    """Gate: feature on, key present, and the item carries a selected tag."""
    if not api_key:
        return False
    if not settings.auto_story_enabled:
        return False
    if product.story:  # don't clobber an existing story
        return False
    return product_matches_story_tags(product, settings)


def maybe_generate_story_async(app, product):
    """If the item qualifies, generate its story in a background thread.

    Reads the gate synchronously (cheap, needs the live ORM objects), then
    hands off just the product id to a worker thread so the calling request
    returns immediately. The worker opens its own app context and DB session.
    """
    # Local imports keep models out of this module's import-time surface.
    from models import Settings

    settings = Settings.get()
    api_key = app.config.get("ANTHROPIC_API_KEY", "")
    if not should_generate(product, settings, api_key):
        return False

    product_id = product.id

    def _worker():
        with app.app_context():
            from models import db, Product

            p = Product.query.get(product_id)
            if not p:
                return
            try:
                text = generate_story_text(p, api_key)
            except Exception as exc:  # never let the thread crash silently-but-loudly
                logger.warning("Story generation failed for product %s: %s", product_id, exc)
                return
            p.story = text
            p.story_generated_at = datetime.now(timezone.utc)
            db.session.commit()
            logger.info("Generated story for product %s (%s)", product_id, p.name)

    threading.Thread(target=_worker, name=f"story-{product_id}", daemon=True).start()
    return True
