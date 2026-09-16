"""Strict classified MP4 filename parser without media or UI dependencies."""
import re
from pathlib import Path
from datetime import datetime
_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:__\d{3})?$', re.ASCII)

def filename_wall(path):
    path=Path(path)
    if path.suffix.lower()!='.mp4':return None
    match=_PATTERN.fullmatch(path.stem)
    if not match:return None
    try:return (datetime.strptime(match[1]+' '+match[2],'%Y-%m-%d %H-%M-%S')-datetime(1970,1,1)).total_seconds()*1000
    except ValueError:return None
