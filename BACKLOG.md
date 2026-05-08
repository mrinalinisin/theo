# Backlog

Ideas and feature requests captured for later. The first section is what's most on my mind right now; the rest are loosely in capture order.

## Content & UI

- [ ] Set delivery date directly on the product detail page (not via the Edit modal). Mirror the inline pattern already used on `/purchases` cards and `/admin` cards.
- [ ] Pressing Escape on an open Edit listing form should dismiss the form.
- [ ] Constrain the delivery-date picker to only allow dates **after** the order/purchase date — `min` attribute on the `<input type="date">`, applied wherever the picker shows up (purchases card, admin card, future detail-page picker).
- [ ] Add ability to **clone a listing** — duplicate an existing product (URL, name, images, tags, notes) into a new Added row. Useful for ordering a second of something or recording variants of the same item.
- [ ] Add reviews on items, with ability to paste-zone photos and link to product videos.
- [ ] Listings should have a link to video instructions.
- [ ] A running list reviewing much-less-appreciated household appliances.
- [ ] A running-list article reviewing only totepacks / convertibles.
- [ ] A running list of best "Over The Door" products — what can you have over the door / railing / window?
- [ ] Publish a collection of objects to GitHub as static HTML.

## Analytics & reporting

- [ ] Re-haul analytics for meaningful inventory graphs. Add weekly and monthly stats — e.g. "You ordered the following items this week."
- [ ] Add a "This Week" view for purchases — what arrived and what's due.
- [ ] Week / Month / Quarterly / Annual report — "You made X purchases this month in these categories."
- [ ] WhatsApp feed of income.
- [ ] Auto-generate a **monthly report webpage** at the end of each month summarising items bought, amount spent, and including pictures of the items. Triggering options to consider since APScheduler was removed: runit cron entry, manual "Generate report" button, or render on-demand when the month-end page is first visited.

## Ranking, sorting, filtering

- [ ] Move Up or Down in product listing. The more up I move it, the more it shows up at the top — sorted by how many Up votes it has.
- [ ] Add a randomizer button in the header to shuffle the listing order.
- [ ] Ability to exclude tags in a view.
- [ ] Add filter by domain.
- [ ] Add a list view for `/purchases` sorted by date of delivery.

## Domain models

- [ ] Re-think how to accommodate consumables (Swiggy, grocery online orders) and services (spa). Define Anytype-style models — e.g. a "Consumables" model with default attributes like `created_at`.
- [ ] "Clothing" model with a default foreign key to a "Size Chart" model.
- [ ] "Devices" and "Furniture" models. Could dimensions be saved for devices and physical items?
- [ ] How to measure thickness of garments?

## Deployment & ops

- [ ] How to deploy locally and run one release-branch deployment while continuously developing locally?
- [ ] Separate development app vs. `sv`-productionized app.
