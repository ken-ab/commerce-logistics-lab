"""Read public model prices using the provider's documented metadata endpoint."""
from datetime import datetime, timezone
import json
from urllib.request import urlopen
from pathlib import Path

url = 'https://aihubmix.com/api/v1/models?model=gpt-5.6-luna'
with urlopen(url, timeout=30) as response:
    data = json.load(response)
models = [{k: row.get(k) for k in ('model_id', 'pricing', 'features', 'context_length', 'max_output')}
          for row in data.get('data', []) if row.get('model_id') == 'gpt-5.6-luna']
result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'source': url,
          'api_documentation': 'https://docs.aihubmix.com/en/api/Models-API',
          'unit_caution': 'API documentation labels USD/1K, but examples use familiar per-million prices. Verify against the visible model card before using for cost limits.',
          'models': models, 'credentials_used': False}
Path(__file__).with_name('aihubmix_price_observation.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2))
