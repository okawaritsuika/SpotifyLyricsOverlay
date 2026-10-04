"""Read public Spotify catalog metadata without retaining account or session data."""
import base64
import binascii
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import urlparse


_ID = r'[A-Za-z0-9]{22}'


class _Scripts(HTMLParser):
    def __init__(self, wanted):
        super().__init__(convert_charrefs=False)
        self.wanted = wanted
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':self.active = dict(attrs).get('id') == self.wanted

    def handle_endtag(self, tag):
        if tag == 'script':self.active = False

    def handle_data(self, data):
        if self.active:self.parts.append(data)


def _script_json(html, script_id, encoded=False):
    if not isinstance(html, str):return None
    parser = _Scripts(script_id)
    try:
        parser.feed(html)
        value = ''.join(parser.parts).strip()
        if encoded:value = base64.b64decode(value, validate=True).decode('utf-8')
        return json.loads(value)
    except (ValueError, UnicodeError, binascii.Error, RecursionError):return None


def _resource_id(value, kind):
    if not isinstance(value, str):return None
    match = re.fullmatch('spotify:' + kind + ':(' + _ID + ')', value)
    return match[1] if match else None


def _source_url(value):
    """Keep only a canonical public resource URL, excluding query and user data."""
    if not isinstance(value, str):return ''
    try:
        parsed = urlparse(value)
        if (parsed.scheme != 'https' or parsed.hostname != 'open.spotify.com'
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return ''
        match = re.fullmatch(r'/(?:intl-[a-z]{2}/)?(?:embed/)?(track|album|artist|playlist)/(' + _ID + r')/?', parsed.path)
        return 'https://open.spotify.com/' + '/'.join(match.groups()) if match else ''
    except ValueError:return ''


def _name(value):
    return value.strip() if isinstance(value, str) else ''


def _seconds(value):
    if isinstance(value, bool):return None
    try:milliseconds = float(value)
    except (TypeError, ValueError, OverflowError):return None
    return milliseconds / 1000 if math.isfinite(milliseconds) and milliseconds > 0 else None


def _artist_names(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list):return None
    items = value['items']
    count = value.get('totalCount')
    if count is not None and (type(count) is not int or count != len(items)):return None
    result = []
    for item in items:
        if not isinstance(item, dict):return None
        profile = item.get('profile')
        name = _name(profile.get('name')) if isinstance(profile, dict) else _name(item.get('name'))
        if not name:return None
        if name not in result:result.append(name)
    return result


def _album_name(value):
    if not isinstance(value, dict) or not _resource_id(value.get('uri'), 'album'):return ''
    return _name(value.get('name'))


def _track(value, album_context, source_url):
    track_id = _resource_id(value.get('uri'), 'track')
    if not track_id:return None
    if 'id' in value and value['id'] != track_id:return None
    title = _name(value.get('name'))
    album = _album_name(value.get('albumOfTrack')) or album_context
    duration = value.get('duration')
    duration = _seconds(duration.get('totalMilliseconds')) if isinstance(duration, dict) else None
    if 'firstArtist' in value or 'otherArtists' in value:
        first = _artist_names(value.get('firstArtist'))
        other = _artist_names(value.get('otherArtists'))
        artists = list(dict.fromkeys(first + other)) if first and other is not None else None
    else:artists = _artist_names(value.get('artists'))
    if not title or not album or duration is None or not artists:return None
    return {'id': track_id, 'title': title, 'artists': artists, 'album': album,
            'duration': duration, 'is_playable': None,
            'url': 'https://open.spotify.com/track/' + track_id, 'source_url': source_url}


def parse_public_state(state, source_url=''):
    """Extract complete public tracks from initialState entities only.

    An album is inherited solely along that album's tracks/items/track wrappers.
    Missing artists, album, duration, or malformed IDs are rejected. Public page
    metadata cannot establish account-specific playability.
    """
    if not isinstance(state, dict):return []
    entities = state.get('entities')
    if not isinstance(entities, dict):return []
    source_url = _source_url(source_url)
    tracks = {}

    def walk(value, album_context='', depth=0):
        if depth > 60:return
        if isinstance(value, dict):
            track = _track(value, album_context, source_url)
            if track:tracks.setdefault(track['id'], track)
            album = _album_name(value)
            for key, child in value.items():
                context = album if key in ('tracks', 'tracksV2') and album else ''
                if key in ('items', 'track', 'data'):context = album_context
                walk(child, context, depth + 1)
        elif isinstance(value, list):
            for child in value:walk(child, album_context, depth + 1)

    walk(entities.get('items'))
    return list(tracks.values())


def parse_public_tracks(html, source_url=''):
    """Parse a public Spotify page's base64 initialState catalog entities."""
    return parse_public_state(_script_json(html, 'initialState', encoded=True), source_url)


def parse_public_catalog(html, source_url=''):
    """Return verified tracks and public album/artist discovery references only.

    References do not prove that an album belongs to the caller's dictionary.
    The collector must restrict which references it follows. Nothing outside
    the public catalog entities is retained, including session/account fields.
    """
    state = _script_json(html, 'initialState', encoded=True)
    result = {'tracks': parse_public_state(state, source_url), 'albums': [], 'artists': []}
    if not isinstance(state, dict):return result
    entities = state.get('entities')
    if not isinstance(entities, dict):return result
    found = {'album': {}, 'artist': {}}

    def walk(value, depth=0):
        if depth > 60:return
        if isinstance(value, dict):
            for kind in found:
                identifier = _resource_id(value.get('uri'), kind)
                profile = value.get('profile')
                name = _name(profile.get('name')) if kind == 'artist' and isinstance(profile, dict) else _name(value.get('name'))
                if identifier and name:
                    item = found[kind].setdefault(identifier, {'id': identifier, 'name': name,
                        'url': 'https://open.spotify.com/' + kind + '/' + identifier})
                    collection = value.get('tracksV2') if kind == 'album' else None
                    if isinstance(collection, dict) and isinstance(collection.get('items'), list):
                        count = collection.get('totalCount')
                        if type(count) is int and count >= 0:
                            item['track_count'] = count
                            item['listed_tracks'] = len(collection['items'])
            for child in value.values():walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:walk(child, depth + 1)

    walk(entities.get('items'))
    result['albums'] = list(found['album'].values())
    result['artists'] = list(found['artist'].values())
    return result


def parse_embed_tracks(html):
    """Return discovery hints only: embed rows do not provide verified artists/album."""
    state = _script_json(html, '__NEXT_DATA__')
    if not isinstance(state, dict):return []
    props = state.get('props', {})
    page = props.get('pageProps', {}) if isinstance(props, dict) else {}
    state = page.get('state', {}) if isinstance(page, dict) else {}
    data = state.get('data', {}) if isinstance(state, dict) else {}
    entity = data.get('entity', {}) if isinstance(data, dict) else {}
    rows = entity.get('trackList', []) if isinstance(entity, dict) else []
    if not isinstance(rows, list):return []
    tracks = {}
    for row in rows:
        if not isinstance(row, dict):continue
        track_id = _resource_id(row.get('uri'), 'track')
        title = _name(row.get('title'))
        duration = _seconds(row.get('duration'))
        if track_id and title and duration is not None:
            tracks.setdefault(track_id, {'id': track_id, 'title': title, 'duration': duration})
    return list(tracks.values())
