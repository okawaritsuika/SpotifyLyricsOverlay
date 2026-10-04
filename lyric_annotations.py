"""Cache supplied translations against existing lines without changing timing data."""
from dataclasses import asdict
from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata

from lyrics import Line,normalized
from lyric_match import match_lyrics
from storage import atomic_json,read_json


@lru_cache(maxsize=4096)
def _local_reading(original):
    from web_lyrics import reading
    value=reading(original)
    # Exceptions are not cached by lru_cache, so a converter that becomes
    # available later can recover without restarting or rewriting lyric data.
    if not isinstance(value,str) or not value.strip():raise ValueError('empty local reading')
    return value.strip()


def readings_for_display(lines):
    """Fill missing Japanese readings for display without changing any Line.

    A split sentence cannot reuse its source's complete reading. Read each
    current phrase with the existing word-based converter instead. Supplied
    readings always win, including when they arrive after a local fallback.
    """
    from web_lyrics import JA
    result=[];failed=set()
    for line in lines:
        value=line.reading
        if not value.strip() and JA.search(line.original) and line.original not in failed:
            try:value=_local_reading(line.original)
            except Exception:failed.add(line.original)
        result.append(value)
    return tuple(result)


def match_annotations(originals,supplied):
    keys=[normalized(line.original) for line in originals]
    other=[normalized(line.original) for line in supplied]
    if keys==other:
        return [line if key else None for key,line in zip(keys,supplied)]
    candidates={}
    for key,line in zip(other,supplied):
        if key:candidates.setdefault(key,set()).add((line.reading,line.korean))
    result=[]
    for key in keys:
        choices=candidates.get(key,set())
        # Repeated phrases with different translations need full sequence agreement.
        result.append(Line('',*next(iter(choices))) if len(choices)==1 else None)
    sentences=match_verified_annotations(originals,supplied)
    # Once the sequence is verified, an unmatched or conflicting position must
    # not borrow the same text's translation from elsewhere in the document.
    return sentences if sentences is not None else result


def _project_annotations(target,supplied,source_positions,target_positions,alignment):
    """Project the smallest complete, exact block between shared boundaries."""
    starts={start:index for index,(row,start,end) in enumerate(target_positions)}
    ends={end:index+1 for index,(row,start,end) in enumerate(target_positions)}
    result=[None]*len(target)
    for position,(index,start,end) in enumerate(source_positions):
        first=starts.get(alignment.source_to_target[start])
        if first is None:continue
        for last in range(position,min(len(source_positions),position+4)):
            other,begin,stop=source_positions[last]
            if last>position and (other!=source_positions[last-1][0]+1 or
                                  begin!=source_positions[last-1][2]):break
            end=ends.get(alignment.source_to_target[stop])
            if end is None:continue
            if end<=first:break
            segment=target_positions[first:end]
            rows=tuple(row for row,_,_ in segment)
            if (len(rows)>4 or rows!=tuple(range(rows[0],rows[-1]+1)) or
                    any(left[2]!=right[1] for left,right in zip(segment,segment[1:]))):break
            source=supplied[index:other+1]
            text=' '.join(line.original for line in source)
            # Preserve existing exact rows of any valid length. Regrouped
            # translations remain small enough to describe a sentence, never
            # a whole verse distributed across unrelated display rows.
            regrouped=len(source)>1 or len(rows)>1
            if (normalized(text)!=normalized(''.join(target[row] for row in rows)) or
                    regrouped and len(normalized(text))>160):break
            if len(source)==1:
                extra=source[0]
            elif all(line.korean.strip() for line in source):
                meanings=[]
                for line in source:
                    meaning=line.korean.strip()
                    # Split rows can already share one complete sentence's
                    # meaning. Joining them again must not multiply it.
                    if not meanings or meanings[-1]!=meaning:meanings.append(meaning)
                extra=Line(text,'',' / '.join(meanings))
            else:break
            for row in rows:result[row]=extra
            # Never grow past the first complete boundary to borrow a
            # neighboring sentence's meaning for an unmatched row.
            break
    return result


def match_sequence_annotations(originals,supplied):
    """Keep complete meaning across verified split, merged or shifted rows.

    Whole-document verification comes first. The smallest exact block may
    cover up to four rows on either side and 160 normalized characters. Its
    complete meaning stays visible for each target row without guessing how
    to divide a translation. Callers must retain target originals and must
    not copy a sentence's reading into individual fragments.
    """
    target=[line.original if isinstance(line,Line) else line for line in originals]
    source=[line.original for line in supplied]
    alignment=match_lyrics(source,target)
    if alignment is None:return None
    _,source_positions=_reduced_annotation_rows(source,())
    _,target_positions=_reduced_annotation_rows(target,())
    result=_project_annotations(target,supplied,source_positions,target_positions,alignment)
    return result if any(line and line.korean.strip() for line in result) else None


def match_verified_annotations(originals,supplied):
    """Combine verified paths without letting a partial match hide another.

    Every path verifies the whole document independently. Transfer only an
    agreed meaning, and keep readings only for a complete matching row.
    """
    sequence=match_sequence_annotations(originals,supplied)
    matches=[sequence] if sequence is not None else []
    if sequence is None or any(not row or not row.korean.strip() for row in sequence):
        matches.extend(rows for matcher in (match_parenthetical_annotations,match_vocalise_annotations)
                       if (rows:=matcher(originals,supplied)) is not None)
    result=[]
    for index,line in enumerate(originals):
        original=line.original if isinstance(line,Line) else line
        choices=[rows[index] for rows in matches if rows[index]]
        meanings={row.korean.strip() for row in choices if row.korean.strip()}
        readings={row.reading.strip() for row in choices if row.reading.strip()
                  and normalized(row.original)==normalized(original)}
        meaning=next(iter(meanings)) if len(meanings)==1 else ''
        reading=next(iter(readings)) if len(readings)==1 else ''
        result.append(Line(original,reading,meaning) if meaning or reading else None)
    return result if matches else None


def merge_verified_annotations(current,sources):
    """Fill gaps from independently aligned documents; keep every original row.

    Competing new translations stay blank. Existing annotations always win,
    and readings only transfer when the entire original row is identical.
    No source timestamps or lyric identities enter this path.
    """
    matches=[]
    for supplied in sources:
        rows=match_verified_annotations(current,supplied)
        matches.append(rows or [None]*len(current))
    merged=[];contributors=set()
    for index,old in enumerate(current):
        korean=old.korean;reading=old.reading
        choices={row[index].korean.strip() for row in matches
                 if row[index] and row[index].korean.strip()}
        if not korean.strip() and len(choices)==1:
            korean=next(iter(choices))
            contributors.update(number for number,row in enumerate(matches)
                                if row[index] and row[index].korean.strip()==korean)
        readings={row[index].reading.strip() for row in matches if row[index]
                  and row[index].reading.strip()
                  and normalized(row[index].original)==normalized(old.original)}
        if not reading.strip() and len(readings)==1:reading=next(iter(readings))
        merged.append(Line(old.original,reading,korean))
    return tuple(merged),tuple(sorted(contributors))


def _parenthetical_spans(rows):
    """Return balanced outer parentheses in normalized document coordinates."""
    spans=[];offset=0
    for row in rows:
        text=unicodedata.normalize('NFKC',row)
        depth=0;start=0;position=offset;pending=[]
        for char in text:
            if char=='(':
                if depth==0:start=position
                depth+=1
            elif char==')':
                if not depth:return None
                depth-=1
                if not depth and position>start:pending.append((start,position))
            position+=len(normalized(char))
        if depth:return None
        spans.extend(pending);offset=position
    return spans


def _reduced_annotation_rows(rows,spans):
    result=[];positions=[];offset=0;reduced_offset=0
    for index,row in enumerate(rows):
        key=normalized(row);end=offset+len(key)
        pieces=[];cursor=offset
        for start,stop in spans:
            if offset<=start<stop<=end:
                pieces.append(key[cursor-offset:start-offset]);cursor=stop
        pieces.append(key[cursor-offset:])
        reduced=''.join(pieces)
        if reduced:
            result.append(reduced)
            positions.append((index,reduced_offset,reduced_offset+len(reduced)))
            reduced_offset+=len(reduced)
        offset=end
    return result,positions


def _outer_parentheses(text):
    text=unicodedata.normalize('NFKC',text)
    spans=[];depth=0;start=0
    for index,char in enumerate(text):
        if char=='(':
            if not depth:start=index
            depth+=1
        elif char==')':
            if not depth:return text,None
            depth-=1
            if not depth:spans.append((start,index+1))
    return text,None if depth else spans


def _paired_main_annotations(supplied,skips):
    """Remove an aside only when its translated parenthesis is explicitly paired.

    Matching outer-parenthesis counts/order in the original and translation
    is required. An unmarked or differently segmented translation stays blank;
    we never guess which Korean words describe a removed backing vocal.
    """
    result=[];offset=0
    for line in supplied:
        original,spans=_outer_parentheses(line.original)
        selected=[index for index,(start,end) in enumerate(spans or ())
                  if (offset+len(normalized(original[:start])),
                      offset+len(normalized(original[:end]))) in skips]
        offset+=len(normalized(original))
        if not selected:
            result.append(line);continue
        def remove(text,parts):
            cursor=0;pieces=[]
            for index in selected:
                start,end=parts[index];pieces.append(text[cursor:start]);cursor=end
            pieces.append(text[cursor:])
            return ''.join(pieces).strip()
        korean,korean_spans=_outer_parentheses(line.korean)
        reading,reading_spans=_outer_parentheses(line.reading)
        # Keep the unreduced original to make exact projection fail closed if
        # the translation doesn't mark corresponding independent asides.
        if not korean_spans or len(korean_spans)!=len(spans):
            result.append(line);continue
        result.append(Line(remove(original,spans),
                           remove(reading,reading_spans) if reading_spans is not None and len(reading_spans)==len(spans) else '',
                           remove(korean,korean_spans)))
    return result


def match_parenthetical_annotations(originals,supplied):
    """Return exact-block annotations after verifying the complete main lyric.

    Only explicitly parenthesized, source-only backing vocals may be skipped.
    The remaining document must pass the ordinary whole-lyric comparison. This
    never supplies timing, changes originals, or guesses a partial meaning.
    """
    source=[line.original for line in supplied]
    target=[line.original if isinstance(line,Line) else line for line in originals]
    source_text=normalized(''.join(source));target_text=normalized(''.join(target))
    if not 100<=min(len(source_text),len(target_text))<=max(len(source_text),len(target_text))<=6000:
        return None
    source_spans=_parenthetical_spans(source)
    if not source_spans:return None
    changes=SequenceMatcher(None,source_text,target_text,autojunk=False).get_opcodes()
    gaps=[(a,b) for kind,a,b,c,d in changes if kind=='delete']
    source_skips=[(start,end) for start,end in source_spans if end-start<=120 and
                  sum(max(0,min(end,b)-max(start,a)) for a,b in gaps)*2>=end-start]
    if not source_skips or sum(end-start for start,end in source_skips)*5>len(source_text):
        return None
    reduced_source,source_positions=_reduced_annotation_rows(source,source_skips)
    reduced_target,target_positions=_reduced_annotation_rows(target,())
    alignment=match_lyrics(reduced_source,reduced_target)
    if alignment is None:return None
    supplied=_paired_main_annotations(supplied,source_skips)
    result=_project_annotations(target,supplied,source_positions,target_positions,alignment)
    return result if sum(bool(line and line.korean.strip()) for line in result)>=2 else None


def _vocalise_rows(rows):
    """Collapse adjacent repetitions of one written non-lexical syllable."""
    reduced=[];positions=[];runs=[];offset=0;previous=None;vocal_length=0
    syllables=('la','ra','na','lu','ru','oh','ah','ら','ラ','な','ナ','る','ル','あ','ア','お','オ')
    for index,row in enumerate(rows):
        text=normalized(row)
        syllable=next((value for value in syllables
                       if re.fullmatch('(?:'+value+'){3,}',text)),None)
        if syllable:
            vocal_length+=len(text)
            if syllable==previous:
                runs[-1]=(syllable,runs[-1][1]+len(text)//len(syllable))
            else:
                reduced.append(syllable);offset+=len(syllable)
                runs.append((syllable,len(text)//len(syllable)))
        else:
            reduced.append(text);positions.append((index,offset,offset+len(text)))
            offset+=len(text)
        previous=syllable
    return reduced,positions,runs,vocal_length


def match_vocalise_annotations(originals,supplied):
    """Match annotations when identical vocalise runs have different counts.

    Only whole rows repeating one syllable at least three times are collapsed.
    The same ordered vocalise runs must exist in both complete documents. All
    remaining lyrics still pass the ordinary strict comparison, and only exact
    complete blocks at the verified sequence position supply a translation.
    This never returns timings or changes any displayed original.
    """
    source=[line.original for line in supplied]
    target=[line.original if isinstance(line,Line) else line for line in originals]
    sizes=[sum(len(normalized(row)) for row in rows) for rows in (source,target)]
    if not 100<=min(sizes)<=max(sizes)<=6000:return None
    left,left_positions,left_runs,left_vocal=_vocalise_rows(source)
    right,right_positions,right_runs,right_vocal=_vocalise_rows(target)
    if (not left_runs or [key for key,count in left_runs]!=[key for key,count in right_runs]
            or left_runs==right_runs or left_vocal*3>sizes[0] or right_vocal*3>sizes[1]):
        return None
    alignment=match_lyrics(left,right)
    if alignment is None:return None
    result=_project_annotations(target,supplied,left_positions,right_positions,alignment)
    return result if sum(bool(line and line.korean.strip()) for line in result)>=2 else None


class LyricAnnotations:
    def __init__(self,store):self.directory=store.directory/'lyric_annotations'

    def load(self,song):
        value=read_json(self.directory/(song.id+'.json'),{})
        if not isinstance(value,dict):raise ValueError('저장된 가사 보완 파일 형식을 확인할 수 없습니다. 원본은 보존했습니다.')
        if value.get('fingerprint')!=song.fingerprint:return [None]*len(song.lines)
        entries=value.get('lines')
        if not isinstance(entries,list) or len(entries)!=len(song.lines):
            raise ValueError('저장된 가사 보완 구절 수가 일치하지 않습니다. 원본은 보존했습니다.')
        fields={'original','reading','korean'}
        if any(entry is not None and (not isinstance(entry,dict) or set(entry)!=fields
               or any(not isinstance(entry[field],str) for field in fields)) for entry in entries):
            raise ValueError('저장된 가사 보완 구절 형식이 올바르지 않습니다. 원본은 보존했습니다.')
        return [Line(**entry) if entry is not None else None for entry in entries]

    def save(self,song,supplied,source):
        matches=match_annotations(song.lines,supplied)
        count=sum(line is not None for line in matches)
        if not count:raise ValueError('일치하는 일본어 구절이 없어 번역을 연결하지 않았습니다.')
        old=self.load(song)
        merged=[Line(previous.original,
                     previous.reading if previous.reading.strip() else current.reading,
                     previous.korean if previous.korean.strip() else current.korean)
                if previous and current else previous or current
                for previous,current in zip(old,matches)]
        atomic_json(self.directory/(song.id+'.json'),{
            'fingerprint':song.fingerprint,'source':source or {},
            'lines':[asdict(line) if line else None for line in merged]})
        return count

    def display_lines(self,song):
        # Optional annotations may fail without blocking the original display;
        # save still calls strict load so it cannot replace the damaged file.
        try:extras=self.load(song)
        except (ValueError,OSError):return tuple(song.lines)
        result=[]
        for original,extra in zip(song.lines,extras):
            result.append(Line(original.original,
                original.reading if original.reading.strip() or extra is None else extra.reading,
                original.korean if original.korean.strip() or extra is None else extra.korean))
        return tuple(result)
