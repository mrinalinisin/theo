# Backlog

Ideas and feature requests captured for later. Roughly in capture order — not priority order.

## Ranking & sorting

- [ ] Move Up or Down in product listing. The more up I move it, the more it shows up at the top — sorted by how many Up votes it has.
- [ ] Add a randomizer button in the header to shuffle the listing order.

## Filtering & views

- [ ] Ability to exclude tags in a view.
- [ ] Add filter by domain.
- [ ] Add a list view for `/purchases` sorted by date of delivery.

## UI & layout

- [ ] Multi-select cards on `/products` and mark the selection as Purchased in one action. **Replaces the Cart → Checkout flow** (remove `/cart` routes + Cart nav link as part of this work).
- [ ] Inline delivery-date editor on `/purchases` cards — set / update an item's expected or actual delivery date without leaving the listing. Mirror the inline pattern already used in the "Date not set" footer of `/purchases/calendar`.

## Relations & boards

- [ ] Why did I buy this piece? Remix boards in the app, so I can mix and match pieces from outfits to bags to shoes.
- [ ] Backlinks between products: "connected to", with meaningful relations like "variant of" or "also see".

## Domain models

- [ ] Re-think how to accommodate consumables (Swiggy, grocery online orders) and services (spa). Define Anytype-style models — e.g. a "Consumables" model with default attributes like `created_at`.
- [ ] "Clothing" model with a default foreign key to a "Size Chart" model.
- [ ] "Devices" and "Furniture" models. Could dimensions be saved for devices and physical items?
- [ ] How to measure thickness of garments?

## Reviews & content

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

## Deployment & ops

- [ ] How to deploy locally and run one release-branch deployment while continuously developing locally?
- [ ] Separate development app vs. `sv`-productionized app.
