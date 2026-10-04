"""Rebuild the small public group-credit snapshot from the bundled dictionary."""
import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from explorer_library import performers
from group_credits import RESOURCE_PATH, save_group_credits
from song_catalog import SongCatalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=RESOURCE_PATH)
    args = parser.parse_args()
    catalog = SongCatalog()
    people = performers(external=catalog.performers)
    count = save_group_credits(people, args.output)
    print(f'Saved {count} group rosters ({args.output.stat().st_size:,} bytes).')


if __name__ == '__main__':main()
