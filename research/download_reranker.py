"""Fetch only pinned public model data; no credentials, pickle or remote Python."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

from research.model_config import ROOT

REPO = 'Qwen/Qwen3-Reranker-0.6B'
REVISION = 'e61197ed45024b0ed8a2d74b80b4d909f1255473'
FILES = ['config.json','generation_config.json','merges.txt','model.safetensors',
         'tokenizer.json','tokenizer_config.json','vocab.json','chat_template.jinja','README.md']


def sha(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file,'sha256').hexdigest()


def main():
    folder = ROOT/'models/Qwen3-Reranker-0.6B'
    folder.mkdir(parents=True, exist_ok=True)
    with urlopen(f'https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true',timeout=30) as response:
        metadata = json.load(response)
    if metadata['sha'] != REVISION:
        raise ValueError('Pinned revision mismatch')
    siblings = {f['rfilename']:f for f in metadata['siblings']}
    records = []
    for name in FILES:
        item = siblings[name]
        expected_size = item['size']
        expected_hash = item.get('lfs',{}).get('sha256')
        target = folder/name
        if not target.exists():
            partial = target.with_suffix(target.suffix+'.part')
            for attempt in range(3):
                size = partial.stat().st_size if partial.exists() else 0
                request = Request(f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}',
                    headers={'Range':f'bytes={size}-'} if size else {})
                try:
                    with urlopen(request,timeout=45) as response:
                        append = size > 0 and response.status == 206
                        if append and not response.headers.get('Content-Range','').startswith(f'bytes {size}-'):
                            raise ValueError('Range response does not match the partial download')
                        with partial.open('ab' if append else 'wb') as output:
                            while chunk := response.read(1024*1024):
                                output.write(chunk)
                    if partial.stat().st_size != expected_size:
                        raise ValueError('Downloaded size mismatch')
                    if expected_hash and sha(partial) != expected_hash:
                        raise ValueError('Model SHA256 differs from repository LFS metadata')
                    partial.replace(target)
                    break
                except Exception as error:
                    print(json.dumps({'file':name,'attempt':attempt+1,'error_type':type(error).__name__}),flush=True)
                    if attempt == 2:
                        raise RuntimeError('Public model download failed; partial bytes retained for explicit resume') from None
                    time.sleep(2)
        digest = sha(target)
        if target.stat().st_size != expected_size or expected_hash and digest != expected_hash:
            raise ValueError('Existing model file failed verification')
        records.append({'file':name,'bytes':target.stat().st_size,'sha256':digest,'upstream_lfs_sha256':expected_hash})
        print(json.dumps(records[-1]),flush=True)
    manifest = {'repository':REPO,'revision':REVISION,'fetched_at':datetime.now(timezone.utc).isoformat(),
        'license':'Apache-2.0 per publisher model card','files':records,
        'usage':'Local ranking only; not a locally trained model; pretraining benchmark overlap is not established.'}
    (ROOT/'evidence/reranker_download.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
