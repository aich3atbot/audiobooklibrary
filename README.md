# Audiobook Library

Self-hosted audiobook manager

- **Manage your audiobook files** - organize audiobooks as `Author/Series/{#} - Title/`, or
  `Author/Title/` when the book has no series
- **Syncs your book list from [Hardcover](https://hardcover.app)** — authors, series, covers,
  and read states (want to read / reading / read, with read dates). Two-way: changing a
  book's state in the UI pushes back to Hardcover.
- **[audiobookshelf](https://audiobookshelf.org/) compatible serve** - listen to your audiobooks on a mobile device using an audiobookshelf compatible client. See [Listening with an Audiobookshelf app](#listening-with-an-audiobookshelf-app) for the clients that have been tested.
- **Download audiobooks** - Search for audiobooks on [AudioBookBay](http://audiobookbay.fi), uses an external torrent client to download books. Currently [Deluge](https://deluge-torrent.org) and [qBittorrent](https://www.qbittorrent.org/) are supported.

You do not need to use all of the features. You can manage your existing audiobook library and listen using an audiobookshelf client, or download audiobooks but manually copy them to a listening device.

## AI Warning

This project is entirely [vibe coded](https://en.wikipedia.org/wiki/Vibe_coding).
Very little human code review has been performed.

Use at your own risk, as you should any software you have not personally written or reviewed.

## Requirements

### Hardcover account

Each full user needs a [Hardcover](https://hardcover.app) account and API token. It is used
to query book metadata, and to get / set book read status.

Get this from [Hardcover](https://hardcover.app) → Settings → Hardcover API → **New API
Key**. The form asks for a name, an expiration, and which permissions the key may use.

#### Permissions

Hardcover keys are scoped: a key can only run the operations its permissions cover. This
app needs five.

| Permission | What it is used for |
| --- | --- |
| `read:me:content` | Identifying the account a token belongs to (`id`, `username`) — what the Settings page's Hardcover check reports. |
| `read:library` | Reading your shelves: which books are on them, their read state and read dates. |
| `write:library` | Pushing read state back — shelving a book, and marking one *reading* or *read*. |
| `read:catalog:data` | Looking up book, edition, author and series metadata. |
| `read:catalog:search` | The Search page, and matching folders in `/imports` to books. |

[**Create a key with exactly these permissions**](https://hardcover.app/account/api/keys/new?scope=read:me:content+read:library+write:library+read:catalog:data+read:catalog:search)
— that link opens the New API Key form with the five already ticked, and nothing else is
needed. A key with the blanket `all` permission works too, but it can do anything your
Hardcover account can, up to deleting it. Keys issued before August 2026 are equivalent to
`all` and keep working.

Two things to know about narrowing further:

- `read:library` covers your public, followers-only and private shelf entries. Ticking only
  `read:library:public` also "works", but privately shelved books simply never appear here,
  which looks like sync silently dropping books.
- Leaving out a permission the app needs isn't a silent failure: Hardcover answers `403`,
  and the last-sync line on the Library and Settings pages reads
  `error: Client error '403 Forbidden'` — the status only, it does not name the missing
  permission. Note that the Settings page's Hardcover *check* only identifies the account,
  so it still says "connected" on a key that is missing, say, `write:library`; the sync
  result is the thing to read.

**Expiry is a real setting.** Once the key's expiration passes it stops working, sync fails
and someone has to paste a new key in on the admin's Users page, so pick the longest expiry
you are comfortable with, or **never**.

The token is what syncing, searching and importing run on. An account without one still
works for browsing and listening to what is already in the library — it just cannot reach
Hardcover, so nothing syncs.

Limited users (Audiobookshelf API only) hold no Hardcover API token at all, but at least one
full user is required to populate your audiobook library.

### BitTorrent Client (optional)

To download audiobooks an external BitTorrent client is required. Currently
[Deluge](https://deluge-torrent.org) and [qBittorrent](https://www.qbittorrent.org/) are
supported.

- A running client with its **web UI** enabled, named in `DOWNLOAD_CLIENT` (`deluge` or
  `qbittorrent`) with `DOWNLOAD_URL` pointing at that UI — e.g.
  `http://host.docker.internal:8112` for Deluge, `http://host.docker.internal:8080` for
  qBittorrent. qBittorrent needs `DOWNLOAD_USERNAME` and `DOWNLOAD_PASSWORD`; Deluge
  authenticates on the password alone, so the username is accepted but unused.
- The client must write completed downloads somewhere this app can see: mount **the
  directory it saves completed downloads to** as `/downloads`. If it completes into
  `/downloads/complete`, that subdirectory is what to mount — not its parent.
- An `INDEX_URL` for AudioBookBay. Its mirrors rotate domains and some serve expired TLS
  certificates, so a plain `http://` URL is sometimes the working one.

## Installation

> [!NOTE]
> The published image is multi-architecture: `linux/amd64` and `linux/arm64`. Docker picks
> the right one automatically, so a Raspberry Pi 4/5, an ARM NAS or Apple Silicon pulls the
> same tag as an x86 server. 32-bit ARM (`armv7`, a Pi 3 or earlier) is not published —
> build it yourself there with `docker compose up --build` (see `DEVELOPMENT.md`).

### Docker Compose

`compose.yaml`:

```yaml
---
services:
  audiobooklibrary:
    container_name: audiobooklibrary
    image: ghcr.io/aich3atbot/audiobooklibrary:latest
    user: "1000:1000"  # Set user to run as, sets file permissions
    ports:
      - "8000:8000"
    environment:
      INDEX_URL:         http://audiobookbay.fi
      ADMIN_PASSWORD:    YOUR_ADMIN_PASSWORD
      DOWNLOAD_CLIENT:   qbittorrent  # or deluge
      DOWNLOAD_URL:      YOUR_DOWNLOAD_URL
      DOWNLOAD_USERNAME: YOUR_DOWNLOAD_USERNAME
      DOWNLOAD_PASSWORD: YOUR_DOWNLOAD_PASSWORD
    volumes:
      - ./config:/config  # Local configuration, database
      - /path/to/audiobooks:/audiobooks  # The organized audiobook library
      - /path/to/audiobook_import/IMPORT:/imports  # Optional: staging area for an existing collection
      - /path/to/downloads/:/downloads  # Optional: the directory your download client writes completed downloads
    restart: unless-stopped
    extra_hosts:
      - "host.docker.internal:host-gateway"  # Required for this container to connect to a BitTorrent client on the same host
```

Variables can be placed in a `.env` file if you prefer, but a `.env` on its own does
nothing — Docker Compose reads it to substitute `${...}` references in `compose.yaml`, and
never passes it to the container by itself. Pick one of:

```yaml
    env_file: .env                    # pass the whole file to the container
```

```yaml
    environment:
      ADMIN_PASSWORD: ${ADMIN_PASSWORD}   # or substitute them one by one
```

### Volumes

| Mount | Purpose |
|---|---|
| `/config` | Local config, database |
| `/audiobooks` | The organized audiobook library |
| `/imports` | Optional: staging area for an existing collection (see below) |
| `/downloads` | Optional: the directory your download client writes *completed* downloads |

> [!NOTE]
> Volumes must be writable by the user the service runs as, you will need to `chown 1000:1000` each volume (assuming you run as user 1000:1000) 


### Environment

The app is configured entirely by the environment variables below (paths excepted — those
are the volume mounts). Only `ADMIN_PASSWORD` is required, on a fresh install; leave
anything marked *(Optional)* out and the app applies the default shown. A variable set to
an empty value counts as unset.

#### Accounts

| Variable | Default | Description |
| --- | --- | --- |
| `ADMIN_PASSWORD` | — | Password for the `admin` account, which exists only to manage user accounts. Required on a fresh install — it creates the account, and the app will not start without it. Afterwards it is optional: left set, it is applied at every start (changing it replaces the stored password and signs the admin out everywhere); removed, the stored password stands. It is the only way to set that password, so put it back if you forget it. |

There is no global Hardcover token — each user account carries its own, set on the admin's
Users page.

#### Downloading

Downloading is enabled only when both `DOWNLOAD_CLIENT` and `DOWNLOAD_URL` are set. Leave
either one out and the download UI is hidden; the rest of the app (library, imports,
Audiobookshelf API) keeps working.

| Variable | Default | Description |
| --- | --- | --- |
| `INDEX_URL` | — | (Optional) Base URL of the AudioBookBay mirror to search, e.g. `http://audiobookbay.fi`. Mirrors rotate domains and some serve expired TLS certificates, so a plain `http://` URL is sometimes the working one. Without it, searching for releases fails. |
| `DOWNLOAD_CLIENT` | — | (Optional) Torrent client to download with: `deluge` or `qbittorrent`. |
| `DOWNLOAD_URL` | — | (Optional) The client's **web UI** URL, e.g. `http://host.docker.internal:8112` (Deluge) or `http://host.docker.internal:8080` (qBittorrent). From a container, the host's client is `host.docker.internal`, never `localhost`. |
| `DOWNLOAD_USERNAME` | — | (Optional) Web UI username. qBittorrent needs it; Deluge authenticates on the password alone and ignores it. |
| `DOWNLOAD_PASSWORD` | — | (Optional) Web UI password. An empty password is valid for Deluge if that is how yours is set up. |
| `DOWNLOAD_LABEL` | — | (Optional) Label to tag this app's torrents with in the client (Deluge Label plugin / qBittorrent category). Best-effort; no label by default. |
| `DOWNLOAD_REMOVE_IMMEDIATELY` | `false` | (Optional) `true` removes the torrent *and its data* from the client right after a successful import. Default leaves it seeding per the client's own settings. |
| `DOWNLOAD_IMPORT_MODE` | `copy` | (Optional) How a finished download becomes library files. `copy` hardlinks (falling back to a real copy) so the torrent keeps its files and goes on seeding; `move` relocates them out of the download directory. Downloads only — importing from `/imports` always moves, whatever this is set to. |
| `DOWNLOAD_WATCH_INTERVAL_SECONDS` | `30` | (Optional) How often to poll the download client for progress and completion. |
| `DOWNLOAD_QUIET_SECONDS` | `120` | (Optional) Fallback completion rule used when the client can't be reached: a download counts as finished when nothing in it has changed for this long. |

#### Hardcover sync

| Variable | Default | Description |
| --- | --- | --- |
| `SYNC_INTERVAL_MINUTES` | `30` | (Optional) How often every user's Hardcover library is pulled. A sync also runs at startup. |

Collection import has no settings of its own: `/imports` is a fixed path, and it always
moves.

#### Listening and read state

Listening in an Audiobookshelf app moves the book's read state, which is pushed to
Hardcover. Both thresholds are plain durations, and either may be `0`.

| Variable | Default | Description |
| --- | --- | --- |
| `MARK_READING_AFTER_MINUTES` | `1` | (Optional) How long you have to listen before a book counts as *currently reading*, dated today. `0` marks it as soon as playback starts. |
| `MARK_READ_TAIL_MINUTES` | `30` | (Optional) Audiobooks usually end in credits you never play, so a book left this close to the end is marked *read* as soon as you start a different book. `0` requires listening all the way through. Capped at half the book's length, so short books still have to be nearly finished. |

#### Converting to M4B

| Variable | Default | Description |
| --- | --- | --- |
| `TRANSCODE_BITRATE` | `64k` | (Optional) Target AAC bitrate when converting an MP3 edition to a single `.m4b`. Halved for a book that is mono throughout, and never raised above the source's own bitrate. |
| `FFMPEG_PATH` | `ffmpeg` | (Optional) Path to the ffmpeg binary. The image ships its own; override only if you mount a different one. |

#### Paths

Paths are not environment variables. The container always reads and writes `/config`,
`/audiobooks`, `/downloads` and `/imports` — you choose what those point at on the host
with the `volumes:` entries in [the compose file above](#volumes).

The one that needs care is `/downloads`: it must be the directory your torrent client
writes **completed** downloads to. If the client completes into `/downloads/complete`, that
subdirectory is what to mount — not its parent.


## Usage

### Users

Open http://localhost:8000, log in as `admin` (password from `ADMIN_PASSWORD`) and create
user accounts — each with its own password and Hardcover API token.

The admin account is only required for managing other users: it has no library of its own
and cannot log in from an Audiobookshelf app.

Everyone shares one `/audiobooks` store, but read state is personal — each user syncs their
own Hardcover library, and a book one user has downloaded simply shows up as *available* to
everyone else.

### Managing books in your Hardcover Library

The **Library** page is your own Hardcover shelves: filter by title, author or series,
narrow by read state or download status, and sort by title, author or recent activity. Each
card carries a read-state dropdown — *Want to read*, *Reading*, *Read* — and changing it
pushes straight back to Hardcover. A *sync pending* badge means the change is stored here
but not yet confirmed by Hardcover.

**Sync now** pulls your library on demand; it also runs at startup and every
`SYNC_INTERVAL_MINUTES` after that. Local changes are pushed before the pull, and
**Hardcover is the source of truth** — if the same book changed on both sides, Hardcover
wins.

To shelve something new, use **Search**, which searches Hardcover itself and adds the book
with the state you pick. A book already in the shared store shows its download status
there, and **Add to my library** shelves it for you without downloading it again. Series
names link to a series page listing every book in it.

Listening in an Audiobookshelf app moves read state too: a book becomes *currently reading*
once you are past `MARK_READING_AFTER_MINUTES`, and *read* when you finish it — see
[Listening and read state](#listening-and-read-state).

### Editions

A book can hold more than one recording — a different narrator, an abridgement, a better
rip. Each is an **edition**: its own folder in the library, its own files and its own label.

Open a book (from a card's cover or title) to see them. Each edition expands to its file
list, and offers **Rename** to change its label — the library folder moves to match
immediately — or **Replace** to download something else over it. **Download another
edition** adds one alongside.

Labels shape the folder layout. Without one a book lands at `Author/Series/{index} - Title/`,
or `Author/Title/` when it has no series; a labelled edition suffixes the series folder
(`Author/Series {Label}/{index} - Title/`), or the book folder when there is no series.
Only an unlabelled edition uses the plain path, so the first time you add a second edition
you are asked to name the existing files as well — from then on every edition of that book
is labelled.

Read state stays with the *book*, not the edition: listening to any one of them moves the
book. Audiobookshelf clients see each edition as its own library item, with its label in
the title, and keep separate progress for each.


### Listening with an Audiobookshelf app

The app serves an [Audiobookshelf](https://audiobookshelf.org/)-compatible API, so an
Audiobookshelf client can play your library. These have been tested:

- Absorb ([android](https://play.google.com/store/apps/details?id=com.barnabas.absorb), [iOS](https://apps.apple.com/us/app/absorb-for-audiobookshelf/id6760673498)) - my favourite audiobookshelf app
- Lissen ([android](https://play.google.com/store/apps/details?id=org.grakovne.lissen))
- Audiobookshelf official ([android](https://play.google.com/store/apps/details?id=com.audiobookshelf.app), [iOS TestFlight](https://testflight.apple.com/join/wiic7QIW))

This is a reimplementation of the parts of the Audiobookshelf API that matter for listening
— browsing, playback, progress, bookmarks — and **not all of it is implemented**, so another
app may work partly or not at all. Some things are left out deliberately: listening history
and statistics, server administration, and anything to do with podcasts or ebooks. An app
that leans on those will find them missing.

Add the server in the app with:

- **Server address** — the same URL as the web UI, e.g. `http://192.168.1.10:8000`. Use the
  machine's LAN address or hostname, not `localhost`: that means the phone itself.
- **Username and password** — an ordinary user account, the same credentials as the web UI.
  There is no separate API key to generate.

The `admin` account cannot be used here — it has no library of its own, and an app signing
in with it is refused exactly as a wrong password would be. Create a user account (full or
limited) and sign in as that.

Everything is one library. Each **edition** is its own item, with its label in the title, so
a book you hold two recordings of appears twice. Progress and bookmarks are per account and
per edition, and follow you between devices; finishing a book marks it read on Hardcover for
full accounts. Signing out in the app revokes that device's session, and other devices are
listed in the app's own device/session screen.

> [!IMPORTANT]
> This API is reachable only from your local network unless you deliberately expose it to
> the internet. The app speaks plain HTTP and has no TLS of its own, so if you do expose it,
> put it behind a reverse proxy that terminates TLS — otherwise passwords and tokens cross
> the internet in the clear. Exposing it is at your own risk.

### Importing existing audiobooks

Mount your current audiobook collection at `/imports` and open the **Imports** page. The
app scans it recursively (any folder directly containing audio files is one book;
`CD1`/`Disc 2` folders are grouped; loose `.m4b` files count individually), identifies
each entry by searching Hardcover (author/series/title from the folder names) with a
confidence badge, and lets you amend any match via library or Hardcover search.
Import rows one at a time, bulk-select, or import everything matched. Confirmed books are
**moved** out of `/imports` into the organized `/audiobooks` layout, and emptied folders
are cleaned up behind them. Imported books belong to no one until users' own Hardcover
libraries claim them: anyone who has the book on Hardcover sees it appear automatically,
and everyone else finds it as *available* in search.

> [!WARNING]
> Importing **moves** files: a book that imports successfully is gone from `/imports`, and
> the copy in `/audiobooks` is then your only one. This is deliberate — draining `/imports`
> is the point of the page — and it is not affected by `DOWNLOAD_IMPORT_MODE`, which applies to
> downloads alone.
>
> So if you are trying the app out and want to keep your existing library as it is, **copy**
> your audiobooks into `/imports` rather than moving them there.

Every matched row has an **Edition…** fold for choosing which edition the files become,
offering, in order: a label of your own, labels the book's series already uses elsewhere,
this book's existing editions (picking one **replaces** it — its current files are deleted,
and you confirm that when you pick it), and finally Hardcover's audiobook editions. Nothing
is preselected; where a Hardcover edition's duration or narrator matches what you are
importing it is badged as a hint, no more. Each row chooses independently, so a bulk import
can label every book differently in one pass.

Leaving the label empty imports to the plain, unlabelled folder. That is refused for a book
whose files are already in the library — the page tells you to label the incoming files
instead, or to rename the existing ones first, so the two editions can sit side by side.

Files the app cannot identify are **left in `/imports`, never deleted** — moving them out
could destroy your only copy — so a folder still sitting there afterwards is the sign that
an entry did not fully drain, and is worth a look.


### Downloading audiobooks

1. Press **Download…** on a book → the app searches AudioBookBay for
   "{author} {title}" (falling back to the title alone) and shows a release picker with
   size, format and posting date. AudioBookBay publishes no seeder counts, so results keep
   the site's own ordering rather than a made-up ranking.
2. **Grab** reads the release's details page for the torrent's info hash, builds a magnet,
   and adds it to your torrent client. The app records the hash.
3. The watcher asks the client about that hash every `DOWNLOAD_WATCH_INTERVAL_SECONDS` —
   its progress is what the Activity page shows — and imports it into `/audiobooks` as soon as
   the client reports the torrent finished. If the client can't be reached, it falls back to
   watching `/downloads` for a folder matching the release name that has stopped changing.
4. Anything that can't be matched or imported shows up on the **Activity** page with retry,
   manual-import (point it at a folder name in `/downloads`), and cancel actions — the app
   never guesses.

**Cancel & delete** on the Activity page removes the torrent *and its downloaded files* from
the client — which also ends any seeding of it — and then stops tracking the release. The UI
asks you to confirm first. If the client can't be reached, the release is still cancelled
here and the app tells you the torrent may still be running so you can remove it yourself.

### Converting audiobooks to m4b

An edition that arrived as a pile of MP3s can be joined into a single chaptered `.m4b`.
Open the book, expand an edition's file list, and press **Convert to M4B…** — the button
only appears when every audio file in the folder really is an MP3 (checked by reading the
files, not by trusting their extensions).

The chapters are the ones your apps already show: embedded ID3 `CHAP` frames or OverDrive
markers if the files carry them, otherwise a `.cue`, `chapters.txt`, ffmetadata or
Audiobookshelf `metadata.json` sitting beside them, and failing all of that one chapter per
MP3 named after the file. Chapter positions come from decoding the files rather than from
their tags, which run systematically long and would otherwise drift by minutes across a
long book. Title, author, narrator, series and cover art are written into the result, and
`TRANSCODE_BITRATE` (default 64k) sets the quality — halved for a mono book, and never
raised above what the source actually holds.

The conversion runs in the background, one book at a time, with progress on the book page
and on **Activity** (where you can also stop it). **The MP3s are deleted** — but only after
the new file has been written, checked for the right amount of audio, tagged and moved into
place, so anything that goes wrong leaves the folder exactly as it was. A `.cue`,
`chapters.txt` or ffmetadata file whose chapters ended up inside the m4b is removed with
them, since it described only the files that are gone. An Audiobookshelf `metadata.json` is
always kept, even when its chapters were the ones used: it also carries the description,
subtitle, series, narrator and tags, none of which this app can write back. Cover art and
everything else stays.

With the default `DOWNLOAD_IMPORT_MODE=copy` your library files are hardlinks to the
still-seeding torrent, so converting costs nothing — the torrent keeps its own copy. If you
imported with `move`, or from `/imports`, the MP3s are your only copy and the conversion is
one-way.

### Limited User Accounts

Limited user accounts can be created to share access to your audiobook library (via the
Audiobookshelf API). A limited user logs in from an Audiobookshelf app, plays everything in
the library and keeps their own progress and bookmarks — but cannot log in to the web UI,
cannot download or import books, and does not need a Hardcover API token. Nothing they
listen to touches anyone's read state.

Promote or demote an account at any time from the admin's Users page. Demoting locks the
web UI immediately and clears that user's Hardcover token, but leaves their app sessions
signed in and their read state intact, so promoting them back restores their library.
