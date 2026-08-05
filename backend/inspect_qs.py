from app.decoded_questions import _load_metadata, _read_settings
from pathlib import Path
metadata = _load_metadata(Path('data/xlsform_metadata.json'))
choices, questions = _read_settings(metadata)
for q in questions:
    if q.get('name') in ('Sector','City_1'):
        print(q)
print('total questions:', len(questions))
