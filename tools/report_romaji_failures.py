"""Build an offline audio and CTC review for failed JSUT-song lines."""

import argparse
from base64 import b64encode
from collections import Counter
from html import escape
import io
import json
from pathlib import Path
import re
import wave
from zipfile import ZipFile

import numpy as np

from benchmark_japanese_alignment import read_json
from faster_whisper.audio import decode_audio


def phrases(labels):
    result, phones = [], []
    for line in labels.splitlines():
        start, end, full = line.split()
        phone = re.search(r'\^\w+-([^+]+)\+', full)[1]
        if phone in ('pau', 'sil', 'br'):
            if phones:
                result.append(phones)
                phones = []
        else:
            phones.append((phone, int(start) / 1e7, int(end) / 1e7))
    if phones:
        result.append(phones)
    return result


def flags(run, row, all_phones, text):
    index = row['index']
    phones = all_phones[index]
    morae = sum(phone in ('a', 'i', 'u', 'e', 'o', 'N', 'cl') for phone, _, _ in phones)
    gap = phones[0][1] - all_phones[index - 1][-1][2] if index else phones[0][1]
    duration = phones[-1][2] - phones[0][1]
    return [name for condition, name in (
        (index == 0 and gap >= 2, 'first_after_2s'),
        (index > 0 and gap >= 2, 'after_2s_pause'),
        (row['repeated'], 'same_text_repeated'),
        (any(phone in 'aiueo' and end - start >= .35 for phone, start, end in phones), 'vowel_at_least_350ms'),
        (morae <= 2, 'one_or_two_morae'),
        (duration > 0 and morae / duration >= 8, 'at_least_8_morae_per_second'),
        (bool(re.search('[A-Za-z]', text)), 'latin_text'),
        (row['wrong_repeat'], 'wrong_repeated_occurrence'),
        (row.get('reason') == 'reading_unavailable', 'reading_unavailable'),
        (abs(run['median_signed_offset_ms'] or 0) >= 200, 'track_median_offset_at_least_200ms'),
    ) if condition]


def clip(samples, start, end, markers, phones):
    rate = 16000
    segment = samples[round(start * rate):round(end * rate)]
    chunks = np.array_split(segment, min(450, len(segment)))
    peak = max(1e-6, float(np.max(np.abs(segment))))
    bars = ''.join(f'M{i*900/len(chunks):.1f},{55 - 45*np.max(y)/peak:.1f}'
                   f'v{45*(np.max(y)-np.min(y))/peak:.1f}' for i, y in enumerate(chunks))
    markers = [(color, label, stamp) for color, label, stamp in markers
               if stamp is not None and start <= stamp <= end]
    def x(stamp):
        return (stamp - start) * 900 / (end - start)
    lines = ''.join(f'<path stroke="{color}" stroke-width="2" d="M{x(stamp):.1f},14v87">'
                    f'<title>{label} {stamp:.3f}s</title></path>' for color, label, stamp in markers)
    activity = ''.join(f'<rect fill="#73958c" x="{x(max(left,start)):.1f}" y="102" '
                       f'width="{x(min(right,end))-x(max(left,start)):.1f}" height="5"/>'
                       for _, left, right in phones if left < end and right > start)
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes((np.clip(segment, -1, 1) * 32767).astype('<i2').tobytes())
    encoded = b64encode(output.getvalue()).decode('ascii')
    return (f'<svg viewBox="0 0 900 110" role="img" aria-label="오디오 파형과 시작점">'
            f'<path d="{bars}" stroke="#7c8799" fill="none"/>{activity}{lines}</svg>'
            f'<audio controls preload="none" src="data:audio/wav;base64,{encoded}"></audio>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('labels', type=Path)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('final', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    manifest = {row['song_id']: row for row in read_json(args.manifest)['tracks']}
    baseline = [run for run in read_json(args.baseline)['runs'] if run['engine'] == 'romaji_ctc']
    final = {run['id']: run for run in read_json(args.final)['runs'] if 'rows' in run}
    report, cards = [], []
    with ZipFile(args.labels) as labels:
        for run in baseline:
            if 'rows' not in run:
                continue
            failures = [row for row in run['rows']
                        if row['signed_error'] is None or abs(row['signed_error']) > .5]
            if not failures:
                continue
            song = run['id']
            entry = manifest[song]
            phones = phrases(labels.read(f'todai_child/{song}.lab').decode('ascii'))
            samples = decode_audio(str(args.manifest.parent / entry['audio']), sampling_rate=16000)
            for row in failures:
                index = row['index']
                new = final[song]['rows'][index]
                text = entry['lines'][index]
                tags = flags(run, row, phones, text)
                report.append({'song': song, 'index': index, 'text': text, 'flags': tags,
                               'label_phones': phones[index], 'baseline': row, 'final': new})
                reference = row['reference']
                left, right = max(0, reference - 1), min(len(samples)/16000, reference + 3)
                markers = [('#16634b', '기준', reference), ('#b3432c', '기존', row['onset']),
                           ('#2b5aa4', '개선', new['onset'])]
                graph = clip(samples, left, right, markers, phones[index])
                trace = {key: value for key, value in row.items() if key in ('reading', 'phones', 'first_pass')}
                cards.append(f'<section><h2>{song} · {index+1}번 구절 · {escape(text)}</h2>'
                             f'<p>기준 {reference:.3f}초 · 기존 {row["onset"]} · 개선 {new["onset"]}'
                             f' · {escape(row.get("reason") or "시각 오차")}</p>'
                             f'<p>{escape(", ".join(tags))}</p>{graph}'
                             f'<details><summary>기존 CTC 경로와 후보</summary><pre>'
                             f'{escape(json.dumps(trace, ensure_ascii=False, indent=2))}</pre></details></section>')
    stats = {'rows': report, 'overlapping_flags': dict(Counter(flag for row in report for flag in row['flags'])),
             'failure_reasons': dict(Counter(row['baseline'].get('reason', 'outside_500ms') for row in report))}
    args.destination.mkdir(parents=True, exist_ok=True)
    (args.destination / 'failure-analysis.json').write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.destination / 'failure-audio.html').write_text(
        '<!doctype html><html lang="ko"><meta charset="utf-8"><title>JSUT-song 정렬 진단</title>'
        '<style>body{font:16px system-ui;margin:36px auto;max-width:930px;padding:0 16px;color:#202633}'
        'section{padding:16px 0;border-top:1px solid #d7dce5}svg{width:100%;background:#f7f8fa}'
        'pre{overflow:auto}details{margin-top:10px}</style>'
        '<h1>JSUT-song 정렬 진단</h1><p>초록은 기준 시각, 빨강은 기존 후보, 파랑은 개선 후보입니다. '
        '파형 아래 띠는 HTS 라벨의 음소 구간입니다.</p>' + ''.join(cards) + '</html>', encoding='utf-8')


if __name__ == '__main__':
    main()
