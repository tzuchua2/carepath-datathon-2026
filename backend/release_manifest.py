"""Describe the v3 release files, including shared v2 reference tables, without duplicating them."""
from hashlib import sha256
from pathlib import Path
import json

import pyarrow.parquet as pq

from backend.release_v3 import DATA, ORIGINALS


def manifest():
    paths = [*(DATA/'revisions/v3').glob('*.parquet'),
             *(DATA/'derived'/(name+'.parquet') for name in ORIGINALS),
             *(DATA/'enrichment'/(name+'.parquet') for name in ('address_contact_quality','external_service_matches'))]
    if len(paths)!=13 or len(set(p.stem for p in paths))!=13:
        raise ValueError('Expected 13 distinct release tables')
    return {p.stem: dict(path=p.relative_to(DATA.parent).as_posix(), rows=pq.ParquetFile(p).metadata.num_rows,
                         sha256=sha256(p.read_bytes()).hexdigest()) for p in paths}


if __name__=='__main__':
    print(json.dumps(manifest(),indent=2,sort_keys=True))
