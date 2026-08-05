from app.decoded_questions import _load_metadata, _read_settings, _normalize_text, _decode_value
from pathlib import Path
metadata = _load_metadata(Path('data/xlsform_metadata.json'))
choices, questions = _read_settings(metadata)
payload = {'City_1':1,'Sector':2}
count=0
for question in questions:
    name = question['name']
    if name not in payload:
        continue
    raw_value = payload.get(name)
    print('FOUND', name, question.get('label'), 'raw=', raw_value)
    qtype=question.get('type','')
    list_name=None
    if isinstance(qtype,str):
        if qtype.startswith('select_one'):
            list_name=qtype.split('select_one',1)[1].strip()
        elif qtype.startswith('select_multiple'):
            list_name=qtype.split('select_multiple',1)[1].strip()
    print('list_name=',list_name,'decoded=', _decode_value(raw_value,list_name,choices,qtype))
    count+=1
print('count',count)
