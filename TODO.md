## TODOs and TADAs!!
[x] UI x DB: Basic app to scrape and add
[x] Backend x UI: Manual listing creation by pasting from clipboard
[x] Backend: Fix memory leak
[x] UI: Move tags from the top to the sidebar
[x] Add Quantity per listing
[x] Add a "Currencies" table and then add "Currency" field per listing, with foreign key to Currencies table entries. Backfill all existing listings' currencies to be INR. In UI, in the form to edit a listing, I should be able to change the currency for that listing's price.
[X] Turn off price tracking by default 
[x] Add Price field in Edit listing form 
[x] Remove unnecessary stats from /shopping-list UI 
[x] Add x button to images scraped automatically so that user can choose which images to save 
[x] Add Currency field to /add-item form 
[x] Add x button to images when editing a listing so that you can remove images not required
[x] Each tagged view can have a total value
[x] Remove 'Add item' button in the top
[x] Sanitize URLs for query params before scraping
[x] After scraping listing, if no price found then default price to 0
[x] Avoid duplicates by checking if the listing URL exists in DB 
[x] Keep only USD, SGD, INR currencies 
[x] Add to the "Edit Product" form the paste image from clipboard to upload images against this listing 
[x] Add endless scroll to the /shopping-list UI 
[x] Add hyperlink to the listing in /purchases view
[x] Remove unecessary stats in /purchases UI
[x] Add hyperlink to / upon click of app title in the sidebar
[x] Create new tag with a random unique colour from the color wheel
[x] Assign random unique colors to existing tags
[x] Before saving new tag capitalize first letter of tag
[x] Fix Entity Too Large during editing a listing
[x] If there is only 1 image in a listing, then set it as the main image
[x] Swap position of "Edit" and "Remove buttons in product listing page
[ ] Card view for multi currency tagged items 
[ ] Gallery view of all images so user can delete unecessary data
[ ] If any listings exist where price is unknown or 0, if any listings exists where there are no main image set, if any listing exists where no tags have been attached to it, create "pricing unknown", "images required", "untagged items" where pricing is 0
[ ] Size chart should be a special image under each listing
[ ] Add variant urls and pictures
[ ] Add sorting to /shopping-list. Sort by 
    [ ] Last modified date



[ ] Safari extension to create data in locally running app
[ ] Setup local server + GUI client for a SQLite
[ ] Push notifications when price drops 
[ ] Bulk edit listings
[ ] Bulk scrapes, what is the limit?
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
