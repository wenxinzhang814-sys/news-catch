"""Bounded OPML parsing only; no links fetched and no configuration writes."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

MAX_FILE_BYTES=1_000_000
MAX_ENTRIES=1000


def local_name(tag):
    return tag.rsplit('}',1)[-1].lower() if isinstance(tag,str) else ''


def parse_opml(data):
    if not isinstance(data,bytes) or not data: raise ValueError('OPML 文件为空')
    if len(data)>MAX_FILE_BYTES: raise ValueError('OPML 文件不能超过 1 MB')
    # Removing zero bytes also recognizes declarations in UTF-16/32 files.
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b',data.replace(b'\x00',b''),re.I):
        raise ValueError('不支持包含 DTD 或实体声明的 OPML 文件，请导出普通 OPML')
    try: root=ET.fromstring(data)
    except (ET.ParseError,ValueError,LookupError): raise ValueError('OPML 格式无效或编码不支持，请使用 UTF-8/UTF-16 编码的 OPML 文件') from None
    if local_name(root.tag)!='opml': raise ValueError('文件不是 OPML 格式')
    body=next((n for n in root if local_name(n.tag)=='body'),None)
    if body is None: raise ValueError('OPML 缺少 body 来源列表')
    rows=[];invalid=0;entries=0
    stack=[(n,[]) for n in reversed(list(body))]
    while stack:
        node,folders=stack.pop()
        if local_name(node.tag)!='outline': continue
        attrs={k.lower():v.strip() for k,v in node.attrib.items()}
        title=attrs.get('text') or attrs.get('title') or ''
        url=attrs.get('xmlurl') or attrs.get('htmlurl') or attrs.get('url')
        if url:
            entries+=1
            if entries>MAX_ENTRIES: raise ValueError('单次 OPML 最多导入 1000 个链接，请拆分文件')
            try:
                parts=urlsplit(url);port=parts.port
                valid=parts.scheme.lower() in ('http','https') and bool(parts.hostname) and not parts.username and not parts.password and len(url)<=8192 and not re.search(r'[\s\x00-\x1f\x7f\\]',url)
            except ValueError: valid=False
            if valid:
                kind='rss' if attrs.get('xmlurl') else 'webpage'
                rows.append(dict(name=(title or parts.hostname)[:200],url=url,kind=kind,mode='articles',enabled=True,group=' / '.join(folders)[:500] or 'OPML 导入'))
            else: invalid+=1
        next_folders=folders+[title] if not url and title else folders
        stack.extend((child,next_folders) for child in reversed(list(node)))
    if not entries: raise ValueError('OPML 中没有可导入的链接（xmlUrl、htmlUrl 或 url）')
    return dict(sources=rows,invalid=invalid,total=entries)
