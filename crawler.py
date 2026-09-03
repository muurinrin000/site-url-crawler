#!/usr/bin/env python3
import argparse, asyncio, gzip, json, os, re, sqlite3, time
from copy import copy
from datetime import datetime, timezone
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
        return urlunparse((p.scheme.lower(),p.netloc.lower(),re.sub(r'/+','/',p.path or '/'),'',q,''))
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
    base=f'{urlparse(start).scheme}://{urlparse(start).netloc}';q=[]
    for u in robots_sitemaps(robots)+[urljoin(base,p) for p in COMMON_SITEMAPS]:
        n=normalize_url(u,True)
        if n and n not in q:q.append(n)
    seen=set();files=[];sources={}
    while q and len(seen)<max_files:
        sm=q.pop(0)
        if sm in seen:continue
        seen.add(sm)
        try:
            r,b=await fetch(session,sm,timeout)
            if r.status>=400:continue
            kind,locs=parse_xml(b,sm)
        except:continue
        files.append(sm)
        if kind=='sitemapindex':
            for loc in locs:
                n=normalize_url(loc,True)
                if n and n not in seen:q.append(n)
        elif kind=='urlset':
            for loc in locs:
                n=normalize_url(loc,keep)
                if n and same_site(n,host,subs) and not skipped_extension(n):sources.setdefault(n,sm)
    return files,sources

def extract_links(html,base,host,subs,keep):
    soup=BeautifulSoup(html,'html.parser');out=[]
    for a in soup.find_all('a',href=True):
        h=a['href'].strip()
        if not h or h.startswith(('#','javascript:','mailto:','tel:','data:')):continue
        n=normalize_url(urljoin(base,h),keep)
        if n and same_site(n,host,subs) and not skipped_extension(n):out.append(n)
    return list(dict.fromkeys(out))
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
def print_summary(con,phase='',processed=None,remaining=None):
    x=via_count(con,'XML');h=via_count(con,'HTML');u=unique_count(con);d=meta_int(con,'duplicate_count')
    checked=con.execute("SELECT COUNT(*) FROM urls WHERE detail_status!='not_checked'").fetchone()[0]
    print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)
    if phase:print(f' {phase}',flush=True)
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)
    print(f'XMLから発見        {x:>8,} URL',flush=True);print(f'HTMLから追加       {h:>8,} URL',flush=True)
    print(f'重複除外           {d:>8,} URL',flush=True);print('────────────────────────────────',flush=True)
    print(f'現在のユニークURL  {u:>8,} URL',flush=True)
    if phase=='DETAIL':
        pct=checked/max(u,1)*100
        print(f'詳細取得済み        {checked:>8,} URL',flush=True)
        print(f'進捗  {bar(pct)}  {pct:5.1f}%',flush=True)
    elif processed is not None:
        # URL_ONLY/AUTOのHTML探索は巡回中に新URLが増えるため、
        # 現時点の処理済み＋待機中を分母とした動的な進捗率。
        rem=max(0,remaining or 0)
        pct=processed/max(processed+rem,1)*100
        print(f'今回HTML確認        {processed:>8,} ページ',flush=True)
        print(f'HTML確認待ち        {rem:>8,} ページ',flush=True)
        print(f'進捗  {bar(pct)}  {pct:5.1f}%',flush=True)
    print('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━',flush=True)

def export_outputs(con,outdir,target,mode):
    outdir.mkdir(parents=True,exist_ok=True);key=host_key(target);df=pd.read_sql_query('SELECT * FROM urls ORDER BY rowid',con)
    if mode=='URL_ONLY':
        out=df[['url','discovered_via','discovered_from','first_directory','second_directory']].copy()
        out.columns=['URL','発見経路','発見元URL','第1階層','第2階層']
    else:
        out=df[['url','discovered_via','discovered_from','first_directory','second_directory','title','h1','status_code','canonical','content_type','detail_status','fetched_at','error']].copy()
        out.columns=['URL','発見経路','発見元URL','第1階層','第2階層','Title','H1','Status Code','Canonical','Content Type','詳細取得状態','取得日時','エラー']
    csv=outdir/f'{key}_{mode.lower()}.csv';xlsx=outdir/f'{key}_{mode.lower()}.xlsx';out.to_csv(csv,index=False,encoding='utf-8-sig')
    summary=pd.DataFrame([['対象サイト',target],['出力モード',mode],['XMLから発見',via_count(con,'XML')],['HTMLから追加',via_count(con,'HTML')],['重複除外',meta_int(con,'duplicate_count')],['ユニークURL',unique_count(con)],['出力日時(UTC)',now_iso()]],columns=['項目','値'])
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
    sem=asyncio.Semaphore(conc);lim=RateLimiter(rps);q=[];seen=set()
    for u,d in seeds:
        if u not in seen:q.append((u,d));seen.add(u)
    processed=0
    while q and processed<max_pages:
        batch=q[:conc*4];q=q[len(batch):]
        res=await asyncio.gather(*[get_html(session,u,sem,lim,timeout,rp,robots_loaded,ua) for u,d in batch])
        for (u,d),(html,code,ct,state,err) in zip(batch,res):
            processed+=1
            if html is not None and d<max_depth:
                rows=[]
                for n in extract_links(html,u,host,subs,keep):
                    rows.append((n,'HTML',u,d+1))
                    if n not in seen:seen.add(n);q.append((n,d+1))
                add_many(con,rows)
        if processed%100<len(batch):print_summary(con,'URL COLLECTION',processed,len(q))
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
    keep=bool(cfg.get('keep_query',False));target=normalize_url(os.environ.get('TARGET_URL') or cfg['target_url'],keep)
    collection=(os.environ.get('COLLECTION_MODE') or 'AUTO').upper();mode=(os.environ.get('OUTPUT_MODE') or 'URL_ONLY').upper()
    if collection not in ('AUTO','XML_ONLY','HTML_CRAWL') or mode not in ('URL_ONLY','DETAIL'):raise SystemExit('Mode が不正です')
    timeout=int(cfg.get('timeout_seconds',25));max_files=int(cfg.get('max_sitemap_files',1000));subs=bool(cfg.get('include_subdomains',False))
    respect=bool(cfg.get('respect_robots_txt',True));max_depth=int(cfg.get('max_depth',50));max_pages=int(cfg.get('max_html_pages_per_run',50000))
    conc=max(1,int(os.environ.get('CONCURRENCY') or cfg.get('concurrency',6)));rps=max(0.1,float(os.environ.get('REQUESTS_PER_SECOND') or cfg.get('requests_per_second',1.5)));ua=cfg.get('user_agent','SiteURLCollector/3.1 (+GitHub Actions)')
    state=Path(cfg.get('state_dir','state'));out=Path(cfg.get('output_dir','output'));host=urlparse(target).netloc.lower().split(':')[0];db=state/f'{host_key(target)}.sqlite3'
    if fresh and db.exists():db.unlink()
    con=init_db(db);add_many(con,[(target,'HTML','START',0)])
    headers={'User-Agent':ua,'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    async with aiohttp.ClientSession(headers=headers,connector=aiohttp.TCPConnector(limit=max(conc*2,8)),timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        rp,robots_url,robots_loaded,robots_text=await get_robots(session,target,timeout)
        if not respect:robots_loaded=False
        print(f'Target URL      : {target}',flush=True);print(f'Collection mode : {collection}',flush=True);print(f'Output mode     : {mode}',flush=True);print(f'Concurrency     : {conc}',flush=True);print(f'Requests/sec    : {rps}',flush=True)
        if collection in ('AUTO','XML_ONLY'):
            files,sources=await discover_xml(session,target,robots_text,timeout,host,subs,keep,max_files)
            add_many(con,[(u,'XML',sm,0) for u,sm in sources.items()]);print(f'[XML] sitemap_files={len(files):,} discovered={len(sources):,}',flush=True)
        if collection in ('AUTO','HTML_CRAWL'):
            base=f'{urlparse(target).scheme}://{urlparse(target).netloc}';seeds=[(target,0)]
            seeds += [(normalize_url(urljoin(base,p),keep),0) for p in COMMON_HTML_SITEMAPS if normalize_url(urljoin(base,p),keep)]
            if collection=='AUTO':seeds += [(r[0],0) for r in con.execute("SELECT url FROM urls WHERE discovered_via='XML'").fetchall()]
            await html_discovery(session,con,seeds,host,subs,keep,timeout,rp,robots_loaded,ua,conc,rps,max_pages,max_depth)
        print_summary(con,'URL COLLECTION',unique_count(con),0)
        if mode=='DETAIL':
            print('STEP2: 保存済みURLの詳細情報を取得します。',flush=True)
            await detail_fetch(session,con,timeout,rp,robots_loaded,ua,conc,rps,max_pages);print_summary(con,'DETAIL')
    xlsx,csv=export_outputs(con,out,target,mode);print(f'Excel: {xlsx}',flush=True);print(f'CSV  : {csv}',flush=True);con.close()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',default='config.json');ap.add_argument('--fresh',action='store_true');a=ap.parse_args()
    asyncio.run(run(json.loads(Path(a.config).read_text(encoding='utf-8')),a.fresh))
if __name__=='__main__':main()
