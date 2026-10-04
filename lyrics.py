"""Read three-line lyric documents without changing the source files."""
from dataclasses import asdict, dataclass
from functools import cached_property
import hashlib
from pathlib import Path
import re
import unicodedata


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if c.isalnum())


@dataclass(frozen=True)
class Line:
    original: str
    reading: str
    korean: str


@dataclass(frozen=True)
class Song:
    id: str
    title: str
    artist: str
    album: str
    lines: tuple[Line, ...]

    @cached_property
    def fingerprint(self):
        # Do not reuse timestamps when the text or line grouping changes.
        return hashlib.sha256(repr(self.lines).encode('utf-8')).hexdigest()

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(value['id'], value['title'], value['artist'], value['album'],
                   tuple(Line(**line) for line in value['lines']))


def parse_document(text, filename='lyrics.txt'):
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    album_match = re.search(r'^HOPEFUL FE@THERS\s*-(.+?)-\s*$', text, re.M)
    album = album_match.group(1) if album_match else Path(filename).stem.split('_')[0]
    parts = re.split(r'^\[\d+\]\s+(.+)$', text, flags=re.M)
    songs = []
    for index in range(1, len(parts), 2):
        heading, body = parts[index:index + 2]
        title, separator, artist = heading.rpartition(' - ')
        if not separator:
            title, artist = heading.strip(), ''
        # Navigation text before/after the lyrics is document data, not lyrics.
        marker = re.search(r'^유튜브 풀버전:.*$', body, re.M)
        if marker:
            body = body[marker.end():]
        body = re.split(r'^(?:이어서 보기:|앞부분 보기:|\[[^\]\n]+편 끝)', body,
                        maxsplit=1, flags=re.M)[0]
        lines = []
        for block in re.split(r'\n\s*\n', body.strip()):
            values = [line.strip() for line in block.splitlines() if line.strip()]
            values = [line for line in values if not set(line) <= set('━─- ')]
            if not values:
                continue
            if len(values) != 3:
                raise ValueError(f'{filename}: {heading} — {len(values)}줄 묶음이 있습니다. '
                                 '원문·독음·번역의 3줄 형식을 확인하세요.')
            lines.append(Line(*values))
        if not lines:
            raise ValueError(f'{filename}: {heading} — 가사가 비어 있습니다.')
        key = '\0'.join((normalized(album), normalized(title), normalized(artist)))
        song_id = hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
        songs.append(Song(song_id, title.strip(), artist.strip(), album, tuple(lines)))
    return songs


def import_folder(folder):
    songs = {}
    for path in sorted(Path(folder).glob('*.txt')):
        for song in parse_document(path.read_text(encoding='utf-8-sig'), path.name):
            if song.id in songs and songs[song.id] != song:
                raise ValueError(f'내용이 다른 중복 곡: {song.title}')
            songs[song.id] = song
    if not songs:
        raise ValueError('[01] 곡 제목으로 시작하는 3줄 형식의 TXT 가사를 찾지 못했습니다.')
    return list(songs.values())


def match_song(songs, title, album='', linked_id=None):
    if linked_id:
        return next((s for s in songs if s.id == linked_id), None)
    title_key = normalized(title)
    candidates = [s for s in songs if normalized(s.title) == title_key]
    if len(candidates) == 1:
        return candidates[0]
    # Never pick an arbitrary song when titles collide.
    album_key = normalized(album)
    if not album_key:
        return None
    exact = [s for s in candidates if normalized(s.album) == album_key]
    if exact:
        return exact[0] if len(exact) == 1 else None
    candidates = [s for s in candidates if normalized(s.album)
                  and normalized(s.album) in album_key]
    return candidates[0] if len(candidates) == 1 else None


def match_recording(songs, title, artist, album='', linked_id=None):
    """Reuse a saved clock only for identified recordings or explicit links."""
    if linked_id:
        return next((song for song in songs if song.id == linked_id), None)
    identity = tuple(normalized(value) for value in (title, artist, album))
    if not all(identity):return None
    candidates = [song for song in songs
                  if tuple(normalized(value) for value in (song.title, song.artist, song.album)) == identity]
    return candidates[0] if len(candidates) == 1 else None
