# -*- coding: utf-8 -*-
"""点播源巡检（每日）：搜索/分类 -> 详情 -> 播放地址可拉流。

规则：
  * 只巡检 CMS 接口站（type 0/1，api 为 http(s)）；type3 脚本站不动；
  * 连续 2 次巡检失败才剔除，避免瞬时抖动误杀；
  * 候选池（config/vod_candidates.json）里三级全通且响应 < 4s 的自动补入；
  * CMS 站按实测耗时排序后重写 config/jsm.json。
"""
import io, json, os, re, ssl, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, 'config', 'jsm.json')
CAND = os.path.join(ROOT, 'config', 'vod_candidates.json')
STATS = os.path.join(ROOT, 'config', 'vod_stats.json')

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX))

UA = 'okhttp/3.8.1'
KEYWORD = '庆余年'
TIMEOUT = 8
MAX_CMS = 40
AUTO_ADD_CANDIDATES = False
DEFAULT_SITE = '影视 · 新浪'  # 首页默认站固定，避免每日重排导致首页站源跳变


def fetch(url, maxbytes=262144, timeout=TIMEOUT):
    url = urllib.parse.quote(url, safe=":/?&=%#+;,@!$'()*~[]|")
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9', 'Connection': 'close'})
    t0 = time.time()
    with OPENER.open(req, timeout=timeout) as r:
        return r.status, r.read(maxbytes), int((time.time() - t0) * 1000)


def base_of(api):
    return api.split('?')[0].strip().rstrip('/')


def parse_ids(txt):
    ids = re.findall(r'"vod_id"\s*:\s*"?(\d+)', txt) or re.findall(r'<id>(\d+)</id>', txt)
    return list(dict.fromkeys(ids))


def play_candidates(txt, limit=4):
    urls = []
    for m in re.finditer(r'"vod_play_url"\s*:\s*"(.*?)"\s*[,}]', txt):
        for seg in m.group(1).replace('\\/', '/').split('#'):
            if '$' in seg:
                seg = seg.split('$', 1)[1]
            seg = seg.strip().rstrip(';')
            if seg.startswith('http') and seg not in urls:
                urls.append(seg)
    if not urls:
        for u in re.findall(r'(?:https?:)?//[^\s"\'<\\#$]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<\\#]*)?', txt):
            if u.startswith('//'):
                u = 'http:' + u
            if u not in urls:
                urls.append(u)
    urls.sort(key=lambda u: 0 if '.m3u8' in u else 1)
    return urls[:limit]


def check_play(url):
    try:
        st, body, ms = fetch(url, 32768)
    except Exception:
        return False, 0
    if st != 200 or not body:
        return False, ms
    if url.split('?')[0].endswith('.m3u8'):
        return b'#EXTM3U' in body[:4096], ms
    return len(body) >= 1024, ms


def probe(site):
    api = site['api']
    base = base_of(api)
    name = site.get('name') or api
    out = {'name': name, 'api': api, 'level': 0, 'mode': '', 'lat': 0}
    txt, ids, ms = '', [], 0
    for ac in ('detail', 'videolist'):
        for suffix in ('&wd=' + urllib.parse.quote(KEYWORD), '&pg=1'):
            try:
                st, body, ms = fetch('%s?ac=%s%s' % (base, ac, suffix))
            except Exception:
                continue
            if st == 200 and body:
                t = body.decode('utf-8', 'replace')
                i = parse_ids(t)
                if i:
                    txt, ids = t, i
                    out['mode'] = 'search' if 'wd=' in suffix else 'category'
                    break
        if ids:
            break
    if not ids:
        return out
    out['level'] = 1
    urls = play_candidates(txt)
    if not urls:
        try:
            st, body, _ = fetch('%s?ac=detail&ids=%s' % (base, ids[0]))
            if st == 200 and body:
                urls = play_candidates(body.decode('utf-8', 'replace'))
        except Exception:
            pass
    if not urls:
        return out
    out['level'] = 2
    for u in urls:
        ok, ms3 = check_play(u)
        if ok:
            out['level'] = 3
            out['lat'] = ms + ms3
            break
    return out


def norm(a):
    x = re.sub(r'^https?://', '', a.strip(), flags=re.I).split('?')[0].rstrip('/')
    m = re.search(r'(.*?/provide/vod)', x, re.I)
    return (m.group(1) if m else x).lower()


def main():
    cfg = json.load(io.open(CFG, encoding='utf-8-sig'))
    stats = {}
    if os.path.exists(STATS):
        try:
            stats = json.load(io.open(STATS, encoding='utf-8'))
        except Exception:
            stats = {}
    seen = {s['api']: s for s in stats.get('sites', {})}

    cms = [s for s in cfg['sites'] if s.get('type') in (0, 1)
           and isinstance(s.get('api'), str) and s['api'].startswith('http')]
    others = [s for s in cfg['sites'] if s not in cms]
    cands = []
    if os.path.exists(CAND):
        cands = json.load(io.open(CAND, encoding='utf-8')).get('sites', [])

    todo, keys = [], set()
    for s in cms + cands:
        k = norm(s['api'])
        if k in keys:
            continue
        keys.add(k)
        todo.append(s)

    print('check %d sites (%d current + %d candidates)' % (len(todo), len(cms), len(cands)))
    results = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(probe, s) for s in todo]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            print('[%3d/%d] L%d %-24s %sms' % (i, len(todo), r['level'],
                                              (r['name'] or '')[:24], r['lat']))

    new_stats = {'updated': time.strftime('%Y-%m-%d %H:%M:%S'), 'sites': {}}
    passed = {}
    for r in results:
        k = norm(r['api'])
        prev = seen.get(k, {})
        fail = 0 if r['level'] == 3 else prev.get('fail', 0) + 1
        rec = {'name': r['name'], 'api': r['api'], 'level': r['level'],
               'lat': r['lat'], 'fail': fail, 'mode': r['mode'],
               'seen': time.strftime('%Y-%m-%d')}
        new_stats['sites'][k] = rec
        if r['level'] == 3 and r['lat'] < 4000:
            passed[k] = (r, prev)

    # 保留：本轮通过，或历史未达 2 次失败
    keep = []
    for s in cms:
        k = norm(s['api'])
        st = new_stats['sites'].get(k, {})
        if k in passed:
            s['_lat'] = passed[k][0]['lat']
            keep.append(s)
        elif st.get('fail', 0) < 2:
            s['_lat'] = 9999
            keep.append(s)
        elif st.get('level', 0) >= 1:
            # 能搜索/出详情但播放地址拉不动，多为 CDN 地域差异，别误杀
            s['_lat'] = 9998
            keep.append(s)
        else:
            print('drop %s (%s) fail=%d' % (s.get('name'), s['api'][:50], st.get('fail', 0)))

    have = {norm(s['api']) for s in keep}
    # 注意：GitHub 运行器在境外，与中国宽带的可达性并不一致，
    # 因此巡检只做"剔除完全不可达的旧站"，不自动补入候选，
    # 新源仍由本地（国内网络）实测后手工入库，避免把境外通、境内不通的站加进来。
    if AUTO_ADD_CANDIDATES:
        for k, (r, prev) in passed.items():
            if k in have or len(keep) >= MAX_CMS:
                continue
            if 'apple_m3u8' in r['api'] or '/api/xml.php' in r['api']:
                continue  # 非标准 CMS 接口，TVBox type1 无法解析
            name = r['name'] or k
            name = re.sub(r'影视\s*[|｜]\s*', '', name)
            name = re.sub(r'[^\u4e00-\u9fa5A-Za-z0-9]+', ' ', name)
            name = re.sub(r'采集|资源|直连|高清|切|官源', '', name).strip()
            base = '影视 · ' + (name or '影视源')
            names = {s.get('name') for s in keep}
            n, nm = 1, base
            while nm in names:
                n += 1
                nm = base + str(n)
            keep.append({'key': 'cms%02d' % (len(keep) + 9), 'name': nm, 'type': 1,
                         'api': r['api'].split('?')[0],
                         'searchable': 1 if r['mode'] == 'search' else 0,
                         'quickSearch': 1 if r['mode'] == 'search' else 0,
                         'filterable': 1, 'changeable': 1, '_lat': r['lat']})
            print('add  %s (%s)' % (nm, r['api'][:50]))

    keep.sort(key=lambda s: (0 if s.get('name') == DEFAULT_SITE else 1, s.get('_lat', 9999)))
    for i, s in enumerate(keep, 1):
        s.pop('_lat', None)
        s['key'] = 'cms%02d' % i

    cfg['sites'] = keep + others
    io.open(CFG, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(cfg, ensure_ascii=False, indent=1))
    io.open(STATS, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(new_stats, ensure_ascii=False, indent=1))
    print('\nfinal CMS=%d  pass=%d  total=%d' % (len(keep), len(passed), len(cfg['sites'])))


if __name__ == '__main__':
    main()
