"""Persistent collection, evidence review and delivery; optional review extraction."""
import concurrent.futures
from contextlib import closing
import difflib
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET
from rules import RuleParser, contains, normalize
from analysis_quality import (ANALYSIS_RULES, FEATURE_SCHEMA, prepare_article,
                              validate_features, apply_features, validate_polarity, version_key)
from analysis_quality import feature_title
from body_fetcher import fetch_body, download_resource, FetchError
from page_changes import (structured_snapshot, change_set, SNAPSHOT_VERSION, CHANGE_ANALYSIS_RULES,
                          classify_changes, CHANGE_FACT_SCHEMA, validate_change_facts)
import source_tracking as tracking
import long_analysis as longform
import analysis_summary as brieftext

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
DATA = Path(os.getenv('TREND_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=8))
DB = DATA / 'trenddesk.sqlite3'
CONFIG_FILE = DATA / 'config.json'
CATEGORIES = ['车载 OTA', 'C 端模型', '语音能力', '风险舆情', '生态合作']
REGIONS = {
 '欧洲':['Europe','European','Germany','France','Spain','Italy','EU','UK','欧洲'],
 '东南亚':['Southeast Asia','Malaysia','Singapore','Indonesia','Vietnam','Thailand','Philippines','东南亚'],
 '俄罗斯及独联体':['Russia','Russian','Kazakhstan','Belarus','Россия','Яндекс','俄罗斯'],
 '中东':['Middle East','Saudi','UAE','Dubai','Qatar','Arabic','中东'],
 '拉美':['Latin America','Brazil','Mexico','Argentina','Chile','Colombia','Brasil','México','拉美']}
UNKNOWN_OPTION = '待确认'
REVIEW_REGIONS = ['东南亚','欧洲','俄罗斯及独联体','中东','拉美','北美','全球','其他','未涉及']
ACTIONS = ['体验验证','持续观察','风险上报','无需动作']
STAGES = ['仅宣布','测试中','灰度','已上线','待确认']
UNKNOWN_OPTION = '待确认'
FIELDS = {'title':'标题','category':'所属维度','region':'地区/国家','source':'站点名称','published_at':'发布时间','link':'原文链接','original_summary':'原文摘要','stage':'发布阶段','processing_status':'处理状态','action':'建议动作','include_in_weekly':'纳入周报','processing_note':'处理建议','language':'涉及语言','company':'公司/产品','summary':'摘要','ai_score':'AI评分','confidence':'置信度','assignee':'处理人','original_title':'原标题','change':'核心变化','relevance':'业务相关性','official_link':'官方链接','aux_links':'辅助链接','evidence':'证据摘录','evidence_zh':'证据中文释义','status':'平台推送状态'}
FIELDS['source']='采集来源（原配置名称）'
FIELDS['site_name']='站点名称'
DEFAULT_FIELD_MAP = {}
FIELD_HELP = {'category':'单选：输出采集池的五个维度名称','region':'单选：输出一个地区/国家选项字符串；多区域或无法判断时输出“其他”','published_at':'ISO 8601 原始发布时间；未知时省略，由接收工作流转换为日期','language':'多选：按清单语言选项输出数组；无法判断时输出 ["待确认"]，表格需有该选项','company':'单选：按清单规范公司名称，无法匹配时输出其他','processing_status':'采集池业务处理状态，独立于平台待审核/待推送/已推送','include_in_weekly':'已被生成的周报收录为是，否则为否；不代表周报已发布','original_summary':'采集原文/摘要的前 1200 字，不使用 AI 中文摘要替代','processing_note':'优先人工填写，否则结合建议动作与核心变化','ai_score':'按业务相关性换算：高=5、中=3、低=1；待确认时省略，非独立评分模型','assignee':'处理人用户 ID；未填写时不输出，接收工作流转换为人员字段','status':'仅表示平台推送状态，请勿映射为采集池处理状态'}
FIELD_HELP.update(site_name='模型识别或人工编辑的原文发布站点；无模型时优先 RSS/网页站点元数据',published_at='北京时间 GMT+8，YYYY/MM/DD HH:MM；发布时间未知时输出空字符串，不使用采集时间替代')
LOCK = threading.RLock()
RUN_LOCK = threading.Lock()
PUSH_LOCK = threading.Lock()
ANALYSIS_LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()
FETCH_JOB = dict(running=False,job_id='',total=0,completed=0,success=0,failed=0,skipped=0,results=[])
ANALYSIS_JOB = dict(running=False,job_id='',mode='',ids=[],total=0,completed=0,success=0,failed=0,results=[],stage='',parts_done=0,parts_total=0)
SOURCE_TEST_SLOTS=threading.BoundedSemaphore(3)
SOURCE_TEST_LOCK=threading.Lock()
SOURCE_TEST_ACTIVE=set()
WEBHOOK_TIMEOUT = 8

def now(): return datetime.now(timezone.utc).isoformat()
def digest(v): return hashlib.sha256(v.encode()).hexdigest()
def plain(v): return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', v or ''))).strip()
def canon(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k not in ('fbclid','gclid')]), ''))
def stamp(v):
    if not v: return ''
    try: d = parsedate_to_datetime(v)
    except (ValueError,TypeError):
        try: d = datetime.fromisoformat(v.replace('Z','+00:00'))
        except (ValueError,TypeError): return ''
    return d.replace(tzinfo=d.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()

def sql(query, args=(), one=False):
    with LOCK, closing(sqlite3.connect(DB, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        cur = db.execute(query, args)
        if query.lstrip().upper().startswith('SELECT'):
            rows = [dict(r) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows
        return cur.lastrowid

def init_db():
    with closing(sqlite3.connect(DB)) as db, db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, title TEXT, original_title TEXT, link TEXT, source TEXT, source_level TEXT, published_at TEXT, summary TEXT, evidence TEXT, evidence_zh TEXT, region TEXT, category TEXT, stage TEXT, relevance TEXT, action TEXT, confidence TEXT, company TEXT, language TEXT, change TEXT, status TEXT, analysis_mode TEXT, matched_rules TEXT, aux_links TEXT DEFAULT '[]', created_at TEXT, updated_at TEXT, revision INTEGER DEFAULT 1, admission_day TEXT);
        CREATE TABLE IF NOT EXISTS raw_items(fingerprint TEXT PRIMARY KEY, data TEXT, decision TEXT, discovered_at TEXT);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, trigger TEXT, source_count INTEGER DEFAULT 0, fetched_count INTEGER DEFAULT 0, matched_count INTEGER DEFAULT 0, candidate_count INTEGER DEFAULT 0, analyzed_count INTEGER DEFAULT 0, filtered_count INTEGER DEFAULT 0, overflow_count INTEGER DEFAULT 0, error TEXT DEFAULT '[]', details TEXT DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS source_state(id TEXT PRIMARY KEY, status TEXT, checked_at TEXT, error TEXT, item_count INTEGER, snapshot TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, batch_key TEXT UNIQUE, payload TEXT, article_ids TEXT, status TEXT, attempts INTEGER DEFAULT 0, response TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS reports(week TEXT PRIMARY KEY, markdown TEXT, event_ids TEXT, generated_at TEXT);
        CREATE TABLE IF NOT EXISTS scheduler(day TEXT PRIMARY KEY, status TEXT, error TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS event_history(id INTEGER PRIMARY KEY,event_id INTEGER,operation TEXT,snapshot TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY,applied_at TEXT);
        ''')
        columns={row[1] for row in db.execute('PRAGMA table_info(events)')}
        for name,default in [('processing_status','待处理'),('processing_note',''),('assignee',''),('manual_body',None),('manual_body_updated_at',''),('deleted_at',''),('site_name',''),('fetched_body',None),('fetched_body_updated_at',''),('fetch_status','未抓取'),('fetch_error',''),('analysis_details','')]:
            if name not in columns: db.execute('ALTER TABLE events ADD COLUMN '+name+' TEXT DEFAULT '+('NULL' if default is None else repr(default)))
        source_columns={row[1] for row in db.execute('PRAGMA table_info(source_state)')}
        for name,definition in [('fail_streak','INTEGER DEFAULT 0'),('last_success_at',"TEXT DEFAULT ''"),('snapshot_version','INTEGER DEFAULT 0')]:
            if name not in source_columns: db.execute('ALTER TABLE source_state ADD COLUMN '+name+' '+definition)
        tracking.schema(db)

def source_level(url):
    host = urlsplit(url).hostname or ''
    if host in ('habr.com','teslamotorsclub.com'): return 'C 级'
    official = ['blog.google','openai.com','developers.openai.com','support.claude.com','mistral.ai','cerence.com','volkswagen-newsroom.com','bmwgroup.com','toyota','polestar','zgh.com','nvidia.com','mediatek.com','aws.amazon.com','microsoft.com','europa.eu','cnil.fr','gov.br','pdp.gov.my','soundhound.com','anthropic.com','deepseek.com','about.fb.com','yandex.com']
    return 'A 级' if any(host == h or host.endswith('.'+h) for h in official) else 'B 级'

def initial_config():
    """Start without private source lists, business filters or receiver mappings."""
    return dict(sources=[], rules=[], regions=[], max_candidates=20, time='10:00',
                lookback_days=7, ai_url='', ai_key='', ai_model='', webhook_url='',
                field_map={}, field_mappings=[])

def config():
    with LOCK:
        if not CONFIG_FILE.exists():
            value = initial_config(); CONFIG_FILE.write_text(json.dumps(value,ensure_ascii=False,indent=2), encoding='utf-8')
        return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))

def public_config():
    c = config()
    for key in ['ai_key','webhook_url']:
        c[key+'_configured'] = bool(c.pop(key,''))
    c['webhook_fields']=[dict(key=k,label=v,target=k,help=FIELD_HELP.get(k,'')) for k,v in FIELDS.items()]
    c['default_field_map']=DEFAULT_FIELD_MAP.copy()
    c['field_mappings']=effective_mappings(c)
    c['review_regions']=REVIEW_REGIONS
    return c

def effective_mappings(c):
    return c.get('field_mappings',[dict(source=k,target=v) for k,v in c['field_map'].items()])

def migrate_webhook_v9():
    """Run on idle service start after the caller has backed up private data."""
    if sql('SELECT name FROM migrations WHERE name=?',('webhook-fields-v9',),True): return
    c=config();rows=effective_mappings(c);targets={r['target'] for r in rows}
    for row in rows:
        if row['source']=='source' and row['target']=='site_name': row['source']='site_name'
        if row['source']=='published_at' and row['target']=='publish_time' and 'release_time' not in targets:
            row['target']='release_time'
    save_config(dict(field_mappings=rows))
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        db.row_factory=sqlite3.Row
        for row in db.execute("SELECT * FROM events WHERE site_name='' OR site_name IS NULL").fetchall():
            db.execute('INSERT INTO event_history(event_id,operation,snapshot,created_at) VALUES (?,?,?,?)',(row['id'],'site_name_migration',json.dumps(dict(row),ensure_ascii=False),now()))
            db.execute('UPDATE events SET site_name=?,revision=revision+1,updated_at=? WHERE id=?',(row['source'] or urlsplit(row['link']).hostname or '待确认',now(),row['id']))
        db.execute('INSERT INTO migrations VALUES (?,?)',('webhook-fields-v9',now()))

def validate_mappings(rows):
    if not isinstance(rows,list) or not 0<=len(rows)<=100: raise ValueError('最多可配置 100 个输出字段')
    cleaned=[];targets=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('source') not in (*FIELDS,'__constant__'): raise ValueError('请选择有效的新闻字段或固定文本')
        target=row.get('target')
        if not isinstance(target,str) or not target.strip() or len(target.strip())>100: raise ValueError('输出字段名不能为空，且最多 100 字符')
        target=target.strip()
        if target in targets: raise ValueError('目标字段名不能重复')
        targets.add(target);item=dict(source=row['source'],target=target)
        if row['source']=='__constant__':
            if not isinstance(row.get('value',''),str) or len(row.get('value',''))>5000: raise ValueError('固定文本最多 5000 字符')
            item['value']=row.get('value','')
        cleaned.append(item)
    return cleaned

def save_config(updates):
    c = config()
    for k in ('sources','rules','regions','max_candidates','time','lookback_days','ai_url','ai_key','ai_model','webhook_url','field_map'):
        if k in updates:
            if k in ('ai_key','webhook_url') and updates[k] == '': continue
            c[k] = updates[k]
    if not 1 <= int(c['max_candidates']) <= 20: raise ValueError('每日候选上限必须在 1–20 之间')
    if not 1 <= int(c['lookback_days']) <= 30: raise ValueError('回看天数必须在 1–30 之间')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', c['time']): raise ValueError('时间格式应为 HH:MM')
    if not isinstance(c['rules'],list): raise ValueError('规则列表无效')
    for rule in c['rules']:
        if not isinstance(rule,dict) or not isinstance(rule.get('name'),str) or not rule['name'].strip(): raise ValueError('规则名称不能为空')
        if not isinstance(rule.get('expression'),str) or not rule['expression'].strip(): raise ValueError('筛选表达式不能为空')
        RuleParser(rule['expression'],'').parse()
        if rule.get('scope') not in ('title','title_content'): raise ValueError('规则范围无效')
    for s in c['sources']:
        if urlsplit(s['url']).scheme not in ('http','https'): raise ValueError('来源必须是 HTTP/HTTPS 链接')
        if s['kind'] not in ('rss','webpage') or s['level'] not in ('A 级','B 级','C 级'): raise ValueError('来源类型或等级错误')
        s['id'] = digest(s['url'])[:16]
    for key in ('ai_url','webhook_url'):
        if c[key] and urlsplit(c[key]).scheme not in ('http','https'): raise ValueError('接口地址必须是 HTTP/HTTPS')
    if 'field_mappings' in updates:
        c['field_mappings']=validate_mappings(updates['field_mappings'])
        c['field_map']={r['source']:r['target'] for r in c['field_mappings'] if r['source']!='__constant__'}
    elif 'field_map' in updates:
        if not isinstance(c['field_map'],dict) or any(k not in FIELDS or not isinstance(v,str) or not v.strip() for k,v in c['field_map'].items()): raise ValueError('字段映射无效')
        if len(set(c['field_map'].values())) != len(c['field_map']): raise ValueError('目标字段名不能重复')
        c.pop('field_mappings',None)
    validate_mappings(effective_mappings(c))
    with LOCK:
        tmp = CONFIG_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(CONFIG_FILE)
    return public_config()

def import_opml(body):
    """Return additions to the caller's current source draft; never save it."""
    import base64,binascii
    from opml_import import parse_opml,MAX_FILE_BYTES
    filename=body.get('filename');encoded=body.get('content_base64');existing=body.get('existing_sources',[])
    if not isinstance(filename,str) or not filename.lower().endswith('.opml'): raise ValueError('请选择 .opml 文件')
    if not isinstance(encoded,str) or len(encoded)>((MAX_FILE_BYTES+2)//3)*4: raise ValueError('OPML 文件不能超过 1 MB')
    if not isinstance(existing,list) or len(existing)>10000: raise ValueError('当前来源草稿无效，请刷新页面后重试')
    try: data=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error): raise ValueError('上传内容无效，请重新选择 OPML 文件') from None
    seen=set()
    for source in existing:
        if not isinstance(source,dict) or not isinstance(source.get('url'),str) or source.get('kind') not in ('rss','webpage'): raise ValueError('当前来源草稿无效，请刷新页面后重试')
        seen.add(source_signature(source))
    result=parse_opml(data);added=[];duplicates=0
    for source in result['sources']:
        key=source_signature(source)
        if key in seen: duplicates+=1;continue
        seen.add(key);source.update(id=digest(source['url'])[:16],level=source_level(source['url']));added.append(source)
    return dict(sources=added,added=len(added),duplicates=duplicates,invalid=result['invalid'],total=result['total'])


def request_bytes(url, data=None, headers=None, timeout=10):
    req = Request(url,data=data,headers={'User-Agent':'TrendDesk/1.0 (+internal research)',**(headers or {})})
    with urlopen(req,timeout=timeout) as r:
        return r.read(3_000_000), r.headers.get_content_charset() or 'utf-8'

def retry_read(url, attempts=2, **kwargs):
    for i in range(attempts):
        try: return request_bytes(url,**kwargs)
        except Exception:
            if i==attempts-1: raise

def collection_read(url, feed=False):
    """Public GETs only; model and Webhook POST transports remain independent."""
    data,_,mime,charset=download_resource(url,feed=feed)
    if feed:
        if mime in ('text/html','application/xhtml+xml') or re.match(br'\s*(?:<!doctype\s+html|<html\b)',data,re.I):
            raise FetchError('订阅地址返回 HTML 页面，可能需要登录、反爬验证或更换 RSS 链接')
        return data,''  # Preserve XML encoding declarations.
    if charset:
        try: return data.decode(charset),'utf-8'
        except (LookupError,UnicodeError): pass
    from trafilatura.utils import decode_file
    return decode_file(data),'utf-8'


def source_signature(source):
    return (source['kind'],canon(source['url']),source.get('mode','articles') if source['kind']=='webpage' else '')


def unique_sources(sources):
    seen=set();result=[]
    for source in sources:
        if not source['enabled']: continue
        key=source_signature(source)
        if key not in seen: result.append(source);seen.add(key)
    return result


def collection_error(exc):
    if isinstance(exc,ET.ParseError): return 'RSS/Atom XML 解析失败：订阅内容格式无效或不是有效订阅源'
    if isinstance(exc,(FetchError,ValueError)): return str(exc)[:500]
    if isinstance(exc,(TimeoutError,URLError)): return '采集连接失败或超时，请检查网站及网络'
    return '采集处理失败：'+type(exc).__name__


def save_source_check(source,status,checked,error,count,snapshot):
    version=SNAPSHOT_VERSION if snapshot is not None and source.get('mode')=='changes' else 0
    pair=None
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        db.row_factory=sqlite3.Row
        old=db.execute('SELECT * FROM source_state WHERE id=?',(source['id'],)).fetchone()
        if snapshot is not None:
            pair=tracking.snapshot_pair(db,source,dict(old) if old else None,snapshot,version,checked)
        tracking.observe(db,source,status,count,checked)
        db.execute('''INSERT INTO source_state(id,status,checked_at,error,item_count,snapshot,fail_streak,last_success_at,snapshot_version)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        status=excluded.status,checked_at=excluded.checked_at,error=excluded.error,item_count=excluded.item_count,
        snapshot=COALESCE(excluded.snapshot,source_state.snapshot),
        fail_streak=CASE WHEN excluded.status='正常' THEN 0 ELSE COALESCE(source_state.fail_streak,0)+1 END,
        last_success_at=CASE WHEN excluded.status='正常' THEN excluded.checked_at ELSE source_state.last_success_at END,
        snapshot_version=CASE WHEN excluded.snapshot IS NOT NULL THEN excluded.snapshot_version ELSE source_state.snapshot_version END''',
        (source['id'],status,checked,error,count,snapshot,0 if status=='正常' else 1,checked if status=='正常' else '',version))
    return pair


def migrate_source_history():
    """Seed known snapshots only; never invent past observations or differences."""
    sources=unique_sources([{**source,'enabled':True} for source in config()['sources']])
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        db.row_factory=sqlite3.Row
        for source in sources:
            if source.get('mode')!='changes' or source['kind']!='webpage': continue
            old=db.execute('SELECT * FROM source_state WHERE id=?',(source['id'],)).fetchone()
            if old and old['snapshot']:
                exists=db.execute('SELECT id FROM source_snapshots WHERE source_key=? AND content_hash=? AND version=? LIMIT 1',(tracking.source_key(source),digest(old['snapshot']),old['snapshot_version'])).fetchone()
                if not exists:tracking.store_snapshot(db,source,old['snapshot'],old['snapshot_version'],old['last_success_at'] or old['checked_at'] or now())


def source_history(key):
    if not isinstance(key,str) or not re.fullmatch(r'[a-f0-9]{24}',key): raise ValueError('来源标识无效')
    with LOCK,closing(sqlite3.connect(DB)) as db:
        return dict(snapshots=tracking.history(db,key))


def compare_snapshots(before_id,after_id):
    with LOCK,closing(sqlite3.connect(DB)) as db:
        return tracking.compare(db,before_id,after_id)


def prune_snapshots():
    with LOCK,closing(sqlite3.connect(DB)) as db,db: tracking.prune(db)


def test_source(body):
    """Diagnostic only: no baselines, observations, events, models or Webhook writes."""
    source=body.get('source')
    if not isinstance(source,dict): raise ValueError('缺少待测试来源')
    if source.get('kind') not in ('rss','webpage') or source.get('mode','articles') not in ('articles','changes'): raise ValueError('来源类型或模式无效')
    if not isinstance(source.get('url'),str) or len(source['url'])>8192: raise ValueError('来源链接无效')
    source={k:source[k] for k in ('url','kind','mode','name') if k in source}
    source.update(id=digest(source['url'])[:16],name=str(source.get('name','测试来源'))[:200],level='B 级')
    key=tracking.source_key(source)
    with SOURCE_TEST_LOCK:
        if key in SOURCE_TEST_ACTIVE: raise ValueError('该来源正在测试，请勿重复提交')
        if not SOURCE_TEST_SLOTS.acquire(False): raise ValueError('已有三个来源测试运行中，请稍后重试')
        SOURCE_TEST_ACTIVE.add(key)
    started=time.monotonic()
    try:
        result=dict(checked_at=now(),source_key=key)
        if source['kind']=='webpage' and source.get('mode')=='changes':
            data,encoding=collection_read(source['url']);snapshot=structured_snapshot(data if isinstance(data,str) else data.decode(encoding,errors='replace'))
            old=sql('SELECT * FROM source_state WHERE id=?',(source['id'],),True)
            usable=bool(old and old['snapshot'] and old['snapshot_version']==SNAPSHOT_VERSION and not longform.unavailable_page(old['snapshot']))
            changes=change_set(old['snapshot'],snapshot) if usable else None
            result.update(ok=True,body_chars=len(snapshot),has_baseline=usable,count=int(bool(changes)),titles=[],message='检测到有效内容变化，未推进正式基线' if changes else '无有效变化，未更新正式基线' if usable else '正文可解析，尚无新版基线；测试不会建立基线',changes=changes)
        else:
            rows=rss(source) if source['kind']=='rss' else webpages(source,config())[0]
            result.update(ok=True,count=len(rows),titles=[r['title'] for r in rows[:3]],message='请求及解析完成'+('，未取得条目，仍需核对链接或规则' if not rows else '')+'；未建新闻')
        return {**result,'seconds':round(time.monotonic()-started,2)}
    except Exception as exc:
        return dict(ok=False,source_key=key,checked_at=now(),seconds=round(time.monotonic()-started,2),message=collection_error(exc),count=0,titles=[])
    finally:
        with SOURCE_TEST_LOCK: SOURCE_TEST_ACTIVE.discard(key);SOURCE_TEST_SLOTS.release()


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True); self.parts=[]; self.links=[]; self.anchor=None; self.skip=0; self.in_title=False; self.title=''; self.date=''; self.site_name=''; self.feed(text)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style','nav','footer','header'): self.skip += 1
        if tag == 'title': self.in_title=True
        if tag == 'a': self.anchor=[a.get('href',''),'']
        if tag == 'meta' and a.get('property',a.get('name','')) in ('article:published_time','date','datePublished'): self.date=stamp(a.get('content',''))
        if tag=='time' and not self.date: self.date=stamp(a.get('datetime',''))
        if tag=='meta' and a.get('property',a.get('name','')) in ('og:site_name','application-name'): self.site_name=plain(a.get('content',''))
    def handle_endtag(self,tag):
        if tag in ('script','style','nav','footer','header'): self.skip=max(0,self.skip-1)
        if tag=='title': self.in_title=False
        if tag=='a' and self.anchor:
            self.links.append(self.anchor); self.anchor=None
    def handle_data(self,text):
        if self.in_title: self.title += text
        if self.anchor: self.anchor[1] += text
        if not self.skip: self.parts.append(text)
    @property
    def text(self): return plain(' '.join(self.parts))

def match_rules(title, body, c):
    return [r['name'] for r in c['rules'] if RuleParser(r['expression'],title if r['scope']=='title' else title+' '+body).parse()]

def rss(source):
    data,_ = collection_read(source['url'],feed=True); root=ET.fromstring(data); rows=[]
    if root.tag.split('}')[-1].lower() not in ('rss','feed','rdf'):
        raise FetchError('订阅内容不是 RSS/Atom XML，请核对来源链接')
    def children(node, names):
        return next((x for x in node if x.tag.split('}')[-1] in names),None)
    def val(node,names):
        x=children(node,names); return ''.join(x.itertext()).strip() if x is not None else ''
    for n in [x for x in root.iter() if x.tag.split('}')[-1] in ('item','entry')][:80]:
        link=val(n,['link']); l=children(n,['link'])
        if not link and l is not None: link=l.get('href','')
        title=plain(val(n,['title']))
        if not title or urlsplit(link).scheme not in ('http','https'): continue
        rows.append(dict(title=title,link=canon(link),body=plain(val(n,['encoded','description','summary','content']))[:12000],published_at=stamp(val(n,['pubDate','published','updated','date'])),site_name=plain(val(n,['source']))))
    return rows

def webpages(source,c):
    data,encoding=collection_read(source['url']); text=data if isinstance(data,str) else data.decode(encoding,errors='replace');page=Page(text)
    if len(page.text)<100: raise ValueError('网页正文不足，可能需要浏览器渲染或登录')
    if source.get('mode')=='changes':
        old=sql('SELECT * FROM source_state WHERE id=?',(source['id'],),True)
        snapshot=structured_snapshot(text)
        if not old or not old['snapshot'] or old['snapshot_version']!=SNAPSHOT_VERSION or longform.unavailable_page(old['snapshot']): return [],snapshot
        changes=change_set(old['snapshot'],snapshot)
        if not changes: return [],snapshot
        return [dict(title=plain(page.title or source.get('name') or urlsplit(source['url']).hostname)+' 页面增删记录',link=canon(source['url']),body=changes['body'],published_at='',page_change=True,page_change_details=changes,site_name=page.site_name or source.get('name') or urlsplit(source['url']).hostname)],snapshot
    links=[]
    for href,text in page.links:
        url=urljoin(source['url'],href); title=plain(text)
        if len(title)<18 or urlsplit(url).netloc!=urlsplit(source['url']).netloc or canon(url)==canon(source['url']): continue
        if match_rules(title,'',c) and url not in [x[0] for x in links]: links.append((url,title))
    rows=[]
    for url,title in links[:3]:
        try:
            data,encoding=collection_read(url); detail=Page(data if isinstance(data,str) else data.decode(encoding,errors='replace'))
            rows.append(dict(title=title,link=canon(url),body=detail.text[:12000],published_at=detail.date,site_name=detail.site_name or source['name']))
        except Exception: continue
    return rows,None

def noise(title):
    return bool(re.search(r'\b(earnings|repurchase|stock price|price target|conference call|fiscal|quarter(?:ly)? results|shares|hiring|coupon|discount|IPO|awards?)\b',title,re.I))

def classify(text):
    if any(contains(text,t) for t in ['privacy','GDPR','AI Act','banned','outage','microphone','personal data','hallucination']): return '风险舆情'
    if any(contains(text,t) for t in ['OTA','software update','Android Auto','CarPlay','infotainment']): return '车载 OTA'
    if any(contains(text,t) for t in ['partnership','partner','integration']): return '生态合作'
    if any(contains(text,t) for t in ['ASR','TTS','speech','voice','recognition']): return '语音能力'
    return 'C 端模型'

def fallback(a,reason):
    return dict(title='待生成中文标题',summary='尚未完成 AI 中文分析，请配置模型或人工填写。',evidence=a['body'][:700] or a['title'],evidence_zh='待翻译',region='待确认',category=classify(a['title']),stage='待确认',relevance='待确认',action='持续观察',confidence='低',company='待确认',language='待确认',change='待确认',analysis_mode=reason,status='待审核',site_name=a.get('site_name') or a.get('source') or urlsplit(a['link']).hostname or '待确认')

def clean_title(title):
    # Remove only explicit placeholder phrases, never inferred market names or facts.
    placeholder=r'(?:(?:具体|实际|服务|业务)?(?:适用|覆盖|影响|上线|发布)?(?:范围|区域|地区|市场|国家|语言|车型|年款|版本|时间|内容|变化|影响|交付方式)(?:及|和|与|、)?)+(?:尚|仍)?(?:待确认|待核实|不明确|未披露|未明确)'
    title=re.sub(r'[（(]\s*'+placeholder+r'\s*[）)]','',title)
    title=re.sub(placeholder,'',title)
    title=re.sub(r'([，,；;：:])\s*(?=[，,；;：:]|$)', '',title)
    return title.strip(' ，,；;：:、-—')

def migrate_titles():
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        if db.execute('SELECT name FROM migrations WHERE name=?',('titles-no-placeholders-v7',)).fetchone(): return 0
        db.row_factory=sqlite3.Row
        count=0
        for row in db.execute("SELECT * FROM events WHERE status IN ('待审核','待推送')").fetchall():
            title=clean_title(row['title'])
            if title and title!=row['title']:
                db.execute('INSERT INTO event_history(event_id,operation,snapshot,created_at) VALUES (?,?,?,?)',(row['id'],'title_migration',json.dumps(dict(row),ensure_ascii=False),now()))
                db.execute('UPDATE events SET title=?,revision=revision+1,updated_at=? WHERE id=?',(title,now(),row['id']));count+=1
        db.execute('INSERT INTO migrations VALUES (?,?)',('titles-no-placeholders-v7',now()))
        return count

def model_json(data):
    """Read compatible chat responses without accepting prose as an analysis."""
    try: payload=json.loads(data)
    except (ValueError,UnicodeError): raise ValueError('模型接口返回非 JSON 数据，请检查后台 API 地址') from None
    if not isinstance(payload,dict): raise ValueError('模型接口返回结构无效，缺少 choices')
    choices=payload.get('choices')
    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):
        raise ValueError('模型接口返回结构无效，缺少 choices')
    choice=choices[0]
    if choice.get('finish_reason') in ('length','max_tokens'):
        raise ValueError('模型输出被截断，请检查模型输出额度或缩短原文后重试')
    message=choice.get('message')
    if not isinstance(message,dict): raise ValueError('模型返回缺少 message')
    if message.get('refusal'): raise ValueError('模型拒绝了本次分析请求')
    content=message.get('content')
    if isinstance(content,list):
        content=''.join(part.get('text','') for part in content if isinstance(part,dict) and part.get('type') in ('text','output_text') and isinstance(part.get('text'),str))
    if not isinstance(content,str) or not content.strip(): raise ValueError('模型未返回分析正文')
    content=re.sub(r'^```(?:json)?\s*|\s*```$','',content.strip())
    try: result=json.loads(content)
    except ValueError:
        # Some compatible gateways add a short preface before their JSON object.
        start=content.find('{')
        if start<0: raise ValueError('模型返回的分析内容不是有效 JSON') from None
        try: result,_=json.JSONDecoder().raw_decode(content[start:])
        except ValueError: raise ValueError('模型返回的分析 JSON 格式错误') from None
    if not isinstance(result,dict): raise ValueError('模型分析结果必须是 JSON 对象')
    return result

def model_request(url,request,c):
    """Use only the configured endpoint/model/key; never switch providers."""
    started=time.monotonic();data=json.dumps(request,ensure_ascii=False,separators=(',',':')).encode();outcome='error'
    try:
        result=retry_read(url,data=data,headers={'Authorization':'Bearer '+c['ai_key'],'Content-Type':'application/json'},timeout=180,attempts=1)[0]
        outcome='success';return result
    except HTTPError as exc:
        if exc.code in (401,403): reason='模型接口鉴权或访问被拒绝（HTTP '+str(exc.code)+'），请检查后台密钥及接口权限'
        elif exc.code==429: reason='模型接口限流或额度不足（HTTP 429），请稍后重试'
        else: reason='模型接口请求失败（HTTP '+str(exc.code)+'），请检查后台 API、模型及服务状态'
        raise ValueError(reason) from None
    except (TimeoutError,URLError) as exc:
        if isinstance(exc,TimeoutError) or isinstance(getattr(exc,'reason',None),TimeoutError):
            raise longform.ModelTimeout('模型请求超时（本次上限 180 秒），原文和已核验分段保留，可重试；请检查模型服务响应或额度') from None
        else: reason='无法连接后台配置的模型 API，请检查网络及接口地址'
        raise ValueError(reason) from None
    except (OSError,ValueError):
        raise ValueError('无法连接后台配置的模型 API，请检查网络及接口地址') from None
    finally:
        print(json.dumps(dict(event='model_request',request_bytes=len(data),seconds=round(time.monotonic()-started,2),status=outcome)),flush=True)

def validate_analysis(result,a,schema):
    result=dict(result)
    if not isinstance(result.get('site_name'),str) or not result['site_name'].strip():
        result['site_name']=a.get('site_name') or a.get('source') or urlsplit(a['link']).hostname or '待确认'
    for key in schema:
        value=result.get(key)
        if key in ('region','language') and isinstance(value,list) and all(isinstance(v,str) for v in value): value='、'.join(value)
        if value is None or value==[]: value=''
        if key=='region' and value in ('','待确认','未知','未披露','未说明'): value='未涉及'
        if key in ('company','language') and value in ('待确认','未知','未披露','未说明'): value=''
        if key=='stage' and value in ('未涉及','未知','未披露'): value=''
        if key=='relevance' and value in ('未涉及','未知'): value=''
        if key=='confidence' and value in ('','未涉及','未知'): value='低'
        if isinstance(value,str): result[key]=value.strip()
    missing=[key for key in ('title','summary') if not isinstance(result.get(key),str) or not result[key]]
    if missing: raise ValueError('模型缺少必填字段：'+', '.join(missing))
    for key in schema:
        if not isinstance(result.get(key),str):raise ValueError('模型字段 '+key+' 必须为文本或空值')
    result={key:result[key] for key in schema}
    if not a.get('manual_source') and len(result['summary'])>300:raise ValueError('中文摘要不能超过300字符，请归并重点而不是截断原文')
    if not a.get('manual_source') and len(result['change'])>650:raise ValueError('核心变化过长，请合并为最多5条，细节保留在完整分析中')
    result['title']=clean_title(result['title'])
    for key,choices in [('category',CATEGORIES),('stage',STAGES),('action',ACTIONS),('relevance',['高','中','低','待确认']),('confidence',['高','中','低'])]:
        if result[key] not in ['', '未涉及', *choices]: raise ValueError('模型字段 '+key+' 的选项无效')
        if result[key]=='未涉及' and key in ('category','action'):result[key]=''
    if not all(re.search(r'[\u4e00-\u9fff]',result[k]) for k in ['title','summary']): raise ValueError('模型未生成中文标题或摘要')
    if result['evidence'] and plain(result['evidence']) not in plain(a['body']+' '+a['title']): raise ValueError('证据摘录未在输入原文找到，请返回逐字连续引用')
    return result

def brief_inventory(scoped,sections,facts,long_mode=False):
    parts=longform.sections_for(scoped,sections) if long_mode else sections
    indexed={s['id']:s for s in parts}
    inventory=brieftext.inventory(facts,{key:s['heading'] for key,s in indexed.items()})
    tail=False;tail_ids=set()
    for part in parts:
        if re.fullmatch(r'latest news|related (?:news|articles|content)|最新消息|相关新闻|相关文章',part['heading'].strip(),re.I):tail=True
        if tail:tail_ids.add(part['id'])
    for fact in inventory:
        if fact.get('section_id') in tail_ids:fact['auxiliary']=True
    groups={}
    if scoped.get('target_version') or (sections and not long_mode):
        for fact in inventory:
            if fact['auxiliary']:continue
            source=indexed.get(fact.get('section_id'),{})
            groups.setdefault(source.get('parent_id') or fact.get('section_id'),[]).append(fact['id'])
    return inventory,list(groups.values())


def analyze_long(scoped,sections,details,c,schema,url,progress=None):
    """All source segments -> checked inventory -> grounded concise publication.

    Every fact remains in analysis_details/evidence; only presentation is brief.
    """
    started=time.monotonic()
    identity=digest(json.dumps({k:c[k] for k in ('ai_url','ai_model','ai_key')},sort_keys=True))
    request=lambda messages:model_request(url,dict(model=c['ai_model'],temperature=0.1,stream=False,messages=messages),c)
    context=[]
    if details and scoped.get('supplemental_body'):
        context_scoped={**scoped,'body':scoped['supplemental_body']}
        context,_=longform.extract(context_scoped,[],None,request,model_json,DATA/'analysis-cache',identity,progress)
    facts,stats=longform.extract(scoped,sections,details,request,model_json,DATA/'analysis-cache',identity,progress)
    metadata={k:scoped[k] for k in ('title','link','source','site_name','target_version') if k in scoped}
    metadata['analysis_date']=datetime.now(TZ).date().isoformat()
    inventory,groups=brief_inventory(scoped,sections,facts,True) if not details else (brieftext.inventory(facts),[])
    generated={'change','evidence','evidence_zh'}
    request_schema={k:v for k,v in schema.items() if k not in generated}
    request_schema.update(brieftext.BRIEF_SCHEMA)
    # Quotation ownership has already been validated against full source. Keep
    # every fact/qualifier for classification without retransmitting long quotes.
    content=dict(article=metadata,schema=request_schema,verified_change_facts=brieftext.compact(inventory)) if details else dict(article=metadata,schema=request_schema,verified_features=brieftext.compact(inventory))
    content['feature_groups']=groups
    if context:content['verified_supplemental_context']=brieftext.compact(brieftext.inventory(context))
    messages=[dict(role='system',content=brieftext.BRIEF_RULES+'\n根据核验事实同时生成中文标题和分类；缺少范围不填标题，不借来源所在地推断。'+ANALYSIS_RULES+('\n'+CHANGE_ANALYSIS_RULES if details else '')),dict(role='user',content=json.dumps(content,ensure_ascii=False,separators=(',',':')))]
    if progress:progress('汇总与复核',stats['parts'],stats['parts'])
    for attempt in range(2):
        response=request(messages)
        parsed=None
        try:
            parsed=model_json(response)
            brief=brieftext.validate_brief(parsed,inventory,groups,flexible=bool(scoped.get('manual_source')))
            parsed.update(summary=brief['summary'],change=brief['change'])
            if details:
                parsed['change_facts']=facts;validate_change_facts(parsed,details)
                parsed['evidence']=next((f['after_evidence'] or f['before_evidence'] for f in facts if f['after_evidence'] or f['before_evidence']))
                parsed['evidence_zh']=parsed['change']
            else:
                parsed=apply_features(parsed,facts,True)
            result=validate_analysis(parsed,scoped,schema);validate_polarity(result,scoped)
            if scoped.get('target_version'):
                target=scoped['target_version'];allowed={version_key(v) for v in re.findall(r'\b\d+(?:\.\d+){2,4}\b',scoped['body'])}|{version_key(target)}
                if any(version_key(v) not in allowed for v in re.findall(r'\b\d+(?:\.\d+){2,4}\b',result['title'])):raise ValueError('标题混入其他版本')
            break
        except ValueError as exc:
            if attempt:raise
            if parsed is not None:messages.append(dict(role='assistant',content=json.dumps({k:v for k,v in parsed.items() if k in request_schema},ensure_ascii=False,separators=(',',':'))))
            messages.append(dict(role='user',content='上次返回未通过校验：'+str(exc)+'。请根据原有事实重新输出全部 schema 字段，不要添加事实。'))
    result['evidence']='\n\n'.join(f['name']+'\n旧：'+(f['before_evidence'] or '（无）')+'\n新：'+(f['after_evidence'] or '（无）') for f in facts) if details else '\n\n'.join(f['section_id']+' '+f['name']+'：'+f['evidence']+'\n阶段：'+f['stage_evidence']+('\n范围：'+f['scope_evidence'] if f['scope_evidence'] else '') for f in facts)
    result['evidence_zh']=result['change']
    result['analysis_details']=brieftext.detail_json(inventory,brief,'page_changes' if details else 'article',context)
    result['analysis_mode']='AI：'+c['ai_model']+' / 完整分段 '+str(stats['parts'])+' 组，复用 '+str(stats['cached'])+' 组'
    result['status']='待推送' if result['relevance']=='高' and result['confidence']=='高' and result['category'] and scoped['level']!='C 级' and scoped['published_at'] and result['region'] not in ('','待确认','其他','未涉及') and result['stage'] not in ('','待确认') else '待审核'
    print(json.dumps(dict(event='long_analysis',chars=len(scoped['body']),facts=len(facts),**stats,seconds=round(time.monotonic()-started,2))),flush=True)
    return result


def analyze(a,c,strict=False,progress=None):
    if not (c['ai_url'] and c['ai_key'] and c['ai_model']):
        if strict: raise ValueError('请先保存完整的模型配置')
        return fallback(a,'未配置 AI')
    schema={'title':'中文：产品/公司 + 具体变化 + 原文明确的适用范围。范围不明确则省略；禁止待确认、待核实等占位语，不编造范围','summary':brieftext.BRIEF_SCHEMA['summary'],'evidence':'逐字引用输入原文的连续短句；没有可摘录内容可留空','evidence_zh':'证据中文释义；未涉及可留空','region':'从 '+('/'.join(REVIEW_REGIONS))+' 选择一个；仅根据新闻适用范围，不用来源所在地推断；原文未涉及选未涉及；多区域但非全球选其他','category':'/'.join(CATEGORIES)+'；不适用可留空','stage':'/'.join(STAGES)+'；原文未涉及可留空','relevance':'高/中/低；不能判断可留空','action':'/'.join(ACTIONS)+'；无适用建议可留空','confidence':'高/中/低','company':'原文涉及的公司/产品；未涉及留空','language':'原文明确的产品支持语言，未涉及留空，不把文章语言当作支持语言','change':'中文核心变化，通常3至5行，最多600字符，保留关键日期与限制，不复制摘要；无实质变化可填未涉及','site_name':'原文发布站点名称，依据 article.site_name、原文链接域名、标题及原文署名识别；不要把报道的产品/公司当成媒体站点；无法识别保留输入站点名称'}
    url=c['ai_url'].rstrip('/')
    if not url.endswith('/chat/completions'): url += '/chat/completions'
    try:
        if not a.get('manual_source') and (len(a['body'])>longform.MAX_BODY_CHARS or len(a.get('supplemental_body',''))>longform.MAX_BODY_CHARS):raise ValueError('原文最多 300000 字符，未截断，请按独立文章拆分')
        scoped,sections=(dict(a),[]) if a.get('page_change') else prepare_article(a)
        # Pasted articles are ordinary full-text analysis. Markdown subheadings
        # (including author/footer blocks) are not a mandatory feature schema.
        # Long sources still use lossless partitioning below.
        if a.get('manual_source'):sections=[]
        change_details=a.get('page_change_details')
        long_mode=max(len(scoped['body']),len(scoped.get('supplemental_body','')))>10000 or len(sections)>12 or bool(change_details and len(classify_changes(change_details)['items'])>12)
        if long_mode:
            return analyze_long(scoped,sections,change_details,c,schema,url,progress)
        request_schema=dict(schema)
        if sections: request_schema['features']=FEATURE_SCHEMA
        # Send the old/new source once per representation rather than the full
        # stored diff object (which also contains repeated display/operations).
        if change_details:
            scoped=dict(scoped,page_change_details={k:change_details[k] for k in ('added','removed')})
            content_article=scoped
        else:content_article=scoped
        if a.get('page_change') and change_details:
            request_schema['change_facts']=CHANGE_FACT_SCHEMA
        if sections or change_details:request_schema.update(brieftext.BRIEF_SCHEMA)
        content=dict(required_keys=list(request_schema),schema=request_schema,article=content_article)
        if sections or change_details:content['fact_id_rule']='features 按 source_sections 顺序（或 change_facts 按 change_items 顺序）对应 f1、f2……，用于 summary_fact_ids 和 key_changes 的 fact_ids；核心变化覆盖全部主要功能，栏目/相关推荐除外'
        if sections: content['source_sections']=sections
        if change_details: content['change_items']=classify_changes(change_details)['items']
        messages=[dict(role='system',content='你是新闻事实分析师，业务视角为海外车载语音本土化。先准确分析全文，再判断业务相关性，不能用相关性筛选替代全文总结。只输出 JSON 对象，包含 schema 列出的字段，尤其 title 中文标题。原文未涉及的字段用空字符串或未涉及，不为填满字段编造地区、公司、语言或阶段，不因信息不完整拒绝总结。features 和 change_facts 按 schema 返回对象数组，其余字段返回字符串。evidence 必须逐字连续引用原文，不能翻译、改写或用省略号拼接。不要以来源等级代替事实置信度。无原文支撑则低置信度。\n'+ANALYSIS_RULES),dict(role='user',content=json.dumps(content,ensure_ascii=False))]
        if progress: progress('分析与证据复核',0,1)
        if a.get('page_change'):
            messages[0]['content']+='\n'+CHANGE_ANALYSIS_RULES
        if sections or change_details:messages[0]['content']+='\n'+brieftext.BRIEF_RULES
        for attempt in range(2):
            data=model_request(url,dict(model=c['ai_model'],temperature=0.1,stream=False,messages=messages),c)
            parsed=None
            try:
                parsed=model_json(data)
                if a.get('page_change') and change_details:
                    change_facts=validate_change_facts(parsed,change_details)
                    parsed=dict(parsed)
                    inventory=brieftext.inventory(change_facts)
                    brief=brieftext.validate_brief(parsed,inventory,flexible=bool(scoped.get('manual_source')))
                    parsed.update(summary=brief['summary'],change=brief['change'])
                if sections:
                    facts=validate_features(parsed.get('features'),sections)
                    # Verify quotes per section first. The aggregate quote may
                    # otherwise concatenate several valid but non-contiguous
                    # sentences, which isn't a valid single evidence excerpt.
                    inventory,groups=brief_inventory(scoped,sections,facts)
                    brief=brieftext.validate_brief(parsed,inventory,groups,flexible=bool(scoped.get('manual_source')))
                    parsed.update(summary=brief['summary'],change=brief['change'])
                    parsed=apply_features(dict(parsed),facts,True)
                result=validate_analysis(parsed,scoped,schema)
                validate_polarity(result,scoped)
                if scoped.get('target_version'):
                    target=scoped['target_version']
                    allowed={version_key(v) for v in re.findall(r'\b\d+(?:\.\d+){2,4}\b',scoped['body'])}|{version_key(target)}
                    if any(version_key(v) not in allowed for v in re.findall(r'\b\d+(?:\.\d+){2,4}\b',' '.join(result[k] for k in ('title','summary','change')))):
                        raise ValueError('分析结果混入其他版本，必须只总结链接对应的 '+target)
                if sections:
                    result['title']=feature_title(result,facts,scoped)
                    # Keep the per-feature audit trail in the existing evidence
                    # field; every individual quote has already been verified.
                    result['evidence']='\n\n'.join(f['name']+'：'+f['evidence']+'\n阶段：'+f['stage_evidence']+('\n范围：'+f['scope_evidence'] if f['scope_evidence'] else '') for f in facts)
                    result['evidence_zh']=result['change']
                if a.get('page_change') and change_details:
                    result['evidence']='\n\n'.join(f['name']+'\n旧：'+(f['before_evidence'] or '（无）')+'\n新：'+(f['after_evidence'] or '（无）') for f in change_facts)
                    result['evidence_zh']=result['change']
                if sections or change_details:result['analysis_details']=brieftext.detail_json(inventory,brief,'page_changes' if change_details else 'article')
                else:result['analysis_details']=''
                break
            except ValueError as exc:
                if attempt: raise
                # A single bounded correction uses the same API/model and original
                # article. Never fill missing news facts from a generic fallback.
                if parsed is not None: messages.append(dict(role='assistant',content=json.dumps({k:v for k,v in parsed.items() if k in request_schema},ensure_ascii=False)))
                messages.append(dict(role='user',content='上次输出未通过校验：'+str(exc)+'。请根据最初文章重新输出完整 JSON 对象，包含全部 '+str(len(request_schema))+' 个字段，尤其 title'+(' 及覆盖所有 source_sections 的 features 数组' if sections else '')+'。未知范围不写进标题，证据只能逐字引用对应原文。计划、Beta 测试不能写成已上线。'))
        result['analysis_mode']='AI：'+c['ai_model']
        result['status']='待推送' if result['relevance']=='高' and result['confidence']=='高' and result['category'] and a['level']!='C 级' and a['published_at'] and result['region'] not in ('','待确认','其他','未涉及') and result['stage'] not in ('','待确认') else '待审核'
        return result
    except Exception as exc:
        if strict:
            reason=str(exc) if isinstance(exc,ValueError) else '模型服务处理异常，请稍后重试'
            raise ValueError(reason) from None
        return fallback(a,'AI 分析失败：'+(str(exc) if isinstance(exc,ValueError) else '模型服务处理异常'))

def get_events(status=None):
    query='SELECT * FROM events'
    if status: query+=' WHERE status=?'
    else: query+=" WHERE status!='已删除'"
    query+=' ORDER BY COALESCE(NULLIF(published_at,\'\'),created_at) DESC'
    if not status: query+=' LIMIT 500'
    rows=sql(query,(status,) if status else ())
    for r in rows:
        r['aux_links']=json.loads(r['aux_links']); r['matched_rules']=json.loads(r['matched_rules'])
    return rows

def event_detail(eid):
    row=sql('SELECT * FROM events WHERE id=?',(int(eid),),True)
    if not row: raise ValueError('动态不存在')
    row['aux_links']=json.loads(row['aux_links']);row['matched_rules']=json.loads(row['matched_rules'])
    raw=sql('SELECT data FROM raw_items WHERE fingerprint=?',(row['fingerprint'],),True)
    original=json.loads(raw['data']) if raw else {}
    row['page_change_details']=original.get('page_change_details')
    row['change_body']=original.get('body','') if original.get('page_change_details') else ''
    row['change_display']=classify_changes(original['page_change_details'])['display_body'] if original.get('page_change_details') else ''
    row['snapshot_pair']=sql('SELECT before_id,after_id FROM event_snapshot_links WHERE event_id=?',(row['id'],),True)
    row['body']=row['manual_body'] or row['fetched_body'] or (json.loads(raw['data']).get('body','') if raw else '') or row['evidence']
    row['body_source']='人工正文' if row['manual_body'] else '自动抓取正文' if row['fetched_body'] else '采集内容/证据'
    row['review_region']=review_region(row['region'])
    return row

def persist_event(eid,fields,revision=None,allowed=None,operation='edit'):
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM events WHERE id=?',(int(eid),)).fetchone()
        if not row: raise ValueError('动态不存在')
        if allowed and row['status'] not in allowed: raise ValueError('动态状态已改变，请刷新后重试')
        if revision is not None and row['revision']!=int(revision): raise ValueError('动态已更新，请重新打开详情后重试')
        db.execute('INSERT INTO event_history(event_id,operation,snapshot,created_at) VALUES (?,?,?,?)',(eid,operation,json.dumps(dict(row),ensure_ascii=False),now()))
        db.execute('UPDATE events SET '+','.join('"'+k+'"=?' for k in fields)+',updated_at=?,revision=revision+1 WHERE id=?',list(fields.values())+[now(),eid])
        # Read the exact committed revision while still holding the process lock.
        # Otherwise a second editor could change the record before we build the
        # model input, pairing their revision with this request's older draft.
        db.commit()
        return event_detail(eid)

def merge_match(a, events):
    for e in events:
        # A monitored URL can change again; each observed content version is distinct.
        if a.get('page_change'):
            if e.get('fingerprint')==a['fingerprint']: return e
            continue
        if canon(e['link'])==canon(a['link']): return e
        if normalize(e['original_title'])==normalize(a['title']): return e
        # Conservative matching: preserve independent releases/versions and markets.
        left,right=normalize(e['original_title']),normalize(a['title'])
        if re.findall(r'\d+(?:\.\d+)*',left)!=re.findall(r'\d+(?:\.\d+)*',right): continue
        if e['published_at'] and a['published_at'] and abs((datetime.fromisoformat(e['published_at'])-datetime.fromisoformat(a['published_at'])).total_seconds())>172800: continue
        if difflib.SequenceMatcher(None,left,right).ratio()>=0.94: return e
    return None

def insert_event(a,result,day):
    fields=dict(fingerprint=a['fingerprint'],original_title=a['title'],link=a['link'],source=a['source'],source_level=a['level'],published_at=a['published_at'],matched_rules=json.dumps(a['matched_rules'],ensure_ascii=False),created_at=now(),updated_at=now(),admission_day=day,**result)
    columns=list(fields)
    with LOCK,closing(sqlite3.connect(DB)) as db,db:
        eid=db.execute('INSERT INTO events ('+','.join('"'+k+'"' for k in columns)+') VALUES ('+','.join('?' for _ in columns)+')',list(fields.values())).lastrowid
        pair=a.get('snapshot_pair')
        if pair: db.execute('INSERT INTO event_snapshot_links VALUES (?,?,?)',(eid,pair.get('before_id'),pair['after_id']))
        return eid

def run_pipeline(trigger='manual',day=None):
    if not RUN_LOCK.acquire(blocking=False): raise ValueError('已有采集任务运行中')
    c=config(); start=now(); today=datetime.now(TZ).date(); admission_day=str(today)
    sources=unique_sources(c['sources'])
    rid=sql('INSERT INTO runs(started_at,status,trigger,source_count) VALUES (?,?,?,?)',(start,'running',trigger,len(sources)))
    try:
        fetched=[]; failures=[]; details=[]
        def collect(s):
            if s['kind']=='rss': return rss(s),None
            return webpages(s,c)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            jobs={pool.submit(collect,s):s for s in sources}
            for job in concurrent.futures.as_completed(jobs):
                s=jobs[job]; checked=now(); snapshot=None
                previous=sql('SELECT snapshot_version FROM source_state WHERE id=?',(s['id'],),True)
                try:
                    rows,snapshot=job.result()
                    for a in rows:
                        a.update(source=s['name'],level=s['level'])
                        a['site_name']=a.get('site_name') or s['name']
                    fetched.extend(rows); error=''; status='正常'; count=len(rows)
                except Exception as exc:
                    rows=[]; count=0; error=collection_error(exc); status='失败'
                    failures.append(dict(source=s['name'],error=error))
                note='已建立新版基线，本次不生成变更新闻' if snapshot is not None and (not previous or previous['snapshot_version']!=SNAPSHOT_VERSION) else ''
                details.append(dict(source=s['name'],status=status,count=count,error=error,note=note))
                pair=save_source_check(s,status,checked,error,count,snapshot)
                for article in rows:
                    if article.get('page_change') and pair: article['snapshot_pair']=pair
        utc=datetime.now(timezone.utc)
        if day:
            from datetime import date
            d=date.fromisoformat(day); lower=datetime.combine(d,datetime.min.time(),TZ).astimezone(timezone.utc); upper=lower+timedelta(days=1)
        else: lower=utc-timedelta(days=int(c['lookback_days'])); upper=utc+timedelta(minutes=5)
        matches=[]; filtered=0
        deleted=sql("SELECT fingerprint,link FROM events WHERE status='已删除'")
        deleted_fingerprints={r['fingerprint'] for r in deleted};deleted_links={canon(r['link']) for r in deleted}
        for a in fetched:
            a['fingerprint']=digest(canon(a['link'])+(':'+digest(a['body']) if a.get('page_change') else ''))
            decision='候选'
            if noise(a['title']): decision='噪音排除'
            elif a['published_at'] and not lower<=datetime.fromisoformat(a['published_at'])<upper: decision='时间窗外'
            elif day and not a['published_at'] and not a.get('page_change'): decision='发布日期待确认'
            a['matched_rules']=match_rules(a['title'],a['body'],c)
            if not a['matched_rules'] and not a.get('page_change'): decision='关键词未匹配'
            # C-end model names alone are too broad: retain the source rule, add a capability gate.
            if a['matched_rules'] and all('Feed 2' in x for x in a['matched_rules']) and not any(contains(a['title'],t) for t in ['voice','live','agent','memory','search','API','pricing','language','countries','rollout','update','speech','语音','语言','隐私','голос','речь']): decision='缺少具体能力变化'
            if a['fingerprint'] in deleted_fingerprints or canon(a['link']) in deleted_links:
                decision='已删除，不重新入池'
                # Keep the original collected body available in the read-only
                # deleted library, even when a feed later changes its summary.
                sql('INSERT INTO raw_items VALUES (?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET decision=excluded.decision',(a['fingerprint'],json.dumps(a,ensure_ascii=False),decision,now()))
            else:
                sql('INSERT INTO raw_items VALUES (?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET data=excluded.data,decision=excluded.decision',(a['fingerprint'],json.dumps(a,ensure_ascii=False),decision,now()))
            if decision=='候选': matches.append(a)
            else: filtered+=1
        matches.sort(key=lambda a:(a['level']=='A 级',any('Feed 1' in x or 'Feed 3' in x or 'Feed 4' in x for x in a['matched_rules']),a['published_at']),reverse=True)
        used=sql('SELECT COUNT(*) AS n FROM events WHERE admission_day=?',(admission_day,),True)['n']; capacity=max(0,int(c['max_candidates'])-used)
        events=get_events(); admitted=0; overflow=0
        for a in matches:
            if sql('SELECT id FROM events WHERE fingerprint=?',(a['fingerprint'],),True): continue
            existing=merge_match(a,events)
            if existing:
                links=existing['aux_links']
                if canon(a['link'])!=canon(existing['link']) and a['link'] not in links:
                    links.append(a['link'])
                    try: persist_event(existing['id'],dict(aux_links=json.dumps(links)),existing.get('revision'),('待审核','待推送','已推送'),'merge_source')
                    except ValueError: pass  # Preserve concurrent edits or deletion.
                continue
            if admitted>=capacity: overflow+=1; continue
            result=analyze(a,c)
            known=[r for r in REGIONS if r in result['region']]
            if known and not any(r in c['regions'] for r in known):
                sql('UPDATE raw_items SET decision=? WHERE fingerprint=?',('非目标区域',a['fingerprint']))
                # It consumed an analysis slot even though it did not enter the event pool.
                capacity-=1
                continue
            eid=insert_event(a,result,admission_day); admitted+=1
            events.append(dict(id=eid,fingerprint=a['fingerprint'],original_title=a['title'],link=a['link'],published_at=a['published_at'],aux_links=[]))
        sql('UPDATE runs SET finished_at=?,status=?,fetched_count=?,matched_count=?,candidate_count=?,analyzed_count=?,filtered_count=?,overflow_count=?,error=?,details=? WHERE id=?',(now(),'partial' if failures else 'success',len(fetched),len(matches),admitted,admitted,filtered,overflow,json.dumps(failures,ensure_ascii=False),json.dumps(details,ensure_ascii=False),rid))
    except Exception:
        sql('UPDATE runs SET finished_at=?,status=?,error=? WHERE id=?',(now(),'failed',json.dumps([{'error':'运行失败，请检查配置及来源'}],ensure_ascii=False),rid)); raise
    finally:
        try: prune_snapshots()
        finally: RUN_LOCK.release()
    return sql('SELECT * FROM runs WHERE id=?',(rid,),True)

def update_event(body,approve=False):
    eid=int(body['id']); row=sql('SELECT * FROM events WHERE id=?',(eid,),True)
    if not row: raise ValueError('动态不存在')
    if row['status']=='已删除': raise ValueError('已删除记录只可查看或恢复')
    editable={'title','company','region','language','category','summary','change','stage','relevance','action','evidence','evidence_zh','confidence','processing_status','processing_note','assignee','site_name'}
    fields={k:v for k,v in body.items() if k in editable}
    if 'title' in fields:
        fields['title']=clean_title(fields['title'])
        if not fields['title']: raise ValueError('请填写有效标题')
    if 'manual_body' in body:
        if not isinstance(body['manual_body'],str): raise ValueError('原文必须为文本')
        fields.update(manual_body=body['manual_body'],manual_body_updated_at=now())
    if 'aux_links' in body:
        links=body['aux_links']
        if not isinstance(links,list) or any(not isinstance(v,str) or urlsplit(v).scheme not in ('http','https') or not urlsplit(v).hostname for v in links): raise ValueError('辅助信源必须为 HTTP/HTTPS 链接，每行一个')
        fields['aux_links']=json.dumps(list(dict.fromkeys(v.strip() for v in links)),ensure_ascii=False)
    merged={**row,**fields}
    if approve and (merged['title']=='待生成中文标题' or not re.search(r'[\u4e00-\u9fff]',merged['title']) or not merged['summary'] or merged['summary'].startswith('尚未完成 AI')):
        raise ValueError('请先填写有效中文标题和摘要，再批准推送')
    if approve: fields['status']='待推送'
    elif row['status'] in ('待推送','已推送'): fields['status']='待审核'
    for key,choices in [('category',['']+CATEGORIES),('stage',['']+STAGES),('action',['']+ACTIONS),('confidence',['','高','中','低']),('relevance',['','高','中','低','待确认']),('processing_status',['待处理','已处理','已忽略'])]:
        if key in fields and fields[key] not in choices: raise ValueError(key+' 的取值无效')
    if any(not isinstance(v,str) for v in fields.values()): raise ValueError('字段必须是文本')
    if fields: return dict(ok=True,article=persist_event(eid,fields,body.get('revision'),('待审核','待推送','已推送')))
    return dict(ok=True,article=event_detail(eid))

def analysis_input(row):
    article=dict(title=row['original_title'],link=row['link'],source=row['source'],level=row['source_level'],published_at=row['published_at'],body=row['body'],site_name=row.get('site_name') or row['source'])
    raw=sql('SELECT data FROM raw_items WHERE fingerprint=?',(row['fingerprint'],),True)
    original=json.loads(raw['data']) if raw else {}
    if row.get('manual_body') and row['manual_body'].strip():
        # A pasted article is the source of truth, not supplementary material
        # to a previously collected diff or incomplete RSS excerpt.
        article['manual_source']=True
        return article
    if original.get('page_change') and original.get('page_change_details'):
        if longform.faulty_changes(original['page_change_details']):
            if not (row.get('manual_body') or row.get('fetched_body')) or longform.unavailable_page(row['body']):
                raise ValueError('历史差异来自网站临时故障页，不能分析为业务变化；请补充正常网页正文后分析')
            article['recovered_from_unavailable_page']=True
            article['title']=row['source']+' 网页正文'
            return article
        article.update(page_change=True,page_change_details=original['page_change_details'],body=original['body'])
        if row['body']!=article['body']: article['supplemental_body']=row['body']
    return article

def require_revision(body):
    if type(body.get('revision')) is not int or body['revision']<1:
        raise ValueError('缺少有效记录修订号，请重新打开详情')


def fetch_progress():
    with LOCK:
        return json.loads(json.dumps(FETCH_JOB,ensure_ascii=False))


def fetch_review_record(row,overwrite=False):
    """Only fetch fields may change; model/human fields and workflow stay intact."""
    eid=row['id'];fields={};length=0;status='failed';message=''
    try:
        current=sql('SELECT status,revision FROM events WHERE id=?',(eid,),True)
        if not current or current['status']!='待审核' or current['revision']!=row['revision']:
            return dict(id=eid,status='skipped',length=0,message='记录已被修改或离开待审核，未抓取')
        if row['manual_body'] and not overwrite:
            fields=dict(fetch_status='跳过',fetch_error='已有人工正文，保留人工内容')
            status='skipped';message=fields['fetch_error']
        else:
            result=fetch_body(row['link'])
            length=len(result['body']);message=result.get('message','')
            fields=dict(fetched_body=result['body'],fetched_body_updated_at=now(),fetch_status='成功',fetch_error=message)
            status='success'
    except ValueError as exc:
        message=str(exc);fields=dict(fetch_status='失败',fetch_error=message)
    except Exception:
        message='网页正文提取失败，请重试或手动粘贴原文';fields=dict(fetch_status='失败',fetch_error=message)
    try:
        article=persist_event(eid,fields,row['revision'],('待审核',),'fetch_body')
    except ValueError:
        return dict(id=eid,status='skipped',length=0,message='抓取期间记录已被修改或状态改变，未覆盖；请重新打开详情')
    return dict(id=eid,status=status,length=length,message=message,article=article)


def acquire_fetch():
    if not FETCH_LOCK.acquire(False): raise ValueError('已有正文抓取运行中，请等待完成')
    if not ANALYSIS_LOCK.acquire(False):
        FETCH_LOCK.release()
        raise ValueError('已有模型分析运行中，请等待完成后抓取')


def fetch_one(body):
    require_revision(body)
    if type(body.get('id')) is not int or body['id']<1: raise ValueError('新闻 ID 无效')
    if type(body.get('overwrite',False)) is not bool: raise ValueError('overwrite 必须为布尔值')
    acquire_fetch()
    try:
        row=event_detail(body['id'])
        if row['status']!='待审核': raise ValueError('仅待审核记录可抓取正文')
        if row['revision']!=body['revision']: raise ValueError('动态已更新，请重新打开详情后重试')
        result=fetch_review_record(row,body.get('overwrite',False))
        return dict(ok=result['status']=='success',**result)
    finally:
        ANALYSIS_LOCK.release();FETCH_LOCK.release()


def fetch_rows(ids):
    if ids is not None:
        if not isinstance(ids,list) or not ids or any(type(v) is not int or v<1 for v in ids):
            raise ValueError('ids 必须为非空新闻 ID 数组')
        ids=set(ids)
    return [r for r in get_events('待审核') if ids is None or r['id'] in ids]


def fetch_review_bodies(ids=None,rows=None):
    """Caller owns fetch/analysis locks. No 500-row dashboard limit here."""
    rows=fetch_rows(ids) if rows is None else rows
    with concurrent.futures.ThreadPoolExecutor(max_workers=3,thread_name_prefix='review-fetch') as pool:
        futures={pool.submit(fetch_review_record,row):row['id'] for row in rows}
        for future in concurrent.futures.as_completed(futures):
            try: result=future.result()
            except Exception: result=dict(id=futures[future],status='failed',length=0,message='抓取任务异常，原有正文已保留')
            result={k:v for k,v in result.items() if k!='article'}
            with LOCK:
                FETCH_JOB['completed']+=1
                FETCH_JOB[{'success':'success','failed':'failed','skipped':'skipped'}[result['status']]]+=1
                FETCH_JOB['results'].append(result)
    return dict(ok=True,**fetch_progress())


def start_fetch_bodies(body):
    rows=fetch_rows(body.get('ids'))
    acquire_fetch()
    try:
        with LOCK:
            FETCH_JOB.clear()
            FETCH_JOB.update(running=True,job_id=digest(now())[:16],total=len(rows),completed=0,success=0,failed=0,skipped=0,results=[],started_at=now(),finished_at='')
        def worker():
            try: fetch_review_bodies(rows=rows)
            finally:
                with LOCK: FETCH_JOB.update(running=False,finished_at=now())
                ANALYSIS_LOCK.release();FETCH_LOCK.release()
        threading.Thread(target=worker,daemon=True,name='review-fetch-batch').start()
        return dict(ok=True,started=True,**fetch_progress())
    except Exception:
        with LOCK: FETCH_JOB['running']=False
        ANALYSIS_LOCK.release();FETCH_LOCK.release()
        raise

def analyze_saved(row,c,manual=False,progress=None):
    try: result=analyze(analysis_input(row),c,strict=True,**({'progress':progress} if progress else {}))
    except ValueError as exc:
        return dict(ok=False,message='分析未完成：'+str(exc),article=event_detail(row['id']))
    if manual: result['status']='待审核'; result['analysis_mode']='人工原文 / '+result['analysis_mode']
    article=persist_event(row['id'],result,row['revision'],('待审核',),'manual_analysis' if manual else 'batch_analysis')
    return dict(ok=True,article=article)

def analyze_one(body):
    if not ANALYSIS_LOCK.acquire(False): raise ValueError('已有分析运行中，请稍后重试')
    try:
        if not isinstance(body.get('manual_body'),str) or not body['manual_body'].strip(): raise ValueError('原文不能为空')
        require_revision(body)
        row=event_detail(body['id'])
        if row['status']!='待审核': raise ValueError('请先将记录退回待审核')
        saved=update_event(body,False)['article']
        return analyze_saved(saved,config(),True)
    finally: ANALYSIS_LOCK.release()


def analysis_progress():
    with LOCK: return json.loads(json.dumps(ANALYSIS_JOB))


def start_analysis_job(body):
    """Save a single draft first, then analyze in a background worker.

    Revision checks at write time still reject edits/deletion during the job.
    The state endpoint deliberately excludes article text and credentials.
    """
    if not ANALYSIS_LOCK.acquire(False): raise ValueError('已有分析或正文抓取运行中，请稍后重试')
    try:
        c=config()
        if not all(c[k] for k in ('ai_url','ai_key','ai_model')): raise ValueError('请先保存完整的模型配置')
        mode=body.get('mode','single');saved=None
        if mode=='single':
            if not isinstance(body.get('manual_body'),str) or not body['manual_body'].strip(): raise ValueError('原文不能为空')
            require_revision(body)
            if event_detail(body['id'])['status']!='待审核': raise ValueError('请先将记录退回待审核')
            saved=update_event(body,False)['article'];rows=[saved]
        elif mode=='batch':
            ids=body.get('ids')
            if not isinstance(ids,list) or not ids or any(type(v) is not int or v<1 for v in ids): raise ValueError('请选择待审核记录')
            rows=[event_detail(eid) for eid in dict.fromkeys(ids[:20])]
            rows=[row for row in rows if row['status']=='待审核']
            if not rows: raise ValueError('没有待审核记录')
        else: raise ValueError('分析模式无效')
        with LOCK:
            ANALYSIS_JOB.clear()
            ANALYSIS_JOB.update(running=True,job_id=digest(now())[:16],mode=mode,ids=[r['id'] for r in rows],total=len(rows),completed=0,success=0,failed=0,results=[],stage='准备分析',parts_done=0,parts_total=0,started_at=now(),finished_at='')
        def progress(stage,done,total):
            with LOCK: ANALYSIS_JOB.update(stage=stage,parts_done=done,parts_total=total)
        def worker():
            try:
                for row in rows:
                    with LOCK: ANALYSIS_JOB.update(current_id=row['id'],stage='分析原文',parts_done=0,parts_total=0)
                    try: result=analyze_saved(row,c,mode=='single',progress)
                    except ValueError as exc: result=dict(ok=False,message=str(exc))
                    except Exception: result=dict(ok=False,message='分析处理异常，已保存的原文和已有结果均保留，请重试')
                    with LOCK:
                        ANALYSIS_JOB['completed']+=1
                        ANALYSIS_JOB['success' if result['ok'] else 'failed']+=1
                        ANALYSIS_JOB['results'].append(dict(id=row['id'],ok=result['ok'],message=result.get('message','')))
            finally:
                with LOCK: ANALYSIS_JOB.update(running=False,stage='分析结束',finished_at=now())
                ANALYSIS_LOCK.release()
        result=dict(ok=True,job=analysis_progress())
        if saved: result['article']=saved
        threading.Thread(target=worker,daemon=True,name='review-analysis').start()
        return result
    except Exception:
        ANALYSIS_LOCK.release()
        raise

def reanalyze(ids):
    if not ANALYSIS_LOCK.acquire(False): raise ValueError('已有分析运行中，请稍后重试')
    try:
        c=config()
        if not all(c[k] for k in ('ai_url','ai_key','ai_model')): raise ValueError('请先保存完整的模型配置')
        results=[]
        for eid in ids[:20]:
            row=event_detail(eid)
            if row['status']!='待审核': continue
            try: result=analyze_saved(row,c)
            except ValueError: result=dict(ok=False,message='记录在分析期间发生变化，未覆盖现有内容')
            results.append(dict(id=eid,ok=result['ok'],message=result.get('message','')))
        return dict(ok=True,results=results)
    finally: ANALYSIS_LOCK.release()

def deletion(body,restore=False):
    require_revision(body)
    if not PUSH_LOCK.acquire(False): raise ValueError('推送进行中，请稍后重试')
    try:
        fields=dict(status='待审核' if restore else '已删除',deleted_at='' if restore else now())
        return dict(ok=True,article=persist_event(body['id'],fields,body['revision'],('已删除',) if restore else ('待审核',),'restore' if restore else 'delete'))
    finally: PUSH_LOCK.release()

def return_to_review(body):
    if not PUSH_LOCK.acquire(False): raise ValueError('推送正在进行，请等待结果后再退回审核')
    try:
        with LOCK,closing(sqlite3.connect(DB)) as db,db:
            row=db.execute('SELECT status,revision FROM events WHERE id=?',(int(body['id']),)).fetchone()
            if not row: raise ValueError('动态不存在')
            if row[0]!='待推送': raise ValueError('仅待推送记录可以退回审核，请刷新页面')
            if 'revision' in body and int(body['revision'])!=row[1]: raise ValueError('动态已更新，请刷新后再退回审核')
            db.execute('UPDATE events SET status=?,updated_at=?,revision=revision+1 WHERE id=?',('待审核',now(),int(body['id'])))
        return dict(ok=True)
    finally: PUSH_LOCK.release()

def region_values(region):
    if region in REVIEW_REGIONS: return [region]
    if not region or region in ('待确认','未知','未披露'): return [UNKNOWN_OPTION]
    countries={'欧洲':['德国','法国','英国','西班牙','意大利'],'东南亚':['马来西亚','新加坡','印度尼西亚','印尼','越南','泰国','菲律宾'],'俄罗斯及独联体':['俄罗斯','哈萨克斯坦','白俄罗斯'],'中东':['沙特','阿联酋','迪拜','卡塔尔'],'拉美':['巴西','墨西哥','阿根廷','智利','哥伦比亚']}
    matches=[]
    for name,aliases in {**REGIONS,'北美':['北美','美国','加拿大','North America','USA','United States','Canada'],'全球':['全球','global','worldwide']}.items():
        if any((normalize(t) in normalize(region) if re.search(r'[\u4e00-\u9fff]',t) else contains(region,t)) for t in [name]+aliases+countries.get(name,[])): matches.append(name)
    return matches or ['其他']

def review_region(region):
    values=region_values(region)
    return values[0] if len(values)==1 and values[0] in REVIEW_REGIONS else '其他'

def release_time(value):
    if not value: return ''
    try:
        date=datetime.fromisoformat(value.replace('Z','+00:00'))
        if date.tzinfo is None: return ''  # Do not invent the timezone of old ambiguous dates.
        return date.astimezone(TZ).strftime('%Y/%m/%d %H:%M')
    except (ValueError,TypeError): return ''

def webhook_values(r,weekly_ids):
    def has(text,term):
        return normalize(term) in normalize(text) if re.search(r'[\u4e00-\u9fff]',term) else contains(text,term)
    values=dict(r)
    values['site_name']=r.get('site_name') or r['source']
    values['published_at']=release_time(r.get('published_at'))
    values['official_link']=r['link'] if r['source_level']=='A 级' else ''
    values['category']={'车载 OTA':'车载语音与竞品OTA','C 端模型':'C端主流AI模型'}.get(r['category'],r['category'])
    # The destination Bitable field is a single-select option, so emit one
    # string instead of the old array shape (for example "欧洲").
    values['region']=review_region(r.get('region',''))
    aliases={'英语':['英语','英文','English'],'德语':['德语','德文','German'],'泰语':['泰语','Thai'],'马来语':['马来语','Malay'],'印尼语':['印尼语','印度尼西亚语','Indonesian'],'俄罗斯语':['俄罗斯语','俄语','Russian'],'法语':['法语','French'],'西班牙语':['西班牙语','西语','Spanish']}
    values['language']=[name for name,terms in aliases.items() if any(has(r.get('language',''),t) for t in terms)] or [UNKNOWN_OPTION]
    companies={'Cerence':['Cerence'],'SoundHound':['SoundHound'],'Google/Gemini':['Google','Gemini'],'OpenAI/ChatGPT':['OpenAI','ChatGPT'],'Anthropic/Claude':['Anthropic','Claude'],'xAI/Grok':['xAI','Grok'],'Meta AI':['Meta'],'DeepSeek':['DeepSeek'],'Mistral':['Mistral'],'Yandex':['Yandex','Алиса','Яндекс'],'Sber/GigaChat':['Sber','GigaChat','ГигаЧат'],'Microsoft/Azure':['Microsoft','Azure'],'AWS':['AWS','Amazon Web Services'],'NVIDIA':['NVIDIA']}
    company=r.get('company','')
    values['company']=next((name for name,terms in companies.items() if any(contains(company,t) for t in terms)),None)
    if not values['company']:
        carmakers=['车企','Tesla','BMW','Mercedes','Volkswagen','Toyota','Geely','BYD','Volvo','Polestar','Zeekr','Chery','Hyundai','Kia','Renault','Stellantis','NIO','Xpeng','吉利','大众','奔驰','宝马','丰田','特斯拉','比亚迪','蔚来','小鹏','极氪']
        values['company']='车企' if any(contains(company,t) for t in carmakers) else '其他'
    raw=sql('SELECT data FROM raw_items WHERE fingerprint=?',(r['fingerprint'],),True)
    values['original_summary']=(r.get('manual_body') or r.get('fetched_body') or (json.loads(raw['data']).get('body','') if raw else '') or r.get('evidence',''))[:1200]
    values['processing_status']=r.get('processing_status') or '待处理'
    values['include_in_weekly']='是' if r['id'] in weekly_ids else '否'
    values['processing_note']=r.get('processing_note') or '建议动作：'+r['action']+'；核心变化：'+r['change']
    values['ai_score']={'高':5,'中':3,'低':1}.get(r['relevance'])
    return values

def preview(day=None):
    rows=get_events('待推送')
    if day: rows=[r for r in rows if r['published_at'] and datetime.fromisoformat(r['published_at']).astimezone(TZ).date().isoformat()<=day]
    c=config(); items=[]
    if rows and not effective_mappings(c): raise ValueError('请先在模型与 Webhook 页配置至少一个输出字段映射')
    weekly_ids={eid for report in sql('SELECT event_ids FROM reports') for eid in json.loads(report['event_ids'])}
    for r in rows:
        values=webhook_values(r,weekly_ids)
        item={}
        for mapping in effective_mappings(c):
            key=mapping['source'];value=mapping.get('value','') if key=='__constant__' else values.get(key,'')
            if key in ('assignee','ai_score') and value in (None,''): continue
            item[mapping['target']]=value
        items.append(item)
    payload=dict(items=items,generatedAt=now())
    payload['previewKey']=digest(json.dumps([[(r['id'],r['revision']) for r in rows],items],ensure_ascii=False,sort_keys=True))
    return rows,payload

def delivery_history(page=1,page_size=20,status='success',query=''):
    page=int(page);page_size=int(page_size)
    if page<1 or not 1<=page_size<=100: raise ValueError('分页参数无效')
    if status not in ('all','success','failed','pending','sending'): raise ValueError('推送状态无效')
    clauses=[];args=[]
    if status!='all': clauses.append('status=?');args.append(status)
    if query:
        clauses.append('(CAST(id AS TEXT) LIKE ? OR batch_key LIKE ? OR payload LIKE ?)')
        args.extend(['%'+str(query)[:200]+'%']*3)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    total=sql('SELECT COUNT(*) AS n FROM deliveries'+where,args,True)['n']
    rows=sql('SELECT id,batch_key,status,attempts,response,created_at,updated_at,article_ids FROM deliveries'+where+' ORDER BY id DESC LIMIT ? OFFSET ?',args+[page_size,(page-1)*page_size])
    for row in rows: row['article_count']=len(json.loads(row.pop('article_ids')))
    return dict(items=rows,total=total,page=page,page_size=page_size)

def delivery_detail(did):
    row=sql('SELECT * FROM deliveries WHERE id=?',(int(did),),True)
    if not row: raise ValueError('推送批次不存在')
    row['payload']=json.loads(row['payload']);row['article_versions']=json.loads(row.pop('article_ids'))
    return row

def validate_edited_payload(edited_payload, rows):
    """Only item fields are editable; envelope metadata belongs to the server."""
    if not isinstance(edited_payload,dict):
        raise ValueError('预览内容必须是 JSON 对象')
    if set(edited_payload)-{'items','generatedAt','previewKey','batchId'}:
        raise ValueError('请在 items 中修改推送字段，不支持新增批次顶层字段')
    items=edited_payload.get('items')
    if not isinstance(items,list):
        raise ValueError('预览 JSON 必须包含 items 数组')
    if len(items)!=len(rows):
        raise ValueError('items 数量须与待推送新闻一致，请仅修改各条新闻的字段')
    if any(not isinstance(item,dict) or not item for item in items):
        raise ValueError('items 中每条新闻必须是非空 JSON 对象')
    try: encoded=json.dumps(items,ensure_ascii=False,allow_nan=False)
    except (TypeError,ValueError): raise ValueError('预览中包含无效 JSON 值') from None
    if len(encoded.encode('utf-8'))>1_000_000: raise ValueError('预览内容过大，最多 1 MB')
    return items

def push(day=None,retry_id=None,expected=None,edited_payload=None):
    if not PUSH_LOCK.acquire(False): raise ValueError('已有推送进行中')
    try:
        c=config()
        if retry_id:
            delivery=sql('SELECT * FROM deliveries WHERE id=?',(int(retry_id),),True)
            if not delivery: raise ValueError('批次不存在')
            if delivery['status']=='success': return delivery
            payload=json.loads(delivery['payload']); versions=json.loads(delivery['article_ids'])
            if any(not sql('SELECT id FROM events WHERE id=? AND revision=? AND status=?',(i,v,'待推送'),True) for i,v in versions): raise ValueError('批次中的动态已编辑或处理，请重新预览新批次')
            current_rows,current=preview(day)
            items={r['id']:item for r,item in zip(current_rows,current['items'])}
            original_key=digest(json.dumps([versions,[items.get(i) for i,_ in versions]],ensure_ascii=False,sort_keys=True))
            if original_key!=payload.get('previewKey'):
                raise ValueError('字段映射或推送内容已改变，请重新预览新批次')
            did=delivery['id']
        else:
            rows,payload=preview(day)
            if edited_payload is not None and not expected: raise ValueError('请先生成预览后再编辑发送')
            if expected and payload['previewKey']!=expected: raise ValueError('待推送内容已改变，请重新预览再发送')
            if edited_payload is not None:
                payload['items']=validate_edited_payload(edited_payload,rows)
            if not rows: return dict(status='empty',message='暂无待推送动态')
            if not c['webhook_url']: return dict(status='not_configured',message='请先配置 Webhook 地址')
            versions=[(r['id'],r['revision']) for r in rows]
            batch=digest(json.dumps([versions,payload['items']],ensure_ascii=False,sort_keys=True))
            delivery=sql('SELECT * FROM deliveries WHERE batch_key=?',(batch,),True)
            if delivery and delivery['status']=='success': return delivery
            payload['batchId']=batch
            if delivery: payload=json.loads(delivery['payload'])
            did=delivery['id'] if delivery else sql('INSERT INTO deliveries(batch_key,payload,article_ids,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',(batch,json.dumps(payload,ensure_ascii=False),json.dumps(versions),'pending',now(),now()))
        if not c['webhook_url']: return dict(status='not_configured',message='请先配置 Webhook 地址')
        sql('UPDATE deliveries SET status=? WHERE id=?',('sending',did))
        for attempt in range(3):
            try:
                data,_=request_bytes(c['webhook_url'],data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Idempotency-Key':payload['batchId']},timeout=WEBHOOK_TIMEOUT)
                response=json.loads(data) if data.strip().startswith(b'{') else {}
                if response.get('code',0) not in (0,'0') or response.get('StatusCode',0) not in (0,'0') or response.get('success') is False: raise ValueError('Webhook 业务返回失败')
                with LOCK,closing(sqlite3.connect(DB)) as db, db:
                    db.execute('UPDATE deliveries SET status=?,attempts=attempts+1,response=?,updated_at=? WHERE id=?',('success','接收端已返回成功',now(),did))
                    for eid,rev in versions: db.execute('UPDATE events SET status=?,updated_at=? WHERE id=? AND revision=?',('已推送',now(),eid,rev))
                break
            except Exception as exc:
                sql('UPDATE deliveries SET status=?,attempts=attempts+1,response=?,updated_at=? WHERE id=?',('failed',type(exc).__name__+'：发送失败，请检查接收端',now(),did))
                if attempt<2: time.sleep(1+attempt)
        return sql('SELECT id,status,attempts,response,created_at,updated_at FROM deliveries WHERE id=?',(did,),True)
    finally: PUSH_LOCK.release()

def report():
    today=datetime.now(TZ).date(); monday=today-timedelta(days=today.weekday()); week=today.strftime('%G-W%V')
    rows=[r for r in get_events() if r['published_at'] and datetime.fromisoformat(r['published_at']).astimezone(TZ).date()>=monday and r['status'] in ('待推送','已推送')]
    rows.sort(key=lambda r:(r['relevance']=='高',r['confidence']=='高',r['source_level']=='A 级'),reverse=True); rows=rows[:5]
    lines=[f'# 海外车载语音 Trend 周报｜{week}','', '## 一、本周结论','',f'本周已有 {len(rows)} 条经确认的动态入选。' if rows else '本周暂无经确认的动态，待审核候选不写成正式结论。','', '## 二、重点动态','']
    for i,r in enumerate(rows,1):
        lines += [f"### {i}. {r['title']}",f"- 发生了什么：{r['summary']}",f"- 覆盖地区/语言：{r['region']} / {r['language']}",f"- 当前阶段：{r['stage']}",f"- 建议动作：{r['action']}",f"- 来源：{r['link']}",'']
    risks=[r for r in rows if r['category']=='风险舆情']
    lines+=['## 三、风险与舆情','', '；'.join(r['title'] for r in risks) if risks else '本周已确认记录中未发现重大风险；待审核信息仍需核实。','', '## 四、建议行动','']
    for r in rows[:3]: lines.append(f"- [ ] {r['action']}：{r['title']}｜负责人及完成时间待分配")
    lines+=['','## 五、持续观察','']+[f"- {r['original_title']}（待核实）" for r in get_events() if r['status']=='待审核'][:5]
    md='\n'.join(lines)
    sql('INSERT INTO reports VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET markdown=excluded.markdown,event_ids=excluded.event_ids,generated_at=excluded.generated_at',(week,md,json.dumps([r['id'] for r in rows]),now()))
    return dict(week=week,markdown=md,count=len(rows))

def overview():
    c=public_config(); states={s['id']:s for s in sql('SELECT * FROM source_state')}
    primary={source_signature(s):s for s in unique_sources(c['sources'])}
    counts={}
    for s in c['sources']:
        if s['enabled']: counts[source_signature(s)]=counts.get(source_signature(s),0)+1
    sources=[{**s,'state':states.get(primary.get(source_signature(s),s)['id']),'duplicate_count':counts.get(source_signature(s),0),'duplicate_of':primary.get(source_signature(s),s)['name']} for s in c['sources']]
    observations={}
    for record in sql('SELECT * FROM source_observation_days ORDER BY day DESC'):
        observations.setdefault(record['source_key'],[]).append(record)
    for source in sources:
        key=tracking.source_key(source);source['source_key']=key
        source['observation']=tracking.observation_status(observations.get(key,[]),changes=source['kind']=='webpage' and source.get('mode')=='changes')
    return dict(config=c,sources=sources,events=get_events(),ready_events=get_events('待推送'),deleted_events=get_events('已删除'),runs=sql('SELECT * FROM runs ORDER BY id DESC LIMIT 20'),deliveries=sql('SELECT id,status,attempts,response,created_at FROM deliveries ORDER BY id DESC LIMIT 20'),running=RUN_LOCK.locked(),analysis_running=ANALYSIS_LOCK.locked() and not FETCH_LOCK.locked(),analysis_job=analysis_progress(),fetch_running=FETCH_LOCK.locked(),fetch_job=fetch_progress())

def scheduler():
    while True:
        try:
            local=datetime.now(TZ); c=config(); day=str(local.date())
            # Persisted day key prevents duplicate daily execution on restart.
            if local.strftime('%H:%M')>=c['time'] and not RUN_LOCK.locked() and not sql('SELECT day FROM scheduler WHERE day=?',(day,),True):
                sql('INSERT INTO scheduler VALUES (?,?,?,?)',(day,'running','',now()))
                try:
                    run_pipeline('scheduled',str(local.date()-timedelta(days=1))); push(str(local.date()-timedelta(days=1)))
                    sql('UPDATE scheduler SET status=?,updated_at=? WHERE day=?',('completed',now(),day))
                    if local.weekday()==4: report()
                except Exception: sql('UPDATE scheduler SET status=?,error=?,updated_at=? WHERE day=?',('failed','自动运行失败，可手动重试',now(),day))
        except Exception: pass
        time.sleep(30)

init_db()
