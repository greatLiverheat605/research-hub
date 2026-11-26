"""Colab-side cooperative checkpoint API. The worker runs the entry again after a commit."""
import hashlib
import os
from pathlib import Path

CHUNK = 8 * 1024 * 1024


class Boundary(BaseException):
    pass


def resume_path():
    value = os.environ.get('RH_RESUME_PATH')
    return Path(value) if value else None


def resume_step():
    return int(os.environ.get('RH_RESUME_STEP', '-1'))


def commit(step, filename):
    if type(step) is not int or not resume_step() < step < 10**16:
        raise ValueError('checkpoint step must increase')
    prefix = os.environ['RH_CKPT_PREFIX']
    digest = hashlib.sha256()
    size = 0
    with open(filename, 'rb') as src:
        while True:
            data = src.read(CHUNK)
            if not data:
                break
            Path(f'{prefix}.{size // CHUNK}').write_bytes(data)
            size += len(data)
            digest.update(data)
    raise Boundary(step, size, digest.hexdigest())
