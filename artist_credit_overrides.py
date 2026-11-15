"""Reviewed singer display credits for specific Spotify recordings."""
import json
from pathlib import Path
from artist_metadata import _text,playback_request,validated_track
from spotify_api import select_track
from work_reuse import reviewed_arrangement


YUKI_AIRA = '結城アイラ'
_YUKI_AIRA_ORCHESTRA_CAST = {
    'FairyTaleじゃいられない': (YUKI_AIRA,),
    '夜空を煌めく星のように': (YUKI_AIRA,),
    'Because': (YUKI_AIRA, '桜庭 薫 (CV.内田雄馬)'),
}


def orchestra_singer_credits(playback):
    """Use singer credits for the three reviewed concert performances by Yuki Aira."""
    arrangement = reviewed_arrangement(playback.title, playback.album)
    return _YUKI_AIRA_ORCHESTRA_CAST.get(arrangement['title'], ()) if arrangement else ()


class ArtistCreditOverrides:
    def __init__(self,directory):
        self.path=Path(directory)/'artist_credit_overrides.json'
        self.stamp=None;self.entries={}

    def get(self,playback):
        try:
            info=self.path.stat();stamp=(info.st_mtime_ns,info.st_size)
        except OSError:return ()
        if stamp!=self.stamp:
            self.stamp=stamp;self.entries={}
            if info.st_size>65536:return ()
            try:value=json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError,ValueError):return ()
            if not isinstance(value,dict) or value.get('version')!=1:return ()
            entries=value.get('entries')
            if not isinstance(entries,list) or len(entries)>128:return ()
            for entry in entries:
                if not isinstance(entry,dict) or entry.get('evidence_kind')!='user_confirmed_audio_and_credits':continue
                track=entry.get('track');names=entry.get('artists')
                if (not isinstance(track,dict) or not isinstance(track.get('artists'),list)
                        or not track['artists'] or not isinstance(names,list) or not 0<len(names)<=200
                        or any(not _text(name,True) or len(name)>512 for name in names)):continue
                request={key:track.get(key) for key in ('title','album','duration')}
                request['artist']=track['artists'][0]
                original=validated_track(request,track)
                if original:
                    if original['id'] in self.entries:self.entries={};return ()
                    self.entries[original['id']]={'track':original,'artists':tuple(names)}
        request=playback_request(playback)
        if not request.get('album'):return ()
        track=select_track(request,[entry['track'] for entry in self.entries.values()])
        return self.entries[track['id']]['artists'] if track and validated_track(request,track) else ()
