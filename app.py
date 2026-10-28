import io, json, re, time, zipfile, hmac, hashlib, base64
from urllib.parse import urlparse, urljoin
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI
from crawler import run_crawl

st.set_page_config(page_title='AI 블로그 노출 · 네이버 검색량 분석기', page_icon='🔎', layout='wide')
st.title('🔎 AI 블로그 노출 · 네이버 검색량 분석기 v3.2')
st.caption('제목 → GPT 검색어 추천 → Google 검색 결과 API → 플랫폼별 노출 확인. 이 도구는 구글 검색 순위를 보장하지 않습니다.')

MODELS = ['gpt-5-nano','gpt-5-mini','gpt-5','gpt-6-luna','gpt-6.1-sol']
UA = {'User-Agent':'Mozilla/5.0 (compatible; KeywordSERPChecker/1.0)'}

def ensure_url(value):
    val=value.strip()
    if not val: raise ValueError('URL을 입력하세요.')
    if not val.startswith(('https://','http://')): val='https://'+val
    host=urlparse(val).hostname or ''
    if not host or host in ('localhost',) or host.endswith(('.local','.internal')):
        raise ValueError('공개된 웹사이트 주소를 넣으세요.')
    return val

def extract_title(html):
    doc=BeautifulSoup(html,'html.parser')
    candidates=[doc.select_one('meta[property="og:title"]'),doc.select_one('h1'),doc.title]
    for e in candidates:
        if e:
            txt=e.get('content','') if e.name=='meta' else e.get_text(' ',strip=True)
            if txt: return re.sub(r'\s+',' ',txt).strip()
    return ''

def fetch_titles(address, maximum):
    address=ensure_url(address)
    s=requests.Session(); s.headers.update(UA)
    r=s.get(address,timeout=15);r.raise_for_status()
    doc=BeautifulSoup(r.text,'html.parser')
    title=extract_title(r.text)
    source_host=(urlparse(address).hostname or '').lower()
    is_article=(doc.select_one('article h1') is not None or doc.select_one('meta[property="article:published_time"]') is not None or '/entry/' in urlparse(address).path or re.search(r'/\d{4}/\d{2}/',address) is not None)
    found=[]; seen=set()
    def add(t,link):
        t=re.sub(r'\s+',' ',str(t)).strip()
        if not t or len(t)<6: return
        if len(t)>180: t=t[:180]
        link=urljoin(address,link)
        if (urlparse(link).hostname or '').lower()!=source_host: return
        if link not in seen:
            seen.add(link);found.append({'게시글 제목':t,'게시글 URL':link})
    if is_article:
        add(title,address)
    else:
        for selector in ['article h2 a','article h3 a','.post-title a','.entry-title a','h2.entry-title a','h3.entry-title a','a[href*="/entry/"]','main h2 a','main h3 a']:
            for a in doc.select(selector): add(a.get_text(' ',strip=True),a.get('href',''))
            if len(found)>=maximum: break
        if not found:
            for a in doc.select('a[href]'):
                href=urljoin(address,a.get('href',''))
                if urlparse(href).hostname==source_host and ('/entry/' in href or re.search(r'/\d{4}/\d{2}/',href)):
                    add(a.get_text(' ',strip=True),href)
        if not found and title: add(title,address)
    return found[:maximum]

def extract_keywords(client,model,title,k):
    instructions=(
        '당신은 한국어 검색의도와 SEO에 정통한 키워드 분석가입니다. '
        '입력 제목 하나에 대해 실사용자가 검색창에 입력할 자연스러운 검색어를 추출하세요. '
        '제목의 핵심 어휘 및 의미에서 크게 벗어나지 말고 억지 조합이나 중복을 피하세요. '
        '최대 지정 수만큼 추천합니다. 반드시 JSON 객체 {"keywords":["검색어",...]}만 출력하세요.'
    )
    resp=client.responses.create(model=model,input=[{'role':'system','content':instructions},{'role':'user','content':f'제목: {title}\n최대 키워드 수: {k}'}],max_output_tokens=1100)
    raw=resp.output_text.strip()
    try: data=json.loads(raw)
    except json.JSONDecodeError:
        m=re.search(r'\{[\s\S]*\}',raw)
        if not m: raise ValueError('AI가 키워드 JSON을 반환하지 않았습니다. 다른 모델을 선택하거나 재시도하세요.')
        data=json.loads(m.group())
    items=data.get('keywords',[])
    if not isinstance(items,list): raise ValueError('키워드 응답 형식이 잘못되었습니다.')
    uniq=[];seen=set()
    for item in items:
        x=re.sub(r'\s+',' ',str(item)).strip()
        if x and x not in seen and len(x)<90:seen.add(x);uniq.append(x)
    return uniq[:k]

def search_serper(key,q,num):
    response=requests.post('https://google.serper.dev/search',headers={'X-API-KEY':key,'Content-Type':'application/json'},json={'q':q,'gl':'kr','hl':'ko','num':num},timeout=30)
    if not response.ok:raise RuntimeError(f'Serper 요청 실패 ({response.status_code}): {response.text[:220]}')
    return response.json().get('organic',[])

def normalize_keyword(q):
    return re.sub(r"\s+", "", str(q)).casefold()

def volume_value(raw):
    # 네이버 API의 '< 10'은 수치가 아닌 구간 값으로 보존
    if raw is None: return '미제공'
    if isinstance(raw, str) and '<' in raw: return '10 미만'
    try: return int(str(raw).replace(',', ''))
    except (ValueError, TypeError): return '미제공'

def volume_sum(pc, mobile):
    if isinstance(pc, int) and isinstance(mobile, int): return pc + mobile
    if pc == '미제공' or mobile == '미제공': return '미제공'
    if pc == '10 미만' and mobile == '10 미만': return '20 미만'
    if isinstance(pc, int) and mobile == '10 미만': return f'{pc}~{pc+9}'
    if isinstance(mobile, int) and pc == '10 미만': return f'{mobile}~{mobile+9}'
    return '미제공'

def get_naver_volume(keyword, access, secret, customer):
    path='/keywordstool'
    millis=str(int(time.time()*1000))
    message=f'{millis}.GET.{path}'.encode('utf-8')
    signature=base64.b64encode(hmac.new(secret.encode('utf-8'), message, hashlib.sha256).digest()).decode('ascii')
    resp=requests.get('https://api.searchad.naver.com'+path,
        params={'hintKeywords': normalize_keyword(keyword), 'showDetail': 1},
        headers={'X-Timestamp':millis,'X-API-KEY':access,'X-Customer':str(customer).strip(),'X-Signature':signature},
        timeout=25)
    if not resp.ok: raise RuntimeError(f'네이버 API HTTP {resp.status_code}: {resp.text[:180]}')
    payload=resp.json()
    target=normalize_keyword(keyword)
    for item in payload.get('keywordList', []):
        if normalize_keyword(item.get('relKeyword','')) == target:
            pc=volume_value(item.get('monthlyPcQcCnt'))
            mobile=volume_value(item.get('monthlyMobileQcCnt'))
            return {'월간 PC 검색량':pc,'월간 모바일 검색량':mobile,'월간 합계':volume_sum(pc,mobile),'네이버 조회 상태':'조회 완료'}
    return {'월간 PC 검색량':'미제공','월간 모바일 검색량':'미제공','월간 합계':'미제공','네이버 조회 상태':'정확히 일치하는 키워드 없음'}

def platform(link):
    host=(urlparse(link).hostname or '').lower()
    if host.endswith('.tistory.com') or host=='tistory.com':return '티스토리'
    if host.endswith('.wordpress.com') or host=='wordpress.com':return '워드프레스'
    return '미확인'

def detect_wordpress(link):
    """Heuristic only: absence of a signature does not rule out WordPress."""
    try:
        parsed=urlparse(link)
        if parsed.scheme not in ('http','https'): return False
        if parsed.hostname in ('localhost',) or not parsed.hostname: return False
        resp=requests.get(f'{parsed.scheme}://{parsed.netloc}/',headers=UA,timeout=4)
        content=resp.text[:180000].lower()
        return 'wp-content/' in content or 'wp-includes/' in content or 'name="generator" content="wordpress' in content
    except requests.RequestException: return False

def same_site(a,b):
    a=(urlparse(a).hostname or '').lower().removeprefix('www.')
    b=(urlparse(b).hostname or '').lower().removeprefix('www.')
    return a==b

with st.sidebar:
    st.subheader('🔑 API 설정')
    openai_key=st.text_input('OpenAI API 키',type='password',help='GPT가 제목을 분석해 검색어를 생성합니다.')
    serper_key=st.text_input('Serper 검색 API 키',type='password',help='실제 Google 검색 결과를 조회합니다. OpenAI 키와 별도입니다.')
    st.divider()
    st.subheader('🟢 네이버 검색광고 API')
    naver_access=st.text_input('Access License (X-API-KEY)',type='password')
    naver_secret=st.text_input('Secret Key',type='password')
    naver_customer=st.text_input('Customer ID',help='검색광고 계정의 고객 ID')
    st.caption('네이버 검색광고 API에서 발급합니다. 비밀키를 파일이나 엑셀에 기록하지 않습니다.')
    model=st.selectbox('키워드 추출 모델',MODELS,index=0,help='접근 권한은 OpenAI 계정마다 다릅니다.')
    st.caption('추천: gpt-5-nano(비용 우선) / gpt-5-mini(균형) / gpt-6.1-sol(복잡한 제목).')
    limit=st.slider('가져올 게시글 수',10,100,100,step=10)
    crawl_pages=st.slider('탐색할 최대 페이지 수',10,100,100,step=10)
    crawl_delay=st.select_slider('페이지 방문 간격',options=[0.2,0.5,1.0,2.0],value=0.5,format_func=lambda v:f'{v}초')
    st.caption('카테고리·페이지네이션·사이트맵을 탐색합니다. 사이트가 차단하거나 robots.txt에서 금지하면 수집하지 않습니다.')
    kw_max=st.slider('제목당 추천 키워드 수',1,15,6)
    analysis_limit=st.number_input('이번에 분석할 제목 수',min_value=1,max_value=100,value=5,step=1,help='소량으로 정상 작동을 확인한 후 숫자를 올리세요.')
    top_n=st.selectbox('검색 결과 확인 범위', [10,20,30,50,100],index=1)
    inspect_wp=st.checkbox('독립 도메인 워드프레스 추가 검사 (느릴 수 있음)',value=False)
    st.caption('검색 비용은 키워드 개수 × 검색 횟수 기준이며, 서비스별 정책에 따릅니다.')

source=st.text_input('티스토리·워드프레스 URL',placeholder='https://example.tistory.com/entry/... 또는 https://example.com')
manual=st.text_area('또는 제목 직접 입력 (한 줄에 제목 하나)',placeholder='실업급여 신청방법 및 조건 변경사항 총정리',height=85)

if 'articles' not in st.session_state: st.session_state.articles=[]
if 'kws' not in st.session_state:st.session_state.kws=[]
if 'serp' not in st.session_state:st.session_state.serp=[]
if 'volume' not in st.session_state: st.session_state.volume={}
if 'volume_errors' not in st.session_state: st.session_state.volume_errors=[]

if st.button('1단계 · 게시글 제목 불러오기',type='primary'):
    try:
        articles=[]
        if source.strip():
            with st.spinner('사이트맵·카테고리·다음 페이지를 순서대로 탐색하고 있어요...'):
                articles, stats=run_crawl(source,max_articles=limit,max_pages=crawl_pages,delay=crawl_delay)
            st.info(f"탐색 페이지 {stats['방문 페이지']}개 · 발견 게시글 {stats['발견 게시글']}개 · 접근 실패 {stats['접근 실패']}개")
            if stats['오류 예시']:
                with st.expander('일부 접근 실패 내역'):
                    st.write(stats['오류 예시'])
        for title in manual.splitlines():
            if title.strip():articles.append({'게시글 제목':title.strip(),'게시글 URL':source.strip()})
        articles=articles[:limit]
        if not articles: st.warning('제목을 찾지 못했습니다. 아래 직접 입력칸에 제목을 입력해 주세요.')
        else:
            st.session_state.articles=articles
            st.session_state.kws=[];st.session_state.serp=[];st.session_state.volume={}
            st.success(f'{len(articles)}개 제목을 불러왔습니다.')
    except Exception as exc: st.error(f'제목 수집 실패: {exc} / 제목을 직접 입력해 계속 진행할 수 있습니다.')

if st.session_state.articles:
    st.subheader('수집한 제목')
    st.dataframe(pd.DataFrame(st.session_state.articles),hide_index=True,use_container_width=True)
    if st.button('2단계 · GPT로 키워드 추출'):
        if not openai_key:
            st.error('OpenAI API 키를 입력하세요.')
        else:
            selected_articles=st.session_state.articles[:int(analysis_limit)]
            client=OpenAI(api_key=openai_key, timeout=35.0, max_retries=0)
            result=[]
            errors=[]
            progress=st.progress(0, text=f'0/{len(selected_articles)}개 제목 완료')
            status=st.empty()
            preview=st.empty()
            for ix, art in enumerate(selected_articles, start=1):
                title=art['게시글 제목']
                status.info(f'[{ix}/{len(selected_articles)}] 분석 중: {title[:70]}')
                try:
                    kws=extract_keywords(client,model,title,kw_max)
                    for kw in kws:
                        result.append({'검사':True,'게시글 제목':title,'게시글 URL':art['게시글 URL'],'키워드':kw})
                except Exception as exc:
                    errors.append(f'{ix}번째 제목 ({title[:35]}): {str(exc)[:350]}')
                progress.progress(ix/len(selected_articles),text=f'{ix}/{len(selected_articles)}개 제목 완료 · 추출 {len(result)}개 · 오류 {len(errors)}개')
                if result:
                    preview.dataframe(pd.DataFrame(result).tail(10),hide_index=True,use_container_width=True)
                st.session_state.kws=list(result)
            status.empty()
            st.session_state.serp=[]
            st.session_state.volume={}
            if result: st.success(f'{len(selected_articles)}개 제목에서 키워드 {len(result)}개 추출했습니다.')
            else: st.warning('추출된 키워드가 없습니다. 아래 오류 내용을 확인하세요.')
            if errors:
                st.error(f'{len(errors)}개 제목 분석 실패')
                with st.expander('자세한 오류 확인',expanded=True):
                    for message in errors: st.code(message)

if st.session_state.kws:
    st.subheader('추천 키워드 — 검사 여부를 수정할 수 있어요')
    edited=st.data_editor(pd.DataFrame(st.session_state.kws),hide_index=True,use_container_width=True,num_rows='dynamic',column_config={'검사':st.column_config.CheckboxColumn('검사'), '키워드':st.column_config.TextColumn('키워드',required=True)})
    st.markdown('### 3단계 · 네이버 월간 검색량 조회')
    st.caption('체크된 키워드만 조회합니다. 같은 키워드는 한 번만 호출하며, 정확히 일치하는 검색량만 사용합니다.')
    if st.button('3단계 · 네이버 검색량 조회'):
        if not all([naver_access,naver_secret,naver_customer]):
            st.error('네이버 검색광고 Access License, Secret Key, Customer ID를 모두 입력하세요.')
        else:
            targets=edited[(edited['검사']==True)&edited['키워드'].notna()]
            distinct=list(dict.fromkeys(q.strip() for q in targets['키워드'].astype(str) if q.strip()))
            if not distinct: st.warning('검색량을 조회할 키워드를 선택하세요.')
            else:
                volume=st.session_state.volume.copy()
                errors=[]
                progress=st.progress(0)
                for i,q in enumerate(distinct):
                    try:
                        volume[q]=get_naver_volume(q,naver_access,naver_secret,naver_customer)
                    except Exception as exc:
                        volume[q]={'월간 PC 검색량':'미제공','월간 모바일 검색량':'미제공','월간 합계':'미제공','네이버 조회 상태':'조회 실패'}
                        errors.append(f'{q}: {exc}')
                    progress.progress((i+1)/len(distinct))
                    if i < len(distinct)-1: time.sleep(0.6)
                st.session_state.volume=volume
                st.session_state.volume_errors=errors
                if errors: st.warning('일부 검색량 조회 오류: '+' / '.join(errors[:3]))
                st.success(f'{len(distinct)}개 키워드 검색량 조회를 마쳤습니다.')
    if st.session_state.volume:
        vol_rows=[{'키워드':q,**data} for q,data in st.session_state.volume.items()]
        st.dataframe(pd.DataFrame(vol_rows),hide_index=True,use_container_width=True)
    st.markdown('### 4단계 · 구글 노출 확인')
    if st.button('4단계 · Google 검색 결과 확인',type='primary'):
        if not serper_key:st.error('Serper 검색 API 키를 입력하세요.')
        else:
            targets=edited[(edited['검사']==True)&edited['키워드'].notna()]
            if len(targets)==0:st.warning('검사할 키워드를 선택하세요.')
            else:
                rows=[];detail=[];errors=[];bar=st.progress(0)
                # 같은 키워드를 여러 제목에서 쓴 경우 검색 1회로 재사용
                cache={}; distinct=list(dict.fromkeys(targets['키워드'].astype(str)))
                for i,q in enumerate(distinct):
                    try: cache[q]=search_serper(serper_key,q,top_n)
                    except Exception as exc: errors.append(f'{q}: {exc}');cache[q]=None
                    bar.progress((i+1)/len(distinct))
                wp_cache={}
                for _,r in targets.iterrows():
                    q=str(r['키워드']);organic=cache.get(q)
                    if organic is None:
                        rows.append({'제목':r['게시글 제목'],'키워드':q,**st.session_state.volume.get(q,{'월간 PC 검색량':'미조회','월간 모바일 검색량':'미조회','월간 합계':'미조회','네이버 조회 상태':'미조회'}),'결과':'조회 실패','티스토리':'미확인','워드프레스':'미확인','내 사이트':'미확인','내 사이트 위치':''});continue
                    ts=[];wp=[];owned=[];unknown=[]
                    for j,item in enumerate(organic):
                        link=item.get('link','');p=platform(link)
                        if inspect_wp and p=='미확인':
                            host=(urlparse(link).hostname or '').lower()
                            if host not in wp_cache: wp_cache[host]=detect_wordpress(link)
                            if wp_cache[host]:p='워드프레스 추정' 
                        try:position=int(item.get('position',j+1))
                        except (TypeError,ValueError):position=j+1
                        is_mine=bool(r['게시글 URL']) and same_site(r['게시글 URL'],link)
                        if p=='티스토리':ts.append(position)
                        elif p in ('워드프레스','워드프레스 추정'):wp.append(position)
                        else:unknown.append(position)
                        if is_mine:owned.append(position)
                        detail.append({'키워드':q,'순위(검색 응답)':position,'검색 제목':item.get('title',''),'검색 결과 URL':link,'플랫폼 (도메인 기준)':p,'내 사이트 도메인 일치':is_mine})
                    rows.append({'제목':r['게시글 제목'],'키워드':q,**st.session_state.volume.get(q,{'월간 PC 검색량':'미조회','월간 모바일 검색량':'미조회','월간 합계':'미조회','네이버 조회 상태':'미조회'}),'결과':'조회 완료','티스토리':f'발견 ({min(ts)}위)' if ts else '미발견','워드프레스':f'발견 ({min(wp)}위)' if wp else '미발견','내 사이트':'발견' if owned else '미발견','내 사이트 위치':min(owned) if owned else ''})
                st.session_state.serp=rows;st.session_state.details=detail
                if errors:st.warning('일부 검색에 실패했습니다: '+ ' / '.join(errors[:3]))
                st.success('검색 결과 조회가 종료되었습니다.')

if st.session_state.serp:
    st.subheader('검색 결과 요약 · 네이버 검색량 포함')
    st.dataframe(pd.DataFrame(st.session_state.serp),hide_index=True,use_container_width=True)
    st.caption('미발견 = 조회한 검색 결과 범위에서 찾지 못함. 구글 전체 미노출을 뜻하지 않습니다. 독립 도메인 워드프레스 추가 검사는 페이지 내 흔적으로 추정하므로 오탐·누락될 수 있습니다.')
    with st.expander('개별 검색 결과 URL 자세히 보기'):
        st.dataframe(pd.DataFrame(st.session_state.get('details',[])),hide_index=True,use_container_width=True)
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        pd.DataFrame(st.session_state.serp).to_excel(writer,index=False,sheet_name='키워드별 요약')
        pd.DataFrame(st.session_state.get('details',[])).to_excel(writer,index=False,sheet_name='구글 검색결과')
        pd.DataFrame([{'키워드':q,**v} for q,v in st.session_state.volume.items()]).to_excel(writer,index=False,sheet_name='네이버 검색량')
    st.download_button('📥 결과 엑셀 다운로드',data=out.getvalue(),file_name='keyword_google_serp.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

if st.session_state.volume and not st.session_state.serp:
    output=io.BytesIO()
    pd.DataFrame([{'키워드':q,**v} for q,v in st.session_state.volume.items()]).to_excel(output,index=False)
    st.download_button('📥 네이버 검색량만 엑셀로 저장',output.getvalue(),file_name='naver_keyword_volume.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

st.divider()
st.caption('네이버 검색량은 구글 검색량이 아니며, 월간 추정치입니다. 10 미만 응답은 0으로 바꾸지 않습니다.')
st.caption('주의: 검색 결과는 지역·시점·개인화·검색 API에 따라 다릅니다. 이것은 검색 상위 결과 점검이며 Search Console의 실제 노출수/클릭수와 다릅니다. API 키는 현재 앱 세션에서만 사용하며 코드에 저장하지 않습니다.')
