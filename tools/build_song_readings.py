"""Build an explicitly generated reading index without changing the catalog."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from song_aliases import generate_readings
from storage import atomic_json


def main():
    catalog=json.loads((ROOT/'resources'/'song_catalog.json').read_text(encoding='utf-8'))
    titles=sorted({song['title'] for song in catalog['songs']})
    entries={title:list(values) for title in titles if (values:=generate_readings(title))}
    destination=ROOT/'resources'/'song_readings.json'
    atomic_json(destination,{'schema_version':1,'generated_at':datetime.now(timezone.utc).isoformat(),
                'basis':'pykakasi 2.3.0 + approximate kana-to-Hangul search spelling',
                'note':'자동 생성한 검색용 독음이며 공식 한국어 제목 또는 번역이 아닙니다. 읽기는 부정확할 수 있습니다.',
                'entries':entries})
    print(f'Generated readings for {len(entries):,} of {len(titles):,} original titles.')


if __name__=='__main__':main()
