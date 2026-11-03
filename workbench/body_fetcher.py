"""Bounded public HTTP fetch and static article extraction for human review.

Never uses the model, Webhook, browser automation, proxies or page JavaScript.
DNS answers are checked and the TCP connection is pinned to a checked address;
every redirect is independently validated before making its connection.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
from email.message import Message
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin, urldefrag, quote
import zlib

MAX_BYTES = 3_000_000
MAX_CHARS = 300_000
ATTEMPT_TIMEOUT = 15
MAX_REDIRECTS = 5
DNS_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='review-dns')


class FetchError(ValueError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def remaining(deadline):
    seconds = deadline-time.monotonic()
    if seconds <= 0:
        raise FetchError('网页访问超时（单次上限 15 秒）', True)
    return seconds


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # Disallow transition formats which could reach IPv4 private networks even
    # when their containing IPv6 prefix appears globally routable.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network('64:ff9b::/96') or address in ipaddress.ip_network('64:ff9b:1::/48'):
            return False
    return address.is_global and not address.is_multicast


def resolve_public(url, deadline):
    if not isinstance(url, str) or len(url) > 8192 or re.search(r'[\x00-\x20\x7f\\]', url):
        raise FetchError('原始链接格式无效')
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise FetchError('原始链接格式无效') from None
    if parsed.scheme not in ('http', 'https') or not host or parsed.username is not None or parsed.password is not None:
        raise FetchError('只允许无用户名密码的 HTTP/HTTPS 原始链接')
    host = host.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise FetchError('禁止抓取本地、内网或云元数据地址')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal:
        if not public_address(host):
            raise FetchError('禁止抓取本地、内网或云元数据地址')
        return parsed, host, port, str(literal)
    try:
        host = host.encode('idna').decode('ascii')
        future = DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        answers = future.result(timeout=remaining(deadline))
    except (FutureTimeout, TimeoutError):
        if 'future' in locals(): future.cancel()
        raise FetchError('域名解析超时', True) from None
    except socket.gaierror:
        raise FetchError('DNS 解析失败，请检查网站域名或网络', True) from None
    except UnicodeError:
        raise FetchError('网站域名无效') from None
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not public_address(address) for address in addresses):
        raise FetchError('域名指向本地、内网或非公网地址，已拒绝抓取')
    return parsed, host, port, addresses[0]


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, deadline):
        super().__init__(host, port, timeout=remaining(deadline))
        self.address, self.deadline = address, deadline

    def connect(self):
        # Numeric IP: do not resolve the host again after validation.
        self.sock = socket.create_connection((self.address, self.port), timeout=remaining(self.deadline))


class PinnedHTTPSConnection(PinnedHTTPConnection):
    def connect(self):
        super().connect()
        try:
            self.sock.settimeout(remaining(self.deadline))
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.close()
            raise


def read_response(response, connection, deadline):
    length = response.getheader('Content-Length')
    if length and length.isdigit() and int(length) > MAX_BYTES:
        raise FetchError('网页响应超过 3 MB，未保存不完整正文')
    encoding = (response.getheader('Content-Encoding') or 'identity').lower().strip()
    if encoding not in ('identity', 'gzip', 'deflate'):
        raise FetchError('网页压缩格式不支持：'+encoding)
    chunks, total, unpacked = [], 0, 0
    decoder = zlib.decompressobj(16+zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS) if encoding != 'identity' else None
    while True:
        sock=connection.sock or getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
        if sock: sock.settimeout(remaining(deadline))
        remaining(deadline)
        chunk = response.read1(min(65536, MAX_BYTES+1-total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise FetchError('网页响应超过 3 MB，未保存不完整正文')
        chunk = decoder.decompress(chunk, MAX_BYTES+1-unpacked) if decoder else chunk
        unpacked += len(chunk)
        if unpacked > MAX_BYTES or (decoder and decoder.unconsumed_tail):
            raise FetchError('解压后的网页超过 3 MB，已停止抓取')
        chunks.append(chunk)
    if length and length.isdigit() and total != int(length):
        raise FetchError('网页响应不完整，未保存不完整正文', True)
    if decoder and not decoder.eof:
        raise FetchError('网页压缩数据不完整', True)
    return b''.join(chunks)


def download_resource(url, feed=False):
    """One 15-second attempt, with a single additional transient-error retry."""
    for attempt in range(2):
        try:
            deadline = time.monotonic()+ATTEMPT_TIMEOUT
            current = urldefrag(url)[0] if isinstance(url,str) else url
            for redirect in range(MAX_REDIRECTS+1):
                parsed, host, port, address = resolve_public(current, deadline)
                cls = PinnedHTTPSConnection if parsed.scheme == 'https' else PinnedHTTPConnection
                with closing(cls(host, port, address, deadline)) as connection:
                    path = quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")
                    if parsed.query: path += '?'+quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~")
                    connection.request('GET', path, headers={
                        'User-Agent':'Mozilla/5.0 (compatible; TrendDesk/1.0; internal news review)',
                        'Accept':('application/rss+xml,application/atom+xml,application/xml,text/xml,text/plain;q=0.8,text/html;q=0.5' if feed else 'text/html,application/xhtml+xml,text/plain;q=0.8'),
                        'Accept-Encoding':'gzip, deflate', 'Connection':'close',
                    })
                    if connection.sock: connection.sock.settimeout(remaining(deadline))
                    response = connection.getresponse()
                    if response.status in (301,302,303,307,308):
                        location = response.getheader('Location')
                        if not location or redirect == MAX_REDIRECTS:
                            raise FetchError('网页重定向无效或次数过多')
                        current = urldefrag(urljoin(current,location))[0]
                        continue
                    if response.status != 200:
                        reasons={401:'需要登录',403:'网站拒绝访问或反爬限制',404:'网页不存在',429:'网站限流'}
                        raise FetchError('HTTP '+str(response.status)+'：'+reasons.get(response.status,'网站服务异常'), response.status in (403,408,429) or response.status >= 500)
                    metadata = Message();metadata['Content-Type'] = response.getheader('Content-Type') or ''
                    mime = metadata.get_content_type()
                    allowed = ('text/html','application/xhtml+xml','text/plain') + (('application/rss+xml','application/atom+xml','application/xml','text/xml','application/rdf+xml') if feed else ())
                    if mime not in allowed:
                        raise FetchError('不支持的内容类型：'+mime+('，需要 RSS/Atom/XML 订阅内容' if feed else '，仅抓取网页正文'))
                    data = read_response(response,connection,deadline)
                    charset = metadata.get_content_charset()
                    return data, current, mime, charset
        except FetchError as exc:
            error = exc
        except (socket.timeout, TimeoutError):
            error = FetchError('网页访问超时（单次上限 15 秒）', True)
        except ssl.SSLError:
            error = FetchError('网站 HTTPS 证书校验或 TLS 连接失败', True)
        except (OSError, http.client.HTTPException, zlib.error):
            error = FetchError('网页连接失败或响应不完整', True)
        if not error.retryable or attempt:
            raise error


def download_html(url):
    """Review extraction compatibility wrapper; feeds retain original XML bytes."""
    data, current, mime, charset = download_resource(url)
    if charset:
        try: data = data.decode(charset)
        except (LookupError,UnicodeError): pass
    return data, current, mime


def extract_body(data, url, mime='text/html'):
    try:
        import trafilatura
        from lxml import html as lxml_html
    except ImportError:
        raise FetchError('正文提取依赖未安装，请运行工作台 start.cmd 完成初始化') from None
    if mime == 'text/plain':
        text = data if isinstance(data,str) else trafilatura.utils.decode_file(data)
    else:
        # Release-note pages can embed earlier releases. Keep the target version
        # and image-alt feature markers so the existing per-feature analysis can
        # verify their boundaries even after menus/ads are removed.
        host = urlsplit(url).hostname or ''
        target = re.search(r'/version/(\d+(?:\.\d+){1,4})(?:/|$)',urlsplit(url).path)
        if (host == 'notateslaapp.com' or host.endswith('.notateslaapp.com')) and target:
            data = scope_tesla_html(data,target[1],lxml_html)
        text = trafilatura.extract(data, url=url, output_format='markdown', include_comments=False,
                                   include_tables=True, include_links=False, include_images=False,
                                   favor_recall=True, deduplicate=True) or ''
        if target and (host == 'notateslaapp.com' or host.endswith('.notateslaapp.com')):
            # Extraction removes image alt-text. Restore the exact version
            # boundary from verified DOM headings, rather than losing coverage.
            tree=lxml_html.fromstring(data)
            parts=[]
            for section in tree.xpath('.//section[contains(concat(" ",normalize-space(@class)," ")," mod-update-feature ")]'):
                images=section.xpath('.//img[@alt]')
                if not images:continue
                alt=images[0].get('alt','')
                match=re.search(r'^Tesla (.+) feature in update (\d+(?:\.\d+){1,4})$',alt)
                if not match:continue
                # Small release-note sections fall below generic article-length
                # heuristics. Their explicitly scoped DOM is already the main
                # content: retain badges and paragraphs without that heuristic.
                fragment='\n'.join(piece.strip() for piece in section.itertext() if piece.strip() and piece.strip()!=alt)
                if len(fragment)<60:raise FetchError('发布说明功能段落未完整提取，请手动粘贴原文')
                parts.append('## '+match[1]+'\n'+alt+'\n'+fragment)
            if parts:text='\n\n'.join(parts)
    text = '\n'.join(re.sub(r'[ \t\xa0]+',' ',line).strip() for line in text.splitlines())
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if re.search(r'The server is temporarily unavailable',text,re.I) and re.search(r'We apologi[sz]e for (?:any |the )?inconvenience',text,re.I):
        raise FetchError('网站返回临时维护/不可用页面，未保存为新闻正文')
    challenge = re.search(r'just a moment|verify (?:that )?you are human|checking your browser|enable javascript and cookies|access denied|captcha', text, re.I)
    if challenge and len(text) < 1000:
        raise FetchError('网站返回登录/验证码/反爬页面，请手动粘贴原文')
    if len(text) < 100:
        raise FetchError('正文为空或过短，可能需要 JavaScript 渲染、登录或手动粘贴')
    if len(text)>MAX_CHARS:raise FetchError('正文超过 300,000 字符，未截断保存，请按独立文章人工补充')
    return text,''


def scope_tesla_html(data,version,lxml_html):
    """Preserve explicit feature alt-text for the existing release-note parser.

    Precise DOM isolation is used only if target-version feature sections are
    found. Other pages still go through the normal extractor.
    """
    try:
        tree=lxml_html.fromstring(data)
    except (ValueError,TypeError):
        return data
    from copy import deepcopy
    from analysis_quality import version_key
    sections=[]
    for section in tree.xpath('//section[contains(concat(" ",normalize-space(@class)," ")," mod-update-feature ")]'):
        markers=[re.search(r'feature\s+in\s+update\s+(\d+(?:\.\d+){1,4})\b',img.get('alt',''),re.I) for img in section.xpath('.//img[@alt]')]
        if any(m and version_key(m[1])==version_key(version) for m in markers):
            sections.append(deepcopy(section))
    if sections:
        root=lxml_html.Element('article')
        title=lxml_html.Element('h1');title.text='Tesla Update '+version;root.append(title)
        for section in sections:
            for element in section.xpath('.//script|.//style|.//*[contains(concat(" ",normalize-space(@class)," ")," button-container ") or contains(concat(" ",normalize-space(@class)," ")," show-mobile ")]'):
                element.drop_tree()
            root.append(section)
        tree=root
    for image in tree.xpath('.//img[@alt]'):
        alt=image.get('alt','')
        if re.search(r'feature\s+in\s+update\s+\d',alt,re.I):
            marker=lxml_html.Element('p');marker.text=alt;image.addprevious(marker)
    return lxml_html.tostring(tree,encoding='unicode')


def fetch_body(url):
    data, final_url, mime = download_html(url)
    text, warning = extract_body(data, final_url, mime)
    return dict(body=text, message=warning)
