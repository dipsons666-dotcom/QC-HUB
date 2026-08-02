from pathlib import Path
import json
import sys
sys.path.insert(0, 'backend')
from app.main import _load_xlsform_metadata, _XLSFORM_METADATA

p = Path('backend/data/xlsform_metadata.json').resolve()
print('candidate', p)
print('exists', p.exists())
try:
    with p.open('r', encoding='utf-8') as f:
        data = json.load(f)
    print('json_type', type(data).__name__)
    print('has_questions', isinstance(data, dict) and 'questions' in data)
    print('questions_count', len(data.get('questions', {})) if isinstance(data, dict) else 'n/a')
except Exception as exc:
    import traceback
    print('json_error', repr(exc))
    traceback.print_exc()

try:
    _load_xlsform_metadata()
    print('loader_type', type(_XLSFORM_METADATA).__name__)
    print('loader_has_questions', isinstance(_XLSFORM_METADATA, dict) and 'questions' in _XLSFORM_METADATA)
    print('loader_questions_count', len(_XLSFORM_METADATA.get('questions', {})) if isinstance(_XLSFORM_METADATA, dict) else 'n/a')
except Exception as exc:
    import traceback
    print('load_error', repr(exc))
    traceback.print_exc()
