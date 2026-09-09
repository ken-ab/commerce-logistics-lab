import json
from pathlib import Path
import pytest

from ranking.freeze import METHOD_FILES, sha, validate


def test_frozen_method_rejects_parameter_or_source_drift(tmp_path):
    files = {}
    for name in METHOD_FILES:
        path = tmp_path/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('frozen fixture',encoding='utf-8')
        files[name] = sha(path)
    path = tmp_path/'freeze.json'
    path.write_text(json.dumps({'status':'frozen_before_final_test','files':files,
        'method':{'instruction':'product','batch_size':16,'max_tokens':512}}),encoding='utf-8')
    validate(path,instruction='product',batch_size=16,max_tokens=512,root=tmp_path)
    with pytest.raises(ValueError,match='differs'):
        validate(path,instruction='generic',batch_size=16,max_tokens=512,root=tmp_path)
    (tmp_path/'ranking/model.py').write_text('changed model',encoding='utf-8')
    with pytest.raises(ValueError,match='input changed'):
        validate(path,instruction='product',batch_size=16,max_tokens=512,root=tmp_path)
