
## Work on these sequentially


[ ] Setup local server + GUI client for a SQLite
[ ] Need the ability to remove photos in the /add-item UI post scrape 
[ ] Clean up junk metadata in existing scrapes like random pricing
[ ] Total value number in INR not required 
[ ] Each tagged view can have a total value
[ ] Add quantity and variant urls and pictures [needs thinking to sort of link to other scrapes]
[ ] Push notifications when price drops 
[ ] sv runit-ize the app 
[ ] Nuke everything after ? and # in url before scraping
[ ] Auto create tags where pricing is 0
[ ] Set default pricing 0 
[ ] Remove the default alt text in the URL textbox in /add-item
[ ] Hot reload in server and UI 
[ ] How can I leverage Metabase like features in Gummi? [Abstract, needs thinking]
[ ] Can each tag be a type corresponding to Anytype types? So that I can get Claude to update listings inside Anytype?
[ ] "Access Denied" domains scrapes need to be successfully re-tried through playwright or some other headless browser
[ ] Some thumbnails are not showing up in /shopping-list, why?
[ ] Not scraping price properly in amazon.com links, why?
[ ] Need a search in UI to search through scrapes
[ ] Is the UI templates in EJS or similar?
[ ] Gallery view to delete unecessary data
[ ] Multiple product scrapes at the same time?
[ ] Check if this product exists 
    [ ] by product page URL 
    [ ] by image matching AI 
[ ] Anytype listing, media creator, data filler
[ ] Safari extension to create data in locally running app


## Done 
[x] UI: Scrape and add
[x] Backend x UI: Manual listing creation by pasting from clipboard
[x] Backend: Fix memory leak
[x] UI: Move tags from the top to the sidebar
[x] Add Quantity per listing
[ ] Add a table with Currencies and then add "Currency" field per listing, with primary key to Currencies table entries 