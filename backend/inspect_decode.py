from app.decoded_questions import decode_submission_to_question_rows
from pathlib import Path
metadata_path = Path('data/xlsform_metadata.json')
rows = decode_submission_to_question_rows({'City_1':1,'Sector':2}, metadata_path=metadata_path)
print(rows)
