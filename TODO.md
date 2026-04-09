

## Features 
[x] UI x DB: Basic app to scrape and add
[x] Backend x UI: Manual listing creation by pasting from clipboard
[x] Backend: Fix memory leak
[x] UI: Move tags from the top to the sidebar
[x] Add Quantity per listing
[ ] Add a "Currencies" table and then add "Currency" field per listing, with foreign key to Currencies table entries. Backfill all existing listings' currencies to be INR. In UI, in the form to edit a listing, I should be able to change the currency for that listing's price.
[ ] Turn off price tracking by default 
[ ] Total value number in INR not required 

[ ] Need the ability to remove photos in the /add-item UI post scrape 
[ ] Clean up junk metadata in existing scrapes like random pricing
[ ] Add quantity and variant urls and pictures [needs thinking to sort of link to other scrapes]

[ ] Each tagged view can have a total value
[ ] Remove the default alt text in the URL textbox in /add-item
[ ] Nuke everything after ? and # in url before scraping
[ ] If no pricing value found in scrape, then set amount to 0
[ ] Auto create tags: "pricing unknown", "images required", "untagged items" where pricing is 0
[ ] Need a search in UI to search through scrapes
[ ] Gallery view to delete unecessary data
[ ] Avoid duplicates by checking if the listing URL exists in DB 
[ ] Safari extension to create data in locally running app
[ ] Setup local server + GUI client for a SQLite
[ ] Push notifications when price drops 
[ ] Size chart should be a special image under each listing
[ ] Bulk edit listings
[ ] Edit listing should also have the paste image to upload 
[ ] Bulk scrapes, what is the limit?
[ ] By default don't track price
[ ] Assign a random colour not previously assigned to any tag in the colour wheel 
[ ] Capitalize the first letter of tag names
[ ] Put a small x on scraped photos so you can remove them if you don't want them
[ ] Listing should have field for "Tracking Link"
[ ] Listing should have link to video instructions


[ ] Hot reload in server and UI 
[ ] Separate development app vs `sv` productionized app 
[ ] How can I leverage Metabase like features in Gummi? [Abstract, needs thinking]
[ ] Can each tag be a type corresponding to Anytype types? So that I can get Claude to update listings inside Anytype?
[ ] "Access Denied" domains scrapes need to be successfully re-tried through playwright or some other headless browser
[ ] Some thumbnails are not showing up in /shopping-list, why?
[ ] Not scraping price properly in amazon.com links, why?
[ ] Is the UI templates in EJS or similar?
[ ] Anytype listing, media creator, data filler
