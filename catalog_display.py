"""Recording-specific catalog credits, used only for the artist display."""
import re
from catalog_autolink import _MatchContext, _album_key, _duration, _song_albums, _solo_title_parts, recording_title_key


def _display_album_key(value):
    # Digital Campaigns releases wrap the same numbered character edition in
    # parentheses. Retain the number and every character/edition label.
    return re.sub(r'\((vol\.\d+[^()]*)\)$',r'\1',_album_key(value),flags=re.I)


class CatalogDisplayCredits:
    def __init__(self, songs, people):
        self.context = _MatchContext(people, songs)
        self.by_title = {}
        for song in songs:
            self.by_title.setdefault(recording_title_key(song['title']), []).append(song)
        self.last_request = None
        self.last_names = ()

    def get(self, playback):
        request = tuple(getattr(playback, field, None) for field in ('title', 'artist', 'album', 'duration'))
        if request == self.last_request:return self.last_names
        self.last_request = request;self.last_names = ()
        title, artist, album, duration = request
        if not title:return ()
        track = dict(id='0' * 22, title=title, artists=[artist], album=album, duration=duration)
        key = recording_title_key(title)
        solo = _solo_title_parts(title)
        candidates = self.by_title.get(key, [])
        if solo:candidates = candidates + self.by_title.get(solo[0], [])
        album_key=_display_album_key(album)
        known_album=any(album_key and album_key in
                        {_display_album_key(value) for value in _song_albums(song)} for song in candidates)
        candidates=[song for song in candidates if self.context.title_matches(song,track)]
        matches=[song for song in candidates if album_key and album_key in
                 {_display_album_key(value) for value in _song_albums(song)}]
        if known_album and not matches:return ()
        album_confirmed=bool(matches)
        if not matches:matches=candidates
        if not matches:return ()
        wanted = self.context.details(matches[0]['artists'])
        if not all(self.context.same_artists(wanted,self.context.details(song['artists'])) for song in matches):
            if not album_confirmed:return ()
            # Length only helps separate casts on the same release; inaccurate
            # catalog lengths must not veto an otherwise unique display cast.
            length=_duration(duration)
            matches=[song for song in matches if not _duration(song.get('duration')) or
                     length and abs(_duration(song['duration'])-length)<=2]
            if not matches:return ()
            wanted=self.context.details(matches[0]['artists'])
            if not all(self.context.same_artists(wanted,self.context.details(song['artists'])) for song in matches):return ()
        if not album_confirmed:
            credits=self.context.expanded(wanted[0])
            if len(credits)!=1:return ()
            person=self.context.lookup.get(next(iter(credits)))
            if person and person.get('kind')=='group':return ()
        actual=self.context.details(track['artists'])
        if any(wanted[1][person]!=actual[1][person] for person in wanted[1].keys() & actual[1].keys()):return ()
        # Drop redundant actor credits but retain any explicit historical CV.
        names = tuple(name for name in matches[0]['artists']
                      if self.context.details([name])[0] & wanted[0])
        self.last_names = names
        return names
