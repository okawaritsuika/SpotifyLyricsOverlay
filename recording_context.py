"""Reject incompatible recording candidates without assigning saved timing owners."""
from dataclasses import dataclass
import hashlib
import math
import weakref


def known_duration(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):return None
    try:return value if math.isfinite(value) and value>0 else None
    except OverflowError:return None


def recording_key(title,artist,album):
    return hashlib.sha256('\0'.join((title,artist,album)).encode()).hexdigest()


@dataclass(eq=False)
class RecordingRequest:
    key:str
    duration:float|None
    generation:int
    song_id:str
    fingerprint:str
    invalidated:bool=False


class RecordingContexts:
    def __init__(self):
        self.observed={}
        self.requests=weakref.WeakSet()

    def observe(self,playback):
        duration=known_duration(playback.duration)
        if duration is None:return
        key=recording_key(playback.title,playback.artist,playback.album)
        anchor,_,generation=self.observed.get(key,(duration,duration,0))
        if abs(duration-anchor)>2:anchor=duration;generation+=1
        self.observed[key]=(anchor,duration,generation)
        # A request has its own fixed start length. It must not revive after a
        # gradual drift or a return to the original length between UI events.
        for request in self.requests:
            if request.key==key and request.duration is not None and abs(request.duration-duration)>2:
                request.invalidated=True

    def capture(self,song,playback=None):
        if playback is not None:
            self.observe(playback)
            key=recording_key(playback.title,playback.artist,playback.album)
            duration=known_duration(playback.duration)
        else:
            key=recording_key(song.title,song.artist,song.album);duration=None
        generation=self.observed.get(key,(None,None,0))[2]
        request=RecordingRequest(key,duration,generation,song.id,song.fingerprint)
        self.requests.add(request)
        return request

    def valid(self,request,song):
        return bool(isinstance(request,RecordingRequest) and not request.invalidated
                    and request.song_id==song.id and request.fingerprint==song.fingerprint
                    and request.generation==self.observed.get(request.key,(None,None,0))[2])

    def candidate_matches(self,song,candidate,playback=None):
        duration=known_duration(candidate.get('duration'))
        if duration is None:return True  # Unknown legacy candidates retain their policy.
        if playback is not None:
            current=known_duration(playback.duration)
            if current is not None and abs(current-duration)>2:return False
        key=candidate.get('recording_key') or recording_key(song.title,song.artist,song.album)
        if not isinstance(key,str):return False
        known=self.observed.get(key)
        return known is None or abs(known[1]-duration)<=2
