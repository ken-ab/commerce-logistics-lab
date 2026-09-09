"""Verify and extract the published experiment records without making API calls."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def contained(name, root=ROOT):
    relative = PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
        raise ValueError('Invalid relative evidence path')
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Evidence must remain in the repository')
    return target


def unpack(root=ROOT):
    manifest = read(root / 'public_release/archives.json')
    extracted = 0
    for archive in manifest['archives']:
        path = contained(archive['path'], root)
        if sha(path) != archive['sha256']:
            raise ValueError('Archive SHA256 mismatch: ' + archive['path'])
        with zipfile.ZipFile(path) as bundle:
            if sorted(bundle.namelist()) != sorted(archive['members']):
                raise ValueError('Unexpected archive members')
            for name, expected in archive['members'].items():
                target = contained(name, root)
                if target.exists():
                    if sha(target) != expected['sha256']:
                        raise FileExistsError('Existing evidence differs; preserve it: ' + name)
                    continue
                data = bundle.read(name)
                if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                    raise ValueError('Evidence content mismatch: ' + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                extracted += 1
    return {'verified_archives': len(manifest['archives']), 'newly_extracted_files': extracted,
            'network_calls': 0, 'paid_calls': 0}


def verified_snapshot(root=ROOT):
    manifest = read(root / 'public_release/release.json')
    for name, digest in manifest['runtime_sha256'].items():
        path = contained(name, root)
        if not path.is_file() or sha(path) != digest:
            raise RuntimeError('Public release changed or is incomplete: ' + name)
    snapshot = read(root / 'public_release/research_snapshot.json')
    return snapshot


if __name__ == '__main__':
    print(json.dumps(unpack(), indent=2))
