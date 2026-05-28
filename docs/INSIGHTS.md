# Insights

Notes I've found worth recording while building Theo. Not a changelog —
just observations, traps, and patterns that might save someone (or
future-me) some time.

Newer entries on top.

---

## Cloning rows with associations

When duplicating a SQLAlchemy row that has many-to-many associations
(like `Product.tags`), you must `db.session.flush()` between
`db.session.add(new_row)` and the tag appends. Reason: the secondary
association table needs the new row's primary key to write the link
rows, and that key isn't generated until the insert is flushed to
the DB. `flush()` does the insert without committing, which is
exactly what you want.

For JSON columns, shallow-copy the list/dict you read from the
source: `list(src.images or [])`. Otherwise the new row shares a
Python list reference with the old row, and mutations on either
silently mutate both.

## Image-path sharing across products

Theo stores image paths as strings in `Product.image_url` and
`Product.images` (JSON list). When a clone references the same
paths, both products visually share the same files on disk. This
turns out to be safe because Theo's edit pipeline writes new
files with timestamped names rather than overwriting — so future
edits to either product stay independent.

The only failure mode is `rm` on the instance/images/ directory,
which would break both. Acceptable for a personal app; in a
multi-tenant or hosted scenario you'd want copy-on-write.

## State machine asymmetry

The listing-states refactor (Added · Purchased · Shipped · Received)
ended up needing inverse buttons for every forward transition
*except* the original "Mark as Purchased" terminal cleanup
(intentionally removed). The "Unmark as Received" gap surfaced
when a user hit an item auto-stamped as received that was actually
still in transit.

Lesson: when designing a state machine, think explicitly about
which transitions need a UI inverse. "User picks the wrong forward
button" is a real case; "no way back without nuking the row" is a
pit users fall into.

## Migrating renamed states

Renaming an enum value while preserving rows is harder than it
first looks. If old `purchased` rows need to become new `received`,
and *new* `purchased` rows are produced by the same migration, a
naive `UPDATE` on the second pass would catch the freshly-renamed
ones too.

Fix: park the old rows under a sentinel value first
(`__migrating_old_purchased__`), then split from the sentinel into
the new states, then rename anything still on the sentinel.

Gate the whole block on a marker (e.g. `Settings.state_refactor_done`)
so it doesn't re-fire on subsequent app starts. The marker becomes
permanent code that's a no-op forever after the first run; cleaner
than deleting migration code that "should never run again."

## Empty-string vs NULL on TEXT columns

SQLAlchemy `db.Column(db.Text, default="")` results in stored empty
strings, not NULLs, when no value is supplied. Filters that look
for "missing" links via `Column.is_(None)` silently miss every
empty-string row.

The defensive predicate is `or_(col.is_(None), col == "")` for
absence and the inverse for presence. Worth a small helper inside
the route to keep the four queries readable:

```python
def _absent(col): return or_(col.is_(None), col == "")
def _present(col): return and_(col.isnot(None), col != "")
```

## Custom Jinja filters > template arithmetic

Whenever a template needs to compute a derived value (days late,
"min" date for a picker, formatted price) — make it a custom Jinja
filter (`@app.template_filter("days_late")`). Templates stay
declarative (`{{ exp|days_late }}`); date math stays in Python
where it's testable.

Don't expose `now()` or `timedelta` to Jinja; that path leads to
Jinja code that's neither template logic nor real Python.

## Browser-native input attributes

Always look for an HTML attribute before writing JS validation:

- `<input type="date" min="...">` — popover greys out invalid
  dates AND form submission rejects them. Two layers, zero JS.
- `<input type="search">` — gives a clear-X affordance and triggers
  `search` events.
- `pattern="..."`, `required`, `step="..."` — same story.

Pure HTML attributes are the cheapest validation you'll ever write.

## Auto-submit on date-input change

`<input type="date" onchange="this.form.submit()">` is a clean
pattern when a Save button feels redundant. Native date inputs
fire `change` only when a date is committed (the popover closes),
not while the user is scrolling, so there's no premature-submit
risk.

Used in: `_purchase_cards.html`, `admin.html`. Felt right both
times.

## Document-level keydown handlers

For keyboard shortcuts that span pages or might fire on elements
that aren't in the DOM at script-load time (like modals before
they open), use `document.addEventListener('keydown', ...)`. One
listener handles every case; doesn't leak as elements come and
go.

For modal dismissal specifically:
```js
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  document.querySelectorAll('.modal-overlay.open')
    .forEach(m => m.classList.remove('open'));
});
```

## CSS positioning context for absolutely-positioned children

When an `.absolute` child appears in the wrong place — usually the
top-right corner of the viewport instead of inside its parent —
the parent (or a closer ancestor) is missing `position: relative`.

Typical case here: `.cart-badge` was `position: absolute`, and
`.nav-cart-link` had `position: relative`. When I added a new
`.nav-compare-link` that reused the badge styles, I forgot to add
`position: relative` to it — badge climbed up to the viewport.

Fix is one line. Worth always remembering to set
`position: relative` on a parent when adding any absolutely-
positioned child.

## Per-card vs body-level state

Toggling visibility on N elements in JS by iterating over them
works, but a single class on `<body>` with a CSS rule like
`body.has-selection .card-select-checkbox { display: flex; }` is
cleaner. One JS line, one CSS rule. CSS does the iteration via
the cascade.

Same trick works for any "all of this kind of element should
react to one global state": print mode, focus mode, drag-active,
etc.

## The `--cols` CSS custom property trick

Render a card grid that needs to support 1–N columns based on
data:

```css
.compare-grid {
  display: grid;
  grid-template-columns: repeat(var(--cols, 2), 1fr);
}
```

```html
<div class="compare-grid" style="--cols: {{ products|length }};">
```

One CSS rule handles 1 / 2 / 3 / 4 / etc. — no `if products|length
== 2` template branches. Custom properties cascade through the
CSS like any other variable, so descendants can also read them.
