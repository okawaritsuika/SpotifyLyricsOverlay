"""Transient browser lyrics for the current track; no persistence operations."""
from lyrics import Line,Song,normalized
from lyric_annotations import match_annotations


class LiveLyrics:
    def __init__(self,timing_store=None):self.timing_store=timing_store;self.clear()
    def clear(self):
        self.track='';self.song=None;self.lines=();self.timing=None
        self.completion_base='';self.completion_indices=();self.completion_count=0

    def complete_saved(self,playback,base,lines,timing):
        from lyric_completion import complete_lines,expand_timing
        import hashlib
        if self.track==playback.key and self.completion_base==base.id:return None
        completed=complete_lines(self.display(base,base.lines),lines)
        if completed is None:return None
        display,indices=completed
        song=Song('completed-'+hashlib.sha256(playback.key.encode()).hexdigest()[:24],
                  playback.title,playback.artist,playback.album,display)
        restored=self.timing_store.load(song) if self.timing_store else None
        self.track,self.song,self.lines=playback.key,song,display
        self.timing=restored or expand_timing(timing,indices)
        self.completion_base=base.id;self.completion_indices=indices
        self.completion_count=sum(i is None for i in indices)
        return song

    def receive(self,playback,base,lines):
        if base and self.track==playback.key and self.completion_base==base.id:
            matches=match_annotations(self.lines,lines)
            display=tuple(Line(old.original,extra.reading or old.reading,extra.korean or old.korean) if extra else old
                          for old,extra in zip(self.lines,matches))
            self.song=Song(self.song.id,self.song.title,self.song.artist,self.song.album,display)
            self.lines=display
            return self.song
        if self.track==playback.key and self.lines:
            previous=match_annotations(lines,self.lines)
            lines=tuple(Line(line.original,line.reading or (old.reading if old else ''),
                             line.korean or (old.korean if old else '')) for line,old in zip(lines,previous))
        if base:
            matches=match_annotations(base.lines,lines)
            if not any(matches):raise ValueError('현재 일본어 가사와 일치하는 구절이 없습니다.')
            originals=self.lines if self.track==playback.key and self.song==base else base.lines
            display=tuple(Line(old.original,extra.reading or old.reading,extra.korean or old.korean) if extra else old
                          for old,extra in zip(originals,matches))
            song=base;timing=None
        else:
            import hashlib
            song=Song('memory-'+hashlib.sha256(playback.key.encode()).hexdigest()[:24],playback.title,playback.artist,playback.album,tuple(lines))
            display=song.lines
            timing={'times':[None]*len(lines),'offset':0.,'source':'확장 실시간 가사 · 메모리 전용'}
            if self.timing_store:timing=self.timing_store.load(song) or timing
            if self.is_temporary(self.song) and self.song.id==song.id:
                if tuple(normalized(x.original) for x in self.song.lines)==tuple(normalized(x.original) for x in song.lines):
                    timing=self.timing
                else:
                    from lyric_follow import native_line_mapping
                    from lyric_completion import expand_timing
                    from timing_quality import timing_origins
                    mapping,_=native_line_mapping([line.original for line in self.song.lines],song.lines,
                                                  onsets_only=True)
                    indices=[None]*len(song.lines)
                    for old,new in sorted(mapping.items()):
                        if indices[new] is None:indices[new]=old
                    if any(origin=='manual' and index not in indices
                           for index,origin in enumerate(timing_origins(self.timing))):
                        # A merged, omitted or shifted row cannot erase a user's
                        # onset. Keep that grouping and only fill annotations.
                        matches=match_annotations(self.lines,lines)
                        display=tuple(Line(old.original,old.reading or extra.reading,old.korean or extra.korean)
                                      if extra else old for old,extra in zip(self.lines,matches))
                        song=Song(self.song.id,self.song.title,self.song.artist,self.song.album,display)
                        timing=self.timing
                    else:timing=expand_timing(self.timing,indices)
        self.track,self.song,self.lines,self.timing=playback.key,song,display,timing
        return song

    def is_temporary(self,song):return bool(song and self.song and self.timing is not None and song.id==self.song.id)

    def display(self,song,fallback):
        return self.lines if song and self.song and song.id==self.song.id else fallback
