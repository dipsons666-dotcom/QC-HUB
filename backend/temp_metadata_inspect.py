from pathlib import Path
import json
from pprint import pprint

p = Path('backend/data/xlsform_metadata.json').resolve()
print('candidate', p)
print('exists', p.exists())
with p.open('r', encoding='utf-8') as f:
    data = json.load(f)
print('root_type', type(data).__name__)
questions = data.get('questions')
print('questions_type', type(questions).__name__)
print('questions_count', len(questions) if questions is not None else 'None')
if isinstance(questions, dict):
    first_key = next(iter(questions), None)
    print('first_key', first_key)
    print('first_meta_type', type(questions[first_key]).__name__)
    pprint(questions[first_key])
else:
    print('questions sample', questions[:3] if questions else questions)
