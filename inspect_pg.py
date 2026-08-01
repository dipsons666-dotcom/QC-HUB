import psycopg2

conn = psycopg2.connect('postgresql://qc_user1:%40Tunji900@localhost:5432/postgres')
cur = conn.cursor()
cur.execute("SELECT current_user, current_database()")
print('current_user/current_database:', cur.fetchone())
cur.execute("SELECT rolname, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = current_user")
print('role_flags:', cur.fetchone())
cur.execute("SELECT datname, datdba::regrole, datacl FROM pg_database WHERE datname = 'qc_hub'")
print('database_info:', cur.fetchone())
cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('public','raw','clean','qc','app')")
print('existing_schemas:', cur.fetchall())
cur.close()
conn.close()
