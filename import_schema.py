import os
from pathlib import Path
import psycopg2

url = os.getenv('DATABASE_URL', 'postgresql://qc_user1:%40Tunji900@localhost:5432/qc_hub')

conn = psycopg2.connect(url)
conn.autocommit = True
cur = conn.cursor()

sql_text = Path('platform_schema.sql').read_text(encoding='utf-8')
cur.execute(sql_text)
print('Schema imported successfully')
cur.close()
conn.close()
