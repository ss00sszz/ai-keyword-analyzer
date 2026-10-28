"""Bounded public-blog crawler: sitemaps, archives, categories and pagination."""
from collections import deque
from urllib.parse import urljoin, urlsplit, urlunsplit, urldefrag, parse_qs
from urllib.robotparser import RobotFileParser
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup

UA = 'BlogKeywordResearchBot/1.0 (+local user-initiated crawler)'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xml;q=0.9,*/*;q=0.7'}
ARTICLE_SELECTORS = ['article h1', 'h1.entry-title', 'h1.post-title', '.entry-content h1']
LIST_SELECTORS = ['article h2 a','article h3 a','.post-title a','.entry-title a','h2.entry-title a','h3.entry-title a','a[href*="/entry/"]','main h2 a','main h3 a', '.post-item a', '.list_content a']
NAV_SELECTORS = ['a[rel="next"]','a.next', '.pagination a', '.paging a', '.category-list a', '.category a', 'a[href*="/category/"]','a[href*="/page/"]','a[href*="?page="]','a[href*="/tag/"]']


def normalize(link, base=None):
    link = urljoin(base, link) if base else link
    link, _ = urldefrag(link)
    p = urlsplit(link)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        return None
    host=p.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost','.local','.internal')):
        return None
    # Resolve to avoid accessing loopback / local addresses via a user-supplied host.
    try:
        records=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
        if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
            return None
    except (socket.gaierror,ValueError):
        return None
    path=p.path or '/'
    return urlunsplit((p.scheme, p.netloc.lower(),path,p.query,''))


def clean_title(text):
    text=re.sub(r'\s+',' ',text or '').strip()
    return text[:180]


def is_article_url(link):
    p=urlsplit(link); path=p.path.lower()
    if re.search(r'/entry/[^/]+|/\d{4}/\d{2}/[^/]+|/archives/\d+|/\d{3,}(?:/|$)', path):return True
    if any(x in path for x in ['/category/','/tag/','/page/','/search','/feed','/wp-json']): return False
    return len([x for x in path.split('/') if x])>=1 and bool(re.search(r'[a-z0-9가-힣-]{9,}',path))


def likely_navigation(link):
    p=urlsplit(link); path=p.path.lower(); query=parse_qs(p.query)
    return (bool(re.search(r'/page/\d+|/category(?:/|$)|/tag(?:/|$)|/archives(?:/|$)',path))
            or 'page' in query or 'paged' in query or bool(re.search(r'/\d+(?:/)?$',path)))


def run_crawl(address, max_articles=100, max_pages=80, delay=0.3, progress=None):
    start=normalize(address if address.startswith(('http://','https://')) else 'https://'+address)
    if not start: raise ValueError('공개된 블로그 주소를 확인해 주세요.')
    host=urlsplit(start).hostname
    root=f'{urlsplit(start).scheme}://{urlsplit(start).netloc}'
    session=requests.Session(); session.headers.update(HEADERS)
    # Restrict redirects as well; requests automatically follows redirects otherwise.
    def get(url):
        response=session.get(url, timeout=12, allow_redirects=False)
        if 300<=response.status_code<400:
            destination=normalize(response.headers.get('Location',''),url)
            if not destination or urlsplit(destination).hostname!=host:return None
            response=session.get(destination,timeout=12,allow_redirects=False)
        response.raise_for_status()
        return response if response.status_code==200 else None
    robots=RobotFileParser()
    robots_url=root+'/robots.txt'
    try:
        rr=get(robots_url)
        if rr and 'text' in rr.headers.get('Content-Type','').lower(): robots.parse(rr.text.splitlines())
        else: robots.parse([])
    except requests.RequestException:
        robots.parse([])
    def allowed(url):
        return urlsplit(url).hostname==host and robots.can_fetch(UA,url)
    results=[]; seen_articles=set(); seen_pages=set(); queue=deque(); queued=set(); failures=[]
    def enqueue(url):
        link=normalize(url,start)
        if link and allowed(link) and link not in queued and link not in seen_pages and len(queued)<max_pages*12:
            queued.add(link);queue.append(link)
    def add(title,url):
        link=normalize(url,start)
        title=clean_title(title)
        if link and allowed(link) and link not in seen_articles and len(title)>=6 and len(results)<max_articles and is_article_url(link):
            seen_articles.add(link);results.append({'게시글 제목':title,'게시글 URL':link})
    enqueue(start)
    if start.rstrip('/')==root.rstrip('/'):
        enqueue(root+'/page/2')
        enqueue(root+'/?page=2')
    # read sitemap(s) separately; a sitemap index can reference child sitemaps.
    sitemap_q=deque([root+'/sitemap.xml',root+'/sitemap_index.xml',root+'/sitemap_index.xml',root+'/post-sitemap.xml',root+'/sitemap.xml?page=1'])
    seen_maps=set()
    while sitemap_q and len(seen_maps)<12 and len(results)<max_articles:
        url=sitemap_q.popleft()
        if url in seen_maps or not allowed(url):continue
        seen_maps.add(url)
        try:
            res=get(url)
            if not res or len(res.content)>5_000_000:continue
            tree=ET.fromstring(res.content)
            for node in tree.iter():
                if node.tag.split('}')[-1]!='loc' or not node.text:continue
                link=normalize(node.text.strip())
                if not link or not allowed(link):continue
                if link.endswith('.xml') or '/sitemap' in urlsplit(link).path and '.xml' in urlsplit(link).path:
                    if len(seen_maps)+len(sitemap_q)<25:sitemap_q.append(link)
                elif is_article_url(link):enqueue(link)
                elif likely_navigation(link):enqueue(link)
        except (ET.ParseError,requests.RequestException,ValueError) as e:
            failures.append(f'사이트맵: {type(e).__name__}')
    pages=0
    while queue and pages<max_pages and len(results)<max_articles:
        url=queue.popleft()
        if url in seen_pages:continue
        seen_pages.add(url);pages+=1
        try:
            response=get(url)
            if not response or 'html' not in response.headers.get('Content-Type','').lower():continue
            if len(response.content)>5_000_000:continue
            doc=BeautifulSoup(response.text,'html.parser')
            for tag in doc(['script','style','noscript']):tag.decompose()
            for sel in LIST_SELECTORS:
                for a in doc.select(sel):
                    if a.get('href'): add(a.get_text(' ',strip=True),urljoin(url,a['href']))
            # Article page fetched from sitemap or an archive: prefer metadata / page headline.
            if is_article_url(url):
                e=doc.select_one('meta[property="og:title"]')
                title=(e.get('content') if e else None) or (doc.select_one('h1').get_text(' ',strip=True) if doc.select_one('h1') else None)
                if title:add(title,url)
            for sel in NAV_SELECTORS:
                for a in doc.select(sel):
                    if a.get('href'):enqueue(urljoin(url,a['href']))
            # Some themes expose pagination without conventional CSS class.
            for a in doc.select('a[href]'):
                label=a.get_text(' ',strip=True).lower()
                dest=urljoin(url,a['href'])
                if likely_navigation(dest) and (len(label)<40 or 'category' in dest or 'page' in dest):enqueue(dest)
            if progress:progress(pages,len(results),len(queue))
            if delay:time.sleep(delay)
        except (requests.RequestException,ValueError) as e:
            failures.append(f'{url}: {str(e)[:80]}')
    return results,{'방문 페이지':pages,'발견 게시글':len(results),'탐색 대기':len(queue),'접근 실패':len(failures),'오류 예시':failures[:3]}
