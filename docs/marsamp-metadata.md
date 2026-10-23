# MarsAmp metadata normalization

MarsAmp keeps the audio libraries read-only. It never renames a NAS file or
rewrites an audio tag. The `tracks` table remains the raw, reversible catalog
of source-relative paths. Canonical display names live separately in
`track_metadata` and are tied to the file modification timestamp that was
actually inspected.

## Precedence and confidence

The displayed title, artist and album use this order:

1. an administrator-reviewed correction (`reviewed`, confidence 100);
2. embedded audio tags from the current file (`embedded`, confidence 65–95);
3. conservative presentation cleanup of the original path-derived value
   (`path`, confidence 25).

Complete embedded tags receive confidence 80–95 according to agreement with
the path-derived artist and cleaned title. Title plus artist receives 75–85.
Partial tags receive 65 and fill only their populated fields. Placeholder tags
such as `artist`, `title`, `unknown artist`, or `unknown album` are rejected.
Empty canonical fields fall back independently to the raw catalog.
Changing a file invalidates its canonical row until that exact file is scanned
again. A reviewed row is never silently overwritten by an automatic scan.

The path fallback removes leading disc/track syntax, filename underscores,
stray leading separators and bracketed `Explicit` markers. It deliberately
does not guess spelling, performers, editions, remasters or release dates.
Titles such as `1999` and `867-5309/Jenny` are protected from numeric-prefix
removal.

## Research basis

The model follows MusicBrainz's distinction between recording titles, release
titles and artist credits. Credited artist spelling and join phrases are
preserved instead of being synthesized, and featured performers are not moved
between title and artist fields automatically:

- <https://musicbrainz.org/doc/Style/Artist_Credits>
- <https://musicbrainz.org/doc/Style/Recording>
- <https://musicbrainz.org/doc/Style/Release>
- <https://musicbrainz.org/doc/Style/Release_Group>

MusicBrainz Picard's cross-format tag mapping informed the normalized fields
for ID3, Vorbis/FLAC and MP4 files, including track/disc values and persistent
MusicBrainz identifiers where a future importer can prove them:

- <https://picard-docs.musicbrainz.org/en/latest/appendices/tag_mapping.html>
- <https://picard-docs.musicbrainz.org/en/latest/variables/tags_basic.html>

MarsAmp does not submit every track to a fuzzy web search. MusicBrainz requires
a meaningful User-Agent and no more than one request per second on average,
and title-only matches are ambiguous. Web enrichment must therefore be cached,
grouped by release and reviewed, or use an AcoustID fingerprint with a
configured application key:

- <https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting>
- <https://picard-docs.musicbrainz.org/en/latest/config/options_fingerprinting.html>

## Operation

The normal catalog rescan also examines tags for new and changed files.
Existing current metadata is skipped, so only the initial campaign reads the
whole library. Progress is committed in batches and survives interruption.
Catalog/audio service remains available because files are opened read-only and
the database write lock is held only for each short batch.

In **Administration → MarsAmp → Metadata names**, an administrator can search
for a track, compare raw and canonical values, and save a confirmed correction.
`EDIT`, `SAVE` and `RESET` confirmations prevent accidental changes. Resetting
returns a track to its current embedded tags. Every reviewed
change records its previous and new values, actor and timestamp in
`track_metadata_audit`.

The administration summary reports canonical coverage and remaining path
fallbacks. A fallback is not presented as independently verified; it means no
usable current embedded or reviewed record exists.

## Backup and rollback

Back up `music.db` with SQLite's online backup mechanism before a large
campaign or service upgrade. Rolling back the service image leaves the NAS
unchanged. The new tables are additive and older images ignore them. Restoring
the database snapshot returns all canonical and reviewed values as one unit.

Do not publish `music.db`, audit databases, file inventories or source-relative
paths. They are deployment data and may disclose a private media collection.

## 2026-10-23 Mars Hotel audit

A read-only tag audit examined all 26,075 indexed tracks from NAS01 and UNR01
without errors in 662.8 seconds. It found:

- 25,462 tracks (97.65%) with complete title, artist and album tags;
- 25,849 with a title, 25,705 with an artist and 25,557 with an album;
- 406 partial-tag tracks and 207 untagged tracks, which retain conservative
  path-derived fallback names;
- 33 rows containing generic placeholder values, which the importer rejects;
- 358 unique tracks used by the curated playlists: 355 have usable tags, the
  other three already have clean path fallbacks, and none has a placeholder.

The private audit database includes source-relative paths and is retained only
in protected recovery storage. It is deliberately excluded from Git.
