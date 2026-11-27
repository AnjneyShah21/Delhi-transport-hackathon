#!/usr/bin/env python3
"""Schema-first inspector for the project data directory; never assumes data columns."""
from __future__ import annotations
import argparse, csv, json, sqlite3, tempfile, zipfile
from pathlib import Path
from datetime import datetime
import pandas as pd

SUPPORTED = {'.csv', '.tsv', '.parquet', '.json', '.jsonl', '.db', '.sqlite', '.sqlite3', '.7z'}

def inspect_table(path: Path, table: str | None = None) -> dict:
    ext = path.suffix.lower()
    if ext in {'.db', '.sqlite', '.sqlite3'}:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        return {'file':str(path),'kind':'sqlite','tables':[inspect_sqlite_table(con,t) for t in tables]}
    if ext == '.zip':
        with zipfile.ZipFile(path) as z:
            members = []
            tables = []
            for info in z.infolist():
                members.append({'name':info.filename,'bytes':info.file_size})
                suffix = Path(info.filename).suffix.lower()
                if suffix not in {'.csv', '.txt'} or info.is_dir() or info.filename.startswith('__MACOSX/') or '/._' in info.filename:
                    continue
                with z.open(info) as stream:
                    for encoding in ('utf-8-sig', 'cp1252', 'latin1'):
                        try:
                            stream.seek(0)
                            df = pd.read_csv(stream, sep=',', encoding=encoding, nrows=10_000, low_memory=False)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        raise ValueError(f'Could not decode {info.filename} using UTF-8, CP-1252, or Latin-1')
                table = frame_report(Path(info.filename), df, f'zip-{suffix[1:]}-sample')
                table.update(member_bytes=info.file_size, encoding=encoding, row_count='not counted; schema/null report uses first 10,000 rows')
                tables.append(table)
            return {'file':str(path),'kind':'zip','members':members,'sampled_tables':tables}
    if ext == '.7z':
        try:
            import py7zr
        except ImportError:
            return {'file':str(path),'kind':'7z','bytes':path.stat().st_size,'note':'Install py7zr to inspect member list.'}
        with py7zr.SevenZipFile(path, 'r') as z:
            infos = z.list()
            members = [{'name':i.filename,'bytes':i.uncompressed,'compressed_bytes':i.compressed} for i in infos]
            sample_member = next((i.filename for i in infos if i.filename.endswith('.db')), None)
        sample = None
        if sample_member:
            with tempfile.TemporaryDirectory(prefix='dkp-inspect-') as directory:
                with py7zr.SevenZipFile(path, 'r') as z:
                    z.extract(path=directory, targets=[sample_member])
                database = Path(directory) / Path(sample_member).name
                sample = inspect_table(database)
                sample['file'] = sample_member
        return {'file':str(path),'kind':'7z','bytes':path.stat().st_size,'members':members,'sampled_database':sample}
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
    names = [c[1] for c in cols]
    temporal = [n for n in names if any(k in n.lower() for k in ('date','time','timestamp','created','updated'))]
    selected = ['COUNT(*)']
    for name in temporal:
        selected.extend([f'MIN("{name}")', f'MAX("{name}")'])
    try:
        values = con.execute(f'SELECT {",".join(selected)} FROM "{table}"').fetchone()
        out['row_count'] = values[0]
        for i, name in enumerate(temporal):
            low, high = values[1+i*2], values[2+i*2]
            if low is None:
                continue
            if 'timestamp' in name.lower() and str(low).replace('.','',1).isdigit():
                d = pd.to_datetime([float(low),float(high)], unit='s', utc=True, errors='coerce')
                out.setdefault('date_ranges_utc_full', {})[name] = {'min':d[0].isoformat(),'max':d[1].isoformat()}
            elif 'time' in name.lower():
                out.setdefault('time_ranges_full', {})[name] = {'min':str(low),'max':str(high)}
            elif 'date' in name.lower() and not any(k in name.lower() for k in ('created','updated')):
                out.setdefault('date_ranges_local_full', {})[name] = {'min':str(low),'max':str(high)}
            elif any(k in name.lower() for k in ('created','updated')):
                d = pd.to_datetime([low,high], errors='coerce', utc=True, format='mixed')
                if d.notna().all(): out.setdefault('date_ranges_utc_full', {})[name] = {'min':d[0].isoformat(),'max':d[1].isoformat()}
    except Exception:
        pass
    return out

def frame_report(path, df, kind):
    date_ranges = {}
    time_ranges = {}
    for c in df.columns:
        name = str(c).lower()
        values = df[c].dropna()
        if not len(values):
            continue
        if 'time' in name and values.astype(str).str.match(r'^\d{1,3}:\d{2}:\d{2}$').all():
            time_ranges[str(c)] = {'min':str(values.astype(str).min()),'max':str(values.astype(str).max())}
        elif 'date' in name and values.astype(str).str.fullmatch(r'\d{8}').all():
            d = pd.to_datetime(values.astype(str), format='%Y%m%d', errors='coerce')
            if d.notna().any(): date_ranges[str(c)] = {'min_local':d.min().date().isoformat(),'max_local':d.max().date().isoformat()}
        elif 'timestamp' in name and pd.to_numeric(values, errors='coerce').notna().all():
            numeric = pd.to_numeric(values, errors='coerce')
            if numeric.median() > 1_000_000_000:
                d = pd.to_datetime(numeric, unit='s', errors='coerce', utc=True)
                if d.notna().any(): date_ranges[str(c)] = {'min_utc':d.min().isoformat(),'max_utc':d.max().isoformat()}
        elif any(k in name for k in ('date','timestamp','created','updated')):
            d = pd.to_datetime(df[c], errors='coerce', utc=True, format='mixed')
            if d.notna().any():
                if 'date' in name and not any(k in name for k in ('created','updated')):
                    date_ranges[str(c)] = {'min_local':d.min().date().isoformat(),'max_local':d.max().date().isoformat()}
                else:
                    date_ranges[str(c)] = {'min_utc':d.min().isoformat(),'max_utc':d.max().isoformat()}
    return {'name':str(path),'kind':kind,'sampled_rows':len(df),'columns':[{'name':str(c),'dtype':str(df[c].dtype),'null_rate':round(float(df[c].isna().mean()),5),'sample':df[c].dropna().astype(str).head(3).tolist()} for c in df.columns], 'date_ranges_utc':date_ranges,'time_ranges':time_ranges,'sample_rows':df.head(5).where(pd.notna(df),None).to_dict(orient='records')}

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
