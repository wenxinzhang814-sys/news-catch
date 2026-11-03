"""Run an independent local copy with its own data and an available port."""
from pathlib import Path
import argparse
import copy
import ctypes
import hashlib
import importlib.metadata
import json
import os
import socket
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
import webbrowser

BASE = Path(__file__).resolve().parent
CODE = BASE / 'workbench'
INFO = json.loads((BASE / 'package-info.json').read_text(encoding='utf-8'))
NAME = INFO['name']


def configure(data):
    for name in list(os.environ):
        if name.startswith('TREND_') or name in ('HOST', 'PORT', 'PYTHONHOME', 'PYTHONPATH'):
            os.environ.pop(name, None)
    os.environ['TREND_DATA_DIR'] = str(data)
    os.environ['HOST'] = '127.0.0.1'
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(CODE))


def modules():
    import engine
    if INFO['edition'] == 'portable':
        # Only the independent distribution uses these public defaults.
        # No original workbench code or local configuration is modified.
        preset = json.loads((CODE / '默认配置' / 'defaults.json').read_text(encoding='utf-8'))
        engine.initial_config = lambda: copy.deepcopy(preset)
    return engine


def check_runtime():
    with tempfile.TemporaryDirectory(prefix='newscatch-check-') as directory:
        configure(Path(directory))
        os.environ['TREND_DISABLE_SCHEDULER'] = '1'
        engine = modules()
        from zoneinfo import ZoneInfo
        from lxml import etree
        import regex
        config = engine.config()
        assert not any(config[k] for k in ('ai_key', 'ai_url', 'ai_model', 'webhook_url'))
        if INFO['edition'] == 'source':
            assert not any(config[k] for k in ('sources', 'rules', 'regions', 'field_map', 'field_mappings'))
        versions = {item.metadata['Name'].lower().replace('_', '-'): item.version
                    for item in importlib.metadata.distributions()}
        for line in (CODE / 'requirements.lock.txt').read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                name, expected = line.strip().split('==')
                assert versions[name.lower().replace('_', '-')] == expected, name
        print(json.dumps(dict(ok=True, name=NAME, python=sys.version.split()[0],
            rss=sum(s['kind'] == 'rss' for s in config['sources']),
            webpages=sum(s['kind'] == 'webpage' for s in config['sources']),
            rules=len(config['rules']), mappings=len(engine.effective_mappings(config)),
            timezone=str(ZoneInfo('Asia/Shanghai')), credentials_empty=True), ensure_ascii=False))


def existing_instance(data, no_browser):
    opener = build_opener(ProxyHandler({}))
    for _ in range(50):
        try:
            record = json.loads((data / 'launcher.json').read_text(encoding='utf-8'))
            port = int(record['port'])
            if not 1 <= port <= 65535:
                continue
            url = f'http://127.0.0.1:{port}/'
            with opener.open(url + 'api/health', timeout=.3) as response:
                health = json.load(response)
            if health['runtime']['pid'] == record['pid']:
                print(NAME + ' 已在运行：' + url)
                if not no_browser:
                    webbrowser.open(url)
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(.1)
    raise OSError('本目录的服务仍在启动，请稍后打开原启动窗口中的地址。')


def instance_mutex(data, no_browser):
    if os.name != 'nt':
        return None, False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    identity = str(BASE).casefold() + '|' + str(data).casefold()
    handle = kernel.CreateMutexW(None, False, 'Local\\NewsWorkbench-' + hashlib.sha256(identity.encode()).hexdigest()[:24])
    error = ctypes.get_last_error()
    if not handle:
        raise OSError('无法创建启动锁，请解压到可写文件夹。')
    if error == 183:
        kernel.CloseHandle(handle)
        existing_instance(data, no_browser)
        return None, True
    (data / 'launcher.json').unlink(missing_ok=True)
    return (kernel, handle), False


def choose_port(requested):
    for port in [requested] if requested else range(4173, 4193):
        with socket.socket() as connection:
            if os.name == 'nt':
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                connection.bind(('127.0.0.1', port))
            except OSError:
                continue
            return port
    raise OSError('端口已被占用，请关闭旧窗口，或用 --port 指定空闲端口。')


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=NAME + ' 本地工作台')
    parser.add_argument('--check', action='store_true', help='离线检查，不创建正式数据')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-scheduler', action='store_true')
    parser.add_argument('--port', type=int)
    parser.add_argument('--data-dir', type=Path, help='独立数据目录，默认 workbench/data')
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error('端口必须在 1–65535 范围内')
    if args.check:
        check_runtime()
        return
    data = (args.data_dir or CODE / 'data').resolve()
    data.mkdir(parents=True, exist_ok=True)
    configure(data)
    lock, already = instance_mutex(data, args.no_browser)
    if already:
        return
    try:
        os.environ['PORT'] = str(choose_port(args.port))
        if args.no_browser:
            os.environ['TREND_NO_BROWSER'] = '1'
        if args.no_scheduler:
            os.environ['TREND_DISABLE_SCHEDULER'] = '1'
        engine = modules()
        engine.config()
        import server
        original_server = server.WorkbenchHTTPServer

        class ReadyServer(original_server):
            def __init__(self, *values, **keywords):
                super().__init__(*values, **keywords)
                record = data / 'launcher.json'
                temporary = record.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(port=self.server_port, pid=os.getpid())), encoding='utf-8')
                temporary.replace(record)

        server.WorkbenchHTTPServer = ReadyServer
        print(NAME + ' 已启动，关闭本窗口后服务与定时任务停止。')
        print('数据保存在：' + str(data))
        server.main()
    finally:
        if lock:
            lock[0].CloseHandle(lock[1])


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n' + NAME + ' 已停止。')
    except Exception as exc:
        print('启动未完成：' + str(exc), file=sys.stderr)
        print('请查看 README.md 或使用说明.md。', file=sys.stderr)
        raise SystemExit(1)
