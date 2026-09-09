"""Load the user's local configuration without evaluating or logging its values."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> dict[str, str]:
    values = {}
    for line in (path or ROOT / '.env').read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = json.loads(value) if value.startswith('"') else value.strip()
    return values


def configure_project_aliases() -> None:
    path = ROOT / '.env'
    values = load_env(path)
    aliases = {
        'COMMERCE_PROVIDER': values['CONTEXT_TEXT_PROVIDER'],
        'COMMERCE_MODEL': values['CONTEXT_TEXT_MODEL'],
        'COMMERCE_BASELINE_MODEL': values['LIVE_TRANSLATION_MODEL'],
        'COMMERCE_JUDGE_PROVIDER': 'aihubmix',
        'COMMERCE_JUDGE_MODEL': values['AIHUBMIX_TEXT_MODEL'],
        'COMMERCE_API_BUDGET_CNY': '300',
    }
    # Preserve original fields and any previously customized commerce settings.
    added = {key: value for key, value in aliases.items() if key not in values}
    if added:
        with path.open('a', encoding='utf-8') as file:
            file.write('\n# Commerce research project: roles and authorized budget\n')
            for key, value in added.items():
                file.write(key + '=' + json.dumps(value, ensure_ascii=False) + '\n')
    print(json.dumps({'updated_file': str(path), 'added_fields': list(added),
                      'secret_values_printed': False}, ensure_ascii=False))


if __name__ == '__main__':
    configure_project_aliases()
