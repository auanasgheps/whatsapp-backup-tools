# WhatsApp Backup Tools v0.80

Version 0.80 brings major enhancements to _WhatsApp Backup Tools_, introducing long-awaited navigation features, interactive communication records, and a modernized viewer interface.

This update unlocks comprehensive Call History logs with a dedicated Call Inspector, Starred bookmarks, Pinned message banners, an Archived chats view, interactive group `@mentions` with 1-to-1 chat links, collapsible long messages, circular video notes, and theme-adaptive group sender colors.


#### **Release highlights:**

> 📞 **Call History & Call Inspector**:
> Browse your complete voice and video call logs directly within the viewer. A new Call Inspector side pane displays caller information, durations, and participants, with a "Jump to this Moment in Chat" button that navigates straight to that exact point in the conversation. Calls are also woven directly into chat timelines as chronological call bubbles.

> ⭐ **Starred Messages & Quick Bookmarks**:
> Access your saved and starred messages globally from the new Starred tab or from an in-chat flyout drawer. Clicking any starred message jumps directly to its original conversation context with a pulsing golden highlight.

> 📌 **Pinned Messages Banner Strip**:
> Important conversations with pinned messages now display an authentic pinned banner strip beneath the chat header. Cycle through multiple pins, dismiss the banner for your session, or jump straight to the pinned bubble with a pulsing emerald highlight.

> 👥 **Interactive Group `@mentions`**:
> Group mentions (`@Alice`, `@Bob`) are now recognized and rendered as styled mention badges. Clicking a mention opens your 1-to-1 conversation with that participant if an archived chat exists.

> 📜 **Collapsible Long Messages & Circular Video Notes**:
> Lengthy text messages are cleanly truncated with a smooth "Read More" / "Show Less" toggle that keeps your timeline readable. Push-to-Video notes are now rendered in their signature circular player with live countdown progress rings.

---

### 📁 WhatsApp Archiver

#### ✨ New Features
- **Group Mention Preservation**: Group `@mentions` are now preserved across backup extractions and incremental merges in `preserved_messages.db`, retaining mention metadata and participant associations even when messages are removed from your phone.
- **iOS Call History Archiving**: Discovers, verifies, and stages WhatsApp call history databases (`CallHistory.sqlite`) from iOS backups across standard and AppDomain paths for browsing in the viewer.

#### ⚡ Improvements
- **Android Mention Ingestion in Incremental Merges**: Extracts and indexes participant mentions from Android incremental backup streams, recording mention metadata into merged databases.

---

### 🌐 WhatsApp Viewer

#### ✨ New Features
- **Archived Chats Tab & Navigation**: A dedicated "Archived" tab in the bottom navigation bar lets you browse archived conversations separately from your active inbox, with tab-aware search and instant chat opening.
- **Call History & Call Inspector**: Browse your complete voice and video call logs with dedicated filters (All, Missed, Video), two-line call cards, and a detailed Call Inspector side pane. A "Jump to this Moment in Chat" button navigates directly to the exact point in the conversation when the call took place.
- **Circular Video Note Player**: Push-to-Video (PTV) video notes now render in an authentic circular player with animated progress rings, countdown timers, fullscreen lightbox viewing, and auto-pause when scrolled out of view.
- **Collapsible Long Messages**: Lengthy text messages exceeding 750 characters or 15 lines are cleanly truncated with a smooth "Read More" ↔ "Show Less" toggle that keeps your timeline readable while preserving emoji formatting and scroll position.
- **Group `@mentions` with 1-to-1 Chat Links**: Mentions in group chats are highlighted as interactive badges. Clicking a mention smoothly opens your 1-to-1 chat with that contact if an archived conversation exists.
- **Group Sender Name Colors**: Group chat participants now receive distinctive, theme-adaptive sender name colors (matching WhatsApp's 10-color palette) across dark and light modes, applied to message headers and quoted reply borders.
- **In-Chat Chronological Call Bubbles**: Voice and video calls are seamlessly woven directly into chat timelines as chronological call bubbles, showing call outcome, duration, and metadata at a glance.
- **Pinned Messages Strip & Jump**: Pinned messages appear in a dedicated banner strip beneath the chat header with multi-pin cycling (`1 of N`), chevrons, session dismissal, and a 1-click jump to the pinned message with a pulsing emerald highlight.
- **Starred Messages & Drawer**: Starred messages can be browsed globally via the sidebar "Starred" tab or per-chat via the header flyout drawer, with 1-click jump to context accompanied by a pulsing gold halo.
- **Tab-Aware Search & Universal Shortcuts**: The search bar dynamically adapts to your active navigation tab—searching messages, call history, starred bookmarks, or archived chats—with instant `Ctrl+K` (or `/`) keyboard shortcuts.
- **Android Calls & Starred Identity Resolution**: Automatically maps privacy Linked Identities (`@lid`) and companion chat rows in Android call logs and starred messages to real contact names and normalized phone numbers for reliable chat navigation.
- **iOS Call History & CoreData Relationship Mapping**: Fully maps iOS `CallHistory.sqlite` CoreData relationships, resolving call participants, outcomes, privacy LIDs, and companion chat links.

#### ⚡ Improvements
- **Captioned Media Containment & Sizing**: Media bubbles tightly wrap captions and photos without unnecessary horizontal space or stretching, using orientation-clamped dimensions for landscape and portrait media.
- **Display Scale Settings**: The Settings panel now features a "Display scale" option that proportionally scales text, sidebar width, and media dimensions together.
- **Dual-Theming & Contrast Polish**: Enhanced light and dark theme contrast with WCAG 2.1 AA compliant accents, refined bubble elevation drop shadows, and borderless message cards.
- **In-Bubble Star & Pin Indicators**: Subtle star (`⭐`) and pin (`📌`) indicator icons are integrated directly into the message metadata cluster next to the timestamp, keeping message headers clean.
- **Modernized UI Shell & Navigation Bar**: Redesigned bottom navigation bar with 4 tabs (Chats, Calls, Starred, Archived), top-right brand header with platform badge, and full keyboard tab navigation.
- **Rich Media Cards in Starred & Pinned Views**: Starred and pinned previews now display rich media snippets (photos, videos with duration, voice notes, documents, locations, contacts, and polls) instead of generic text.
- **Time Gap Jump Indicators**: Jumping to a chat moment from a call or starred message displays a contextual hint banner with the exact time difference when the nearest message is outside the immediate window.
- **iOS High-Performance Message Pagination**: Optimized message timeline queries for iOS to scan physical columns directly, eliminating temporary sorting tables and accelerating scrolling in large group chats.
- **iOS Pinned Announcement De-referencing**: Pin announcements with empty parent pointers are de-referenced against target message stanza IDs, suppressing raw hex hashes and ensuring readable banner text.

#### 🐛 Bug Fixes
- **Grouped Media Caption Visibility**: Fixed an issue where photo and video album captions placed on the first item in iOS backups were not shown on the collapsed album card.
- **Long Message Grapheme Clustering & Toggle Spacing**: Fixed long message collapse splitting multi-character emoji sequences (such as flags and zero-width joiners) and adjusted focus ring spacing on the Read More toggle.
- **Quote Jump Auto-Expansion**: Jumping to a quoted message that is collapsed now automatically expands the message to reveal the referenced text.
- **Sidebar Search Leakage During Infinite Scroll**: Prevented infinite scroll pagination in Calls and Starred lists from appending unfiltered items when an active search query is present.
- **Android Archived Chat Retention**: Prevented empty archived chats or archived chats whose messages are recorded under a companion row from being excluded from the chat list.
- **iOS Empty Archived Chat Retention**: Ensured archived chats with zero messages remain accessible in the Archived tab.

---

### 🚀 How to Upgrade

Update to the latest version using pip:

```bash
pip install --upgrade whatsapp-backup-tools
```

> 💡 **Windows users:** If `pip` is not recognized, run `py -m pip install --upgrade whatsapp-backup-tools` instead.
