"""Refresh the factual imas-db character/CV/color bundle."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imas_people_sources import COLOR_URL, CV_URL, fetch, main, parse_people  # noqa: E402,F401


if __name__ == '__main__':main()
