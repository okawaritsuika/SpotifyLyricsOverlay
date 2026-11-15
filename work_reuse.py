"""Local evidence for sharing lyrics between credited performances of a work.

This is a lyric association, never authority for sharing a recording's clock.
"""
from functools import lru_cache
from pathlib import Path
import re
import unicodedata

from lyrics import normalized
from saved_annotations import _artist_identity


ANNIVERSARY_WORKS = (
    'Brand New Theater!', 'UNION!!', 'Flyers!!!', 'Glow Map', 'Harmony 4 You',
    '夢にかけるRainbow', 'Crossing!', 'グッドサイン', '7Days A Week!!',
)
_ANNIVERSARY_TITLES = {normalized(title): title for title in ANNIVERSARY_WORKS}
_MILLION_ALBUM = re.compile(r'\bTHE\s+IDOLM@STER\s+(?:765\s+)?MILLION\b', re.I)


def anniversary_work(playback):
    """Recognize the full-length vocal versions of the selected Million works."""
    from recording_reuse import title_identity

    title, artist, album = (getattr(playback, field, '') for field in ('title', 'artist', 'album'))
    if any(not isinstance(value, str) for value in (title, artist, album)):return ''
    key, edit = title_identity(title)
    canonical = _ANNIVERSARY_TITLES.get(key)
    if not canonical:return ''
    performer = _artist_identity(artist)
    if not performer:return ''
    if edit:
        if not edit.startswith('vocal:'):return ''
        for credit in edit.removeprefix('vocal:').split(':'):
            if credit not in ('solo', 'ソロ') and _artist_identity(credit) != performer:return ''
    known = _index().get((key, performer), set())
    if known:
        works = {work for work, series, _official in known if series == 'million'}
        return canonical if works == {key} else ''
    return canonical if _MILLION_ALBUM.search(unicodedata.normalize('NFKC', album)) else ''


_ORCHESTRA_ALBUM = ('THE IDOLM@STER 20th anniversary ORCHESTRA CONCERT '
                    'SYMPHONY OF BRILLIANT STARS コンサートアルバム')
# The official album credits explicitly name singers for these performances:
# https://shop.asobistore.jp/products/detail/223858-00-00-00
# An orchestra album, a composer credit, or a generic Live suffix is not enough.
_ORCHESTRA_VOCALS = {
    ('Day', '恋花'): {'765', 'million'},
    ('Day', 'ARRIVE TO STAR'): {'sidem'},
    ('Day', 'Wonder Scale'): {'gakuen'},
    ('Day', 'FairyTaleじゃいられない'): {'million'},
    ('Day', '夜空を煌めく星のように'): {'sidem'},
    ('Day', 'Because'): {'sidem'},
    ('Night', 'statice'): {'shiny'},
    ('Night', 'ホントウノワタシ'): {'million'},
    ('Night', 'ささのはに、うたかたに。'): {'cinderella'},
    ('Night', '小さな野望'): {'gakuen'},
    ('Night', '細氷'): {'765'},
    ('Night', 'あらかねの器'): {'cinderella'},
}


def is_brilliant_stars_album(album):
    return isinstance(album, str) and normalized(album) == normalized(_ORCHESTRA_ALBUM)


def reviewed_arrangement(title, album):
    """Identify a reviewed vocal work without changing the recording identity."""
    if not is_brilliant_stars_album(album):return None
    match = re.fullmatch(r'(.+?)\s*\((Day|Night) 第2幕「BRILLIANT STAGE」より\) - Live',
                         unicodedata.normalize('NFKC', title).strip())
    if not match:return None
    for (performance, work), series in _ORCHESTRA_VOCALS.items():
        if match[2] == performance and normalized(match[1]) == normalized(work):
            return {'title': work, 'series': frozenset(series)}
    return None


def arrangement_text_matches(source, playback):
    """Require the official performance and a known original performer/work."""
    reviewed = reviewed_arrangement(playback.title, playback.album)
    if not reviewed or work_title(source['title']) != normalized(reviewed['title']):return False
    known = _index().get((work_title(source['title']), _artist_identity(source['artist'])), set())
    return any(work == normalized(reviewed['title']) and series in reviewed['series']
               for work, series, _official in known)


def work_title(title):
    # Use identical label handling in cache lookup and catalog evidence.
    # A title match still needs both performers' known records below.
    from recording_reuse import title_identity
    return title_identity(title)[0]


@lru_cache(maxsize=1)
def _index():
    from song_catalog import read_catalog
    from live_catalog import _reviewed_work_aliases, _song_work_key
    resources=Path(__file__).resolve().parent/'resources'
    value=read_catalog(resources/'song_catalog.json')
    if value is None:return {}
    aliases=_reviewed_work_aliases();index={}
    def add(title,artists,work,series,official=False):
        title=work_title(title)
        if not title or not work or not series:return
        for credit in (*artists,', '.join(artists)):
            artist=_artist_identity(credit)
            if artist:index.setdefault((title,artist),set()).update((work,item,official) for item in series)
    for song in value['songs']:
        if song.get('kind')!='song':continue
        series=set(song.get('series',()))-{'other','unknown'}
        work=normalized(_song_work_key(song,aliases))
        # Apply the same explicit version-label handling on catalog and request.
        if song.get('id','').removeprefix('mb:') not in aliases:work=work_title(song['title'])
        add(song['title'],song['artists'],work,series)
    for alias in aliases.values():
        # These bundled mappings include recording credits and primary source
        # URLs. Unlike general search aliases, they explicitly name the work.
        add(alias['recording_title'],alias['artists'],work_title(alias['work_title']),
            set(alias['series']),True)
    return index


def same_work(source,playback):
    """Require known performances of one unambiguous work and shared series."""
    index=_index()
    left=index.get((work_title(source['title']),_artist_identity(source['artist'])),set())
    right=index.get((work_title(playback.title),_artist_identity(playback.artist)),set())
    shared={a[0] for a in left for b in right
            if a[0]==b[0] and (a[1]==b[1] or a[2] and b[2])}
    return len(shared)==1
