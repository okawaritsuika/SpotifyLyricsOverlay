"""Local evidence for sharing lyrics between credited performances of a work.

This is a lyric association, never authority for sharing a recording's clock.
"""
from functools import lru_cache
from pathlib import Path

from lyrics import normalized
from saved_annotations import _artist_identity


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
