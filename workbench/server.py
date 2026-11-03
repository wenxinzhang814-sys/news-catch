"""Start with start.cmd or the project virtual environment's Python."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import json
import os
import hashlib
import socket
import threading
import webbrowser
import engine as e

RELEASE = '2026-11-02.1'
STARTED_AT = e.now()
CODE_FILES = ('server.py','engine.py','analysis_summary.py','analysis_quality.py','long_analysis.py','app.js','index.html')
CODE_ID = hashlib.sha256(b''.join((e.ROOT/name).read_bytes() for name in CODE_FILES)).hexdigest()[:16]

def runtime_info():
    return dict(pid=os.getpid(),release=RELEASE,code_id=CODE_ID,started_at=STARTED_AT)

class WorkbenchHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR allows two live HTTP servers on the same endpoint.
    # Both can then receive requests while sharing the database but running
    # different imported Python code. Bind exclusively before starting tasks.
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()

class Handler(BaseHTTPRequestHandler):
    def send(self, data, code=200, mime='application/json; charset=utf-8'):
        content=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(content)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        try: self.wfile.write(content)
        except (BrokenPipeError,ConnectionResetError): pass
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in ('/','/index.html','/styles.css','/live.css','/app.js'):
            name='index.html' if path=='/' else path[1:]
            mime={'html':'text/html','css':'text/css','js':'text/javascript'}[name.rsplit('.',1)[1]]
            return self.send((e.ROOT/name).read_bytes(),mime=mime+'; charset=utf-8')
        if path=='/api/health': return self.send(dict(ok=True,aiConfigured=bool(e.config()['ai_key'] and e.config()['ai_url'] and e.config()['ai_model']),webhookConfigured=bool(e.config()['webhook_url']),runtime=runtime_info()))
        if path=='/api/state': return self.send(dict(e.overview(),runtime=runtime_info()))
        if path=='/api/config': return self.send(e.public_config())
        if path=='/api/sources': return self.send(dict(sources=e.public_config()['sources'],rules=e.config()['rules']))
        if path in ('/api/sources/snapshots','/api/sources/compare'):
            try:
                params=parse_qs(urlsplit(self.path).query)
                return self.send(e.source_history(params['source_key'][0]) if path.endswith('/snapshots') else e.compare_snapshots(params['before_id'][0],params['after_id'][0]))
            except (KeyError,ValueError,TypeError): return self.send(dict(error='快照参数无效、快照不存在或不允许比较'),400)
        if path=='/api/articles': return self.send(dict(articles=e.get_events()))
        if path=='/api/articles/fetch-bodies': return self.send(e.fetch_progress())
        if path=='/api/articles/analysis-jobs': return self.send(e.analysis_progress())
        if path=='/api/articles/deleted': return self.send(dict(articles=e.get_events('已删除')))
        if path in ('/api/deliveries','/api/deliveries/detail'):
            try:
                params=parse_qs(urlsplit(self.path).query)
                if path.endswith('/detail'): return self.send(dict(delivery=e.delivery_detail(params['id'][0])))
                return self.send(e.delivery_history(params.get('page',[1])[0],params.get('page_size',[20])[0],params.get('status',['success'])[0],params.get('q',[''])[0]))
            except (KeyError,ValueError): return self.send(dict(error='推送记录不存在或查询参数无效'),400)
        if path=='/api/articles/detail':
            try: return self.send(dict(article=e.event_detail(parse_qs(urlsplit(self.path).query)['id'][0])))
            except (KeyError,ValueError): return self.send(dict(error='动态不存在或 ID 无效'),404)
        if path=='/api/preview':
            rows,payload=e.preview(); return self.send(dict(count=len(rows),payload=payload))
        if path=='/api/report': return self.send(e.sql('SELECT * FROM reports ORDER BY generated_at DESC LIMIT 1',one=True) or dict(markdown='点击“生成本周周报”获取真实数据周报。'))
        return self.send(dict(error='Not found'),404)
    def do_POST(self):
        origin=self.headers.get('Origin')
        if origin and urlsplit(origin).netloc!=self.headers.get('Host'):
            return self.send(dict(error='仅接受同一工作台页面的请求'),403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length>20_000_000: raise ValueError('单次请求超过20MB，请分批提交')
            body=json.loads(self.rfile.read(length) or b'{}'); path=urlsplit(self.path).path
            if path=='/api/config': return self.send(e.save_config(body))
            if path=='/api/sources/test': return self.send(e.test_source(body))
            if path=='/api/sources/import-opml': return self.send(e.import_opml(body))
            if path=='/api/run':
                if e.RUN_LOCK.locked(): return self.send(dict(error='已有采集运行中'),409)
                threading.Thread(target=e.run_pipeline,daemon=True).start()
                return self.send(dict(started=True),202)
            if path=='/api/articles/update':
                e.require_revision(body)
                return self.send(e.update_event(body,body.get('approve',False)))
            if path=='/api/articles/return': return self.send(e.return_to_review(body))
            if path=='/api/articles/analyze': return self.send(e.analyze_one(body))
            if path=='/api/articles/analysis-jobs': return self.send(e.start_analysis_job(body),202)
            if path=='/api/articles/fetch-body': return self.send(e.fetch_one(body))
            if path=='/api/articles/fetch-bodies': return self.send(e.start_fetch_bodies(body),202)
            if path=='/api/articles/delete': return self.send(e.deletion(body))
            if path=='/api/articles/restore': return self.send(e.deletion(body,True))
            if path=='/api/articles/approve':
                records=body.get('articles')
                if not isinstance(records,list) or not records or any(not isinstance(r,dict) or 'id' not in r or 'revision' not in r for r in records):
                    raise ValueError('批准必须提供每条记录的 ID 和修订号，请刷新后重试')
                for r in records: e.require_revision(r)
                for r in records: e.update_event(dict(id=r['id'],revision=r['revision']),True)
                return self.send(dict(ok=True))
            if path=='/api/articles/reanalyze':
                return self.send(e.reanalyze(body.get('ids',[])))
            if path=='/api/push': return self.send(e.push(retry_id=body.get('retry_id'),expected=body.get('previewKey'),edited_payload=body.get('payload')))
            if path=='/api/report': return self.send(e.report())
            return self.send(dict(error='Not found'),404)
        except (ValueError,KeyError,TypeError) as exc: return self.send(dict(error=str(exc)),400)
        except Exception: return self.send(dict(error='服务处理失败，请查看运行记录'),500)

def main():
    e.config()
    try:
        server=WorkbenchHTTPServer((os.getenv('HOST','127.0.0.1'),int(os.getenv('PORT','4173'))),Handler)
    except OSError as exc:
        if exc.errno in (48,98,10013,10048):
            raise SystemExit('工作台端口已被占用；请使用已打开的工作台，或关闭原服务后再启动。未启动第二个服务。') from None
        raise
    server.daemon_threads=True
    e.migrate_titles()
    e.migrate_webhook_v9()
    e.migrate_source_history()
    e.sql("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'",(e.now(),))
    if os.getenv('TREND_DISABLE_SCHEDULER')!='1': threading.Thread(target=e.scheduler,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}/'
    print('News Catch: '+url+' | release='+RELEASE+' | pid='+str(os.getpid())+' | code='+CODE_ID,flush=True)
    if os.getenv('TREND_NO_BROWSER')!='1': webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()

if __name__=='__main__': main()
