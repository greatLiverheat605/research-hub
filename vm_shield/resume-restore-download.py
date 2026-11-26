#!/usr/bin/env python3
"""Resume the Apple restore image with three checked HTTP range downloads."""
import concurrent.futures
import json
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request

repo = Path(__file__).resolve().parents[1]
url = json.loads((repo / '.private/vm-tools/restore-info.json').read_text())['url']
assert urllib.parse.urlparse(url).hostname.endswith('.cdn-apple.com')
target = repo / '.private/residential-vm/images/macos.ipsw'
with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as response:
    total = int(response.headers['Content-Length'])
downloaded = target.stat().st_size if target.exists() else 0
assert downloaded <= total
if downloaded == total:
    print('Apple restore image already fully downloaded.')
    raise SystemExit(0)
plan_path = target.with_suffix('.download.json')
if plan_path.exists():
    plan = json.loads(plan_path.read_text())
    assert plan['total'] == total and plan['base'] == downloaded and plan['url'] == url
else:
    width = (total - downloaded + 2) // 3
    plan = {'url': url, 'total': total, 'base': downloaded,
            'ranges': [[start, min(start + width - 1, total - 1)]
                       for start in range(downloaded, total, width)]}
    plan_path.write_text(json.dumps(plan))

def fetch(item):
    index, (start, end) = item
    part = target.with_suffix('.part' + str(index))
    expected = end - start + 1
    for attempt in range(4):
        present = part.stat().st_size if part.exists() else 0
        assert present <= expected
        if present == expected:
            return part
        request = urllib.request.Request(url, headers={'Range': f'bytes={start + present}-{end}'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                assert response.status == 206
                assert response.headers['Content-Range'] == f'bytes {start + present}-{end}/{total}'
                with part.open('ab') as output:
                    while True:
                        chunk = response.read(min(4 * 1024 * 1024, expected - output.tell()))
                        if not chunk:
                            break
                        output.write(chunk)
            if part.stat().st_size == expected:
                print(f'Apple restore segment {index + 1} complete.', flush=True)
                return part
        except Exception:
            if attempt == 3:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError('Incomplete restore segment')

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    parts = list(pool.map(fetch, enumerate(plan['ranges'])))
# Assemble to a separate file, so interruption cannot corrupt the resumable base.
assembled = target.with_suffix('.assembled')
with assembled.open('wb') as output:
    with target.open('rb') as base:
        shutil.copyfileobj(base, output, 8 * 1024 * 1024)
    for part in parts:
        with part.open('rb') as source:
            shutil.copyfileobj(source, output, 8 * 1024 * 1024)
assert assembled.stat().st_size == total
assembled.replace(target)
for part in parts:
    part.unlink()
plan_path.unlink()
print('Apple restore image download complete; Apple installer will validate the image.', flush=True)
