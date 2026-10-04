"""Build an allowlisted source distribution with sanitized public seed data."""
import argparse
from datetime import datetime
from pathlib import Path
import shutil
from zipfile import ZipFile,ZipInfo,ZIP_DEFLATED

MODULES='''analysis_limits app appearance audio_alignment audio_diagnostics audio_install audio_job audio_progress audio_sync audio_worker audio_runtime realtime_lyrics compact_audio_runtime explorer_symbols
auto_audio batch_translation batch_translation_dialog catalog_autolink catalog_link_queue catalog_sync spotify_playlist spotify_public_catalog playlist_lyrics_job playlist_lyrics_store playlist_lyrics_sources extension_inbox live_lyrics live_timing lyric_annotations lyric_completion lyric_follow lyric_edits lyrics manual_lyrics manual_dialog media spotify_lifecycle wiki_singers wiki_part_store
native_timing process_audio reader_bridge reader_dialog internal_reader browser_worker browser_visibility background_lyrics public_lyrics spotify_api spotify_worker spotify_volume volume_popup lyric_cache realtime_audio recording_context recording_reuse work_reuse screen_lyrics shortcut_dialog shortcuts storage subtitle sync app_version app_updates data_updates update_dialog
sync_review timing_quality web_dialog web_lyrics windows connection_dialog lyricsplus_job lyricsplus_provider alsong_provider artist_names artist_metadata artist_header artist_layer lyric_match lyric_asides artist_colors ui_theme explorer_dialog explorer_widgets explorer_library song_catalog catalog_sources spotify_catalog spotify_desktop spotify_controls local_library live_catalog live_catalog_update imas_live_sources imas_people_sources group_credits song_aliases user_annotations lyric_performers lyric_parts saved_annotations app_paths frozen_entry optional_runtime'''.split()
SUPPORT=('README.md','LICENSE','BUNDLE_SIZE.md','DISTRIBUTION.md','RELEASE_NOTES.md','.gitignore','.gitattributes','requirements.txt','requirements-audio.txt','requirements-audio-gpu.txt','setup.ps1',
         'setup-audio.ps1','setup-browser.ps1','requirements-browser.txt','requirements-build.txt','native_page.js','run.bat','package_release.py','build_exe.py')
EXTENSION=('background.js','document_match.js','lyric_text.js','manifest.json','overlay_transfer.js',
           'popup.css','popup.html','popup.js','reader.js','README.md','source_suggestions.js','spotify_link.js')


def release_paths(base):
    base=Path(base).resolve()
    paths=[base/(name+'.py') for name in MODULES]+[base/name for name in SUPPORT]
    paths += [base/'extension'/name for name in EXTENSION]
    paths += [base/'resources'/'artist_names'/name for name in ('imas_legacy.json','imas_modern.json')]
    paths += [base/'resources'/'artist_colors'/name for name in ('imas_legacy.json','imas_modern.json')]
    paths += [base/'resources'/'song_catalog.json']
    paths += [base/'resources'/'recording_kind_corrections.json']
    paths += [base/'resources'/'reviewed_recording_releases.json']
    paths += [base/'resources'/'spotify_recording_aliases.json']
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
    paths += [base/'tools'/'build_song_readings.py']
    paths += [base/'resources'/'group_credits.json']
    paths += sorted((base/'resources'/'series_symbols').glob('*'))
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
    """Create the same verified music-only package used by in-app updates."""
    base=Path(base).resolve()
    seed=base/'resources'/'seed_data'
    paths=[path for path in release_paths(base) if seed in path.parents]
    if not paths or not (seed/'_manifest.json').is_file():
        raise ValueError('Public seed data is required')
    from data_updates import validate_package
    output=Path(output_dir).resolve()/'SpotifyLyricsOverlay-data.zip'
    output.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(output,'w',compression=ZIP_DEFLATED) as archive:
        for path in paths:
            entry=ZipInfo(path.relative_to(seed).as_posix())
            entry.compress_type=ZIP_DEFLATED
            entry.external_attr=0o100644 << 16
            archive.writestr(entry,path.read_bytes())
    validate_package(output)
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
