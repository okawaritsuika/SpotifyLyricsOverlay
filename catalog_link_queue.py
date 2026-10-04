"""Resumable dictionary search order; all library writes belong to the UI thread."""
import math
import time
from pathlib import Path

from catalog_autolink import cached_pool,local_matches
from explorer_library import search_key
from storage import atomic_json,read_json


class CatalogLinkQueue:
    def __init__(self,directory,songs,library,people=()):
        self.path=Path(directory)/'spotify_catalog_checks.json'
        value=read_json(self.path,{})
        self.checked=value.get('checked',{}) if isinstance(value,dict) else {}
        if not isinstance(self.checked,dict):self.checked={}
        self.library=library;self.people=people;self.update(songs)
        self.pool=cached_pool(directory)

    def update(self,songs):
        self.songs=list(songs)
        self.groups={}
        order={'song':0,'unknown':1,'instrumental':2,'spoken':3}
        for song in sorted(self.songs,key=lambda item:order.get(item.get('kind',item.get('type')),1)):
            self.groups.setdefault(search_key(song['title']),[]).append(song)

    def checked_at(self,key):
        stamp=self.checked.get(key)
        if type(stamp) not in (int,float) or stamp<=0:return 0
        try:return stamp if math.isfinite(stamp) else 0
        except OverflowError:return 0

    def coverage(self):
        """Count current rows and normalized title groups separately.

        A title can have several recordings, only some of which are linked.
        Saved searches record attempts, not proof that Spotify lacks a song.
        Old dictionary keys and malformed timestamps do not count as attempts.
        """
        linked={song['id'] for song in self.songs if self.library.linked_track(song['id'])}
        exclusions=set(self.library.value.get('automatic_link_exclusions',[]))
        checked={key for key in self.groups if self.checked_at(key)}
        linked_titles=set();fully_linked=set();pending=set();opted_out_titles=set()
        eligible_linked=eligible_count=opted_out=0
        for key,group in self.groups.items():
            eligible_count+=len(group)
            linked_count=sum(song['id'] in linked for song in group)
            eligible_linked+=linked_count
            if linked_count:linked_titles.add(key)
            if linked_count==len(group):fully_linked.add(key)
            if any(song['id'] not in linked and song['id'] not in exclusions for song in group):
                pending.add(key)
            group_opted_out=sum(song['id'] in exclusions for song in group)
            opted_out+=group_opted_out
            if group_opted_out:opted_out_titles.add(key)
        return {'total_recordings':len(self.songs),
                'total_title_groups':len({search_key(song['title']) for song in self.songs}),
                'eligible_recordings':eligible_count,
                'excluded_recordings':len(self.songs)-eligible_count,
                'title_groups':len(self.groups),
                'linked_recordings':sum(song['id'] in linked for song in self.songs),
                'linked_all_titles':len({search_key(song['title']) for song in self.songs if song['id'] in linked}),
                'linked_eligible_recordings':eligible_linked,'linked_titles':len(linked_titles),
                'fully_linked_titles':len(fully_linked),'checked_titles':len(checked),
                'unchecked_titles':len(self.groups)-len(checked),
                'checked_unresolved_titles':len(checked & pending),'pending_titles':len(pending),
                'opted_out_recordings':opted_out,'opted_out_titles':len(opted_out_titles)}

    def status_text(self):
        """Describe observed coverage without implying the queue proves absence."""
        value=self.coverage()
        text=(f"전체 {value['total_recordings']:,}음원 · 연결 {value['linked_recordings']:,}음원"
              f"/{value['linked_all_titles']:,}곡 · 자동검색 대상 {value['title_groups']:,}곡 중 "
              f"{value['checked_titles']:,}곡 시도 · 미검색 {value['unchecked_titles']:,}곡"
              f" · 시도 후 미확정 {value['checked_unresolved_titles']:,}곡")
        if value['excluded_recordings']:
            text+=f" · 대사·연주 제외 {value['excluded_recordings']:,}음원"
        if value['opted_out_recordings']:
            text+=f" · 자동 연결 해제 {value['opted_out_recordings']:,}음원"
        return text

    def seed(self):
        matches=local_matches([song for group in self.groups.values() for song in group],self.pool,self.people)
        return self.library.link_automatic_many(matches)

    def next_song(self,preferred=(),skip=()):
        now=time.time();seen=set()
        # Keep explicit selections first, then search unseen titles before retries.
        keys=sorted(self.groups,key=lambda key:bool(self.checked_at(key)))
        for song in [*preferred,*(self.groups[key][0] for key in keys)]:
            if not song:continue
            key=search_key(song['title'])
            if key in skip:continue
            if key in seen:continue
            seen.add(key)
            checked=self.checked_at(key)
            if checked and 0<=now-checked<86400:continue
            pending=[item for item in self.groups.get(key,()) if not self.library.linked_track(item['id'])
                     and item['id'] not in self.library.value.get('automatic_link_exclusions',[])]
            if pending:return song
        return None

    def apply(self,song,tracks):
        key=search_key(song['title'])
        pool={item['id']:item for item in [*self.pool,*tracks] if isinstance(item,dict) and item.get('id')}
        self.pool=list(pool.values())
        matches=local_matches(self.groups.get(key,()),self.pool,self.people)
        linked=self.library.link_automatic_many(matches)
        self.checked[key]=time.time()
        atomic_json(self.path,{'schema_version':1,'checked':self.checked})
        return linked
