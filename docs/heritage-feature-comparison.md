# Pirates 1.9, Eagles 3.1.1 and the recovered production release

This report compares the stock features found in the surviving Pirates BBS 1.9 source, the later Eagles BBS 3.1.1 source and documentation, and the Mars Hotel release deployed on October 18, 2026. It is a source-level and behavior-level comparison, not a claim that every historical Mars Hotel site customization survived.

## Evidence and method

The comparison used these fixed inputs:

- Pirates BBS 1.9: the original `pbbs-1.9.tar.Z` archive from the [Linux BBS archive](https://www.ibiblio.org/pub/linux/system/bbs/), SHA-256 `2e5e815a6b3f8b6ad5d5fcf962900704b4e6a07789ccfdfc323def55cefe7bd0`. Its `ReadMe` identifies it as the code running on The Mars Hotel on November 25, 1991.
- Eagles BBS 3.1.1: the [surviving eBBS source](https://github.com/virtadpt/eBBS/tree/b141543e501215b4b6cafa3b32c5ff0a65b95e4a) at commit `b141543e501215b4b6cafa3b32c5ff0a65b95e4a`, including `EBBS-Guide`, `ChangeLog`, `config/menu.desc` and the C source.
- Current release: the recovered Python source corresponding to the deployed October 16–17 images and the public recovery repository tree at commit `4dc5d00a67b0a1e0b6d845dbf4adda4dcd5d4c9a` before this report.
- Runtime checks: read-only inspection of the production containers plus isolated model and compatibility tests. No production container, network, volume or database was changed.

The Pirates archive does not contain the referenced `irc2.5.1`, `protocols` and `more` directories. Their integration and supported behavior are still documented by the archive's `ReadMe`, menu definitions and launcher code. The Eagles comparison describes stock Eagles 3.1.1. A locally configured external program is counted only where the supplied distribution demonstrates the hook.

Status has the following meaning:

- **Complete**: the useful behavior is present in the current release.
- **Partial**: its main behavior exists, but one or more documented operations are absent.
- **Replaced**: the same user need is met through a materially different design.
- **Absent**: no current equivalent was found.
- **Added**: current behavior with no stock Pirates or Eagles equivalent.

The judgment column answers the requested practical question:

- **Better**: stronger or more complete for the same user need.
- **Sane**: an intentional modern or security tradeoff whose removal is reasonable.
- **Worse**: a useful historical capability has been lost.
- **Mixed**: the current implementation improves some behavior and loses some.

## Overall finding

The current Mars Hotel is a real descendant of the Pirates/Eagles design rather than a name-and-theme recreation. The original social core survives: self-service accounts, message boards with unread scans, private mail, member presence, split-screen Talk, public chat, files, IRC, administration, one-key menus and the familiar Mars Hotel presentation. The database, authentication, transport and isolation designs are substantially safer and more reliable.

Parity is incomplete. The largest functional losses from Eagles are Internet mail and forwarding, user uploads and classic transfer protocols, runtime-configurable menus and external programs, the cloak and pager-override controls, legacy character-set mappings, and some advanced chat commands. These omissions are documented below rather than represented as recovered features.

## Access, accounts and terminal interface

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| Public connection | Login shell/rlogin-era terminal access; the design could also be reached through the host's network services | Terminal login, commonly Telnet in period deployments | Forced-command SSH on port 2223; no Telnet listener | Replaced | **Better** transport security; **Sane** loss of period protocols |
| Authentication before use | User ID and password; `new` creates an account | User ID and password, with optional on-demand account creation | User ID and password required; self-registration remains; guest and anonymous sessions are prohibited | Complete | **Better** |
| Password storage | Traditional `crypt`; only eight password characters were significant | Same fixed-record password design and eight-character significance | Argon2id/libsodium hashes, modern length handling, temporary-password workflow and secret-backed bootstrap | Replaced | **Better** |
| User identity and profile | User ID, name and basic account record | Display name, real name/address/email fields, plan/query, signature and last-login data | Immutable user ID, display name, private email, public “about,” timezone, signature and last-login data | Complete | **Better** privacy defaults; account ID rename is intentionally absent |
| New-account workflow | Interactive self-registration | Configurable on-demand registration | Interactive self-registration plus administrator-created accounts | Complete | **Better** |
| Session limits | Compile-time overall limit, normally 36 | Overall and per-account login limits | 48 concurrent sessions by default, three per account, leases, expiry cleanup and administrator termination | Complete | **Better** |
| Full-screen terminal UI | Termcap screens and single-key menus | Full-screen menus, novice/expert modes and VT arrow navigation | Full-screen ANSI interface, one-key menus, responsive narrow-terminal handling and full-screen editors | Complete | **Better** overall |
| Terminal type and character mapping | User-selected terminal type | User terminal setting plus configurable input/output character mappings | Terminal size and capability detection; UTF-8/ANSI and CP437 door handling; no account-selectable terminal or arbitrary 8-bit mapping | Partial | **Mixed**: safer modern defaults, less support for vintage terminals |
| Welcome, history, information and help | Welcome/issue text, help and GNU license command | Configurable welcome and help content | Welcome/history and contextual help are present; no runtime text editor or dedicated GPL menu command | Partial | **Mixed** |
| Novice/expert menu modes | Static one-key menus | Separate novice/expert display behavior | Always-visible concise menus with contextual help | Replaced | **Sane** |
| Cloak/hide presence | Privileged cloak command | Account cloak and list override controls | No cloak control | Absent | **Worse** for historical parity; clearer presence behavior |
| Disabled/shared/exempt accounts | Access level and account flags | Disabled, shared and cleanup-exempt account flags | Active/disabled state, roles and cleanup exemption; shared-login behavior is not offered | Partial | **Better** for accountability; shared accounts are a **Sane** omission |

## Message boards

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| Select, list, read and post | Core main-menu functions | Preserved and expanded | Board picker, message list, reader and editor | Complete | **Better** |
| New-message scan | `New` scans unread posts | Preserved with per-user scan state | Unread scan across boards using per-message receipts | Complete | **Better** |
| Zap and Visit | Zap marks current board read; Visit marks selected boards for scanning | Preserved | Both behaviors implemented | Complete | **Better** |
| Message and board counts | Board listing and message navigation | Board/message counts in menus | Dedicated count display and board metadata | Complete | **Better** |
| Reply and thread relationships | Reply from reader, primarily sequential navigation | Follow-up relationships and sequential/thread-follow operations | Replies carry a durable parent relationship; no dedicated “follow entire thread” command | Partial | **Mixed** |
| Read navigation | Next, previous, page, go-to, last and sequential unread | Configurable read menu with expanded navigation | List paging, selection, next/previous and unread navigation | Complete | **Sane** modern mapping |
| Per-user read state | A byte for every account in each post directory record | Same general fixed-record lineage | Normalized SQLite read receipts | Replaced | **Better** concurrency and scale |
| Board permissions | Numeric access levels | 32-bit read/post masks and operation permissions | Per-board read/post roles, administrator role and explicit managers | Complete | **Better** maintainability |
| Board management | Create board and basic sysop deletion | Create/change/delete, managers and no-zap control | Create, rename, configure roles/managers/read-only/no-zap, archive and delete operations | Complete | **Better** |
| Edit, move, delete and protect | Authors can delete; sysop operations vary by command | Edit, move, delete/range-delete and mark/protect messages | Authors/managers can edit or delete as authorized; managers can move and protect; no range-delete command | Partial | **Mixed**; safer individual operations, lower bulk efficiency |
| Capacity | Fixed files and compile-time account coupling | Up to 80 boards and 2,048 posts per board in documented releases | SQLite-backed storage with bounded UI queries and no equivalent fixed 2,048-message limit | Replaced | **Better** |
| Expiry/cleanup utilities | File-oriented manual administration | `delacct` and maintenance utilities; source still listed several cleanup TODOs | Account inactivity report and manual decisions; no automated post-expiry or bulk inactive-account deletion | Partial | **Worse** for large-site operations; safer against accidental loss |

## Private mail

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| Local private mail | Send, new/read, reply and delete | Preserved | Inbox, composer, reader, reply, forward and delete | Complete | **Better** |
| Group addressing | Basic local recipient behavior | Group send and reply-all | Multiple local recipients and reply-all, bounded in the UI | Complete | **Better** |
| Sent mail and drafts | Not present | No durable sent/draft folders | Sent folder and persistent drafts | Added | **Better** |
| Signatures | No Eagles-style profile signature | Configurable account signature | Account signatures supported | Complete | **Better** |
| Internet email input/output | Not in the stock Pirates mailer | `bbsmail`/`bbsmaild` and sendmail integration | No external email gateway | Absent | **Worse** than Eagles |
| Forward mail/posts/files to email | Not present | Account forwarding for mail and menu operations for post/file forwarding | No email forwarding | Absent | **Worse** than Eagles |
| Mail limits and storage | Fixed binary records | Documented 2,048-message mailbox limit | SQLite mailbox storage without that fixed limit | Replaced | **Better** |
| Tagging/cleanup | Basic delete | Mail tagging and cleanup were still listed as TODO work in the surviving source | Individual delete and folder views | Partial | **Sane** relative to the code that actually survived |

## File libraries and transfers

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| File areas and catalogs | Select/list file boards | File boards, directories and listing | Reviewed file areas, catalog browsing and metadata | Complete | **Better** |
| Text-file viewing | `Text` command | Preserved | Safe bounded text renderer | Complete | **Better** |
| Downloads | Kermit, X/Y/Zmodem and protocol executables when separately installed | Kermit, X/Y/Zmodem and ASCII; FTP was recommended for unreliable Internet terminal transfers | One-use SSH download tickets, size limits, checksums and a bounded Base64 fallback | Replaced | **Better** security and Internet reliability |
| Uploads | Supported through installed protocol executables | Upload area and classic protocols | No user upload path | Absent | **Worse** for exchange functionality; **Sane** for the public attack surface |
| Protocol selection | User selected an installed external protocol | Configurable protocol menu | No protocol selector; transfer method follows the secure download path | Replaced | **Sane** |
| Nested directory browsing | Filesystem-oriented areas | Subdirectory navigation | Curated logical areas and historical catalog instead of arbitrary traversal | Partial | **Better** containment, less flexible browsing |
| File access control | Unix/chroot permissions and command access | File menus lacked the per-area ACL depth of message boards | Reviewed catalog plus service-level authorization and path validation | Replaced | **Better** |
| Search and provenance | Not present | Basic list/search behavior | Searchable catalog, hashes and source/provenance metadata | Added | **Better** |

## Presence, Talk, Chat and IRC

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| User/member list | Account list and online list | Full/short lists, activity modes and profile query | Member directory, public profiles, online state and activity; private email is hidden | Complete | **Better** |
| Activity modes | Session state used by presence tools | Expanded activity display | Named activity is recorded by the session service | Complete | **Better** |
| Private page/Talk | Page, accept/reject and split-screen two-person conversation | Preserved with pager controls | Page, answer/reject, split panes, typing notices, timeout and disconnect cleanup | Complete | **Better** |
| Do-not-disturb and page override | Page rejection behavior | Pager setting plus selected override users | Do-not-disturb is present; no per-user override allowlist | Partial | **Worse** than Eagles |
| Talk monitor | Poll/monitor behavior around pending conversations | Monitor command | Session-local monitor behavior | Partial | **Mixed** |
| Pirates public Chat | One broadcast room, eight-character alias and daemon startup | Replaced by a richer room service | Multi-room chat with aliases and event history | Complete | **Better** |
| Eagles room chat core | N/A | Rooms, who/whois/whoin, private messages, actions, invites, locks/secrets, keys, room operators, kick and ignore | Rooms, who/whois, private messages, actions, invites, lock/secret, room operator, kick, ignore and MOTD | Partial | **Better** core implementation; several commands are missing |
| Eagles extended chat commands | N/A | Persistent ignore plus `/wall`, `/long`, `/date`, and optional `/roll`/`/think`; configured restricted-user behavior | Ignore lasts for the session; no direct equivalents for those commands or static restricted-chat accounts | Absent/Partial | **Worse** than Eagles |
| IRC | Bundled IRC 2.5.1 integration was documented; its client removed risky commands | External IRC could be attached to a runtime menu; the stock menu included an example hook | Embedded client for the private ngIRCd service plus a constrained bridge to public `#MarsHotel` | Replaced | **Better** containment; less arbitrary IRC client functionality is a **Sane** tradeoff |
| External IRC reachability | Depended on the separately installed IRC tree and network | Depended on local menu configuration | Bridge code and production services are present; recovery verified local integration, while independent public-network reachability was not revalidated | Partial verification | No functional claim beyond the evidence |

## Administration and extensibility

| Historical feature | Pirates 1.9 | Eagles 3.1.1 | Current production | Status | Judgment |
|---|---|---|---|---|---|
| Account administration | View/edit account, change level and delete user | Add/delete/edit accounts and permission masks | Create, inspect, disable/enable, assign roles, reset temporary password and delete | Complete | **Better** workflow; user IDs remain immutable |
| Access control | Numeric levels, with level 255 as sysop | 32-bit account flags and roughly 95 operation permissions | Named roles and explicit capabilities, plus board roles/managers | Replaced | **Better** auditability; less arbitrary than Eagles |
| Inactive-account cleanup | Cleanup command | `delacct` supports reporting/dry-run and bulk cleanup with exemptions | Inactivity report and exemption flag; deletion remains a deliberate per-account action | Partial | **Worse** bulk efficiency; safer default |
| Board administration | Create/delete and sysop operations | Create/change/delete, managers and access masks | Full board configuration, managers, archive and audit | Complete | **Better** |
| Session kick | Limited to process/session-era tools | Kick and operator tools | Inspect and terminate active sessions through administration | Complete | **Better** |
| Sysop Unix shell | Level-255 shell | Could be exposed through configurable external commands | No BBS shell | Absent | **Better** security; a **Sane** removal |
| Runtime menu configuration | Menus compiled in C | Menus/read menus and modes loaded from configuration | Menus are defined in reviewed Python code | Absent | **Worse** site customization; **Sane** reduction in executable configuration |
| Arbitrary external programs | Hard-coded launchers for IRC/protocols | `$exec` menu entries and configurable external applications | Only explicitly implemented and sandboxed games/services | Absent | **Worse** extensibility; **Better** containment |
| Alternate editor | Built-in editor | User-selectable internal/external editor | One maintained full-screen editor | Partial | **Sane** |
| Welcome/menu editing | Installed text files | Runtime-configured menus and editable site text | Version-controlled artwork and text; update requires a deployment | Partial | **Worse** operator convenience; **Better** reproducibility |
| System logging | Flat log files | `bbslog` and related utilities | Structured audit records, service health and container logs | Replaced | **Better** |
| External helper programs | `bbsrf`, protocol and IRC launchers | `addacct`, `delacct`, `bbslog`, `bbfinger`, `bbsmail(d)` | Auth/session/board/mail/file/chat services and admin UI; no Internet-mail or finger endpoint | Partial | **Mixed** |
| Storage and concurrency | Shared chroot with binary records and process locking | Expanded fixed-record/file design | Transactional SQLite databases split by service, migrations, leases and isolated volumes | Replaced | **Better** |
| Runtime privilege boundary | Setuid-root launcher, chroot and later UID drop | One BBS operating-system identity in a chroot-oriented design | Forced SSH command, per-service non-root UIDs, read-only root filesystems, dropped capabilities and isolated networks | Replaced | **Better** |

## Current additions beyond the surviving heritage releases

These functions are deployed and recovered, but they should not be mistaken for stock Pirates 1.9 or Eagles 3.1.1 features.

| Current feature | Evidence and completeness | Judgment |
|---|---|---|
| Galactic Arcade | Native Oregon Trail, WOPR/Joshua, Maze, Global Thermonuclear War and chess are implemented. Zork and LORD door integration are present; Zork story data remains separately supplied. The public LORD 4.02a distribution and public registration material are included. TradeWars was not installed. | **Added**; maintains the period BBS experience without claiming unrecovered games |
| DataNet information services | Weather, news, solar/HF, amateur radio, earthquakes, markets, ISS/satellites and television/movie collectors with caching and graceful unavailable states | **Added** and operational where upstream data and optional credentials permit |
| Historical file catalog | Recovered media can be described, hashed and searched without exposing production paths or unchecked uploads | **Added/Better** |
| Security auditability | Named roles, audit events, health checks, secret-backed configuration and a reproducible Compose deployment | **Added/Better** |
| Containerized service separation | Authentication, sessions, boards, mail, files, chat, games, information, IRC and gateway have explicit data and network boundaries | **Added/Better** |

## Completeness verdict

For the Pirates 1.9 baseline, every essential interactive social subsystem has a current implementation or a deliberate modern replacement. The clear parity gaps are uploads, classic selectable transfer protocols, cloak, user-selected terminal configuration, and the sysop shell. The first four affect historical behavior; omitting the shell is a security improvement.

For Eagles 3.1.1, the current release preserves most of the user-facing core and improves its storage, authentication, session cleanup, mail organization, board permissions and deployment model. The meaningful regressions are:

1. no external Internet mail gateway or mail/post/file forwarding;
2. no uploads or selectable Kermit/X/Y/Zmodem/ASCII protocols;
3. no runtime-configurable menus or arbitrary external-program entries;
4. no cloak, list override or pager override allowlist;
5. no arbitrary legacy character-set mappings or alternate editor selection;
6. no persistent chat ignore list and several Eagles chat convenience commands;
7. no bulk inactive-account deletion, runtime welcome editor or account-ID rename.

The current behavior is saner where an old feature depended on Telnet-era trust, setuid execution, shared accounts, shell access, unchecked filesystem traversal or arbitrary command launching. Internet mail, uploads and safe operator extensibility are still useful product capabilities; they are called **Worse** or **Mixed**, not dismissed as security removals.

## Validation performed

The recovery comparison used source inspection and one isolated compatibility run against the current tree. The compatibility run passed checks for:

- last-login and member-directory privacy;
- public profiles and signatures;
- administrator account creation and cleanup exemption;
- per-user board read state, New, Zap and Visit;
- board managers, protect, move and delete operations;
- local group mail;
- byte-exact authorized file download behavior.

The test container used `--network none` and a read-only root filesystem. Earlier recovery validation also passed Python syntax/import checks, the complete recovered model suite, clean Docker builds, isolated service health checks, SSH authentication, local IRC integration and secret scans. Public external IRC reachability was not independently revalidated during this comparison.

Production at the time of comparison continued to use these exact image families: gateway `sha256:d0210d11...`, auth/session/mail/chat `sha256:9682aca8...`, board `sha256:457a4c1d...`, file `sha256:3392cb80...`, game `sha256:993ca780...`, door `sha256:e39f6612...`, information `sha256:59e83087...`, IRC `sha256:7b2cd0a6...` and bridge `sha256:b4946b61...`. All were inspected without mutation; the only host-published BBS endpoint remained SSH.

## Historical context

The surviving Eagles source describes itself as the successor to Eagles 2, which was based on Pirates, while retaining the established interface through a largely rewritten implementation. Contemporary background is preserved in the [Linux Journal history of the Mars Hotel and Eagles BBS](https://www.linuxjournal.com/article/2789). This report evaluates implementation evidence, so period descriptions were used as context rather than as proof that an optional feature was present in the stock source.
