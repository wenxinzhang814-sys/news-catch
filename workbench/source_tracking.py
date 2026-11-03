"""SQLite snapshot history, pinned evidence pairs and formal daily observations."""
import hashlib
import json
from datetime import datetime,timedelta,timezone
from page_changes import change_set

TZ=timezone(timedelta(hours=8))


def source_key(source):
    from urllib.parse import urlsplit,urlunsplit
    from urllib.parse import parse_qsl,urlencode
    p=urlsplit(source['url']);query=urlencode([(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')])
    url=urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path.rstrip('/'),query,''))
    return hashlib.sha256(json.dumps([source['kind'],url,source.get('mode','articles') if source['kind']=='webpage' else ''],ensure_ascii=False).encode()).hexdigest()[:24]


def schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS source_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,source_key TEXT NOT NULL,source_id TEXT,url TEXT,kind TEXT,mode TEXT,version INTEGER,content_hash TEXT,content TEXT,created_at TEXT,last_seen_at TEXT);
    CREATE INDEX IF NOT EXISTS snapshot_source_time ON source_snapshots(source_key,id DESC);
    CREATE TABLE IF NOT EXISTS event_snapshot_links(event_id INTEGER PRIMARY KEY,before_id INTEGER,after_id INTEGER);
    CREATE TABLE IF NOT EXISTS source_observation_days(source_key TEXT,day TEXT,success_count INTEGER DEFAULT 0,positive_count INTEGER DEFAULT 0,failure_count INTEGER DEFAULT 0,last_checked_at TEXT,PRIMARY KEY(source_key,day));
    ''')


def store_snapshot(db,source,text,version,checked):
    key=source_key(source);digest=hashlib.sha256(text.encode()).hexdigest()
    previous=db.execute('SELECT id,content_hash,version FROM source_snapshots WHERE source_key=? ORDER BY id DESC LIMIT 1',(key,)).fetchone()
    if previous and previous[1]==digest and previous[2]==version:
        db.execute('UPDATE source_snapshots SET last_seen_at=? WHERE id=?',(checked,previous[0]));return previous[0]
    cur=db.execute('INSERT INTO source_snapshots(source_key,source_id,url,kind,mode,version,content_hash,content,created_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (key,source['id'],source['url'],source['kind'],source.get('mode','articles'),version,digest,text,checked,checked))
    return cur.lastrowid


def snapshot_pair(db,source,old,new,version,checked):
    before=None
    if old and old['snapshot']:
        before=store_snapshot(db,source,old['snapshot'],old.get('snapshot_version',0),old.get('checked_at') or checked)
    after=store_snapshot(db,source,new,version,checked)
    return dict(before_id=before,after_id=after)


def prune(db):
    # Linked news, including deleted news, retains both sides indefinitely.
    db.execute('''DELETE FROM source_snapshots WHERE id NOT IN
        (SELECT before_id FROM event_snapshot_links WHERE before_id IS NOT NULL UNION SELECT after_id FROM event_snapshot_links WHERE after_id IS NOT NULL)
        AND id NOT IN (SELECT id FROM (SELECT id,ROW_NUMBER() OVER(PARTITION BY source_key ORDER BY id DESC) AS n FROM source_snapshots) WHERE n<=10)''')


def observe(db,source,status,count,checked):
    day=datetime.fromisoformat(checked.replace('Z','+00:00')).astimezone(TZ).date().isoformat()
    db.execute('''INSERT INTO source_observation_days VALUES (?,?,?,?,?,?) ON CONFLICT(source_key,day) DO UPDATE SET
        success_count=success_count+excluded.success_count,positive_count=positive_count+excluded.positive_count,
        failure_count=failure_count+excluded.failure_count,last_checked_at=excluded.last_checked_at''',
        (source_key(source),day,int(status=='正常'),int(status=='正常' and count>0),int(status!='正常'),checked))


def observation_status(rows,today=None,changes=False):
    today=today or datetime.now(TZ).date()
    by_day={r['day']:r for r in rows};current=by_day.get(str(today));cursor=today
    # Today's check may not have run yet. Yesterday can end a valid streak.
    if current is None: cursor-=timedelta(days=1)
    streak=0
    while True:
        row=by_day.get(str(cursor))
        if not row or row['success_count']<1 or row['positive_count'] or row['failure_count']: break
        streak+=1;cursor-=timedelta(days=1)
    return dict(zero_days=streak,suspected=streak>=7 and not changes,no_change=streak>=7 and changes,last_observation=max(by_day) if by_day else '',label=('连续'+str(streak)+'天无变化' if changes else '连续'+str(streak)+'天未取得条目') if streak else '观察不足或已中断')


def history(db,key):
    db.row_factory=__import__('sqlite3').Row
    return [dict(row) for row in db.execute('''SELECT s.id,s.source_key,s.version,length(s.content) AS chars,s.created_at,s.last_seen_at,
        EXISTS(SELECT 1 FROM event_snapshot_links l WHERE l.before_id=s.id OR l.after_id=s.id) AS pinned
        FROM source_snapshots s WHERE source_key=? ORDER BY s.id DESC''',(key,))]


def compare(db,before_id,after_id):
    db.row_factory=__import__('sqlite3').Row
    before=db.execute('SELECT * FROM source_snapshots WHERE id=?',(int(before_id),)).fetchone()
    after=db.execute('SELECT * FROM source_snapshots WHERE id=?',(int(after_id),)).fetchone()
    if not before or not after: raise ValueError('快照不存在或已超出保留范围')
    if before['source_key']!=after['source_key']: raise ValueError('只允许比较同一来源、类型及模式的快照')
    if before['id']>after['id']: raise ValueError('请选择较早版本作为变更前快照')
    compatible=before['version']==after['version'] and before['version']>0
    message='' if compatible else '旧格式或不同版本快照只供查看，不生成可靠差异'
    changes=None
    if compatible:
        from body_fetcher import FetchError
        try: changes=change_set(before['content'],after['content'])
        except FetchError: message='差异超过 60,000 字符，请核对下方前后全文；未截断正文或差异'
    return dict(before=dict(before),after=dict(after),compatible=compatible,message=message,changes=changes)
