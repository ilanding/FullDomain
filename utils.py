import re
from pathlib import Path
def base_html_name(fn):
    p=Path(fn)
    if p.suffix.lower()!=".html": raise ValueError("Only .html files are accepted")
    return p.stem
def names(text): return [x.strip().lower() for x in re.split(r"[\s,]+",text) if x.strip()]
def valid_domain(d): return bool(re.fullmatch(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",d.lower()))
