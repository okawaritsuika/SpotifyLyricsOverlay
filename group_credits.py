"""Small, offline group rosters used only to supplement performer displays.

These facts describe group membership, never the recording's singer credits.
The playback path reads this compact snapshot rather than the song dictionary.
"""
from functools import lru_cache
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from app_paths import CATALOG_RELEASE_STAMP, catalog_snapshot_time
from artist_colors import _artist_ranges
from artist_credit_overrides import YUKI_AIRA
from artist_names import character_name_only, name_key
from work_reuse import is_brilliant_stars_album


RESOURCE_PATH = Path(__file__).resolve().parent / 'resources' / 'group_credits.json'
MAX_BYTES = 4 * 1024 * 1024
_CONTEXT = ('idolm@ster', 'idolmaster', 'アイドルマスター', '初星学園',
            'shiny colors', 'million live', 'cinderella girls', 'sidem')
_CV = re.compile(r'\s*[（(]CV\s*[.:：]?\s*([^\s.:：()（）][^()（）]*)[)）]\s*$', re.I)
_PROJECT_NAMES = frozenset(name_key(name) for name in
                          ('初星学園', '하츠보시 학원', 'Hatsuboshi Gakuen', 'Hatsuboshi Academy'))


def _strings(values):
    return list(dict.fromkeys(value.strip() for value in values
                             if isinstance(value, str) and value.strip()))


def _names(person):
    aliases = person.get('aliases', [])
    return _strings([person.get('name'), person.get('ko'), person.get('display')]
                    + (aliases if isinstance(aliases, list) else []))


def is_project_credit(person):
    """An affiliation roster must never expand a recording's artist display.

    Known names also protect older saved snapshots without ``credit_scope``.
    """
    return (person.get('credit_scope') == 'project'
            or any(name_key(name) in _PROJECT_NAMES for name in _names(person)))


def _url(value):
    if not isinstance(value, str) or len(value) > 2048:return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username
    except ValueError:return False


def _sources(person, fields):
    return _strings(source for field in fields for source in person.get(field, []) if _url(source))


def _valid_text(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 1000
            and not any(char in value for char in '\0\r\n'))


def _valid_strings(value, limit=10000):
    return isinstance(value, list) and len(value) <= limit and all(_valid_text(item) for item in value)


def valid_snapshot(snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != 2:
        return False
    groups, members = snapshot.get('groups'), snapshot.get('members')
    if not isinstance(groups, list) or len(groups) > 5000:return False
    if not isinstance(members, list) or len(members) > 10000:return False
    if not all(_valid_strings(snapshot.get(field, []))
               for field in ('ambiguous_aliases', 'ambiguous_idol_aliases')):return False
    for person in groups + members:
        if (not isinstance(person, dict) or not all(_valid_text(person.get(field))
                for field in ('id', 'name', 'display')) or not _valid_strings(person.get('aliases'), 1000)
                or not isinstance(person.get('source_urls'), list)
                or len(person['source_urls']) > 1000 or not all(_url(url) for url in person['source_urls'])):
            return False
    member_ids = {person['id'] for person in members}
    if len(member_ids) != len(members):return False
    if len({person['id'] for person in groups}) != len(groups):return False
    for group in groups:
        if (not _valid_strings(group.get('members')) or not group['members']
                or any(identity not in member_ids for identity in group['members'])
                or group.get('credit_scope', 'ensemble') not in ('ensemble', 'project')):return False
    for person in members:
        if person.get('member_status') not in ('confirmed', 'unconfirmed', 'ambiguous', 'cycle'):return False
        voices = person.get('voice_actors')
        if not isinstance(voices, list) or len(voices) > 30:return False
        for voice in voices:
            if (not isinstance(voice, dict) or not _valid_text(voice.get('name'))
                    or not _valid_text(voice.get('display'))):return False
    return True


class GroupCredits:
    def __init__(self, snapshot):
        self.generated_at = catalog_snapshot_time(snapshot)
        self.release_at = catalog_snapshot_time(snapshot, (CATALOG_RELEASE_STAMP,))
        self.groups = {group['id']: group for group in snapshot['groups']}
        self.members = {member['id']: member for member in snapshot['members']}
        choices = {}
        for kind, people in (('group', self.groups.values()), ('idol', self.members.values())):
            for person in people:
                if kind == 'idol' and person.get('member_status') != 'confirmed':continue
                for alias in _names(person):choices.setdefault(name_key(alias), set()).add((kind, person['id']))
        ambiguous = set(snapshot.get('ambiguous_aliases', []))
        self.names = {key: next(iter(owners)) for key, owners in choices.items()
                      if len(owners) == 1 and key not in ambiguous}
        self.known_names = set(choices) | ambiguous
        idol_choices = {}
        for person in self.members.values():
            if person.get('member_status') != 'confirmed':continue
            for alias in _names(person):idol_choices.setdefault(name_key(alias), set()).add(person['id'])
        ambiguous_idols = set(snapshot.get('ambiguous_idol_aliases', []))
        self.idol_names = {key: next(iter(owners)) for key, owners in idol_choices.items()
                           if len(owners) == 1 and key not in ambiguous_idols}
        voice_choices = {}
        for person in self.members.values():
            if person['member_status'] != 'confirmed':continue
            for voice in person['voice_actors']:
                identity = name_key(voice['name'])
                for name in (voice['name'], voice['display']):
                    voice_choices.setdefault(name_key(name), set()).add(identity)
        self.voice_names = {key: next(iter(owners)) for key, owners in voice_choices.items()
                            if len(owners) == 1}

    @lru_cache(maxsize=256)
    def with_voices(self, artists, album):
        context = unicodedata.normalize('NFKC', album).casefold()
        idolmaster = any(word in context for word in _CONTEXT)
        result = []
        for artist in artists:
            pieces, cursor = [], 0
            for start, end in _artist_ranges(artist, self.known_names):
                part = artist[start:end]
                pieces.append(artist[cursor:start])
                cursor = end
                normalized = unicodedata.normalize('NFKC', part)
                key = name_key(part)
                owner = self.names.get(key)
                short = len(key) == 1 or bool(re.fullmatch(r'[a-z0-9_.+-]{1,5}', key))
                # Existing actor parentheses, versions or ambiguous/group names
                # stay exactly as supplied. A roster never proves a song credit.
                if (owner is None or owner[0] != 'idol' or (short and not idolmaster)
                        or any(char in normalized for char in '()[]{}')):
                    pieces.append(part)
                    continue
                member = self.members[owner[1]]
                if character_name_only(member['name']):
                    pieces.append(part)
                    continue
                voices = member['voice_actors']
                choices = {name_key(voice['name']): voice['name'] for voice in voices}
                if len(choices) != 1:
                    pieces.append(part)
                    continue
                trimmed = part.rstrip()
                pieces.append(trimmed + ' (CV. ' + next(iter(choices.values())) + ')' + part[len(trimmed):])
            pieces.append(artist[cursor:])
            result.append(''.join(pieces))
        return tuple(result)

    @lru_cache(maxsize=256)
    def rosters(self, artists, album):
        context = unicodedata.normalize('NFKC', album).casefold()
        idolmaster = any(word in context for word in _CONTEXT)
        found_groups, explicit_members = [], set()
        # First inspect individual character credits without preserving group
        # aliases. A catalog may also name "Character A, Character B" as a
        # group, which must not hide the already-explicit character list.
        for artist in artists:
            for start, end in _artist_ranges(artist, self.idol_names):
                part = unicodedata.normalize('NFKC', artist[start:end].strip())
                identity = self.idol_names.get(name_key(_CV.sub('', part)))
                if identity is not None:explicit_members.add(identity)
        for artist in artists:
            for start, end in _artist_ranges(artist, self.known_names):
                part = artist[start:end].strip()
                key = name_key(part)
                normalized = unicodedata.normalize('NFKC', part)
                cv_credit = _CV.search(normalized)
                if cv_credit:key = name_key(normalized[:cv_credit.start()])
                short = len(key) == 1 or bool(re.fullmatch(r'[a-z0-9_.+-]{1,5}', key))
                if cv_credit:owner = ('idol', self.idol_names[key]) if key in self.idol_names else None
                else:owner = self.names.get(key)
                if owner is None:continue
                kind, identity = owner
                if kind == 'group' and short and not idolmaster:continue
                if kind == 'idol':explicit_members.add(identity)
                elif identity not in found_groups:found_groups.append(identity)
        # A subset of named singers must not be expanded to the whole unit.
        return tuple(self.groups[identity] for identity in found_groups
                     if not is_project_credit(self.groups[identity])
                     and not explicit_members.intersection(self.groups[identity]['members']))

    @lru_cache(maxsize=256)
    def lines(self, artists, album):
        result = []
        for group in self.rosters(artists, album):
            labels = []
            for member_id in dict.fromkeys(group['members']):
                member = self.members[member_id]
                if member['member_status'] != 'confirmed':
                    labels.append(member['display'] + ' (구성원 미확인)')
                    continue
                if character_name_only(member['name']):
                    labels.append(member['display'])
                    continue
                voices = ' / '.join(dict.fromkeys(voice['display'] for voice in member['voice_actors']))
                labels.append(member['display'] + ' (CV. ' + (voices or '성우 미확인') + ')')
            result.append(group['display'] + ' 멤버: ' + ', '.join(labels))
        return tuple(result)


@lru_cache(maxsize=8)
def _read_snapshot(path, modified, changed, size):
    try:
        with Path(path).open('rb') as stream:raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:return None
        snapshot = json.loads(raw)
        return GroupCredits(snapshot) if valid_snapshot(snapshot) else None
    except (OSError, ValueError, TypeError, RecursionError):return None


def _catalog(path):
    try:
        path = Path(path)
        status = path.stat()
        if status.st_size > MAX_BYTES:return None
        return _read_snapshot(str(path), status.st_mtime_ns, status.st_ctime_ns, status.st_size)
    except (OSError, ValueError, TypeError):return None


def _preferred_catalog(update_path):
    update = _catalog(update_path) if update_path is not None else None
    if update is not None and update.release_at is None:
        return update
    base = _catalog(RESOURCE_PATH)
    if update is None or (base is not None and base.generated_at is not None and base.generated_at > update.release_at):
        return base
    return update


def group_member_lines(artists, album='', update_path=None):
    """Return display-only roster sections; original artist values stay intact."""
    if isinstance(artists, str):artists = (artists,)
    if not isinstance(artists, (list, tuple)):return ()
    names = tuple(value for value in artists if isinstance(value, str) and value.strip())
    if not names:return ()
    catalog = _preferred_catalog(update_path)
    return catalog.lines(names, album if isinstance(album, str) else '') if catalog else ()


@lru_cache(maxsize=256)
def _performer_options(catalog, artists, album):
    names = {}
    character_voices, voices = {}, {}
    has_group = False
    known = catalog.known_names if catalog else ()
    voice_names = catalog.voice_names if catalog else {}
    for artist in artists:
        for start, end in _artist_ranges(artist, known):
            name = artist[start:end].strip()
            if not name:continue
            plain = unicodedata.normalize('NFKC', name)
            cv = _CV.search(plain)
            key = name_key(plain[:cv.start()] if cv else plain)
            owner = catalog.names.get(key) if catalog else None
            if key in _PROJECT_NAMES:continue
            if owner and owner[0] == 'group':
                if is_project_credit(catalog.groups[owner[1]]):continue
                has_group = True
                # Some unit aliases are themselves lists of named characters.
                parts = [name[left:right].strip() for left, right in
                         _artist_ranges(name, catalog.idol_names)] if catalog else []
                for part in parts:
                    member = catalog.idol_names.get(name_key(_CV.sub('', unicodedata.normalize('NFKC', part))))
                    if member:names.setdefault(member, part)
                continue
            individual = catalog.idol_names.get(key) if catalog else None
            identity = individual or key
            names.setdefault(identity, name)
            if individual or cv:
                credited_voices = character_voices.setdefault(identity, set())
                if cv:
                    credit = cv[1].strip()
                    for left, right in _artist_ranges(credit, voice_names):
                        actor = name_key(credit[left:right])
                        if actor:credited_voices.add(voice_names.get(actor, actor))
                elif individual:
                    credited_voices.update(name_key(voice['name'])
                                           for voice in catalog.members[individual]['voice_actors'])
            elif key in voice_names or (key == name_key(YUKI_AIRA) and is_brilliant_stars_album(album)):
                voices.setdefault(voice_names.get(key, key), name)
    if catalog:
        for group in catalog.rosters(artists, album):
            has_group = True
            for member in group['members']:
                names.setdefault(member, catalog.members[member]['name'])
    solo = ()
    if not has_group and len(character_voices) == 1:
        identity, credited_voices = next(iter(character_voices.items()))
        if len(credited_voices) == 1 and voices.keys() <= credited_voices:
            solo = (names[identity],)
    elif not has_group and not character_voices and len(voices) == 1:
        solo = tuple(voices.values())
    return tuple(names.values()), solo


def performer_options(artists, album='', update_path=None):
    """Return manual choices in header order and the voice-backed solo fallback."""
    catalog = _preferred_catalog(update_path)
    return _performer_options(catalog, tuple(artists), album)


def with_character_voices(artists, album='', update_path=None):
    """Copy artist display strings, adding only a unique explicitly known CV.

    Call before artist_label for Korean display; never store this as recording
    identity or use it to change Spotify searches or timing/provider matching.
    """
    if isinstance(artists, str):artists = (artists,)
    if not isinstance(artists, (list, tuple)):return ()
    names = tuple(value for value in artists if isinstance(value, str))
    if not any(value.strip() for value in names):return names
    catalog = _preferred_catalog(update_path)
    return catalog.with_voices(names, album if isinstance(album, str) else '') if catalog else names


def save_group_credits(people, path):
    """Derive a public-facts snapshot when the dictionary is explicitly loaded."""
    from explorer_library import build_performer_lookup, resolve_group_members
    from storage import atomic_json

    people = list(people)
    idol_lookup = build_performer_lookup(people, kind='idol')
    group_lookup = build_performer_lookup(people, kind='group')
    choices, idol_choices = {}, {}
    for person in people:
        for alias in _names(person):
            choices.setdefault(name_key(alias), set()).add(person['id'])
            if person.get('kind') == 'idol':idol_choices.setdefault(name_key(alias), set()).add(person['id'])
    groups, members = [], {}

    def remember_member(member, identity, confirmed):
        voices = [{'name': voice['name'], 'display': voice.get('display') or voice['name']}
                  for voice in member.get('voice_actors', []) if isinstance(voice, dict) and voice.get('name')]
        members[identity] = {'id': identity, 'name': member['name'],
            'display': member.get('display') or member['name'], 'aliases': _names(member),
            'member_status': 'confirmed' if confirmed else member.get('member_status', 'unconfirmed'),
            'voice_actors': voices, 'source_urls': _sources(member, ('source_urls', 'voice_actor_sources'))}

    # Individual credits also need CV facts for idols with no group membership.
    for person in people:
        if person.get('kind') == 'idol':remember_member(person, person['id'], True)
    for group in people:
        if group.get('kind') != 'group':continue
        resolved = resolve_group_members(group, people, idol_lookup=idol_lookup, group_lookup=group_lookup)
        if not resolved:continue
        member_ids = []
        for member in resolved:
            confirmed = member.get('kind') == 'idol'
            identity = member['id'] if confirmed else 'unknown:' + hashlib.sha256(
                (group['id'] + '|' + member['name']).encode('utf-8')).hexdigest()[:24]
            remember_member(member, identity, confirmed)
            member_ids.append(identity)
        groups.append({'id': group['id'], 'name': group['name'],
            'display': group.get('display') or group['name'], 'aliases': _names(group),
            'members': list(dict.fromkeys(member_ids)),
            'source_urls': _sources(group, ('source_urls', 'membership_sources'))})
        if is_project_credit(group):groups[-1]['credit_scope'] = 'project'
    snapshot = {'schema_version': 2, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'note': 'Display-only group rosters and individual character voices, not recording-specific singing credits. Sources are public membership and voice-actor facts.',
        'groups': groups, 'members': list(members.values()),
        'ambiguous_aliases': sorted(key for key, owners in choices.items() if len(owners) > 1),
        'ambiguous_idol_aliases': sorted(key for key, owners in idol_choices.items() if len(owners) > 1)}
    if not valid_snapshot(snapshot):raise ValueError('그룹 구성원 표시 자료가 올바르지 않습니다.')
    atomic_json(path, snapshot)
    return len(groups)
