# Audiobookshelf API contract (pinned)

Shapes verified against the ABS server source (`advplyr/audiobookshelf` @ `82aec5f`,
2026-07) and the official mobile app (`advplyr/audiobookshelf-app`). This is the
implementation reference for our ABS-compatible API — do not code these endpoints from
memory; check here, and when in doubt re-check the source.

Conventions used below: each of our user accounts maps to an ABS `root`-permission user
(a deliberate simplification — apps only gate features on it); the token `userId` is the
account's stable uuid, and mediaProgress/bookmarks/sessions are scoped to that user. The
single shared library is `lib_audiobooks` (its catalogue is the same for every user).

**A library item is one *edition* of a book** (a book can hold several recordings):
item ids are `li_<edition.id>`, `media.id` is `bk_<edition.id>`, the item "ino" is the
edition id, and the audio file "ino" is our audio_file row id as a string. Progress and
bookmarks are per edition; finishing any edition marks the *book* read on Hardcover.
When a book has 2+ editions in the library, the payload `title` (and `displayTitle` in
play sessions) carries the edition's label — `"Chamber of Secrets (Narrator)"` — so
list views can tell the items apart (display-only; the stored title is unchanged), and
`narrators`/`narratorName`/filterdata narrators come from the edition's Hardcover
narrator credits or its label.

## Auth

Tokens are HS256 JWTs, payload `{userId, username, type: "access"|"refresh", exp}`.
Access expiry 1h, refresh 30d (ABS defaults). Accepted via `Authorization: Bearer <t>`
**or `?token=<t>` query param** (streaming URLs rely on this). Legacy tokens have no
`exp` and no `type` and are accepted indefinitely.

**Endpoints that must take no auth at all** (upstream `Auth.ignorePatterns` + the
`/public` router, and the app version-gates on the `serverVersion` we advertise, so these
are not optional):

- `GET /api/items/:id/cover` — since server 2.17.0 the apps send *no* token with cover
  requests (Android loads them through Glide's bare `HttpURLConnection`, which carries no
  headers). Requiring auth here shows blank covers everywhere, with 401s in the log.
- `GET /api/authors/:id/image` — same exemption upstream; we 302 to the author's
  Hardcover photo (`author.image_url`), 404 when there is none.
- `GET /public/session/:id/track/:index` — see Playback; the session id is the credential.

Our UI's cookie-redirect middleware must leave `/public/` alone too — a 303 to `/login`
hands the HTML login page to the app's audio player (`UnrecognizedInputFormatException`),
which then retries forever.

- `POST /login` — JSON `{username, password}`. The mobile app sends header
  `x-return-tokens: true`; when present include `user.refreshToken`, else set it null and
  put the refresh token in a `refresh_token` cookie. Response (same payload for
  `/api/authorize` and `/auth/refresh`):

```json
{
  "user": {
    "id": "<uuid>", "username": "...", "email": null, "type": "user",
    "token": "<legacy JWT (no exp)>", "isOldToken": false,
    "accessToken": "<JWT 1h>", "refreshToken": "<JWT 30d or null>",
    "mediaProgress": [ <oldMediaProgress...> ],
    "seriesHideFromContinueListening": [], "bookmarks": [],
    "isActive": true, "isLocked": false, "lastSeen": <ms>, "createdAt": <ms>,
    "permissions": {"download": true, "update": true, "delete": true, "upload": true,
      "createEreader": true, "accessAllLibraries": true, "accessAllTags": true,
      "accessExplicitContent": true, "selectedTagsNotAccessible": false},
    "librariesAccessible": [], "itemTagsSelected": [], "hasOpenIDLink": false
  },
  "userDefaultLibraryId": "lib_audiobooks",
  "serverSettings": { "id": "server-settings", "version": "<serverVersion>", ... },
  "ereaderDevices": [],
  "Source": "docker"
}
```

`type` is deliberately `"user"`, never `root`/`admin`: clients unlock a whole
server-administration UI for those (users, backups, tasks, server settings, uploads, API
keys, item/library editing) and we serve none of it, so advertising admin would only offer
screens that 404. The `permissions` block is what gates playback and downloads, and there
everything is granted.

- `POST /auth/refresh` — refresh token from `x-refresh-token` header (mobile) or
  `refresh_token` cookie; 401 `{error}` if missing/invalid. Returns login payload with new
  `accessToken` (include new `refreshToken` only when the header form was used).
- `POST /api/authorize` — bearer-authenticated; returns the login payload.
- `POST /logout[?allDevices=1]` — revokes the session behind the presented refresh token
  (`x-refresh-token` header or cookie), or all of that user's sessions with `allDevices=1`.
  Answers JSON `{success: true}` to token-bearing clients and a 303 to `/login` for the UI
  form, which posts the same route. Must stay reachable without a session cookie.
- `GET /api/me/sessions?page=&itemsPerPage=` → `{total, numPages, page, itemsPerPage,
  sessions: [{id: <uuid>, ipAddress, userAgent, deviceInfo, createdAt, updatedAt,
  current}]}`. `current` is resolved from the refresh token on the request. `deviceInfo`
  is upstream's *parsed* user agent; we don't parse UAs and leave it null — clients label
  their own sessions from the raw `userAgent` prefix and fall back to "unknown device".
  The list includes the user's **browser** sessions as well as their apps (upstream lists
  them too), so a `DELETE` here can sign a web session out; a browser row is never
  `current`, since currency comes from the request's refresh token.
- `DELETE /api/me/sessions/:id` — 400 on a non-uuid id (clients rely on this), 404 if it
  isn't the caller's, else 200 and that device is signed out.

**Sessions are what make revocation real.** Access tokens stay stateless, but each refresh
token gets an `auth_session` row — `kind="abs"`; the web UI's own logins share the table
as `kind="ui"` (SHA-256 of the credential, never the credential itself) — and
`/auth/refresh` requires one — otherwise a "sign out" on a lost phone would leave it
working for the token's full 30 days. Two consequences to preserve: refresh tokens carry a
random `jti` so that two logins in the same second can't mint the *same* token (the
payload is otherwise just user + type + 1s-resolution expiry, and sessions are keyed on
it); and rotation keeps the previous token usable for a 10-minute grace window, because
clients fire concurrent refreshes and the loser must not be logged out. In the grace case
we answer with a new access token and **no** refresh token — upstream returns the winning
token, which we can't, holding only its hash.

## Discovery (public, no auth)

- `GET /status` → `{"app": "audiobookshelf", "serverVersion": "<x>", "isInit": true,
  "language": "en-us", "authMethods": ["local"], "authFormData": {}}`
- `GET /ping` → `{"success": true}`
- `GET /healthcheck` → 200

## Libraries

- `GET /api/libraries` → `{"libraries": [<library>]}` where library =
  `{id, name, folders: [{id, fullPath, libraryId, addedAt}], displayOrder: 1, icon:
  "audiobookshelf", mediaType: "book", provider: "custom", settings: {coverAspectRatio: 1,
  disableWatcher: true, skipMatchingMediaWithAsin: false, skipMatchingMediaWithIsbn: false,
  autoScanCronExpression: null, audiobooksOnly: true, hideSingleBookSeries: false,
  onlyShowLaterBooksInContinueSeries: false, metadataPrecedence: [], podcastSearchRegion:
  "us", markAsFinishedPercentComplete: null, markAsFinishedTimeRemaining: 10}, lastScan:
  <ms|null>, lastScanVersion: null, createdAt: <ms>, lastUpdate: <ms>}`
- `GET /api/libraries/:id` → library (with `?include=filterdata`: `{filterdata: {...},
  issues: 0, numUserPlaylists: 0, library}`)
- `GET /api/libraries/:id/items?limit=&page=&sort=&desc=&filter=&minified=&include=` →
  `{results: [<libraryItemMinified>], total, limit, page, sortBy, sortDesc, filterBy,
  mediaType: "book", minified, collapseseries: false, include: "", offset}`
  (limit=0 means all; offset = page*limit)
- `GET /api/libraries/:id/personalized?limit=` → array of shelves:
  `{id: "continue-listening"|"recently-added"|"listen-again", label, labelStringKey,
  type: "book", entities: [<libraryItemMinified>], total}`
  (continue-listening = in-progress not finished, ordered by progress lastUpdate desc;
  recently-added = by addedAt desc; listen-again = finished, by finishedAt desc)
- `GET /api/libraries/:id/filterdata` → `{authors: [{id, name}], genres: [], tags: [],
  series: [{id, name}], narrators: [], languages: [], publishers: [], publishedDecades: []}`
- `GET /api/libraries/:id/series` / `/authors` → paged results like items (low priority;
  official app browse pages).
- `GET /api/libraries/:id/series/:seriesId` and `GET /api/series/:seriesId` → the same
  `Series.toOldJSON` = `{id, name, nameIgnorePrefix, description: null, addedAt, updatedAt,
  libraryId}`, plus `totalDuration` (not upstream here, but it is on the series *list* and
  clients show it). `?include=progress` adds `{libraryItemIds, libraryItemIdsFinished,
  isFinished}`. 404 for an unknown series. This is the series page *header* only — clients
  list the books through `/items?filter=series.<id>`.
- `GET /api/authors/:id?include=items[,series]` → `Author.toOldJSON` =
  `{id, asin: null, name, description: null, imagePath: null, libraryId, addedAt,
  updatedAt}`; with `items` add `libraryItems: [<minified>]`, with `series` also
  `series: [{id, name, items: [<minified, metadata.series flattened to {id, name,
  nameIgnorePrefix, sequence}>]}]` sorted by sequence. 404 for an unknown author. This is
  the author landing page in third-party clients. `imagePath` is `"internal"` when we hold
  a Hardcover photo (clients only test it for null before requesting
  `/api/authors/:id/image`), else null.

**Filters** (`?filter=` on `/items`) are `<group>.<base64 value>`, url-decoded then
base64-decoded, or a bare group (`issues`, `missing`). Groups: genres, tags, series,
authors, progress, narrators, publishers, publishedDecades, missing, languages, tracks,
ebooks. We answer `series.<ser_id>`, `authors.<aut_id>`, `narrators.<name>` and
`progress.<finished|in-progress|not-started|not-finished>` (per requesting user); any
other group matches nothing, as upstream does with an empty column. `sort=sequence`
applies **only** under a `series.` filter — ABS drops it otherwise.

**A `series.` filter changes both the order and the item shape.** Upstream appends a
sequence sort to whatever the client asked for, and an *unrecognised* sort key produces no
ordering of its own (`getOrder` returns `[]`) — which is the only reason clients sending
`sort=media.metadata.series.sequence` (Absorb does; it is not a real ABS sort key) get
reading order instead of alphabetical. It also hangs the filtered series off each minified
item as `media.metadata.series = {id, name, sequence}` (`LibraryItem.getByFilterAndSort`),
and that is where clients read the per-book sequence badge — the minified metadata
otherwise carries only the `seriesName` string.

## Library items

`libraryItemMinified`:
```json
{"id": "li_1", "ino": "1", "oldLibraryItemId": null, "libraryId": "lib_audiobooks",
 "folderId": "fol_audiobooks", "path": "<abs dir>", "relPath": "<rel dir>",
 "isFile": false, "mtimeMs": <ms>, "ctimeMs": <ms>, "birthtimeMs": <ms>,
 "addedAt": <ms>, "updatedAt": <ms>, "isMissing": false, "isInvalid": false,
 "mediaType": "book",
 "media": {"id": "<book uuid>", "metadata": <metaMinified>, "coverPath": "<path|null>",
   "tags": [], "numTracks": N, "numAudioFiles": N, "numChapters": N,
   "duration": <sec>, "size": <bytes>, "ebookFormat": null},
 "numFiles": N, "size": <bytes>}
```
`metaMinified` = `{title, titleIgnorePrefix, subtitle: null, authorName, authorNameLF,
narratorName: "", seriesName: "<Series #idx>"|"", genres: [], publishedYear: null,
publishedDate: null, publisher: null, description: null, isbn: null, asin: null,
language: null, explicit: false, abridged: false}`

**Three item shapes, and item detail is not the minified one.** `toOldJSONMinified()`
(above) is for list/shelf endpoints only. `GET /api/items/:id` **without** `expanded=1`
returns `toOldJSON()`: the base fields plus `lastScan`/`scanVersion`, `libraryFiles`, and
a full `media` = `{id, libraryItemId, metadata: <expanded>, coverPath, tags, audioFiles,
chapters, ebookFile}` — no `numTracks`/`numAudioFiles`/`numChapters`/`duration`/`size`
counters and **no `tracks`**. Clients that skip `expanded=1` (Lissen) build their chapter
list from `media.chapters`, falling back to `media.audioFiles`; answering them with the
minified shape leaves a book unplayable ("The book has no chapters").

`GET /api/items/:id?expanded=1&include=progress` → minified fields plus:
- `media.metadata` gains `authors: [{id, name}]`, `narrators: []`,
  `series: [{id, name, sequence: "<idx as string>"}]`, `descriptionPlain`
- `media.audioFiles`: `[{index, ino: "<af id>", metadata: {filename, ext: ".m4b",
  path, relPath, size, mtimeMs, ctimeMs, birthtimeMs}, addedAt, updatedAt,
  trackNumFromMeta, discNumFromMeta: null, trackNumFromFilename: null,
  discNumFromFilename: null, format: null, duration, bitRate: null, language: null,
  codec: null, timeBase: null, channels: null, channelLayout: null, chapters: [],
  embeddedCoverArt: null, metaTags: null, mimeType}]`
- `media.chapters`: `[{id, start, end, title}]` (seconds — embedded chapters per file,
  shifted by that file's start offset; one per track for files with none),
  `media.ebookFile: null`,
  `media.tracks`: audioTracks (see playback), `media.duration`, `media.size`,
  `media.libraryItemId`
- top-level `lastScan: null`, `scanVersion: null`, `libraryFiles: []` and, with
  include=progress, `userMediaProgress`
- `POST /api/items/batch/get` — body `{libraryItemIds: [...]}` → `{libraryItems:
  [<expanded>]}`; **403** (not 400) on an empty list. Third-party clients resolve series
  search hits through this.
- `GET /api/items/:id/cover?width=&height=&format=&raw=` → image bytes (local cover file
  if present, else 302 to the Hardcover CDN cover_url). **No auth** — see Auth above.

## Playback

- `POST /api/items/:id/play` — body `{deviceInfo: {deviceId, clientName, clientVersion,
  manufacturer, model, sdkVersion?}, forceDirectPlay, forceTranscode,
  supportedMimeTypes: [...], mediaPlayer}`. We always direct-play (reject with 500 only if
  no audio files). Response = playback session:

```json
{"id": "play_<uuid>", "userId": "...", "libraryId": "lib_audiobooks",
 "libraryItemId": "li_1", "bookId": "<book uuid>", "episodeId": null,
 "mediaType": "book", "mediaMetadata": <metadata expanded>, "chapters": [...],
 "displayTitle": "...", "displayAuthor": "...", "coverPath": "...",
 "duration": <sec>, "playMethod": 0, "mediaPlayer": "<from req>",
 "deviceInfo": {"id": "...", "deviceId": "...", ...}, "serverVersion": "<x>",
 "date": "2026-07-11", "dayOfWeek": "Friday", "timeListening": 0,
 "startTime": <sec resume point>, "currentTime": <sec>, "startedAt": <ms>,
 "updatedAt": <ms>,
 "audioTracks": [{"index": 1, "startOffset": 0, "duration": <sec>, "title": "<filename>",
   "contentUrl": "/api/items/li_1/file/<ino>", "mimeType": "audio/mp4", "codec": null,
   "metadata": <audio file metadata block>}],
 "libraryItem": <expanded library item>}
```
  playMethod: 0=direct play. startTime = saved progress currentTime (0 if finished — ABS
  restarts finished books). One open session per user+device: opening a new one drops the
  device's previous session (clients re-open on every playback error).
- `GET /public/session/:id/track/:index` — **how the app actually direct-plays** since
  server 2.22.0 (advplyr/audiobookshelf#4263): it ignores the session's `contentUrl` and
  streams here, unauthenticated, matching `audioTracks[].index` (1-based). 404 for an
  unknown session or index; Range support required (seeking). Only the pre-2.22.0 path
  uses `contentUrl` with `?token=`.
- `GET /api/items/:id/file/:ino` — stream with HTTP Range support (`?token=` auth).
- `GET /api/items/:id/file/:ino/download` — same but `Content-Disposition: attachment`
  (the app's offline download fetches every audio file this way).
- `POST /api/session/:id/sync` — `{currentTime, timeListened, duration}` → update
  session + media progress; 200 `{success: true}`-ish (server returns sendStatus(200)).
- `POST /api/session/:id/close` — same body (optional) then discard session; 200.
- `POST /api/session/local` — the app syncs offline playback: body is a full local
  PlaybackSession JSON incl. `libraryItemId`, `currentTime`, `timeListened`, `duration`,
  `updatedAt`; update progress from it; 200 `{}`.
- `POST /api/session/local-all` — `{sessions: [...], deviceInfo}` → same, batch; 200
  `{results: []}`-ish.

## Search, playlists/collections, bookmarks, stats (post-audit additions)

- `GET /api/libraries/:id/search?q=&limit=12` — the app's Search tab. Response (current
  source shape; the docs-site `matchKey`/`matchText` fields were removed):
  `{book: [{libraryItem: <expanded item>}], narrators: [], tags: [], genres: [],
  series: [{series: {id, name}, books: [<minified item>]}], authors: [<author entry
  with numBooks>]}`. 400 when `q` is empty.
- `GET /api/libraries/:id/playlists` and `/collections` — we return empty paged results
  (`{results: [], total: 0, limit, page}`) so those app tabs render instead of erroring.
- Bookmarks (`user.bookmarks` in the login payload):
  `POST /api/me/item/:id/bookmark` `{time, title}` → bookmark JSON
  `{libraryItemId, title, time, createdAt}` (400 invalid); `PATCH` same body updates the
  bookmark matched **by time** (404 unknown); `DELETE /api/me/item/:id/bookmark/:time`.
- `GET /api/me/listening-stats` — zeroed `{totalTime, items: {}, days: {}, dayOfWeek: {},
  today: 0, recentSessions: []}` (we don't persist listening sessions).
- `GET /api/me/stats/year/:year` — zeroed year-in-review stats except
  `numBooksFinished`/`numBooksListened`, counted from media_progress finished_at.
- `PATCH /api/me/progress/:id` also accepts a bare `progress` fraction (0–1); when
  `currentTime` is absent it maps to `progress × duration`.

## Me / progress

`oldMediaProgress`:
```json
{"id": "<uuid>", "userId": "<uuid>", "libraryItemId": "li_1", "episodeId": null,
 "mediaItemId": "<book uuid>", "mediaItemType": "book", "duration": <sec>,
 "progress": <0..1>, "currentTime": <sec>, "isFinished": false,
 "hideFromContinueListening": <bool>, "ebookLocation": null, "ebookProgress": null,
 "lastUpdate": <ms>, "startedAt": <ms>, "finishedAt": <ms|null>}
```
- `GET /api/me` → old user JSON (includes `mediaProgress` array).
- `PATCH /api/me/progress/:libraryItemId` — any of `{duration, currentTime, progress,
  isFinished, hideFromContinueListening, createdAt, finishedAt}` → upsert progress; 200.
  `createdAt`/`finishedAt` are epoch ms and are how clients **edit a book's start/finish
  dates**: ABS shows the start date as the progress row's `createdAt` (our
  `media_progress.started_at`, reported back as `startedAt`), so a client re-sends the row
  with a new one. Both mirror onto the book's shelf entry — that is what reaches Hardcover
  — without moving its read state.
- `DELETE /api/me/progress/:id` — `:id` is the **progress** id (`prog_<n>`), not an item
  id, and only the caller's own row (someone else's is a 404, as upstream). Clears the
  listening position only; read state is book-level and lives on Hardcover.
- `GET /api/me/progress/:id/remove-from-continue-listening` (progress id) and
  `GET /api/me/series/:id/remove-from-continue-listening` (series id) → set
  `hideFromContinueListening`, return the old user JSON. Upstream's series form hides a
  series from its *Continue Series* shelf, which we don't serve, so ours hides that
  series' own books from Continue Listening. The flag clears itself as soon as
  `currentTime` moves (`MediaProgress.applyProgressUpdate`), unless the same request sets
  it explicitly — that is what makes a client's "reset progress" (sync 0, then PATCH with
  the flag) stick.
- Finished rule (server-side, applies to session sync too): finished when
  `duration - currentTime <= markAsFinishedTimeRemaining` (default 10s), or when the
  client sends `isFinished: true`. **Finished → mark book read on Hardcover**, dated
  today.

Read state is driven by progress in three more places, all in `app/abs/progress.py`
(ours, not ABS's — ABS has no notion of a shelf). Every progress route funnels through
`apply_progress`, so they apply to session sync, local/offline sync and the PATCH alike:

- **Started**: `currentTime >= MARK_READING_AFTER_MINUTES` (default 1) and the row is not
  finished → the book becomes *currently reading* on Hardcover, dated today. Fires once
  (the state is no longer "not reading" afterwards); a book already marked *read* promotes
  too, since playing it again is a re-listen. At 0 the first sync after playback starts
  promotes it — there is no hook on `POST /api/items/:id/play`.
- **Near-finished sweep**: audiobooks are usually abandoned in the trailing credits, so a
  book left within `MARK_READ_TAIL_MINUTES` of the end (default 30) is marked finished
  *and* read the moment progress arrives for a **different book** (other editions of the
  same book don't count). The tail is capped at half the duration so a book shorter than
  it still has to be listened most of the way through; at 0 only a complete listen counts.
- **Re-listen**: a finished row resynced at least 60s in but before the near-finish mark is
  un-finished. This is the one case where a sync clears `isFinished` on its own — a stray
  `currentTime: 0` or a rewind into the last chapter does not. That 60s floor is fixed, not
  `MARK_READING_AFTER_MINUTES`, which may be 0.

## socket.io (minimal shim)

The app connects socket.io to the server root after login and emits `auth` with the
token; reply with `init` → `{userId, username, usersOnline: []}` (bad token → emit
`auth_failed` `{message}`). Other events the app listens for (`user_updated`,
`user_item_progress_updated`, `playlist_added`) are optional pushes — safe to never emit.
Socket failure only shows a "disconnected" indicator; REST keeps working.

## Client call sequence (official app)

1. `GET /status` (server check) → 2. `POST /login` (`x-return-tokens: true`) →
3. socket connect + `auth` → 4. `GET /api/libraries` → 5. personalized/items browsing →
6. `POST /api/items/:id/play` → stream `/public/session/:id/track/:index` with Range
(pre-2.22.0 servers: `contentUrl`) → 7. `/api/session/:id/sync`
every ~15s, `close` on stop. Offline: download each audio file + cover, then
`/api/session/local(-all)` when back online. Token expiry → `POST /auth/refresh` with
`x-refresh-token`.
