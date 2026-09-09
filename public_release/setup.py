"""Explicit local setup commands. No package installation or paid model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

from public_release.evidence import ROOT, read, sha, unpack

UPSTREAM = ('https://github.com/anthropics/commerce-agents.git', 'fd4d59224ab96b43c6dc6888207c67b3bd5a24cf')
ESCI_REVISION = '7916cdf6ab75a462e77f20ab40428a10923998d5'


def init():
    result = unpack()
    for source, target in [('.env.example', '.env'), ('delivery_budget_policy.example.json', 'delivery_budget_policy.json')]:
        if not (ROOT / target).exists():
            shutil.copyfile(ROOT / source, ROOT / target)
    # Frozen clients construct these ledgers before the operational adapter replaces them.
    for target in ['research/budget_policy.json', 'model_selection_100/budget_policy.json']:
        if not (ROOT / target).exists():
            shutil.copyfile(ROOT / 'delivery_budget_policy.example.json', ROOT / target)
    return result


def upstream():
    destination = ROOT / 'upstream/commerce-agents'
    git = shutil.which('git')
    if not git:
        raise RuntimeError('Git must be available on PATH')
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([git, 'clone', '--no-checkout', UPSTREAM[0], str(destination)], check=True)
        subprocess.run([git, '-C', str(destination), 'checkout', '--detach', UPSTREAM[1]], check=True)
    revision = subprocess.check_output([git, '-C', str(destination), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != UPSTREAM[1]:
        raise RuntimeError('Existing upstream checkout differs; preserve it and inspect before continuing')
    return {'upstream_commit': revision, 'packages_installed': False}


def catalog():
    init()
    audit = read(ROOT / 'evidence/esci_data_audit.json')
    destination = ROOT / 'upstream/esci-data/shopping_queries_dataset'
    destination.mkdir(parents=True, exist_ok=True)
    for row in audit['files']:
        name = row['file']
        path = destination / name
        if path.exists():
            if path.stat().st_size != row['expected_bytes'] or sha(path) != row['expected_sha256']:
                raise ValueError('Existing source file differs: ' + name)
            continue
        url = 'https://media.githubusercontent.com/media/amazon-science/esci-data/' + ESCI_REVISION + '/shopping_queries_dataset/' + name
        partial = path.with_suffix('.download')
        with urllib.request.urlopen(url, timeout=120) as response, partial.open('wb') as target:
            shutil.copyfileobj(response, target, 1024 * 1024)
        if partial.stat().st_size != row['expected_bytes'] or sha(partial) != row['expected_sha256']:
            raise ValueError('Downloaded file failed its pinned checksum: ' + name)
        partial.replace(path)
    from commerce_lab.catalog import build_catalog
    return build_catalog()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'upstream', 'catalog'])
    action = parser.parse_args().action
    print(json.dumps({'init': init, 'upstream': upstream, 'catalog': catalog}[action](), indent=2))
