"""Build an allowlisted source distribution with sanitized public seed data."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZipFile,ZipInfo,ZIP_DEFLATED

MODULES='''analysis_limits app appearance desktop_workspace catalog_workspace overlay_fonts audio_alignment audio_capture_cache audio_diagnostics audio_install audio_job audio_progress audio_sync audio_worker audio_runtime realtime_lyrics compact_audio_runtime explorer_symbols
japanese_alignment japanese_phonetics romaji_ctc pyshiro_refine local_analysis local_analysis_dialog local_analysis_worker sync_reanalysis sync_reanalysis_dialog sync_reanalysis_sources character_voice sony_singer_identity singing_voice_separation singing_overlap singing_assignment singing_analysis
auto_audio batch_translation batch_translation_dialog spotify_translation_sources personal_resources translation_resource_dialog catalog_autolink catalog_display catalog_link_queue catalog_sync spotify_playlist spotify_public_catalog playlist_analysis_panel playlist_lyrics_job playlist_lyrics_store playlist_lyrics_sources extension_inbox live_lyrics live_timing lyric_annotations lyric_completion lyric_follow lyric_edits lyrics manual_lyrics manual_dialog manual_sync_dialog media spotify_lifecycle wiki_singers wiki_part_store
native_timing process_audio reader_bridge reader_dialog internal_reader browser_worker browser_visibility background_lyrics public_lyrics spotify_api spotify_worker spotify_volume volume_popup lyric_cache lyric_merge realtime_audio recording_context recording_reuse work_reuse screen_lyrics shortcut_dialog shortcuts storage subtitle sync app_version app_updates data_updates update_dialog
sync_review timing_quality web_dialog web_lyrics imas_lyrics imas_timetag windows tray_icon connection_dialog lyricsplus_job lyricsplus_provider alsong_provider alsong_dialog artist_names artist_metadata artist_credit_overrides artist_header artist_layer lyric_match lyric_asides artist_colors ui_theme explorer_dialog explorer_widgets explorer_library song_catalog catalog_sources spotify_catalog spotify_desktop spotify_controls local_library live_catalog live_catalog_update imas_live_sources imas_people_sources group_credits song_aliases user_annotations lyric_performers lyric_parts saved_annotations app_paths frozen_entry optional_runtime missing_audio quiet_audio spotify_audio_route'''.split()
SUPPORT=('README.md','JAPANESE_ALIGNMENT.md','LOCAL_AUDIO_ANALYSIS.md','VOICE_ANALYSIS.md','LICENSE','BUNDLE_SIZE.md','DISTRIBUTION.md','RELEASE_NOTES.md','.gitignore','.gitattributes','requirements.txt','requirements-audio.txt','requirements-audio-gpu.txt','setup.ps1',
         'setup-audio.ps1','setup-browser.ps1','requirements-browser.txt','requirements-build.txt','native_page.js','run.bat','package_release.py','build_exe.py')
EXTENSION=('background.js','document_match.js','lyric_text.js','manifest.json','overlay_transfer.js',
           'popup.css','popup.html','popup.js','reader.js','README.md','source_suggestions.js','spotify_link.js',
           'icons/16.png','icons/24.png','icons/32.png','icons/48.png','icons/128.png')


def release_paths(base):
    base=Path(base).resolve()
    paths=[base/(name+'.py') for name in MODULES]+[base/name for name in SUPPORT]
    paths += [base/'extension'/name for name in EXTENSION]
    # Ship the companion's source and lockfile, never its installed runtime.
    desktop=base/'desktop_ui'
    paths += [desktop/name for name in ('package.json','package-lock.json')]
    for pattern in ('*.js','*.cjs','*.html','*.css'):
        paths += sorted(desktop.glob(pattern))
    paths += [base/'resources'/'artist_names'/name for name in ('imas_legacy.json','imas_modern.json')]
    paths += [base/'resources'/'artist_colors'/name for name in ('imas_legacy.json','imas_modern.json')]
    paths += [base/'resources'/'song_catalog.json']
    paths += [base/'resources'/'recording_kind_corrections.json']
    paths += [base/'resources'/'reviewed_recording_releases.json']
    paths += [base/'resources'/'spotify_recording_aliases.json']
    paths += [base/'resources'/'spotify_playlist_catalog.json']
    paths += [base/'resources'/'lyric_parts.json']
    paths += [base/'resources'/'live_catalog.json']
    paths += [base/'resources'/'live_event_links.json']
    paths += [base/'resources'/'live_work_aliases.json']
    paths += [base/'resources'/'live_source_corrections.json']
    paths += [base/'resources'/'imas_db_people.json']
    paths += [base/'resources'/'song_aliases.json']
    paths += [base/'resources'/'song_readings.json']
    paths += [base/'tools'/'enrich_imas_db_people.py']
    paths += [base/'tools'/'collect_spotify_links.py']
    paths += [base/'tools'/'audit_playlist_lyrics.py']
    paths += [base/'tools'/'build_saved_playback_seed.py']
    paths += [base/'tools'/'build_song_readings.py']
    paths += [base/'tools'/'benchmark_japanese_alignment.py']
    paths += [base/'tools'/name for name in ('benchmark_romaji_profiles.py', 'report_romaji_failures.py')]
    paths += [base/'tools'/'prepare_jsut_song_benchmark.py']
    paths += [base/'tools'/'benchmark_character_voice.py']
    paths += [base/'tools'/name for name in ('benchmark_singing_voice.py', 'benchmark_singing_separation.py',
                                             'benchmark_escape_voice.py', 'sweep_singing_thresholds.py', 'benchmark_singing_v2.py',
                                             'prepare_solo_profiles.py', 'export_playlist_mp3.py', 'playlist_youtube_mp3.py')]
    paths += [base/'resources'/'group_credits.json']
    paths += [base/'resources'/'gakuen_groups.json']
    paths += [base/'resources'/'app.ico',base/'resources'/'app.png']
    paths += sorted((base/'resources'/'series_symbols').glob('*'))
    paths += sorted((base/'resources'/'fonts').glob('*.ttf'))
    paths += sorted((base/'resources'/'fonts').glob('*-OFL.txt'))
    paths += [base/'tools'/'render_series_symbols.cjs']
    paths += [base/'tools'/'build_live_catalog.py']
    paths += [base/'tools'/'build_live_gap_supplement.py']
    paths += [base/'tools'/'audit_live_catalog.py']
    paths += [base/'tools'/'build_group_credits.py']
    paths += [base/'tools'/'build_lyric_part_expansion.py']
    paths += sorted((base/'resources'/'catalog_sources').glob('*'))
    paths += sorted((base/'resources'/'licenses').glob('*.txt'))
    paths += sorted((base/'resources'/'seed_data').rglob('*.json'))
    paths += sorted((base/'tests').glob('test_*.py'))
    paths += sorted((base/'tests').glob('*.test.js'))
    paths += [base/'tests'/'title_variants.json',base/'tests'/'fixtures'/'reviewed-wrong-original.json']
    paths += [base/'tests'/'verify_source_distribution.py']
    paths += [base/'tools'/'verify_catalog_part_candidates.py',base/'tools'/'verify_sidem_part_candidates.py']
    paths += sorted((base/'.github'/'workflows').glob('*.yml'))
    paths += [base/'public_data.py',base/'release_audit.py']
    paths += [base/'tests'/'__init__.py'] if (base/'tests'/'__init__.py').is_file() else []
    missing=[str(p.relative_to(base)) for p in paths if not p.is_file()]
    if missing:raise ValueError('Missing release files: '+', '.join(missing))
    paths=sorted(set(paths))
    from release_audit import audit_paths
    audit_paths(base,paths)
    return paths


def stage_source(base,destination):
    """Create a new clean tree; never copy arbitrary workspace content or .git."""
    base=Path(base).resolve();destination=Path(destination).resolve()
    paths=release_paths(base)
    destination.mkdir(parents=True,exist_ok=False)
    for path in paths:
        target=destination/path.relative_to(base)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
    return destination


def package(base):
    base=Path(base).resolve()
    paths=release_paths(base)
    output=base/'release'/('SpotifyLyricsOverlay-source-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.zip')
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output,'x',compression=ZIP_DEFLATED) as archive:
        for path in paths:archive.write(path,'SpotifyLyricsOverlay/'+path.relative_to(base).as_posix())
    return output


def package_data(base, output_dir):
    """Create the verified music and public catalog package for in-app updates."""
    from data_updates import CATALOG_FILES, validate_package

    base=Path(base).resolve()
    seed=base/'resources'/'seed_data'
    manifest_path=seed/'_manifest.json'
    paths={path.relative_to(seed).as_posix():path for path in release_paths(base)
           if seed in path.parents and path != manifest_path}
    if not paths or not manifest_path.is_file():
        raise ValueError('Public seed data is required')
    catalogs={name:base/'resources'/name for name in CATALOG_FILES}
    if catalogs.keys() & paths.keys():
        raise ValueError('Public catalog snapshots must be stored under resources/')
    manifest=json.loads(manifest_path.read_bytes())
    manifest['files']+=len(catalogs)
    manifest['bytes']+=sum(path.stat().st_size for path in catalogs.values())
    paths.update(catalogs)
    output=Path(output_dir).resolve()/'SpotifyLyricsOverlay-data.zip'
    output.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='.data-release-',dir=output.parent) as directory:
        candidate=Path(directory)/output.name
        with ZipFile(candidate,'w',compression=ZIP_DEFLATED) as archive:
            for name in sorted([*paths,'_manifest.json']):
                entry=ZipInfo(name)
                entry.compress_type=ZIP_DEFLATED
                entry.external_attr=0o100644 << 16
                body=paths[name].read_bytes() if name in paths else json.dumps(manifest,ensure_ascii=False).encode('utf-8')
                archive.writestr(entry,body)
        validate_package(candidate)
        candidate.replace(output)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--stage',type=Path,help='Create a new clean source directory instead of ZIP')
    mode.add_argument('--data',action='store_true',help='Create only the public music data update ZIP')
    args=parser.parse_args()
    base=Path(__file__).resolve().parent
    result=(stage_source(base,args.stage) if args.stage else
            package_data(base,base/'release') if args.data else package(base))
    print(result)
    if result.is_file():print('Source distribution bytes:',result.stat().st_size)
