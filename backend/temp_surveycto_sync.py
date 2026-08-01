from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.database import get_session_local, get_engine
from app.main import get_survey_platform_config, fetch_submission_payloads, _hash_payload
from app.models import RawSurveyCTOSubmission, ImportJob

load_dotenv(Path('.').resolve() / '.env', override=True)
config = get_survey_platform_config()
items = fetch_submission_payloads(config)
print('fetched', len(items))

SessionLocal = get_session_local()
db = SessionLocal()
stored = 0
updated = 0
skipped = 0

for item in items:
    submission_id = str(item.get('submission_id') or item.get('id') or item.get('KEY') or item.get('submissionkey') or '')
    if not submission_id:
        skipped += 1
        continue

    payload_data = item.get('data') or item.get('payload') or item
    if not isinstance(payload_data, dict):
        payload_data = {'value': payload_data}

    instrument_code = str(item.get('instrument_code') or config['instrument_code'])
    source_hash = _hash_payload(payload_data)

    existing = db.execute(select(RawSurveyCTOSubmission).where(RawSurveyCTOSubmission.submission_key == submission_id)).scalars().first()
    if existing is None:
        raw_submission = RawSurveyCTOSubmission(
            instrument_code=instrument_code,
            submission_key=submission_id,
            source_hash=source_hash,
            raw_payload=payload_data,
            fetched_at=datetime.now(timezone.utc),
        )
        db.add(raw_submission)
        db.flush()
        import_job = ImportJob(submission_key=submission_id, instrument_code=instrument_code, status='queued')
        db.add(import_job)
        stored += 1
    else:
        existing.instrument_code = instrument_code
        existing.raw_payload = payload_data
        existing.source_hash = source_hash
        existing.fetched_at = datetime.now(timezone.utc)
        updated += 1

try:
    db.commit()
finally:
    db.close()

print('stored', stored)
print('updated', updated)
print('skipped', skipped)

engine = get_engine()
with engine.connect() as conn:
    raw_count = conn.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
    import_job_count = conn.execute(select(func.count()).select_from(ImportJob)).scalar_one()
    print('raw_count', raw_count)
    print('import_job_count', import_job_count)
