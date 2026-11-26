#!/usr/bin/env python3
"""Schema-first inspector for the project data directory; never assumes data columns."""
from __future__ import annotations
import argparse, csv, json, sqlite3, zipfile
from pathlib import Path
from datetime import datetime
import pandas as pd

SUPPORTED = {'.csv', '.tsv', '.parquet', '.json', '.jsonl', '.db', '.sqlite', '.sqlite3'}

def inspect_table(path: Path, table: str | None = None) -> dict:
    ext = path.suffix.lower()
    if ext in {'.db', '.sqlite', '.sqlite3'}:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        return {'file':str(path),'kind':'sqlite','tables':[inspect_sqlite_table(con,t) for t in tables]}
    if ext == '.zip':
        with zipfile.ZipFile(path) as z:
            return {'file':str(path),'kind':'zip','members':[{'name':i.filename,'bytes':i.file_size} for i in z.infolist()]}
    if ext not in SUPPORTED:
        return {'file':str(path),'kind':'unsupported','bytes':path.stat().st_size}
    if ext == '.parquet': df = pd.read_parquet(path)
    elif ext == '.json': df = pd.read_json(path, lines=False, nrows=10000)
    elif ext == '.jsonl': df = pd.read_json(path, lines=True, nrows=10000)
    else: df = pd.read_csv(path, sep='\t' if ext == '.tsv' else ',', nrows=10000, low_memory=False)
    return frame_report(path, df, ext)

def inspect_sqlite_table(con, table):
    cols = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    df = pd.read_sql_query(f'SELECT * FROM "{table}" LIMIT 10000', con)
    out = frame_report(Path(table), df, 'sqlite-table')
    out['declared_columns'] = [{'name':c[1], 'type':c[2]} for c in cols]
    try: out['row_count'] = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except Exception: pass
    return out

def frame_report(path, df, kind):
    date_ranges = {}
    for c in df.columns:
        if any(k in str(c).lower() for k in ('date','time','timestamp','created','updated')):
            d = pd.to_datetime(df[c], errors='coerce', utc=True)
            if d.notna().any(): date_ranges[str(c)] = {'min_utc':d.min().isoformat(),'max_utc':d.max().isoformat()}
    return {'name':str(path),'kind':kind,'sampled_rows':len(df),'columns':[{'name':str(c),'dtype':str(df[c].dtype),'null_rate':round(float(df[c].isna().mean()),5),'sample':df[c].dropna().astype(str).head(3).tolist()} for c in df.columns], 'date_ranges_utc':date_ranges,'sample_rows':df.head(5).where(pd.notna(df),None).to_dict(orient='records')}

def main():
    p=argparse.ArgumentParser(); p.add_argument('--data-dir',default='data'); p.add_argument('--output',default='data_report.json'); a=p.parse_args()
    root=Path(a.data_dir); files=sorted(x for x in root.rglob('*') if x.is_file()) if root.exists() else []
    results=[]; errors=[]
    for f in files:
        try: results.append(inspect_table(f))
        except Exception as e: errors.append({'file':str(f),'error':f'{type(e).__name__}: {e}'})
    report={'generated_at':datetime.now().astimezone().isoformat(),'data_dir':str(root.resolve()),'file_count':len(files),'inspected':results,'errors':errors,'note':'Date ranges are reported in UTC when parseable. Large CSVs are sampled up to 10,000 rows; row counts are not inferred.'}
    Path(a.output).write_text(json.dumps(report,indent=2,default=str))
    print(json.dumps(report,indent=2,default=str))
if __name__=='__main__': main()
