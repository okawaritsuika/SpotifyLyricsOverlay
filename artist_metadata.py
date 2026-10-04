"""Validated display-only Spotify credits, cached separately from lyric identity."""
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import unicodedata

from spotify_api import select_track, text_key


MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 512


def _text(value, required=False):
    return (isinstance(value, str) and len(value) <= 4000
            and not any(unicodedata.category(char) in ('Cc', 'Cf') for char in value)
            and (not required or bool(value.strip())))


def request_identity(request):
    if not isinstance(request, dict):return None
    values = tuple(request.get(name) for name in ('title', 'artist', 'album'))
    duration = request.get('duration')
    if (any(not _text(value, index < 2) for index, value in enumerate(values))
            or isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 0 < duration <= 604800 or not math.isfinite(duration)):return None
    return '\0'.join(values), float(duration)


def playback_request(playback):
    return {name: getattr(playback, name, None) for name in ('title', 'artist', 'album', 'duration')}


def validated_track(request, value):
    """Retain only credits from a complete, matching recording, never account data."""
    if request_identity(request) is None or not isinstance(value, dict):return None
    track_id = value.get('id')
    artists = value.get('artists')
    duration = value.get('duration')
    if (not isinstance(track_id, str) or not re.fullmatch(r'[A-Za-z0-9]{22}', track_id)
            or not _text(value.get('title'), True) or not _text(value.get('album'))
            or not isinstance(artists, list) or not 0 < len(artists) <= 200
            or any(not _text(name, True) or len(name) > 512 for name in artists)
            or sum(len(name) for name in artists) > 32000
            or isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 0 < duration <= 604800 or not math.isfinite(duration)):return None
    track = {name: value[name] for name in ('id', 'title', 'album', 'duration')}
    track['artists'] = list(dict.fromkeys(name.strip() for name in artists))
    # Display credits retain the same exact album boundary as lyric timing.
    if request['album'] and text_key(request['album']) != text_key(track['album']):return None
    return track if select_track(request, [track]) is not None else None


class ArtistMetadata:
    def __init__(self, directory):
        self.path = Path(directory) / 'artist_metadata.json'
        self.entries = OrderedDict()
        self._load()

    def _safe(self):
        for path in (self.path, *self.path.parents):
            try:info = path.lstat()
            except FileNotFoundError:continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:return False
        return True

    def _load(self):
        try:
            if not self._safe():return
            with self.path.open('rb') as stream:data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:return
            value = json.loads(data)
            if not isinstance(value, dict) or value.get('version') != 1:return
            entries = value.get('entries')
            if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:return
            for item in entries:
                if not isinstance(item, dict):continue
                request = item.get('request')
                identity = request_identity(request)
                track = validated_track(request, item.get('track'))
                if identity and track:
                    request = {name: request[name] for name in ('title', 'artist', 'album', 'duration')}
                    self.entries[identity] = {'request': request, 'track': track}
        except (OSError, ValueError, TypeError, RecursionError):return

    def get(self, playback):
        identity = request_identity(playback_request(playback))
        entry = self.entries.get(identity)
        return tuple(entry['track']['artists']) if entry else ()

    def put(self, playback, value):
        request = playback_request(playback)
        identity = request_identity(request)
        track = validated_track(request, value)
        if identity is None or track is None:return False
        self.entries[identity] = {'request': request, 'track': track}
        self.entries.move_to_end(identity)
        while len(self.entries) > MAX_ENTRIES:self.entries.popitem(last=False)
        self._save()
        return True

    def _save(self):
        temporary = None
        try:
            if not self._safe():return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                data = json.dumps({'version': 1, 'entries': list(self.entries.values())},
                                  ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                if len(data) <= MAX_BYTES:break
                self.entries.popitem(last=False)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='artist_metadata-', suffix='.tmp',
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            if self._safe():os.replace(temporary, self.path)
        except (OSError, ValueError):pass
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
