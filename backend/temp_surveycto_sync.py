import argparse
import json
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.database import get_session_local, get_engine
from app.main import get_survey_platform_config, fetch_submission_payloads, _hash_payload
from app.models import RawSurveyCTOSubmission, ImportJob


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    tmp_path.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run SurveyCTO sync for QC-HUB')
    parser.add_argument('--status-file', type=str, help='Path to write sync result JSON')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.status_file:
        status_path = Path(args.status_file)
    else:
        status_path = None

    try:
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

        result = {
            'ok': True,
            'stored': stored,
            'updated': updated,
            'skipped': skipped,
            'raw_count': None,
            'import_job_count': None,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        try:
            engine = get_engine()
            with engine.connect() as conn:
                result['raw_count'] = conn.execute(select(func.count()).select_from(RawSurveyCTOSubmission)).scalar_one()
                result['import_job_count'] = conn.execute(select(func.count()).select_from(ImportJob)).scalar_one()
        except Exception:
            pass

        if status_path is not None:
            write_json(status_path, result)
        print(json.dumps(result, default=str))
        return 0
    except Exception as exc:
        error_result = {'ok': False, 'error': str(exc), 'timestamp': datetime.now(timezone.utc).isoformat()}
        if status_path is not None:
            try:
                write_json(status_path, error_result)
            except Exception:
                pass
        print(json.dumps(error_result, default=str))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
