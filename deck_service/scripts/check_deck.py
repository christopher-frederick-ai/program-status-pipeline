#!/usr/bin/env python3
"""Thin CLI wrapper: check_deck.py REQUEST.json OUTDIR  (REQUEST.json = a fixture: sidecar + milestone_rows)"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deck_service.checks import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
