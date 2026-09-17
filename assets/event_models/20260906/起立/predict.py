# -*- coding: utf-8 -*-
"""Portable entry point: python detect.py --raw input.json --output hints.csv."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
from algorithm.detector import main
if __name__=='__main__':main()
