# Changelog

---

- **Docs rearchitecture**: Full docs restructure to reflect two-tool structure. `README.md` rewritten as a dual-tool landing page (archiver feature list + viewer quick-start). `docs/prerequisites.md` trimmed to shared Python/package setup, now split into wab-archiver and wab-viewer package sections. Archiver docs split into `docs/archiver/index.md`, `docs/archiver/setup-android.md`, `docs/archiver/setup-ios.md`, `docs/archiver/command-reference.md`. Viewer docs moved to `docs/viewer/index.md`. Old `docs/running-the-script.md` and `docs/chat-viewer.md` removed. All `python wa_media_archiver.py` / `python wa_chat_viewer.py` examples updated to `python -m wab_archiver` / `python -m wab_viewer`. Test path references updated (`chat_viewer/app.js` → `wab_viewer/chat_viewer/app.js`). README now includes viewer features and viewer quick-start section.

- **Rebranding: wab-tools** (repo, `wab_archiver/`, `wab_viewer/`): Project renamed from "WhatsApp Media Archiver" to "WhatsApp Backup Tools" (`wab-tools`). CLI tools renamed to `wab-archiver` and `wab-viewer`. Package restructured: source code moved to `wab_archiver/` and `wab_viewer/` subpackages; shared utilities (`db`, `hashing`, `source_detection`) extracted to `shared/`. `pyproject.toml` added with entry points for `pip install -e .`. Old root-level scripts (`wa_media_archiver.py`, `wa_chat_viewer.py`, `archive_db.py`, etc.) removed. Tests updated to import from new package paths. All 304 tests pass.

- **Scroll-while-loading race condition** (`chat_viewer/app.js`): Opening a chat and scrolling up before the initial load completed permanently broke pagination. Three-part fix: (1) removed `if (loading) return` from the scroll handler so scroll events always record intent; (2) moved `const gen = _loadGen` before the `if (loading) return` guard in `loadMessages` so the generation counter is captured at the right moment (fixes the latent ordering bug where `gen === _loadGen` never worked for concurrent loads); (3) added `pendingLoad` flag — when a scroll event arrives while `loading` is true, the direction is stored in `pendingLoad` and automatically retried right after `loading` is cleared in `finally`, so no manual scroll retry is needed.

- **`renderBubble` duplicate `bubble.appendChild(meta)` bug** (`chat_viewer/app.js`): Reactions implementation introduced a second `bubble.appendChild(meta)` call at the top of the function, leaving the original one at the end. Every message rendered with its meta/timestamp row appearing twice. Fixed by removing the duplicate at the end of `renderBubble`, keeping only the one immediately after the timestamp is appended. Reactions block now appends directly to `bubble` after `meta`.

- **Message reaction emoji in chat bubbles** (`wa_chat_viewer.py`, `chat_viewer/app.js`, `chat_viewer/app.css`): Android reactions are now shown on message bubbles. The reaction emoji (👍❤️😂 etc.) appear as pill badges below the bubble — right-aligned for sent messages, left-aligned for received. Multiple reactions from different people are deduplicated: two 👍 show as `👍 2`, different emoji show as separate badges. The data comes from `message_add_on → message_add_on_reaction` in the exported WA DB, aggregated per message via `GROUP_CONCAT`. Old DB exports without those tables get `NULL` gracefully (no error). `reactions` column added to `recent_messages` schema with an `ALTER TABLE` migration for pre-existing archive DBs. iOS reactions (stored in protobuf blobs) are not yet implemented — the iOS query path returns `NULL` for `reactions` as a placeholder. Tests added in `TestAndroidReactions`.

  *SQL prepare-time issue:* `CASE WHEN (SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_add_on') = 1` inside a scalar subquery in the SELECT clause fails at SQL parse time if the table doesn't exist (not row-evaluation time). Fixed by checking table existence in Python inside `create_app()` and assigning `viewer._ANDROID_SELECT` to one of two pre-built string variants.

- **Chat loading performance: WA DB index + recent_messages cache** (`wa_chat_viewer.py`, `archive_db.py`): Two structural improvements to chat open latency.

  *WA DB composite index:* `create_app()` now runs `CREATE INDEX IF NOT EXISTS idx_message_chat_ts ON message(chat_row_id, timestamp)` against the exported `msgstore.db` on first start. Idempotent — skipped on subsequent starts. This changes the chat-open query from a full table scan to an index seek for group chats, and bounds the scan to `LIMIT 50` for contact chats.

  *`recent_messages` cache:* The archive DB (`.wa_media_archiver.db`) now includes a `recent_messages` table storing the last 100 denormalized messages per chat. `api_messages()` checks this table first on initial load (sub-ms, no WA DB hit). On a cache miss it falls back to the WA DB and silently backfills `recent_messages` via `INSERT OR REPLACE` so subsequent opens are instant. Pagination (`?before=`/`?after=`) always hits the WA DB (now fast with the index) to stay current. Schema: `(chat_id, chat_type, msg_id, timestamp_ms, sender, from_me, archive_path, media_type, media_name, text_body, quoted_text, quoted_sender, quoted_ts)` with PK on `(chat_id, chat_type, msg_id)` and a covering index on `(chat_id, chat_type, timestamp_ms DESC)`.

  *Schema migration:* `create_app()` runs `CREATE TABLE IF NOT EXISTS recent_messages ...` against `_archive_conn` on startup, so pre-existing archive DBs (created before this feature) get the table automatically.

  *Async backfill:* The `recent_messages` write on cache miss runs in a daemon thread so the HTTP response returns immediately with the first messages; the cache population happens in the background.

  *Stale spinner fix:* `api_chat_index_status` checks `indexed_chats` under `_indexing_lock`. When it finds a stale `"done"` state from a crashed background thread (no `indexed_chats` entry), it clears the stale key from `_indexing_state` and returns `"idle"` — which allows `api_messages` to start a new background thread. Previously the stale key was left in place, so retries were never triggered.

  *Indexing never triggered on cache hit:* the FTS indexing trigger (`indexed_chats` check) was inside the cache-miss branch of `api_messages`. Once `recent_messages` was populated by the first visit, every re-visit returned from cache without ever reaching the trigger — un-indexed chats were never indexed after the first open. Fixed by extracting the trigger into `_maybe_start_indexing()` and calling it on both the cache-hit and cache-miss paths.

- **WhatsApp text formatting rendered in chat bubbles** (`chat_viewer/app.js`, `chat_viewer/app.css`): Added `waFormat()` function that converts WhatsApp markdown syntax to HTML before display. Supports: `*bold*` → `<strong>`, `_italic_` → `<em>`, `~strikethrough~` → `<s>`, `` `code` `` → `<code>`, ` ```block``` ` → `<pre><code>`, `> quote` → `<blockquote>`, `# / ## / ###` headings, `- / * / 1.` lists. Code spans and blocks are protected from inner formatting. URLs are still linkified. `highlight()` now calls `waFormat()` instead of `linkify()`, so search highlighting works on formatted text. CSS added for all new element types scoped to `.msg-text` and `.msg-caption`; `.msg-caption` also gains `white-space: pre-wrap; word-break: break-word` (was missing).

- **WhatsApp system contact COALESCE ordering bug** (`wa_chat_viewer.py`): Old archives have `folder = 'Unknown (000)'` for number `'0'` in `arch.contacts`. All chat list and `api_chat_info` COALESCE chains had `con.folder` before the `'0'` → `'WhatsApp'` guard, so the stale folder name always won. Fixed by moving the `CASE WHEN ... = '0'` arm to position 2 (before `con.folder`) in both Android and iOS chat list queries and iOS `api_chat_info`. Android `api_chat_info` Python guard reordered so `chat_id == '0'` is checked first.

- **WhatsApp system contact named "Unknown (000)"** (`android_handler.py`, `ios_handler.py`, `wa_chat_viewer.py`): JID `0@s.whatsapp.net` (WhatsApp's own notification sender) had no special-case handling, so it fell through to the `Unknown (000)` folder/display name. Fixed by injecting `'0': 'WhatsApp'` into the contacts dict after parsing (both Android and iOS), and adding a leading `CASE WHEN ... = '0' THEN 'WhatsApp'` arm to the iOS chat viewer sender and display_name COALESCE chains.

 Messages with a media type but no archived file fell into the `msg-unavailable` branch which used `textContent`, so any URL in `text_body` was rendered as plain text with no `<a>` tag. Fixed by switching to `innerHTML = esc(icon) + ' ' + linkify(text_body)` so URLs are linkified.

 The date picker, Go, and Clear buttons were rendering on a new line starting at the left when the search nav row was visible. Wrapped them in `#date-group` (flex row) with `margin-left: auto` so they always push to the right edge of `#toolbar-expanded`.

- **Lightbox overlay blocking all clicks after close** (`chat_viewer/app.js`): `_closeLb()` relied solely on the `lb-fade-out` `animationend` event to remove `#img-lightbox`. If the animation didn't fire (e.g. `prefers-reduced-motion`, animation interrupted), the `position:fixed; inset:0; z-index:1000` div stayed in the DOM invisibly blocking all link and image clicks. Fixed by adding a 300ms `setTimeout` fallback: `setTimeout(() => lb.isConnected && lb.remove(), 300)`.



### Fixed

- **iOS receipts: new blob format (2026+) not decoded** (`wa_chat_viewer.py`): WhatsApp introduced a new `ZRECEIPTINFO` format where timestamps are stored as `(delta_seconds, event_type)` offsets from the message send time (field9 sub-entries inside field2 device entries), with no top-level `base_ts` (field3). The parser now detects this format by the absence of field3 and decodes it using `msg_ts_s` fetched from `ZWAMESSAGE.ZMESSAGEDATE`. event_type >= 1 (not 3) = delivered (take min delta across devices); event_type == 3 = read. Tested against real 2026 messages.

- **iOS read receipts broken for all 1-to-1 chats** (`wa_chat_viewer.py`): the per-entry `status` field in `ZRECEIPTINFO` is always `0` for 1-to-1 messages (regardless of actual read state). The parser was gating timestamps on `status >= 1`, so `delivered_ts` and `read_ts` were always `None`. Fix: `_parse_ios_receipt_blob` now takes an `is_group` parameter. For 1-to-1 (`is_group=False`), `base_ts` is used directly as the delivered timestamp and `base_ts + delta` as the read timestamp (when `delta > 0`). Group logic is unchanged. The call site now fetches `ZGROUPINFO IS NOT NULL` alongside the blob.

- **iOS group sender garbled for `@lid` contacts** (`wa_chat_viewer.py`, `backup_reader.py`): WhatsApp stores a binary protobuf blob in `ZWAMESSAGE.ZPUSHNAME` for `@lid` group senders instead of a readable name. The sender `COALESCE` was picking this up as the display name and rendering garbage (e.g. `CPm0jtMGIABIAZABAPABAtgC/6TXnuXrlQM=`). Also fixed: real address book names were not being used — `arch.contacts` was built from push names so saved names like "Pasquale D'Agnese" showed as "Pasquale Rocket". Fix: `backup_reader.py` now saves `ContactsV2.sqlite` to the output directory (same location as `ChatStorage.sqlite`) instead of a temp file, so it persists across runs. `wa_chat_viewer.py` attaches `ContactsV2.sqlite` at connection time and loads it into a temp table `_ios_contacts` (JID → full name, covering both `@lid` and `@s.whatsapp.net` JIDs). `_IOS_SELECT` joins this table as the top name source. For `@lid` senders, `ZPUSHNAME` is also skipped entirely since it is always a protobuf blob. Lookup priority: `ContactsV2` → `arch.contacts` → `ZWACHATSESSION.ZPARTNERNAME` → `ZWAPROFILEPUSHNAME` → `ZPUSHNAME` (non-`@lid` only) → numeric fallback. Gracefully degrades when `ContactsV2.sqlite` is absent.

- **iOS new-format receipt timestamps wrong (off by 60×)** (`wa_chat_viewer.py`): field9 delta values in the 2026+ `ZRECEIPTINFO` format are in **minutes**, not seconds. The parser was treating them as seconds, so a 62-minute read offset showed as ~1 minute. Verified against real DB: message sent 13:55 CEST, read delta=62 → 14:57 CEST (correct), previously showed 13:56. Fix: multiply `ds` by 60 before storing in `_parse_new_entry`. Tests updated accordingly.

- **iOS group new-format receipts: spurious 09:25 delivered time for all members** (`wa_chat_viewer.py`): group blobs write a placeholder `ev=1 delta=0` entry for every member at send time — not an actual delivery confirmation. The parser was emitting `delivered_ts = msg_ts_s * 1000` for these, so every member showed as delivered at the exact send time. Fix: suppress `delivered_ts` when `dd == 0` in the group new-format path. Members with only a placeholder entry now show `—` for both delivered and read, which is accurate. Delivery timestamps are only shown when `delta > 0`.

- **iOS group read receipts: member names all `?`, info not populated** (`wa_chat_viewer.py`): the 2026+ new-format `ZRECEIPTINFO` group blobs store per-member identity in `field 1` (phone bytes, same BCD-hex encoding as the old format) — not in `field 2` (a JID string as originally assumed). `_parse_new_entry` was silently ignoring `field 1` and returning an empty `jid`, causing every receipt row to render as `?` with no name. Fix: `_parse_new_entry` now decodes `field 1` using hex-encode of bytes after the `0x8C` prefix, stripping a trailing `f` padding nibble for odd-length numbers (e.g. `103624826949719f` → `103624826949719`). Name resolution in the group path now first tries `_ios_contacts` (ContactsV2 address book, keyed by `<num>@lid`), then falls back to `arch.contacts`. The API handler also now filters out self-receipt entries: blobs contain an entry for the sender's own device(s) whose `@lid` number never appears as a `ZWAGROUPMEMBER` for the chat; these are excluded by cross-referencing the known member JIDs before returning the result.

- **Photos not opening in fullscreen from chat view** (`chat_viewer/app.js`): two broken variable references introduced during a prior refactor. `openLightboxAt` used `prevBtn` without declaring it (`const prevBtn = document.createElement('button')` was missing). `showLightbox` used `lb` without creating the div element first. Both threw a `ReferenceError` at the point of first use, silently aborting the click handler. Fixed by adding the missing declarations in both functions.

### Changed (`chat_viewer/app.css`, `app.js`, `template.py`):
  - Overlays fade in/out: `#settings-modal`, `#media-gallery`, `#chat-info-panel`, `#msg-details-popup` all use `opacity + visibility` transitions instead of `display` toggling. Modals and panels also scale/translate on enter.
  - Lightbox fades in on open; close button, backdrop click, and Escape key trigger a fade-out animation before DOM removal via a `_closeLb()` helper.
  - Scroll-to-bottom button fades in/out via `.visible` class instead of `display` toggling.
  - `#toolbar-expanded` slides open with `max-height` transition instead of `display` toggling.
  - Archive year rows slide open with `max-height` transition (chevron rotation was already animated).
  - Global hover transitions added to all interactive elements that were missing them: buttons, filter pills, pref buttons, nav buttons, search result items, archive year headers, settings/close buttons.

### Refactor

- **Split `chat_viewer/template.py`** into three separate files to make the frontend maintainable:
  - `chat_viewer/app.css` (~713 lines) — all custom CSS extracted verbatim from the template
  - `chat_viewer/app.js` (~1,803 lines) — all JavaScript extracted verbatim; `outputRoot` now reads `window.OUTPUT_ROOT` instead of the Jinja expression
  - `chat_viewer/template.py` shrunk from 2,666 to ~150 lines (HTML only)
- **`wa_chat_viewer.py`**: added `send_from_directory` import, `_CHAT_VIEWER_DIR` constant, and two new routes `/static/app.css` + `/static/app.js`
- **`tests/test_wa_chat_viewer.py`**: updated `TestHtmlTemplate` to read JS from `chat_viewer/app.js` directly instead of from `HTML_TEMPLATE`

### Added

- **`wa_chat_viewer.py` — Media gallery pagination**: `/api/media` now accepts a `before` cursor and returns at most `GALLERY_PAGE_SIZE` (100) items per page. New `/api/media/count` endpoint returns total, archived, and per-type counts for the stats bar without fetching full rows.
- **`chat_viewer/app.js` — Incremental gallery loading**: gallery grid loads the first 100 items on open and fetches more as the user scrolls, via an `IntersectionObserver` on a sentinel `div`. The stats bar is populated immediately from `/api/media/count` in parallel with the first page fetch.
- **`chat_viewer/app.js` — Incremental archive view append**: `_appendToArchiveView()` inserts new items into existing year/month nodes as pages load, preserving collapsed/expanded state. `_createArchiveYearBlock()` extracted as shared helper.
- **`chat_viewer/app.js` — Lightbox load-more**: the `›` button and `ArrowRight` key trigger a page fetch when the lightbox reaches the last loaded item and more items remain.
- **`wa_chat_viewer.py` — `/api/chat-info`**: returns display name, phone number (contacts), conversation first/last timestamps, sent/received/total message counts. For groups: members list (`group_participants` → unique-senders fallback) and top-5 senders.
- **`wa_chat_viewer.py` — `/api/chat-info/media-size`**: async filesystem scan; sums `os.path.getsize()` over all `archive_copies` rows for the chat's folder prefix. Returns `{"bytes": N}`.
- **`chat_viewer/app.js` — Chat info panel**: ℹ button in chat header opens a compact modal with all stats. Media size loads concurrently with main stats (spinner until resolved). "Open Gallery" link switches to gallery view. Closes via ✕, ESC, or backdrop click.
- **`tests/test_wa_chat_viewer.py`**: 8 new tests covering contact fields, sent/received counts, group members, group_participants fallback, cache-sourced timestamps, media-only mode, and media-size endpoint.
- **`wa_chat_viewer.py` — link detection**: `_ANDROID_IS_LINK` / `_IOS_IS_LINK` predicates detect `message_type=0` rows whose body contains `http://` or `https://`. `_ANDROID_SELECT` / `_IOS_SELECT` now return `media_type='link'` for these rows.
- **`wa_chat_viewer.py` — `/api/media/links`**: returns all link messages for a chat (unpaginated). Separate from `/api/media` to avoid contaminating the paginated media stream.
- **`chat_viewer/app.js` — Links gallery view**: gallery fetches `/api/media/links` in parallel with the count on open. Link cards span full grid width (`grid-column: 1/-1`), show the URL and sender·date. Links are hidden by default; clicking the Links pill reveals them.
- **`tests/test_wa_chat_viewer.py`**: `test_media_count_includes_links`, `test_media_links_endpoint` (regression guard: `/api/media` must never return `media_type='link'` rows).
- **`wa_chat_viewer.py` — document detection**: `_ANDROID_IS_DOCUMENT_UNDOWNLOADED` / `_IOS_IS_DOCUMENT_UNDOWNLOADED` predicates detect non-zero `message_type` rows with no `file_path` but a non-empty `media_name` — undownloaded documents invisible to the archived-media query.
- **`wa_chat_viewer.py` — `/api/media/documents`**: returns all undownloaded document messages for a chat. Mirrors `/api/media/links` design.
- **`wa_chat_viewer.py` — `/api/media/count`**: updated to fetch media, document, and link rows separately; documents and links are included in `total` and `by_type` but never count as "missing".
- **`chat_viewer/app.js` — Documents gallery view**: gallery fetches `/api/media/documents` in parallel with counts and links. Document cards appear before link cards; correct month dividers derived from each item's own timestamp via a `secondaryLastMonth` tracker.

### Fixed

- **`wa_chat_viewer.py` — Range media serving**: range requests now seek to the byte offset instead of loading the full file into memory. Large video scrubbing no longer allocates the whole file per request.
- **`wa_chat_viewer.py` — Background indexing connection leak**: `_bg_index_chat` now wraps the SQLite connection in a `try/finally` so it is always closed even if indexing fails.
- **`wa_chat_viewer.py` — Protobuf parser robustness**: `_parse_ios_receipt_blob` now skips fixed-width fields (wire types 1 and 5) instead of aborting entry parsing, matching the protobuf spec.
- **`wa_chat_viewer.py` — Path traversal guard**: `serve_media` now uses `Path.relative_to` instead of string `startswith` for semantically correct path containment check.
- **`wa_chat_viewer.py` — Preferences value validation**: `POST /api/preferences` now validates the value against an explicit allowlist (`VALID_PREF_VALUES`) in addition to the key.
- **`wa_chat_viewer.py` — Dead code removed**: `_build_fts_index`, `_fts_android`, `_fts_ios` deleted (superseded by lazy per-chat indexing). Dead `wa_conn = None` and `get_output_root()` wrapper also removed.
- **`wa_chat_viewer.py` — iOS sender SQL deduplicated**: `_IOS_SENDER_JID` constant extracted to replace triple repetition of the JID extraction expression in `_IOS_SELECT`.
- **`wa_chat_viewer.py` — Background error logging**: `_bg_index_chat` now logs a full traceback on failure instead of just `str(e)`.
- **`tests/test_wa_chat_viewer.py`**: added range-request tests, concurrent double-indexing race test, and full `TestIosReceiptBlobParser` suite (5 cases including wire-type regression).
- **Creator "Created by" showing WhatsApp LID instead of phone number**: Android `message_type=7` sender JID resolved through `jid_map` to get real phone number.
- **`fmtTs` ReferenceError in `renderGalleryItem`**: `fmtTs` was scoped inside `openChatInfo()`; link card meta now inlines `toLocaleDateString` directly.
- **Group archive toolbar alignment**: `#media-archive-actions` uses `margin-left: auto` so the collapse/expand buttons always anchor to the right, even in group chats where the Received/Sent tabs are hidden.
- **Links/docs month dividers**: secondary items (documents, links) now emit month headers derived from their own timestamps rather than the last loaded media item's header.
- **Archive view premature "No media"**: `buildArchiveView` checks the `galleryAllLoaded` flag to distinguish in-flight loading from a genuinely empty chat.

---

## [0.37] — 2026-07-26

### Added

- **`wa_chat_viewer.py` — In-chat search**: search box in the chat header with match count and ↑/↓ navigation; matched bubble gets an accent outline; jumping to a result not currently in the DOM loads the surrounding window automatically.
- **`wa_chat_viewer.py` — Date picker**: date input in the chat header; selecting a date jumps the viewport to messages around that date. The Go button shows a loading spinner while navigating.
- **`wa_chat_viewer.py` — Group chat sender names**: inbound group messages now show the sender's display name instead of the raw phone number. Quoted-message sender labels resolved the same way; fallback is `+<number>` for unknown contacts.
- **`wa_chat_viewer.py` — iOS group display names**: group chats now show the group subject instead of the internal JID string.
- **`wa_chat_viewer.py` — Media gallery**: new `/api/media` route powers a fullscreen media grid per chat, with a stats bar (total count + per-type breakdown, missing noted). Clicking an item opens a lightbox or new tab; "→ in chat" button jumps to the message. Media is grouped by month with full-width section headers; current-year headers show only the month name.
- **`wa_chat_viewer.py` — Video lightbox**: clicking a video opens it in a fullscreen player instead of an image viewer.
- **`wa_chat_viewer.py` — Day-separator pills**: a date pill is injected between messages on different calendar days in all render paths.
- **`wa_chat_viewer.py` — Toolbar toggle**: search and date picker hidden behind a 🔍 icon to keep the header compact.
- **`wa_chat_viewer.py` — Loading spinners**: spinner at the relevant end of the chat while older/newer messages load.
- **`wa_chat_viewer.py` — Media unavailable placeholder**: media with no archived file shows a greyed italic placeholder with an icon matching the media type.
- **`wa_chat_viewer.py` — Sidebar two-section search**: sidebar search shows contact name matches first, then FTS message results below a separator.
- **`wa_chat_viewer.py` — Sidebar preview**: the chat list subtitle now shows the last message preview (e.g. "You: See you tomorrow" or "📷 Photo") instead of a message count. Media items show an emoji label; text is truncated to 60 characters.
- **`wa_chat_viewer.py` — Sidebar indexing indicator**: a small spinner appears next to the chat name while FTS indexing is in progress; search shows "Search not ready yet…" until indexing completes.
- **`wa_chat_viewer.py` — Message details popup (delivery & read receipts)**:
  - Hovering a sent chat bubble reveals a small ⓘ icon at the top-right of the bubble.
  - Clicking the icon opens a compact popup with delivery and read timestamps.
  - **1-to-1 chats**: popup shows "Delivered" and "Read" rows with timestamps (or "—" if unavailable).
  - **Group chats**: popup shows a table of members with per-member delivered/read timestamps.
  - If no receipt data exists the popup shows "No receipt data available." Dismiss by clicking outside or pressing Esc.
  - **Android backend** (`receipt_user` table): gracefully skips if the table does not exist (old DBs).
  - **iOS backend** (`ZWAMESSAGEINFO.ZRECEIPTINFO` protobuf blob): a minimal inline protobuf parser (~80 lines, zero external deps) decodes per-member phone, status, and timestamp delta.
- **`chat_viewer/template.py` — Chat viewer UX improvements**:
  - **No redundant load attempts at chat boundaries**: module-level flags (`noMoreOlder` / `noMoreNewer`) skip the spinner and network request when the end of history is reached; flags reset on chat switch and on `jumpToTimestamp`.
  - **Sidebar auto-scrolls to active chat**: covers both direct clicks and items opened from global search results.
  - **Global search jumps to matched message with glow**: clicking a text result centers the viewport on the matching message, which glows three times via a CSS animation.
  - **Clickable URLs in messages**: a new `linkify()` function wraps `https?://…` URLs in `<a target="_blank" rel="noopener noreferrer">` anchors, with trailing punctuation stripped.
- **`wa_chat_viewer.py` — Archive view year tree**: the contacts/groups view mirrors the on-disk folder structure as an expandable year tree split into Received / Sent tabs. Each year node is collapsible; Expand all / Collapse all buttons control all nodes at once. The toggle is hidden for group chats.
- **`wa_media_archiver.py` — Multiple media source roots (Android)**: `-wa` / `--wa_root` now accepts multiple values by repeating the flag. The resolver uses a collect-then-select strategy: zero-byte placeholders are filtered out; the largest file wins when content differs; identical MD5s are silently deduplicated; same-size different-content ties fall back to the first root. A new `source_conflicts_report.csv` is written when any file resolved differently across roots. TOML config accepts both `wa_root = "/path"` and `wa_root = ["/a", "/b"]`.
- **`wa_media_archiver.py` — Enforce `.toml` config extension**: passing a non-`.toml` file via `--config` now exits with a clear error.

### Fixed

- **`wa_chat_viewer.py` — Sender label in 1-on-1 chats**: sender name badge was shown for all received messages; now restricted to group chats only.
- **`wa_chat_viewer.py` — Pagination duplicates**: fixed three root causes of cursor drift and duplicate messages on scroll.
- **`wa_chat_viewer.py` — Quoted sender fallback**: quoted sender falls back to the chat display name in 1-to-1 chats when the field is empty.
- **`wa_chat_viewer.py` — Chat list 500 error**: column alias collision in `GROUP BY`/`ORDER BY` caused a server error on some databases.

### Changed

- **Refactor: `chat_viewer/` package**: extracted `HTML_TEMPLATE` into `chat_viewer/template.py`; `wa_chat_viewer.py` shrinks from 2367 to 967 lines.
- **`chat_viewer/template.py` — Pico CSS v2 migration**: replaced the hand-written style block with Pico CSS v2.1.1 as the design foundation. Pico is vendored (`chat_viewer/pico.min.css`) — no CDN dependency.
- **`wa_chat_viewer.py` — Background FTS indexing**: opening a chat no longer blocks on FTS index building. The first page of messages is returned immediately; indexing continues in a background thread.
- **`wa_chat_viewer.py` — Lazy per-chat FTS indexing**: messages are now indexed per chat on first open instead of all at startup. Startup is instant; the cache file starts near-zero in size.
- **`wa_chat_viewer.py` — Contentless FTS cache**: cache stores only the FTS inverted index — no text or sender copy. Cache file is a fraction of the source DB size.
- **`wa_chat_viewer.py` — Live queries**: all message and chat data is queried live from the source DB with the archive DB attached; no full message cache.
- **`wa_chat_viewer.py` — Unsaved contact display**: unsaved contacts show their phone number instead of an internal ID.

---

## [0.36] — 2026-07-21

### Added

- **`wa_chat_viewer.py` — Chat Viewer companion script**: a Flask web server that lets you browse and search archived WhatsApp chats in a browser.
  - Lists all contacts and groups in a sidebar with message counts and last activity
  - Paginated chat timeline (oldest → newest), lazy-loaded via cursor-based API
  - Full-text search across all messages (scoped to current chat or global)
  - Streaming media playback with HTTP Range support — video/audio seeking works in all browsers
  - Image lightbox on click; document download links
  - DOM windowing keeps ~100 message elements in the DOM regardless of chat length
  - Builds a cache DB (`.wa_chat_viewer_cache.db`) from the source WA DB (`msgstore.db` / `ChatStorage.sqlite`) + archive DB; rebuilds on `--rescan` or when archive rows change
  - Zero build step: `python wa_chat_viewer.py <output_root>` and a browser opens
  - `docs/chat-viewer.md` for user documentation; `tests/test_wa_chat_viewer.py` for unit and route tests (`wa_chat_viewer.py`, `docs/chat-viewer.md`, `tests/test_wa_chat_viewer.py`)

---

## [0.35] — 2026-06-10

### Added

- **Config file support (`--config`, `--generate-config`)**: users can now save persistent settings in a `config.toml` file instead of retyping long commands every run.
  - `--generate-config` writes an annotated `example-config.toml` next to the script and exits. Rename it to `config.toml` to activate it.
  - `--config PATH` loads a specific file; if omitted, the script auto-detects `config.toml` in the script folder or the current working directory. One file found → confirmation prompt. Two files found → error (use `--config` to disambiguate).
  - All persistent arguments are supported in the config file, including `--since`. `--dry-run` and `--limit` are intentionally excluded (one-off per run).
  - CLI arguments always win over config file values.
  - Unknown keys in the config file exit with a clear error listing valid keys.
  - Uses `tomllib` (stdlib since Python 3.11) — zero extra dependency. (`wa_media_archiver.py`)
- **Bumped minimum Python version to 3.11** (from 3.10) — required by `tomllib`. The version gate at startup and all documentation updated accordingly. (`wa_media_archiver.py`, `docs/prerequisites.md`)

### Changed

- **Extracted `archive_db.py`**: all archive database logic moved from `wa_media_archiver.py` into a new companion module — `open_archive_db`, `check_db_health`, contacts/groups persistence, file tracking, folder name sync, and group folder resolution. `wa_media_archiver.py` imports from it via `archive_db.*` calls. Main script reduced from ~1420 to ~1146 lines.

### Fixed

- **macOS Full Disk Access error on iOS backup**: reading files inside `~/Library/Application Support/MobileSync/Backup/` fails with `PermissionError` on macOS unless the Terminal process has Full Disk Access. The raw traceback is now replaced with a clear message directing the user to System Settings → Privacy & Security → Full Disk Access. Affects `detect_encrypted` (Info.plist / Manifest.plist) and `build_manifest_map` (Manifest.db). (`backup_reader.py`)
- **Windows paths with backslashes in `config.toml` are now auto-corrected**: instead of exiting with a cryptic TOML parse error, the script converts backslashes in quoted string values to forward slashes, writes the corrected file back to disk, and continues the run. Genuinely invalid TOML still exits with an error. (`wa_media_archiver.py`)
- **`--ios_backup` + `--wa_root` mutual exclusion not enforced**: the validation check was dead code — the second `parser.error()` call followed an unconditional `sys.exit`, so passing both flags was silently accepted. Split into two independent checks. (`wa_media_archiver.py`)
- **`--config=path` (equals-sign form) silently ignored**: the `sys.argv` peek for the config path only matched the space-separated form `--config PATH`. Replaced with a loop that handles both `--config PATH` and `--config=PATH`. (`wa_media_archiver.py`)
- **TOML unquoted date literal for `since` causes `TypeError`**: `since = 2024-01-01` (no quotes) is valid TOML and parses as `datetime.date`. The value is now silently normalised to an ISO string before `set_defaults`. Any other unexpected type (e.g. integer) exits with a clear error message. (`wa_media_archiver.py`)
- **WhatsApp Status media incorrectly archived as contact media (iOS)**: media files with `@status` in their `ZMEDIALOCALPATH` were being matched by the iOS query and routed into the sender's contact folder as received media. Status updates are not conversation media. Excluded from both the group and 1-to-1 subqueries, and from `build_ios_group_subjects_query`. (`ios_handler.py`)

### Documentation

- `docs/running-the-script.md`: added `--config` and `--generate-config` to the command reference and added a new **Config File** section explaining the format, auto-detection rules, and precedence. Windows backslash auto-correction noted.

---

## [0.34] — 2026-06-08

### Added

- **Startup file check**: if any required companion script (`adb_extractor.py`, `android_handler.py`, `backup_reader.py`, `ios_handler.py`) is missing from the same folder, the script now exits immediately with a clear error listing the missing files and a download instruction, instead of a cryptic `ModuleNotFoundError`.
- **Python version check**: the script now exits immediately with a clear message if run under Python < 3.10, rather than crashing with a cryptic `TypeError` on the `int | None` type annotation syntax.
- **Upfront dependency check**: `check_dependencies()` now runs immediately after argument parsing, before any file I/O or ADB calls. If `adb` is not on PATH (for `--mode adb`), `wa-crypt-tools` is not installed (for `--e2e_key`), or `iphone-backup-decrypt` is not installed (for `--ios_password`), all missing dependencies are reported together in a single error so users can install everything in one go rather than discovering issues one run at a time. (`wa_media_archiver.py`)
- **`--timezone` argument**: IANA timezone name (e.g. `Europe/Rome`, `America/New_York`, `UTC`) for consistent year folder assignment and report timestamps, regardless of the machine's local clock. Affects which `<YYYY>/` subfolder files land in and the `timestamp_human` column in `missing_media_report.csv`. Also applied to `--since` date interpretation. Uses `zoneinfo` (stdlib, Python 3.10+); on Windows requires `pip install tzdata`. Defaults to machine local time — no behaviour change when omitted. (`wa_media_archiver.py`)

### Fixed

- **Wrong or invalid `--e2e_key` crashes with `AttributeError`**: `KeyFactory.new()` returns `None` for an unrecognised key file; passing `None` to `db.decrypt()` raised `AttributeError: 'NoneType' object has no attribute 'get'` deep inside `wa_crypt_tools`. The key object is now validated before decryption, and `db.decrypt()` is wrapped in a try/except so any failure (wrong key, corrupt backup) exits with a clear message. (`wa_media_archiver.py`)
- **Null timestamp silently dropped from missing report**: messages with `timestamp=NULL` that had a valid media file were counted as a warning but never appeared in `missing_media_report.csv`. Now treated consistently with other missing rows — counted in `missing` and written to the report. (`wa_media_archiver.py`)
- **Rename collision re-fires warning on every run**: when a contact or group folder rename was skipped because the target folder already existed on disk, the in-memory index was not updated. Every subsequent run would re-detect the mismatch and log the same collision warning indefinitely. The index is now updated to the new name even when the on-disk rename is skipped. (`wa_media_archiver.py`)
- **`--contacts` + `--ios_backup` silently applied wrong contact format**: passing `--contacts` (Android ADB format) alongside `--ios_backup` would use the Android contacts and silently skip loading `ContactsV2.sqlite`. The combination is now rejected at startup with a clear error directing users to `--ios_contacts` instead. (`wa_media_archiver.py`)
- **File transfers from SMB/network shares incorrectly counted as errors on macOS and Linux**: `shutil.copy2` calls `copystat()` which reads extended attributes and ownership from the source. On SMB mounts this raises `[Errno 1] Operation not permitted` even with full read access, causing the copy to be reported as failed and the WhatsApp timestamp to not be applied. Switched to `shutil.copy` (data only) and isolated the `set_file_times` call in its own try/except so a utime failure on the destination is logged at DEBUG without affecting the copy count. (`wa_media_archiver.py`)

### Changed

- **Android cycle detection deduplicated**: `build_number_map` now collects all cyclic nodes and emits a single `DEBUG` line (`N cycle(s) ignored`) instead of one `WARNING` per node, matching the iOS behaviour added in v0.33.

### Removed

- Stale `extract_files(preserve_folders=True)` comment in `backup_reader.py` (that bulk-extract approach was superseded by the lazy per-file resolver in v0.33).
- Version reference (`v0.12`) from the restore-mode "no restore data" error message.
- `ZFROMJID` column from the iOS test schema fixture (it was removed from the required-columns set in v0.33 but left behind in the test helper).

---

## [0.33] — 2026-05-30

### Added

- **iOS encrypted backup support**: pass `--ios_password <password>` to decrypt an encrypted iPhone backup. Uses `iphone-backup-decrypt` (PyPI) to unlock the keybag, decrypt `ChatStorage.sqlite`, `ContactsV2.sqlite`, and all WhatsApp media to a temp folder.
- **`ChatStorage.sqlite` saved to output folder**: both plaintext and encrypted iOS backup paths now copy the extracted database to `<output>/ChatStorage.sqlite` (with overwrite warning), matching Android's behaviour and enabling re-runs without re-extracting from the backup. (`backup_reader.py`, `wa_media_archiver.py`)
- **`backup_reader.extract_plaintext()`** and **`backup_reader.extract_encrypted()`**: new functions encapsulate the full iOS extraction path (manifest map, DB extraction, contacts extraction). The inline code that was previously embedded in `_prepare_input()` has moved here. (`backup_reader.py`)

---

## [0.32] — 2026-05-30

### Fixed

- **Dry-run miscounts copies after a contact/group rename**: `sync_folder_names` and `sync_group_names` now skip updating the in-memory folder index when `dry_run=True`. Previously the index was updated to the new (not-yet-renamed) folder names even in dry-run mode, causing `process_rows` to route files to non-existent paths and report them all as "would copy" instead of "would skip". (`wa_media_archiver.py`)
- **`original_filename` blank for null-path rows with `media_name`**: moved the `filename` computation before the null-`file_path` early-continue in `process_rows`, so `_build_missing_row` now receives the correct filename for those rows. (`wa_media_archiver.py`)
- **`media_name` used as filename for non-document files on Android**: added a `CASE WHEN file_path LIKE 'Media/WhatsApp Documents/%' THEN media_name END` SQL guard to both union blocks in `build_query()`, matching the same guard already present in the iOS query. Previously the raw `media_name` column was returned for all Android file types; `media_name` is only meaningful for documents. (`android_handler.py`)

---

## [0.31] — 2026-05-30

### Refactored

- **`process_rows` decomposed into focused helpers**: extracted `_build_missing_row()` (unified missing-report dict builder, replacing two near-identical inline blocks), `_route_group()` (resolves dest dir/filename for group messages), `_route_contact()` (resolves dest dir/filename for 1-to-1 messages), and `_copy_or_skip()` (dry-run, dedup, conflict resolution, copy, DB record). `process_rows` is now a ~50-line coordination loop. No behaviour changes.

---

## [0.30] — 2026-05-30

### Refactored

- **`main()` decomposed into focused functions**: the ~360-line god function has been split into `parse_args()` (argument parsing and validation), `_warn_network_paths()` (UNC share warning), `_prepare_input()` (iOS backup extraction, ADB pull, contacts loading, decryption), and `run_forward_mode()` (query execution, archival loop, index persistence, reports). `main()` is now a ~17-line dispatcher. No behaviour changes.

---

## [0.29] — 2026-05-29

### Fixed

- **Contact folders named "Unknown" when contacts were not provided**: the contacts table only stored `number → folder`, so after a first run without `--contacts` produced "Unknown" folders, subsequent runs had no display name to fall back on. Added a `display_name` column to the contacts table (with automatic migration for existing DBs via `ALTER TABLE`). When contacts are provided, the display name is saved alongside the folder. On runs without `--contacts`, the stored display name is used to build the correct folder name. Replaces the v0.28 partial fix (which only preserved the stale folder string).

---

## [0.28] — 2026-05-29

### Fixed

- **Contact folders named "Unknown" on re-run without `--contacts`**: when no contacts file is provided, `contacts` is empty and `build_contact_folder_name` received the raw phone number, producing "Unknown (00...)". The folder index loaded from the archive DB already holds the correct name from a previous run — the code now checks it first and only falls back to `build_contact_folder_name` if the number isn't already known.

---

## [0.27] — 2026-05-29

### Fixed

- **`--limit` excluded 1-to-1 chats**: with the outer-wrapper LIMIT from v0.20, group rows (first in the UNION ALL) consumed the entire cap, returning zero 1-to-1 rows. Changed to `LIMIT N//2` applied per block inside each UNION ALL subquery — both chat types are always represented, and total rows remain ≤ N. Applied to both Android and iOS queries. Log message and `--limit` docs updated to reflect the new behaviour.

---

## [0.26] — 2026-05-29

### Fixed

- **Decryption produced garbage output**: `DatabaseFactory.from_file(msg)` reads the file header and advances the file position. The code then did `msg.seek(0)` and re-read the entire file, passing header+payload to `db.decrypt()` which expects only the payload. Fixed by removing the `seek(0)` and reading the remaining bytes after `from_file` returns. The decrypt output is now correct zlib-compressed SQLite (`78 da` magic), which `zlib.decompress()` handles correctly. Reverted the ZIP/SQLite magic-byte branching introduced in v0.24–v0.25 (unnecessary). Removed unused `import io` and `import zipfile`.

---

## [0.25] — 2026-05-29

### Fixed

- **Decryption crash on ZIP-wrapped backup**: newer WhatsApp backups wrap the SQLite database in a ZIP file inside the encrypted container. The decrypted bytes start with `PK\x03\x04` (ZIP magic) rather than `SQLite format 3` or a zlib header, causing `zlib.decompress()` to raise `zlib.error`. Added a ZIP branch: when the magic matches, the `.db` entry is extracted from the ZIP in memory. Detection order: SQLite magic → ZIP magic → zlib.

---

## [0.24] — 2026-05-29

### Fixed

- **Decryption still produced a corrupt half-sized database**: the zlib-vs-plain-SQLite detection was heuristic (catch `zlib.error`), but `zlib.decompress()` can return a partial result without raising if the input happens to have a valid zlib header followed by truncated data. Replaced with a deterministic check: if the first 15 bytes of the decrypted output equal `SQLite format 3`, it's already a plain SQLite file and is used as-is; otherwise it is zlib-decompressed. No ambiguity, no partial reads.
- **Note**: users on v0.21 also need to update — the previous two fixes (v0.22, v0.23) were not yet picked up.

---

## [0.23] — 2026-05-29

### Fixed

- **Corrupt decrypted database written to disk**: `zlib.decompressobj().decompress()` processes bytes greedily and does not verify the zlib checksum, so it can return partial/corrupt data without raising `zlib.error`, causing the fallback to be skipped and an invalid SQLite file to be written. Replaced with `zlib.decompress()`, which validates the full stream and raises `zlib.error` on any failure, making the plain-SQLite fallback reliable.

---

## [0.22] — 2026-05-29

### Fixed

- **Decryption crash on `wa-crypt-tools` newer versions**: `DatabaseFactory.from_file` requires a `BufferedReader` (supports `.peek()`), not a `BytesIO`. The code was wrapping the file bytes in `io.BytesIO` before passing it, causing `AttributeError: '_io.BytesIO' object has no attribute 'peek'`. Fixed by passing the open file handle directly, then seeking back to 0 to read the raw bytes for `decrypt()`. Unused `import io` removed.

---

## [0.21] — 2026-05-29

### Fixed

- **iOS JID without `@` produced empty sender**: `SUBSTR(jid, 1, INSTR(jid, '@') - 1)` returns an empty string when `INSTR` returns 0 (no `@` in the JID). Empty sender in a group chat fell through to `'Me'`, and empty sender in 1-to-1 chat produced an empty folder name. All three JID extractions in `ios_handler.py` (`build_ios_query` group block, `build_ios_query` 1-to-1 block, `build_ios_number_map`) now use a `CASE WHEN INSTR(...) > 0` guard that falls back to the full JID string.

### Tests

- `TestBuildIosQuery`: added four tests — `test_group_sender_jid_without_at_uses_full_jid`, `test_1to1_sender_jid_without_at_uses_full_jid`, `test_group_sender_normal_jid_strips_at_suffix`, `test_1to1_sender_normal_jid_strips_at_suffix`. All run the actual SQL against an in-memory SQLite database.

---

## [0.20] — 2026-05-29

### Fixed

- **`--limit` double-count**: the LIMIT clause was applied independently to each UNION ALL block (groups and 1-to-1), so `--limit N` could return up to `2N` rows. Moved to the outer query wrapper so it is a true total cap on both Android and iOS.
- **`--since` suppressed rename detection**: the group subjects queries (Android `build_group_subjects_query`, iOS `build_ios_group_subjects_query`) were filtered by `since_ms`, so groups that renamed before the `--since` cutoff were not detected and their folders were not updated. Both functions now unconditionally scan all known groups.
- **`backup_reader` bypassed the logger**: all error/exit paths in `backup_reader.py` used `print(..., file=sys.stderr)` instead of the logger, so backup errors never appeared in `wa_media_archiver.log`. All three functions (`detect_encrypted`, `build_manifest_map`, `extract_to_temp`) now accept and use a `logger` parameter.

### Changed

- **`wa-crypt-tools` no longer auto-installs**: the script no longer runs `pip install wa-crypt-tools` silently on first use. If the package is missing, it exits with a clear one-liner: `pip install wa-crypt-tools`.
- **Empty contacts warning**: `load_contacts` now logs a warning when the contacts file is read but yields no WhatsApp contacts (e.g. due to a missing device permission), rather than silently proceeding with raw phone-number folder names.

### Tests

- `TestBuildQuery`: added `test_limit_applies_to_combined_result` (LIMIT appears once, at end of query) and `test_group_subjects_query_has_no_date_filter`.
- `TestBuildIosGroupSubjectsQuery`: replaced since-clause tests with `test_no_date_filter`.
- `TestDetectEncrypted`, `TestBuildManifestMap`, `TestBuildManifestMapDomain`, `TestExtractToTemp`: updated all calls to pass `logger`.
- `TestLoadAndroidContacts`: added `test_empty_contacts_emits_warning`.

---

## [0.19] — 2026-05-29

### Changed

- **Windows native support**: `wa_media_archiver.py` now runs end-to-end on Windows. The separate PowerShell companion (`windows_extractor_companion.ps1`) is removed.
- ADB extraction logic extracted from `main()` into a new `adb_extractor.py` module (cross-platform, used by all OSes for `--mode adb`). Functions: `check_adb`, `check_device_connected`, `pull_msgstore`, `pull_contacts`.
- `--mode adb` now checks that a device is actually connected before attempting a pull (previously only checked that `adb` was on PATH).
- `pull_contacts` now filters output to WhatsApp lines (`@s.whatsapp.net`) before writing to disk, consistent with what the PS1 companion did. Previously all Android contacts were written to the temp file.
- ADB-pulled files are written to a dedicated temp directory (cleaned up on exit via `atexit`) rather than the current working directory.
- Network share warning: if `--output` or `--wa_root` is a UNC path (`\\server\...`), a warning is logged at startup about potential performance degradation and timestamp preservation issues.

### Removed

- `windows_extractor_companion.ps1` — superseded by the native Python `--mode adb` workflow.

### Tests

- `TestCheckAdb` (2 tests): PATH found / not found.
- `TestCheckDeviceConnected` (4 tests): connected device, no devices, unauthorized device, ADB failure.
- `TestPullMsgstore` (3 tests): regular path, business path, failure propagation.
- `TestPullContacts` (2 tests): WhatsApp line filtering, failure propagation.

---

## [0.18] — 2026-05-22

### Added

- `--business` flag — targets WhatsApp Business instead of the regular WhatsApp app. No behaviour change for users who don't pass it.
  - **Android `--mode adb`**: pulls from `com.whatsapp.w4b/WhatsApp Business/Databases/msgstore.db.crypt15` instead of the regular path.
  - **iOS `--ios_backup`**: reads the `AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared` domain from `Manifest.db` instead of the regular WhatsApp domain.
  - **Android `--wa_root`**: no change needed — `validate_wa_root` checks for `Media/` regardless of the parent folder name; the user simply points `--wa_root` at their `WhatsApp Business/` folder.
- `_WA_BUSINESS_DOMAIN` constant added to `backup_reader.py`.
- `build_manifest_map` now accepts an optional `domain` parameter (default: `_WA_DOMAIN`). Callers pass `_WA_BUSINESS_DOMAIN` when `--business` is set.

### Tests

- `TestBuildManifestMapDomain` (3 tests): verifies that `build_manifest_map` with the default domain excludes business files, that the `domain` parameter correctly filters to business-only files, and that `_WA_BUSINESS_DOMAIN` has the correct value.

---

## [0.17] — 2026-05-22

### Changed

- Android-specific logic extracted from `wa_media_archiver.py` into a new `android_handler.py` module, mirroring the existing `ios_handler.py` split. Moved verbatim: `validate_schema`, `validate_wa_root`, `build_number_map`, `build_group_subjects_query`, `build_query`, and the three associated constants (`_REQUIRED_TABLES`, `_REQUIRED_COLUMNS`, `_MEDIA_SUBFOLDERS`). The main script now calls these as `android_handler.<function>()`.
- `load_contacts` added to `android_handler.py`, replacing the inline ADB CSV parsing that was previously embedded in `main()`. Returns `{phone_number: display_name}` — same format as `ios_handler.load_ios_contacts`.
- `import ios_handler` and `import backup_reader` moved to module level, consistent with `import android_handler`. The deferred `from ... import` blocks that appeared in both branches of the `if args.ios_backup` / `else` split inside `main()` are removed; all three handler modules are now imported once at the top of the file.
- No behaviour change. This is a pure code organisation refactor.

### Tests

- `TestLoadAndroidContacts` added (6 tests): covers normal parsing, multiple contacts, email address exclusion, empty file, missing file, and `None` path.

---

## [0.16] — 2026-05-22

### Added

- **iOS support** — first-class archiving from `ChatStorage.sqlite` (iOS WhatsApp database). Platform is auto-detected; the same archive structure, re-run safety, and all existing features apply to iOS archives.
- `--ios_backup PATH` — reads directly from an iPhone backup directory (the folder containing `Manifest.db`). The script extracts `ChatStorage.sqlite` and `ContactsV2.sqlite` from the backup hash tree at startup; no third-party extraction tool required.
- `--ios_contacts PATH` — optionally supply a pre-extracted `ContactsV2.sqlite`. Auto-extracted from `--ios_backup` if omitted.
- `backup_reader.py` — new module handling the iPhone backup format: `detect_encrypted` (exits with a user-friendly message if the backup is encrypted), `build_manifest_map` (builds a `{relativePath: abs_hash_path}` dict from `Manifest.db` once at startup — O(1) per-file lookup during processing), `extract_to_temp` (copies a single file from the backup hash tree to a temp file).
- `ios_handler.py` — new module handling the WhatsApp iOS schema: `validate_ios_schema`, `validate_ios_wa_root` (pre-extracted mode), `build_ios_query`, `build_ios_group_subjects_query`, `build_ios_number_map` (number change tracking via `ZCONTACTABID` session grouping — best-effort for contacts in the address book), `load_ios_contacts` (reads `ContactsV2.sqlite`).
- `detect_db_platform(path)` — inspects a WhatsApp database and returns `'ios'` or `'android'` by checking for the `ZWAMESSAGE` table. Used when `--wa_root` is provided without `--ios_backup` to auto-detect which query builder to use.
- iOS pre-extracted mode: passing `--msgstore ChatStorage.sqlite` with `--wa_root` (pointing to the extracted `AppDomainGroup` folder) and `--ios_contacts` works as an alternative to `--ios_backup` for advanced users.
- Restore mode iOS guard: if the archive was built from an iOS backup (original paths start with `Message/`), restore mode exits immediately with a clear error. iOS original paths have no useful reconstruction target outside the iPhone backup format.

### Fixed

- iOS group messages with a NULL `ZPARTNERNAME` were misrouted to the 1-to-1 branch because `process_rows` detects groups via `chat_subject is not None`. Fixed by adding `AND cs.ZPARTNERNAME IS NOT NULL` to the group block in `build_ios_query`, consistent with Android's `AND chat.subject IS NOT NULL` guard.
- `ZWAMEDIAITEM.ZTITLE` (`media_name`) is now only used for files whose `ZMEDIALOCALPATH` contains `Documents`, matching Android's document-only behaviour. Previously, any non-NULL `ZTITLE` on an image or video would override the hash-based filename.
- `--mode adb` combined with `--ios_backup` is now rejected at startup with a clear error. Previously, ADB would pull an Android backup and then the iOS backup path would overwrite `args.msgstore`, silently producing a broken run.
- `--contacts` (Android ADB format) passed alongside `--ios_backup` now emits a warning that iOS auto-extracted contacts will be ignored, instead of silently discarding them.
- Restore mode iOS guard replaced a fragile `LIMIT 1` heuristic with explicit `LIKE 'Message/%'` and `LIKE 'Media/%'` probes. A pure iOS archive is still rejected; a mixed iOS+Android archive now warns and proceeds with Android entries only, rather than potentially blocking a valid Android restore.

### Changed

 Android passes `lambda fp: os.path.join(wa_root, *fp.split('/'))`. iOS backup passes `lambda fp: manifest_map.get(fp)`. This allows the same processing loop to handle both platforms without branching.
- `process_rows()`: filename logic simplified — `filename = media_name if media_name else os.path.basename(file_path)`. The previous `is_document` gate checked for an Android-specific path prefix and is no longer needed; `media_name` is only populated for documents on both platforms, so the simplified form is correct for Android and required for iOS (iOS paths do not contain `WhatsApp Documents/`).
- `process_rows()`: source file hashing is now lazy. Previously `file_md5(src)` was computed unconditionally before checking whether the destination exists. Now: if the destination is absent, the file is copied first and hashed from the destination; if the destination exists, both sides are hashed for collision detection. On large re-runs (e.g. 20 GB backup, 49,000 already-archived files), the backup is never read for files that are already present in the archive. **Note:** this optimisation applies to real runs only — dry runs still read the source file when the destination already exists, because `resolve_unique_dest` must hash both sides to determine whether files are identical.
- `--wa_root` is no longer required when `--ios_backup` is provided (or when `--mode restore` is used).

### Notes

- The archive database schema is unchanged. iOS phone numbers (stripped from JIDs) and `ZWACHATSESSION.Z_PK` IDs are in the same format as their Android equivalents — no migration needed.
- Encrypted iPhone backups are detected and rejected at startup with a clear message. Full encrypted backup support is deferred to a future release.

---

## [0.15] — 2026-05-15

### Added

- Documents support: `Media/WhatsApp Documents/` is now archived alongside all other media types. The original filename is preserved using `media_name` from the database, which is how WhatsApp stores it on device (e.g. `Annual Report 2023.pdf` rather than a generic `DOC-20230115-WA0001.pdf`).
- `wa-crypt-tools` is now installed automatically on first use if not present, rather than aborting with an error. If the automatic install fails, a clear message with the manual command is shown.

### Changed

- Script renamed from `wa_archiver` to `wa_media_archiver` throughout: filename, log file (`wa_media_archiver.log`), archive database (`.wa_media_archiver.db`), and CLI program name.

---

## [0.14] — 2026-05-15

### Added

- Schema validation on startup: required tables (`message`, `message_media`, `chat`, `jid`, `jid_map`) and required columns within each are checked against the live WhatsApp DB before any query runs. The script aborts with a precise error listing exactly which tables or columns are missing, rather than failing later with a cryptic SQLite error. Logs `Schema validated.` on success.
- Version logged at startup (`=== WhatsApp Archiver v0.14 started ===`), making bug reports immediately useful. `__version__` constant defined at the top of the script.
- `--wa_root` validated for correct folder structure before any query runs. Aborts with a diagnostic hint for the two most common mistakes: passing the `Media/` folder directly, or passing a subfolder inside `Media/`. Warns (without aborting) if `Media/` is present but contains none of the expected WhatsApp media subfolders.
- ADB mode now checks for `adb` on the PATH before attempting any pull, replacing an unintelligible `FileNotFoundError` with a clear install instruction. Both ADB commands (`pull` and contacts query) now catch `CalledProcessError` and log ADB's own stderr output (e.g. "no devices/emulators found") with a plain-English hint, instead of raising a Python traceback.

### Changed

- Decrypted `msgstore.db` now written to `<output>/msgstore.db` instead of the current working directory. The output folder is the only path the user has explicitly configured, making the location predictable regardless of where the script is launched from.

---

## [0.13] — 2026-05-14

### Changed

- MD5 hashes stored as `BLOB` (16 bytes) instead of hex `TEXT` (32 bytes), halving hash storage and index size. The duplicate report still displays hashes as hex strings.
- `PRAGMA auto_vacuum = INCREMENTAL` set on database creation, so deleted rows release space without a manual VACUUM.
- `PRAGMA quick_check` and `PRAGMA foreign_key_check` run automatically each time the archive database is opened. The script aborts with a clear error if either check fails.
- `ANALYZE` runs at the end of each forward run to keep query-planner statistics current as the database grows.
- Contact and group folder renames now also update the corresponding `archive_copies` paths in the database. Previously, renaming a folder on disk left stale paths that would cause restore mode to report files as unrestorable.

### Notes

- **Breaking schema change.** The existing `.wa_archiver.db` must be deleted before running v0.13 for the first time. A fresh run will rebuild it.

---

## [0.12] — 2026-05-14

### Changed

- Replaced four separate index files with a single SQLite database (`.wa_archiver.db`):
  - `contacts` table — contact folder index (was `.wa_archiver_index.json`)
  - `groups` table — group folder index (was `.wa_archiver_group_index.json`)
  - `files` + `archive_copies` tables — file archive map, replacing both `.wa_archiver_hash_index.json` and `.wa_archiver_restore_index.json`. Archive paths that were previously stored in two separate files are now stored once.
- File records are written to the database per-file during processing within a single transaction, rather than accumulated in memory and flushed at the end.
- Duplicate detection now uses a SQL query instead of an in-memory dict scan.
- Restore mode reads file-to-archive mappings from the database instead of a separate file.

### Fixed

- Restore mode was issuing one database query per archived original path (N+1 problem). Now uses a single query and groups results in memory.

---

## [0.11] — 2026-05-14

### Added

- Group rename detection: when a WhatsApp group is renamed, the archive folder is automatically renamed on the next run (mirrors existing contact rename behaviour).
- Groups with identical names are now handled correctly. Each distinct group, identified by its stable `chat_row_id`, gets its own folder. Naming conflicts are resolved with a numeric suffix, e.g. `Family Chat (2)`. The assignment is stable across re-runs.
- New internal group folder index persists the `chat_row_id` → folder name and last known subject mapping across runs.

### Fixed

- Contact rename: when a rename was skipped because the target folder already existed, the contact index was incorrectly updated to point to the new name. On the next run, new media for that contact would be routed to the wrong folder. The index is now left unchanged when a rename is skipped.
- Same bug was present in the new group rename logic and fixed there as well.

---

## [0.10]

### Added

- Restore mode is now fully operational. `--mode restore` reconstructs the original `WhatsApp/Media/` folder tree from the archive without needing the original device or database.
- Restore mode supports `--dry-run`.
- `restore_report.csv` written after a restore run — one row per issue (unrestorable, collision, or copy error) with the original path, source archive path, and status.
- `--wa_root` is no longer required when `--mode restore`.

---

## [0.9]

### Added

- Hash index and restore index are now loaded at startup and persisted at end of run, enabling duplicate tracking and restore capability across re-runs.
- Duplicate media CSV report generated from the hash index at end of each run.

### Changed

- `resolve_unique_dest()` now accepts an optional pre-computed `src_hash` to avoid hashing the same file twice.

---

## [0.8]

### Changed

- Group sender display name falls back to a formatted phone number (`00<number>`) instead of a raw JID string when the sender is not in the contacts list.

---

## [0.7]

### Added

- `Media/WhatsApp Video Notes/` and `Media/WhatsApp Animated Gifs/` added to the set of archived media types.
- Restore mode infrastructure: restore index records each original WhatsApp path alongside the archive paths it was copied to. `run_restore_mode()` skeleton introduced (not yet fully operational — completed in v0.10).

### Changed

- Index files written atomically using a `.tmp` file and `os.replace()` to prevent corruption if the process is interrupted mid-write.

---

## [0.6]

### Added

- `--mode adb`: automatic pull of the encrypted database and contacts directly from a connected Android device via ADB. Temporary files cleaned up on exit via `atexit`.
- Encrypted backup support: `msgstore.db.crypt15` files can be passed directly; the script decrypts on the fly using `wa-crypt-tools`. The `--e2e` key argument enables this.
- Progress log line every 1,000 rows during processing.
- Hash index introduced (`load_hash_index`, `save_hash_index`) for tracking the MD5 of each archived file — groundwork for duplicate detection and restore.

---

## [0.5]

### Added

- `--since DATE` argument: only include messages on or after a given date (`YYYY-MM-DD`).
- `--limit N` argument: cap rows returned per query block, independently for group and 1-to-1 chats. Useful for test runs.

### Changed

- The date filter and row cap introduced as hardcoded values in v0.3 are now fully dynamic via command-line arguments.

### Removed

- Termux contact loading mode.

---

## [0.4]

### Added

- Contact number consolidation: when a contact changes their phone number, WhatsApp records the change in `message_system_number_change`. The script now reads these records to map old numbers to the current one, so all media for that contact ends up in a single folder regardless of which number sent it. Number-change chains (A→B→C) are resolved to their canonical endpoint, with cycle detection.
- Persistent contact folder index (`.wa_archiver_index.json`): folder name assignments survive re-runs. Files for the same contact are always routed to the same folder even across multiple script executions.
- Contact rename detection: `sync_folder_names()` compares the current contact name against the persisted index. If the name has changed, the folder on disk is renamed automatically before processing begins, keeping the archive consolidated.

---

## [0.3]

### Added

- `Media/WhatsApp Voice Notes/` added to the set of archived media types.
- Date filter (from 2022-01-01) and row cap (250 per query block) applied inside the SQL query. These values were hardcoded in this version and made configurable in v0.5.

---

## [0.2]

### Added

- Missing media CSV report: files referenced in the database but not found on disk are now collected and written to `missing_media_report.csv` at the end of the run, with message ID, timestamp, sender, chat name, direction, and download URL.
- Human-readable timestamp formatting for log and report output.

### Changed

- Group chat and 1-to-1 chat queries split into explicit `UNION ALL` blocks, making the selection logic for each chat type independent.

### Fixed

- Identical-file detection log messages downgraded from INFO to DEBUG to reduce noise during normal runs.

---

## [0.1]

Initial release.

- Archives WhatsApp media into a structured `Contacts/` and `Groups/` folder hierarchy.
- Year subfolders and `Sent/`/`Received/` subfolders for 1-to-1 chats.
- Sender name appended to filenames in group chats; own messages tagged `_Me`.
- Contact names resolved from an ADB-exported contacts file.
- File modification timestamps set to the original WhatsApp message timestamp.
- Collision handling: files with the same name but different content are renamed with a numeric suffix rather than overwritten.
- Dry-run mode.
