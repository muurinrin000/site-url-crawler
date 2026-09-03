import xml.etree.ElementTree as ET
import signal
import time
#!/usr/bin/env python3
import argparse, asyncio, gzip, json, os, re, sqlite3, time
from copy import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
import aiohttp, pandas as pd
from bs4 import BeautifulSoup
from defusedxml import ElementTree as DET

SKIP_EXTENSIONS={'.jpg','.jpeg','.png','.gif','.webp','.svg','.ico','.pdf','.zip','.rar','.7z','.mp3','.wav','.m4a','.mp4','.mov','.avi','.wmv','.css','.js','.json','.txt','.woff','.woff2','.ttf','.eot','.doc','.docx','.xls','.xlsx','.ppt','.pptx'}
TRACKING_PARAMS={'utm_source','utm_medium','utm_campaign','utm_term','utm_content','gclid','fbclid','yclid','msclkid'}
COMMON_SITEMAPS=('/sitemap.xml','/sitemap_index.xml','/sitemap-index.xml','/sitemap/sitemap.xml')
COMMON_HTML_SITEMAPS=('/sitemap/index.html','/sitemap.html','/sitemap/')

CANCEL_REQUESTED = False

def _request_cancel(signum, frame):
    global CANCEL_REQUESTED
    if not CANCEL_REQUESTED:
        CANCEL_REQUESTED=True
        print("\n[CANCEL] 終了要求を受信しました。新しいURL取得を停止し、再開用データを保存します。",flush=True)

def install_signal_handlers():
    for name in ("SIGTERM","SIGINT"):
        sig=getattr(signal,name,None)
        if sig is not None:
            signal.signal(sig,_request_cancel)

def cancellation_requested():
    return CANCEL_REQUESTED

def now_iso(): return datetime.now(timezone.utc).isoformat()
def host_key(url): return re.sub(r'[^a-z0-9._-]+','_',urlparse(url).netloc.lower())
def normalize_url(url,keep_query=False):
    try:
        p=urlparse(url.strip())
        if p.scheme not in ('http','https') or not p.netloc:return None
        q=''
        if keep_query:
            pairs=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
            q=urlencode(pairs,doseq=True)
        scheme='https' if p.scheme.lower() in ('http','https') else p.scheme.lower()
        path=re.sub(r'/+','/',p.path or '/')
        # Keep trailing-slash differences as-is because /page and /page/
        # can be different resources on some servers.
        return urlunparse((scheme,p.netloc.lower(),path,'',q,''))
    except:return None
def same_site(url,host,subs=False):
    h=urlparse(url).netloc.lower().split(':')[0]; return h==host or (subs and h.endswith('.'+host))
def skipped_extension(url): return any(urlparse(url).path.lower().endswith(x) for x in SKIP_EXTENSIONS)
def directories(url):
    p=[x for x in urlparse(url).path.split('/') if x]
    a='/'+p[0]+'/' if p else '/'; b='/'+('/'.join(p[:2]))+'/' if len(p)>=2 else a
    return a,b

def init_db(path):
    path.parent.mkdir(parents=True,exist_ok=True); con=sqlite3.connect(path)
    con.execute('''CREATE TABLE IF NOT EXISTS urls(
    url TEXT PRIMARY KEY, discovered_via TEXT, discovered_from TEXT,
    first_directory TEXT, second_directory TEXT, depth INTEGER DEFAULT 0,
    detail_status TEXT DEFAULT 'not_checked', title TEXT DEFAULT '', h1 TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0, canonical TEXT DEFAULT '', content_type TEXT DEFAULT '',
    fetched_at TEXT DEFAULT '', error TEXT DEFAULT '')''')
    con.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
    con.execute('''CREATE TABLE IF NOT EXISTS html_queue(
        url TEXT PRIMARY KEY,
        depth INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending'
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS title_queue(
        url TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'pending'
    )''')
    con.commit(); return con
def meta_int(con,key):
    r=con.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone(); return int(r[0]) if r else 0
def meta_add(con,key,n):
    con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(key,str(meta_int(con,key)+n))); con.commit()
def add_many(con,rows):
    added=dupes=0
    for url,via,source,depth in rows:
        a,b=directories(url)
        cur=con.execute('INSERT OR IGNORE INTO urls(url,discovered_via,discovered_from,first_directory,second_directory,depth) VALUES(?,?,?,?,?,?)',(url,via,source,a,b,depth))
        if cur.rowcount==1:added+=1
        else:dupes+=1
    if dupes: con.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',('duplicate_count',str(meta_int(con,'duplicate_count')+dupes)))
    con.commit(); return added,dupes
def unique_count(con):return con.execute('SELECT COUNT(*) FROM urls').fetchone()[0]
def via_count(con,via):return con.execute('SELECT COUNT(*) FROM urls WHERE discovered_via=?',(via,)).fetchone()[0]

async def fetch(session,url,timeout):
    async with session.get(url,timeout=timeout,allow_redirects=True) as r:return r,await r.read()
async def get_robots(session,start,timeout):
    p=urlparse(start); u=f'{p.scheme}://{p.netloc}/robots.txt'; rp=RobotFileParser();rp.set_url(u);text='';loaded=False
    try:
        r,b=await fetch(session,u,timeout)
        if r.status<400:text=b.decode(r.charset or 'utf-8',errors='replace');rp.parse(text.splitlines());loaded=True
    except:pass
    return rp,u,loaded,text
def robots_sitemaps(text):
    out=[]
    for line in text.splitlines():
        if ':' in line:
            k,v=line.split(':',1)
            if k.strip().lower()=='sitemap' and v.strip():out.append(v.strip())
    return out
def parse_xml(data,url):
    if url.lower().endswith('.gz') or data[:2]==b'\x1f\x8b':data=gzip.decompress(data)
    root=DET.fromstring(data);kind=root.tag.rsplit('}',1)[-1].lower()
    return kind,[e.text.strip() for e in root.iter() if e.tag.rsplit('}',1)[-1].lower()=='loc' and e.text]
async def discover_xml(session,start,robots,timeout,host,subs,keep,max_files):
    """
    Exhaustive sitemap discovery:
    - robots.txt Sitemap entries
    - common sitemap entry points
    - recursively follows every sitemap index / nested sitemap
    - records failed sitemap fetches
    - deduplicates sitemap files and page URLs
    """
    from collections import deque

    base=f"{urlparse(target).scheme or 'https'}://{urlparse(target).netloc}"
    candidates=[]

    # Sitemap declarations in robots.txt.
    for line in (robots_text or "").splitlines():
        if line.lower().startswith("sitemap:"):
            u=line.split(":",1)[1].strip()
            if u:
                candidates.append(u)

    # Common sitemap locations.
    for path in (
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
        "/sitemap/sitemap.xml",
        "/sitemap/index.xml",
        "/wp-sitemap.xml",
    ):
        candidates.append(urljoin(base,path))

    q=deque()
    queued=set()
    visited=set()
    failed=[]
    page_sources={}
    sitemap_files=[]

    def enqueue(u):
        if not u:
            return
        u=urljoin(base,u.strip())
        pu=urlparse(u)
        if pu.scheme not in ("http","https"):
            return
        # Sitemap files must remain on the target host unless subdomains are allowed.
        if not same_host(u,host,subs):
            return
        if u not in queued and u not in visited:
            queued.add(u)
            q.append(u)

    for u in candidates:
        enqueue(u)

    print(f"[XML] sitemap entry candidates={len(q):,}",flush=True)

    while q:
        if cancellation_requested():
            print("[CANCEL] XML探索の新規取得を停止します。",flush=True)
            break

        sm_url=q.popleft()
        queued.discard(sm_url)
        if sm_url in visited:
            continue
        visited.add(sm_url)

        # max_files is a safety ceiling, not a normal stopping point.
        if len(visited)>max_files:
            print(f"[XML][WARN] sitemapファイル上限 {max_files:,} に到達しました。未確認={len(q):,}",flush=True)
            break

        try:
            await lim.wait() if 'lim' in locals() else asyncio.sleep(0)
        except Exception:
            pass

        try:
            async with session.get(sm_url,timeout=timeout,allow_redirects=True) as r:
                status=r.status
                raw=await r.read()
                final_url=str(r.url)
                ct=(r.headers.get("Content-Type") or "").lower()
        except Exception as e:
            failed.append((sm_url,str(e)))
            print(f"[XML][FAIL] {sm_url} :: {e}",flush=True)
            continue

        if status>=400:
            failed.append((sm_url,f"HTTP {status}"))
            print(f"[XML][FAIL] {sm_url} :: HTTP {status}",flush=True)
            continue

        # gzip sitemap support.
        if sm_url.lower().endswith(".gz") or "gzip" in ct:
            try:
                import gzip
                raw=gzip.decompress(raw)
            except Exception as e:
                failed.append((sm_url,f"gzip: {e}"))
                print(f"[XML][FAIL] {sm_url} :: gzip {e}",flush=True)
                continue

        try:
            text=raw.decode("utf-8-sig",errors="replace")
            root=ET.fromstring(text)
        except Exception as e:
            failed.append((sm_url,f"XML parse: {e}"))
            print(f"[XML][FAIL] {sm_url} :: XML parse {e}",flush=True)
            continue

        sitemap_files.append(final_url)
        tag=root.tag.lower()

        # Namespace-agnostic loc extraction.
        locs=[]
        for elem in root.iter():
            if elem.tag.lower().endswith("loc") and elem.text:
                locs.append(elem.text.strip())

        if tag.endswith("sitemapindex"):
            before=len(q)
            for child in locs:
                enqueue(child)
            print(f"[XML][INDEX] {sm_url} -> child={len(locs):,} / queue={len(q):,}",flush=True)
        elif tag.endswith("urlset"):
            added=0
            for page in locs:
                nu=normalize_url(page,keep_query=keep)
                if nu and same_host(nu,host,subs):
                    if nu not in page_sources:
                        added+=1
                    page_sources.setdefault(nu,sm_url)
            print(f"[XML][URLSET] {sm_url} -> URLs={len(locs):,} / new={added:,} / total={len(page_sources):,}",flush=True)
        else:
            # Some sites return valid XML with a non-standard wrapper.
            child_xml=[u for u in locs if re.search(r"(?i)(sitemap|\.xml(?:\.gz)?(?:$|\?))",u)]
            page_like=[u for u in locs if u not in child_xml]
            for child in child_xml:
                enqueue(child)
            for page in page_like:
                nu=normalize_url(page,keep_query=keep)
                if nu and same_host(nu,host,subs):
                    page_sources.setdefault(nu,sm_url)
            print(f"[XML][OTHER] {sm_url} -> childXML={len(child_xml):,} / URLs={len(page_like):,}",flush=True)

    print(f"[XML] sitemap files checked={len(visited):,} success={len(sitemap_files):,} failed={len(failed):,}",flush=True)
    print(f"[XML] unique page URLs={len(page_sources):,}",flush=True)
    if q:
        print(f"[XML][WARN] 未確認sitemap={len(q):,}",flush=True)
    if failed:
        print("[XML][WARN] 取得失敗したsitemapがあります。ログの [XML][FAIL] を確認してください。",flush=True)

    return sitemap_files,page_sources
def extract_links(html,base,host,subs,keep):
    soup=BeautifulSoup(html,'html.parser');out=[]
    for a in soup.find_all('a',href=True):
        h=a['href'].strip()
        if not h or h.startswith(('#','javascript:','mailto:','tel:','data:')):continue
        n=normalize_url(urljoin(base,h),keep)
        if n and same_site(n,host,subs) and not skipped_extension(n):out.append(n)
    return list(dict.fromkeys(out))
def extract_title(html):
    s=BeautifulSoup(html,'html.parser')
    return ' '.join(s.title.get_text(' ',strip=True).split()) if s.title else ''

def extract_detail(html,base):
    s=BeautifulSoup(html,'html.parser');title=' '.join(s.title.get_text(' ',strip=True).split()) if s.title else ''
    h=s.find('h1');h1=' '.join(h.get_text(' ',strip=True).split()) if h else ''
    c=s.find('link',rel=lambda v:v and 'canonical' in str(v).lower());can=urljoin(base,c.get('href')) if c and c.get('href') else ''
    return title,h1,can

class RateLimiter:
    def __init__(self,rps):self.interval=1/max(rps,.1);self.lock=asyncio.Lock();self.next=0;self.penalty=0
    async def wait(self):
        async with self.lock:
            now=time.monotonic();target=max(self.next,self.penalty)
            if target>now:await asyncio.sleep(target-now)
            self.next=time.monotonic()+self.interval
    async def penalize(self,s):
        async with self.lock:self.penalty=max(self.penalty,time.monotonic()+s)
async def get_html(session,url,sem,limiter,timeout,rp,robots_loaded,ua):
    if cancellation_requested(): return None,None,'','cancelled','cancel requested'
    if robots_loaded and not rp.can_fetch(ua,url):return None,0,'','blocked','robots.txt'
    async with sem:
        await limiter.wait()
        try:
            r,b=await fetch(session,url,timeout)
            if r.status==429:await limiter.penalize(15)
            elif r.status in (503,504):await limiter.penalize(5)
            ct=(r.headers.get('Content-Type') or '').lower()
            if 'text/html' not in ct and 'application/xhtml+xml' not in ct:return None,r.status,ct,'nonhtml',''
            return b.decode(r.charset or 'utf-8',errors='replace'),r.status,ct,'ok',''
        except Exception as e:return None,0,'','error',f'{type(e).__name__}: {e}'

def bar(p,w=10):
    n=max(0,min(w,round(w*p/100)));return '█'*n+'░'*(w-n)

def format_duration(seconds):
    if seconds is None or seconds < 0:
        return "計算中"
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}時間{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"

def eta_clock(seconds):
    if seconds is None or seconds < 0:
        return "計算中"
    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    return (now_jst + timedelta(seconds=seconds)).strftime("%H:%M頃")

def print_summary(con,phase='',processed=None,remaining=None,started_at=None):
    x=via_count(con,'XML');h=via_count(con,'HTML');u=unique_count(con);d=meta_int(con,'duplicate_count')
    checked=con.execute("SELECT COUNT(*) FROM urls WHERE detail_status!='not_checked'").fetchone()[0]
    print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)
    if phase:print(f' {phase}',flush=True)
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)
    print(f'XMLから発見        {x:>8,} URL',flush=True);print(f'HTMLから追加       {h:>8,} URL',flush=True)
    print(f'同一URLを再発見    {d:>8,} 回',flush=True);print('────────────────────────────────',flush=True)
    print(f'現在のユニークURL  {u:>8,} URL',flush=True)
    if phase=='DETAIL':
        pct=checked/max(u,1)*100
        print(f'詳細取得済み        {checked:>8,} URL',flush=True)
        print(f'進捗  {bar(pct)}  {pct:5.1f}%',flush=True)
    elif phase=='XML TITLE' and processed is not None:
        rem=max(0,remaining or 0)
        pct=processed/max(processed+rem,1)*100
        print(f'今回タイトル取得    {processed:>8,} ページ',flush=True)
        print(f'タイトル取得待ち    {rem:>8,} ページ',flush=True)
        print(f'進捗  {bar(pct)}  {pct:5.1f}%',flush=True)
        if started_at is not None:
            elapsed=max(time.monotonic()-started_at,0.001)
            speed=processed/elapsed if processed>0 else 0.0
            eta=(rem/speed) if speed>0 else None
            print(f'経過時間            {format_duration(elapsed)}',flush=True)
            print(f'処理速度            {speed:.2f} ページ/秒',flush=True)
            print(f'残り時間の目安      約{format_duration(eta)}' if eta is not None else '残り時間の目安      計算中',flush=True)
            print(f'完了予想            {eta_clock(eta)}' if eta is not None else '完了予想            計算中',flush=True)
    elif processed is not None:
        # URL_ONLY/AUTOのHTML探索は巡回中に新URLが増えるため、
        # 現時点の処理済み＋待機中を分母とした動的な進捗率。
        rem=max(0,remaining or 0)
        pct=processed/max(processed+rem,1)*100
        print(f'今回HTML確認        {processed:>8,} ページ',flush=True)
        print(f'HTML確認待ち        {rem:>8,} ページ',flush=True)
        print(f'進捗  {bar(pct)}  {pct:5.1f}%',flush=True)
        if started_at is not None:
            elapsed=max(time.monotonic()-started_at,0.001)
            speed=processed/elapsed if processed>0 else 0.0
            eta=(rem/speed) if speed>0 else None
            print(f'経過時間            {format_duration(elapsed)}',flush=True)
            print(f'処理速度            {speed:.2f} ページ/秒',flush=True)
            print(f'残り時間の目安      約{format_duration(eta)}' if eta is not None else '残り時間の目安      計算中',flush=True)
            print(f'完了予想            {eta_clock(eta)}' if eta is not None else '完了予想            計算中',flush=True)
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)


def write_completion_marker(host, collection_mode, complete, pending=0):
    marker_dir=Path("run_meta")
    marker_dir.mkdir(parents=True,exist_ok=True)
    payload={
        "host":host,
        "collection_mode":collection_mode,
        "complete":bool(complete),
        "pending":int(pending or 0),
        "finished_at":now_iso(),
    }
    (marker_dir/"completion.json").write_text(
        json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"
    )


def export_outputs(con,outdir,target,mode):
    outdir.mkdir(parents=True,exist_ok=True);key=host_key(target);df=pd.read_sql_query('SELECT * FROM urls ORDER BY rowid',con)
    if mode=='URL_ONLY':
        out=df[['url','title','discovered_via','discovered_from','first_directory','second_directory']].copy()
        out.columns=['URL','ページタイトル','発見経路','発見元URL','第1階層','第2階層']
    else:
        out=df[['url','discovered_via','discovered_from','first_directory','second_directory','title','h1','status_code','canonical','content_type','detail_status','fetched_at','error']].copy()
        out.columns=['URL','発見経路','発見元URL','第1階層','第2階層','Title','H1','Status Code','Canonical','Content Type','詳細取得状態','取得日時','エラー']
    csv=outdir/f'{key}_{mode.lower()}.csv';xlsx=outdir/f'{key}_{mode.lower()}.xlsx';out.to_csv(csv,index=False,encoding='utf-8-sig')
    summary=pd.DataFrame([['対象サイト',target],['出力モード',mode],['XMLから発見',via_count(con,'XML')],['HTMLから追加',via_count(con,'HTML')],['同一URLを再発見',meta_int(con,'duplicate_count')],['ユニークURL',unique_count(con)],['出力日時(UTC)',now_iso()]],columns=['項目','値'])
    dirs=out.groupby('第1階層',dropna=False).size().reset_index(name='URL数').sort_values('URL数',ascending=False)
    with pd.ExcelWriter(xlsx,engine='openpyxl') as w:
        out.to_excel(w,index=False,sheet_name='URL一覧');summary.to_excel(w,index=False,sheet_name='集計');dirs.to_excel(w,index=False,sheet_name='第1階層集計')
        for ws in w.book.worksheets:
            ws.freeze_panes='A2'
            for cell in ws[1]:f=copy(cell.font);f.bold=True;cell.font=f
            for col in ws.columns:
                m=min(max((len(str(c.value)) if c.value is not None else 0) for c in col),80);ws.column_dimensions[col[0].column_letter].width=max(10,m+2)
    return xlsx,csv

async def html_discovery(session,con,seeds,host,subs,keep,timeout,rp,robots_loaded,ua,conc,rps,max_pages,max_depth):
    sem=asyncio.Semaphore(conc);lim=RateLimiter(rps)
    for u,d in seeds:
        con.execute("INSERT OR IGNORE INTO html_queue(url,depth,state) VALUES(?,?,'pending')",(u,d))
    con.commit()
    processed=0
    started_at=time.monotonic()
    while processed<max_pages:
        if cancellation_requested():
            print('[CANCEL] HTML巡回の新規処理を停止します。',flush=True)
            break
        rows=con.execute(
            "SELECT url,depth FROM html_queue WHERE state='pending' ORDER BY rowid LIMIT ?",
            (min(conc*4,max_pages-processed),)
        ).fetchall()
        if not rows: break
        res=await asyncio.gather(*[
            get_html(session,u,sem,lim,timeout,rp,robots_loaded,ua) for u,d in rows
        ])
        for (u,d),(html,code,ct,state,err) in zip(rows,res):
            if state=='cancelled':
                continue
            con.execute("UPDATE html_queue SET state='done' WHERE url=?",(u,))
            processed+=1
            if html is not None:
                con.execute(
                    'UPDATE urls SET title=?,status_code=?,content_type=?,fetched_at=? WHERE url=?',
                    (extract_title(html),code,ct,now_iso(),u)
                )
                if d<max_depth:
                    discovered=[]
                    for n in extract_links(html,u,host,subs,keep):
                        discovered.append((n,'HTML',u,d+1))
                        con.execute(
                            "INSERT OR IGNORE INTO html_queue(url,depth,state) VALUES(?,?,'pending')",
                            (n,d+1)
                        )
                    add_many(con,discovered)
            con.commit()
        pending=con.execute("SELECT COUNT(*) FROM html_queue WHERE state='pending'").fetchone()[0]
        if processed%100<len(rows):
            print_summary(con,'URL COLLECTION',processed,pending,started_at)
    pending=con.execute("SELECT COUNT(*) FROM html_queue WHERE state='pending'").fetchone()[0]
    print_summary(con,'URL COLLECTION',processed,pending,started_at)
    if pending:
        print(f'今回の巡回上限に到達。残り {pending:,} ページは Fresh start OFF で続きから再開できます。',flush=True)
    else:
        print('HTML巡回は完了しました。',flush=True)
    return processed


async def title_fetch(session,con,urls,timeout,rp,robots_loaded,ua,conc,rps,max_pages):
    sem=asyncio.Semaphore(conc);lim=RateLimiter(rps)

    # XMLで見つかったURLだけをタイトル取得対象にする。
    for u in urls:
        con.execute("INSERT OR IGNORE INTO title_queue(url,state) VALUES(?,'pending')",(u,))
    con.commit()

    processed=0
    started_at=time.monotonic()

    while processed<max_pages:

        if cancellation_requested():

            print('[CANCEL] タイトル取得の新規処理を停止します。',flush=True)

            break
        rows=con.execute(
            "SELECT url FROM title_queue WHERE state='pending' ORDER BY rowid LIMIT ?",
            (min(conc*4,max_pages-processed),)
        ).fetchall()
        if not rows:
            break

        page_urls=[r[0] for r in rows]
        res=await asyncio.gather(*[
            get_html(session,u,sem,lim,timeout,rp,robots_loaded,ua) for u in page_urls
        ])

        for u,(html,code,ct,state,err) in zip(page_urls,res):
            if state=='cancelled':
                continue
            con.execute("UPDATE title_queue SET state='done' WHERE url=?",(u,))
            processed+=1
            if html is not None:
                con.execute(
                    "UPDATE urls SET title=?,status_code=?,content_type=?,fetched_at=?,error=? WHERE url=?",
                    (extract_title(html),code,ct,now_iso(),err,u)
                )
            else:
                con.execute(
                    "UPDATE urls SET status_code=?,content_type=?,fetched_at=?,error=? WHERE url=?",
                    (code,ct,now_iso(),err,u)
                )
            con.commit()

        pending=con.execute("SELECT COUNT(*) FROM title_queue WHERE state='pending'").fetchone()[0]
        if processed%100<len(rows):
            print_summary(con,'XML TITLE',processed,pending,started_at)

    pending=con.execute("SELECT COUNT(*) FROM title_queue WHERE state='pending'").fetchone()[0]
    print_summary(con,'XML TITLE',processed,pending,started_at)
    if pending:
        print(f'今回のタイトル取得上限に到達。残り {pending:,} ページは Fresh start OFF で続きから再開できます。',flush=True)
    else:
        print('XML掲載URLのタイトル取得は完了しました。',flush=True)
    return processed


async def detail_fetch(session,con,timeout,rp,robots_loaded,ua,conc,rps,max_pages):
    sem=asyncio.Semaphore(conc);lim=RateLimiter(rps);processed=0
    while processed<max_pages:
        rows=con.execute("SELECT url FROM urls WHERE detail_status='not_checked' ORDER BY rowid LIMIT ?",(min(conc*4,max_pages-processed),)).fetchall()
        if not rows:break
        urls=[r[0] for r in rows];res=await asyncio.gather(*[get_html(session,u,sem,lim,timeout,rp,robots_loaded,ua) for u in urls])
        for u,(html,code,ct,state,err) in zip(urls,res):
            title=h1=can=''
            if html is not None:title,h1,can=extract_detail(html,u);state='done'
            con.execute('UPDATE urls SET detail_status=?,title=?,h1=?,status_code=?,canonical=?,content_type=?,fetched_at=?,error=? WHERE url=?',(state,title,h1,code,can,ct,now_iso(),err,u));processed+=1
        con.commit()
        if processed%100<len(urls):print_summary(con,'DETAIL')
    return processed

async def run(cfg,fresh=False):
    install_signal_handlers()
    keep=bool(cfg.get('keep_query',False));target=normalize_url(os.environ.get('TARGET_URL') or cfg['target_url'],keep)
    collection=(os.environ.get('COLLECTION_MODE') or 'AUTO').upper();mode=(os.environ.get('OUTPUT_MODE') or 'URL_ONLY').upper()
    if collection not in ('AUTO','XML_ONLY','XML_TITLE','HTML_CRAWL') or mode not in ('URL_ONLY','DETAIL'):raise SystemExit('Mode が不正です')
    timeout=int(cfg.get('timeout_seconds',25));max_files=int(cfg.get('max_sitemap_files',1000));subs=bool(cfg.get('include_subdomains',False))
    respect=bool(cfg.get('respect_robots_txt',True));max_depth=int(cfg.get('max_depth',50));max_pages=int(cfg.get('max_html_pages_per_run',50000))
    conc=max(1,int(os.environ.get('CONCURRENCY') or cfg.get('concurrency',6)));rps=max(0.1,float(os.environ.get('REQUESTS_PER_SECOND') or cfg.get('requests_per_second',1.5)));ua=cfg.get('user_agent','SiteURLCollector/3.13 (+GitHub Actions)')
    state=Path(cfg.get('state_dir','state'));out=Path(cfg.get('output_dir','output'));host=urlparse(target).netloc.lower().split(':')[0];db=state/f'{host_key(target)}.sqlite3'
    if fresh:
        for fp in (db, Path(str(db)+'-wal'), Path(str(db)+'-shm')):
            if fp.exists():
                fp.unlink()
    con=init_db(db);add_many(con,[(target,'HTML','START',0)])
    headers={'User-Agent':ua,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    async with aiohttp.ClientSession(headers=headers,connector=aiohttp.TCPConnector(limit=max(conc*2,8)),timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        rp,robots_url,robots_loaded,robots_text=await get_robots(session,target,timeout)
        if not respect:robots_loaded=False
        print(f'Target URL      : {target}',flush=True);print(f'Collection mode : {collection}',flush=True);print(f'Output mode     : {mode}',flush=True);print(f'Concurrency     : {conc}',flush=True);print(f'Requests/sec    : {rps}',flush=True)
        print(f'Page/run limit  : {max_pages:,} pages',flush=True)
        xml_sources={}
        if collection in ('AUTO','XML_ONLY','XML_TITLE'):
            files,sources=await discover_xml(session,target,robots_text,timeout,host,subs,keep,max_files)
            xml_sources=sources
            # IMPORTANT: XML URLs are always written to the cumulative URL table first.
            # The per-run HTML limit applies only to HTML fetches, never to exported URL count.
            add_many(con,[(u,'XML',sm,0) for u,sm in sources.items()])
            con.commit()
            print(f'[XML] sitemap_files={len(files):,} discovered={len(sources):,}',flush=True)
            print(f'[XML] cumulative_unique={unique_count(con):,}',flush=True)
        if collection=='XML_TITLE':
            print('STEP2: XML掲載URLだけのページタイトルを取得します。内部リンク探索は行いません。',flush=True)
            await title_fetch(
                session,con,list(xml_sources.keys()),timeout,rp,robots_loaded,ua,
                conc,rps,max_pages
            )
        if collection in ('AUTO','HTML_CRAWL'):
            base=f'{urlparse(target).scheme}://{urlparse(target).netloc}';seeds=[(target,0)]
            seeds += [(normalize_url(urljoin(base,p),keep),0) for p in COMMON_HTML_SITEMAPS if normalize_url(urljoin(base,p),keep)]
            if collection=='AUTO':
                # Queue every URL currently known to the cumulative corpus.
                # Existing html_queue rows are INSERT OR IGNORE, so already-finished pages stay finished.
                seeds += [(r[0],0) for r in con.execute("SELECT url FROM urls").fetchall()]
            await html_discovery(session,con,seeds,host,subs,keep,timeout,rp,robots_loaded,ua,conc,rps,max_pages,max_depth)
        print_summary(con,'URL COLLECTION',unique_count(con),0)
        if mode=='DETAIL':
            print('STEP2: 保存済みURLの詳細情報を取得します。',flush=True)
            await detail_fetch(session,con,timeout,rp,robots_loaded,ua,conc,rps,max_pages);print_summary(con,'DETAIL')
    # Keep state only while resumable work remains.
    if collection=='XML_ONLY':
        pending_work=0
        crawl_complete=True
    elif collection=='XML_TITLE':
        pending_work=con.execute("SELECT COUNT(*) FROM title_queue WHERE state='pending'").fetchone()[0]
        crawl_complete=(pending_work==0)
    else:
        pending_work=con.execute("SELECT COUNT(*) FROM html_queue WHERE state='pending'").fetchone()[0]
        crawl_complete=(pending_work==0)

    if cancellation_requested():
        crawl_complete=False
        print('[CANCEL] 再開用stateを保持します。',flush=True)
    write_completion_marker(host,collection,crawl_complete,pending_work)
    print(f'[STATE] complete={crawl_complete} pending={pending_work:,}',flush=True)
    print(f'[EXPORT] cumulative unique URLs = {unique_count(con):,}',flush=True)
    xlsx,csv=export_outputs(con,out,target,mode);print(f'Excel: {xlsx}',flush=True);print(f'CSV  : {csv}',flush=True);con.close()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',default='config.json');ap.add_argument('--fresh',action='store_true');a=ap.parse_args()
    asyncio.run(run(json.loads(Path(a.config).read_text(encoding='utf-8')),a.fresh))
if __name__=='__main__':main()
