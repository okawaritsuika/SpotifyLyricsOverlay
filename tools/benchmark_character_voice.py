"""Benchmark enrolled singers with disjoint tracks and controlled vocal overlays."""
import argparse
from itertools import combinations
import json
from pathlib import Path
import shutil
import sys
from time import perf_counter
import wave
from zipfile import ZipFile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_voice import SAMPLE_RATE, VoiceAnalyzer, VoiceAudio, _quality


def fixture(corpus_zip, filenames, destination):
    with ZipFile(corpus_zip) as archive:
        members = {Path(name).name: name for name in archive.namelist() if name.endswith('.wav')}
        for name in filenames:
            path = destination / name
            if not path.is_file():
                with archive.open(members[name]) as source, path.open('wb') as output:
                    shutil.copyfileobj(source, output)
            yield path


def normalized_windows(audio, count):
    windows = []
    length = 2 * SAMPLE_RATE
    for start in range(0, len(audio.samples) - length + 1, length):
        samples = audio.samples[start:start + length]
        quality = _quality(samples)
        if quality['active_fraction'] >= .85 and quality['tonal_fraction'] >= .75 and quality['rms_dbfs'] >= -36:
            windows.append(samples * (.04 / np.sqrt(np.mean(samples ** 2))))
        if len(windows) == count:
            return windows
    if not windows:
        raise ValueError('No active two-second excerpts in a corpus file')
    return windows


def cases(windows_by_singer):
    for count in range(1, len(windows_by_singer) + 1):
        for selected in combinations(windows_by_singer, count):
            for excerpts in zip(*(windows_by_singer[name] for name in selected)):
                samples = np.sum(excerpts, axis=0, dtype=np.float32) / np.sqrt(np.float32(count))
                yield selected, VoiceAudio(samples, 'singing', 'benchmark-controlled-mixture')


def run_scores(analyzer, profiles, probes):
    return [(truth, analyzer.score(audio, ranges=[(0, 2000)], profiles=profiles)[0]['scores'])
            for truth, audio in probes]


def calibrate(candidate, scored):
    positive = [scores[candidate] for truth, scores in scored
                if candidate in truth and scores[candidate] is not None]
    negative = [scores[candidate] for truth, scores in scored
                if candidate not in truth and scores[candidate] is not None]
    if not positive or not negative:
        return None

    def merit(value):
        correct = sum(score > value + .025 for score in positive) / len(positive)
        correct += sum(score < value - .025 for score in negative) / len(negative)
        return correct, value

    return float(max(np.linspace(-.97, .97, 195), key=merit))


def evaluate(scored, analyzer):
    cases_by_count = {}
    for truth, scores in scored:
        predicted = analyzer._summarize(scores)
        entry = cases_by_count.setdefault(str(len(truth)), {'cases': 0, 'exact': 0, 'decisive': 0,
                                                           'rank1_correct': 0})
        entry['cases'] += 1
        entry['decisive'] += predicted['status'] == 'estimated'
        entry['exact'] += (predicted['status'] == 'estimated' and set(truth) == set(predicted['singers']))
        ranked = [(score, name) for name, score in scores.items() if score is not None]
        if len(truth) == 1 and ranked:
            entry['rank1_correct'] += max(ranked)[1] in truth
    return cases_by_count


def separation(args, directory):
    excerpt = directory / 'music-excerpt.wav'
    with wave.open(str(args.music_wav), 'rb') as source, wave.open(str(excerpt), 'wb') as output:
        output.setparams(source.getparams())
        rate = source.getframerate()
        source.setpos(min(35 * rate, max(0, source.getnframes() - 10 * rate)))
        output.writeframes(source.readframes(10 * rate))
    cache = directory / 'separation'
    analyzer = VoiceAnalyzer(cache, models_dir=args.models_dir, device=args.device, singer_model='spectral')
    started = perf_counter()
    first = analyzer.prepare(excerpt)
    separated = perf_counter()
    second = analyzer.prepare(excerpt)
    reused = perf_counter()
    return {'seconds_of_audio': first.duration_ms / 1000, 'separation_or_cache_s': separated - started,
            'cache_reuse_s': reused - separated, 'identical': bool(np.array_equal(first.samples, second.samples)),
            'stem_rms': float(np.sqrt(np.mean(first.samples ** 2))), 'preparation_id': first.preparation_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jsut-zip', type=Path, required=True)
    parser.add_argument('--ojama-zip', type=Path, required=True)
    parser.add_argument('--models-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--nus-dir', type=Path, help='Optional ADIZ and JLEE NUS-48E directories')
    parser.add_argument('--music-wav', type=Path, help='Optional stereo PCM music excerpt for vocal separation')
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--backend', action='append', choices=('sony', 'ecapa', 'spectral'))
    args = parser.parse_args()
    directory = args.output.parent / 'voice-benchmark-fixtures'
    directory.mkdir(parents=True, exist_ok=True)

    # Each recording belongs to exactly one of enrollment, calibration or evaluation.
    jsut = ('003.wav', '004.wav', '007.wav', '010.wav', '012.wav', '014.wav', '015.wav', '016.wav', '019.wav')
    ojama = ('akaikutsu.wav', 'amefuri.wav', 'chatsumi.wav', 'chouchou.wav', 'furusato.wav',
             'genkotsuyamanotanukisan.wav', 'harugakita.wav', 'harunoogawa.wav', 'hato.wav')
    files = {'JSUT': list(fixture(args.jsut_zip, jsut, directory)),
             'OJaMa': list(fixture(args.ojama_zip, ojama, directory))}
    splits = {name: {'enrollment': (0, 3), 'calibration': (3, 6), 'test': (6, 9)} for name in files}
    if args.nus_dir:
        for name in ('ADIZ', 'JLEE'):
            files[name] = sorted((args.nus_dir / name).glob('sing-*.wav'))
            if len(files[name]) != 4:
                parser.error(f'{name} requires four sing-*.wav recordings')
            splits[name] = {'enrollment': (0, 2), 'calibration': (2, 3), 'test': (3, 4)}
    decoder = VoiceAnalyzer(directory, singer_model='spectral')
    decoded = {name: [decoder.prepare(path, isolated=True) for path in paths] for name, paths in files.items()}

    def subset_for(name, recordings, subset):
        start, end = splits[name][subset]
        return recordings[start:end]

    probes = {subset: {name: [window for song in subset_for(name, recordings, subset)
                             for window in normalized_windows(song, 4)] for name, recordings in decoded.items()}
              for subset in ('calibration', 'test')}
    results = {'device': args.device, 'candidate_count': len(files),
               'recordings': {name: [path.name for path in paths] for name, paths in files.items()},
               'splits': splits, 'backends': {}}
    if args.music_wav:
        results['separation'] = separation(args, directory)
        print('separation', results['separation'], flush=True)
    for backend in args.backend or ('sony', 'ecapa', 'spectral'):
        analyzer = VoiceAnalyzer(directory, singer_model=backend, models_dir=args.models_dir, device=args.device)
        started = perf_counter()
        profiles = {name: [sample for song in subset_for(name, recordings, 'enrollment')
                           for sample in analyzer.enroll(song, ranges=[(0, 12000)])]
                    for name, recordings in decoded.items()}
        enrolled = perf_counter()
        calibration = run_scores(analyzer, profiles, cases(probes['calibration']))
        analyzer.thresholds = {name: calibrate(name, calibration) for name in files}
        calibrated = perf_counter()
        tested = run_scores(analyzer, profiles, cases(probes['test']))
        finished = perf_counter()
        results['backends'][backend] = {'model_id': analyzer.model_id('singing'),
                                        'exemplars': {name: len(value) for name, value in profiles.items()},
                                        'thresholds': analyzer.thresholds,
                                        'enrollment_s': enrolled - started,
                                        'calibration_s': calibrated - enrolled,
                                        'evaluation_s': finished - calibrated,
                                        'test_by_singer_count': evaluate(tested, analyzer)}
        args.output.write_text(json.dumps(results, indent=2, allow_nan=False), encoding='utf-8')
        print(backend, results['backends'][backend]['test_by_singer_count'], flush=True)
        if backend == 'ecapa' and args.nus_dir:
            speech_file = next((args.nus_dir / 'ADIZ').glob('read-*.wav'), None)
            if speech_file:
                started = perf_counter()
                dialogue = analyzer.prepare(speech_file, kind='speech')
                samples = analyzer.enroll(dialogue, ranges=[(0, 12000)])
                excluded = analyzer.score(dialogue, ranges=[(0, 2000)], profiles=profiles)[0]['status']
                results['dialogue'] = {'samples': len(samples), 'model_id': analyzer.model_id('speech'),
                                       'wrong_kind_comparison_status': excluded, 'seconds': perf_counter() - started}
                args.output.write_text(json.dumps(results, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
