# Audiobook Library

I want to write a program to manage audiobooks.

It needs to:
- Integrate with HardCover API (https://hardcover.app/) to retrieve a user's book list. Respect HardCover's states (want to read, reading, read including date)
- Search for books using Prowlarr's API
- Monitor a download directory for books to be downloaded
- Move downloaded books into an audiobooks folder, renaming as necessary

The UI needs to:
- List books (author, name, series information, cover art, read state, downloaded state)
- Update read state
- Allow searching books (via HardCover API) by name, author, series. For searched books set state and allow download

Architecture
- Python, using any appropriate web framework
- sqlite database
- Run in a single container

# Future Work (currently outside the scope of this build):
- Probide an API to:
  - list known books
  - download books from the audiobook library
  - mark books as reading, read
- Write a audiobook player companion app for Android and iOS, similar to Smart Audiobook Player (https://play.google.com/store/apps/details?id=ak.alizandro.smartaudiobookplayer&hl=en_AU), that can download books from the Audiobook Library, play audiobooks, and update read status
