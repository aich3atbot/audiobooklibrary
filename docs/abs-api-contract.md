# Audiobookshelf API contract (pinned)

Shapes verified against the ABS server source (`advplyr/audiobookshelf` @ `82aec5f`,
2026-07) and the official mobile app (`advplyr/audiobookshelf-app`). This is the
implementation reference for our ABS-compatible API — do not code these endpoints from
memory; check here, and when in doubt re-check the source.

Conventions used below: our single user maps to an ABS `root` user; the single library is
`lib_audiobooks`; library item ids are `li_<book.id>`; audio file "ino" is our audio_file
row id as a string.

## Auth

Tokens are HS256 JWTs, payload `{userId, username, type: "access"|"refresh", exp}`.
Access expiry 1h, refresh 30d (ABS defaults). Accepted via `Authorization: Bearer <t>`
**or `?token=<t>` query param** (streaming/cover URLs rely on this). Legacy tokens have no
`exp` and no `type` and are accepted indefinitely.

- `POST /login` — JSON `{username, password}`. The mobile app sends header
  `x-return-tokens: true`; when present include `user.refreshToken`, else set it null and
  put the refresh token in a `refresh_token` cookie. Response (same payload for
  `/api/authorize` and `/auth/refresh`):

```json
{
  "user": {
    "id": "<uuid>", "username": "...", "email": null, "type": "root",
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

- `POST /auth/refresh` — refresh token from `x-refresh-token` header (mobile) or
  `refresh_token` cookie; 401 `{error}` if missing/invalid. Returns login payload with new
  `accessToken` (include new `refreshToken` only when the header form was used).
- `POST /api/authorize` — bearer-authenticated; returns the login payload.
- `POST /logout` — 200.

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

`GET /api/items/:id?expanded=1&include=progress` → minified fields plus:
- `media.metadata` gains `authors: [{id, name}]`, `narrators: []`,
  `series: [{id, name, sequence: "<idx as string>"}]`, `descriptionPlain`
- `media.audioFiles`: `[{index, ino: "<af id>", metadata: {filename, ext: ".m4b",
  path, relPath, size, mtimeMs, ctimeMs, birthtimeMs}, addedAt, updatedAt,
  trackNumFromMeta, discNumFromMeta: null, trackNumFromFilename: null,
  discNumFromFilename: null, format: null, duration, bitRate: null, language: null,
  codec: null, timeBase: null, channels: null, channelLayout: null, chapters: [],
  embeddedCoverArt: null, metaTags: null, mimeType}]`
- `media.chapters`: `[{id, start, end, title}]` (seconds), `media.ebookFile: null`,
  `media.tracks`: audioTracks (see playback), `media.duration`, `media.size`,
  `media.libraryItemId`
- top-level `libraryFiles: []` and, with include=progress, `userMediaProgress`
- `GET /api/items/:id/cover?width=&height=&format=&raw=` → image bytes (local cover file
  if present, else proxy the Hardcover CDN cover_url)

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
  restarts finished books).
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

## Me / progress

`oldMediaProgress`:
```json
{"id": "<uuid>", "userId": "<uuid>", "libraryItemId": "li_1", "episodeId": null,
 "mediaItemId": "<book uuid>", "mediaItemType": "book", "duration": <sec>,
 "progress": <0..1>, "currentTime": <sec>, "isFinished": false,
 "hideFromContinueListening": false, "ebookLocation": null, "ebookProgress": null,
 "lastUpdate": <ms>, "startedAt": <ms>, "finishedAt": <ms|null>}
```
- `GET /api/me` → old user JSON (includes `mediaProgress` array).
- `PATCH /api/me/progress/:libraryItemId` — any of `{duration, currentTime, progress,
  isFinished, hideFromContinueListening, finishedAt}` → upsert progress; 200.
- Finished rule (server-side, applies to session sync too): finished when
  `duration - currentTime <= markAsFinishedTimeRemaining` (default 10s), or when the
  client sends `isFinished: true`. **Finished → mark book read on Hardcover.**

## socket.io (minimal shim)

The app connects socket.io to the server root after login and emits `auth` with the
token; reply with `init` → `{userId, username, usersOnline: []}` (bad token → emit
`auth_failed` `{message}`). Other events the app listens for (`user_updated`,
`user_item_progress_updated`, `playlist_added`) are optional pushes — safe to never emit.
Socket failure only shows a "disconnected" indicator; REST keeps working.

## Client call sequence (official app)

1. `GET /status` (server check) → 2. `POST /login` (`x-return-tokens: true`) →
3. socket connect + `auth` → 4. `GET /api/libraries` → 5. personalized/items browsing →
6. `POST /api/items/:id/play` → stream contentUrl with Range → 7. `/api/session/:id/sync`
every ~15s, `close` on stop. Offline: download each audio file + cover, then
`/api/session/local(-all)` when back online. Token expiry → `POST /auth/refresh` with
`x-refresh-token`.
