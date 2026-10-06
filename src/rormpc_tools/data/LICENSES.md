# Data shipped with rormpc-tools

`hits-seed.jsonl.gz` is a cache seed for `hits`, so a new install ranks every Billboard year-end chart at once instead
of spending about 17 minutes of MusicBrainz requests per decade. It is written by `hits seed` from a local cache and
imported once per seed file into an empty or older cache, adding only what the cache lacks. It holds the year-end charts and, per chart
entry, the MusicBrainz recording `hits` chose (and artist genres), and ListenBrainz popularity per recording.

- MusicBrainz identifiers, names and release dates are MusicBrainz core data, CC0 1.0
  (https://musicbrainz.org/doc/About/Data_License).
- MusicBrainz tags and genres (the `tags` of a match and the `a-*` artist genre records) are MusicBrainz
  supplementary data, CC BY-NC-SA 3.0, by the MusicBrainz community, https://musicbrainz.org.
- The year-end charts (`chart` records: position, title, artist) are read from the Wikipedia articles "Billboard
  Year-End Hot 100 singles of YEAR", CC BY-SA 4.0, https://en.wikipedia.org.
- Popularity (`lb` records) comes from the ListenBrainz API (https://listenbrainz.org), MetaBrainz Foundation; check
  the ListenBrainz data license before reusing it on its own.
- The choice of which recording matches a chart entry is the output of `hits`' matching (BSD-3-Clause, like the
  code).
