# -*- coding: utf-8 -*-
"""自建直播清单守卫：只维护自己的清单，死线用外部源实测替补。

分层（不是什么源都要全测）：
  L1 只实测当前清单里的线路（约 650 条，2 分钟）
  L2 仅对出现死线的频道，去替补池里找同频道候选，实测通过才补
  L3 外部线路必须「列表可达 + 首分片可播 + 延迟不劣于原线路 1.5 倍」才准入
  全死的频道：近 14 天有存活记录的保留 1 条待复测，否则直接剔除（避免清单虚胖）

输出：config/live.m3u + config/live_stats.json
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, 'config', 'live_sources.json')
STATS_PATH = os.path.join(ROOT, 'config', 'live_stats.json')
OUT_PATH = os.path.join(ROOT, 'config', 'live.m3u')

UA = ('Mozilla/5.0 (Linux; Android 9; TV) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36')
MIRRORS = ['https://gh-proxy.com/', 'https://gh.llkk.cc/', 'https://ghfast.top/', '']
TIMEOUT_LIST = 8
TIMEOUT_SEG = 5
# 首分片测速只在"国内网络"跑时开启：GitHub runner 在境外，按它的下载速度排线路会害了国内用户。
SEG_PROBE = os.environ.get('WATCH_LIVE_SEG_PROBE') == '1'
WORKERS = 40
MAX_PER_CHANNEL = 2
KEEP_DEAD_DAYS = 14
EXTINF = re.compile(r'^#EXTINF:(-?[0-9.]+)\s*(.*)$')
JUNK = re.compile(r'^\d{4}-\d{2}-\d{2}|^\d{2}:\d{2}:\d{2}|测试|备用|废弃|失效')


def log(m):
    print(m, flush=True)


def fetch(url, timeout=TIMEOUT_LIST, limit=65536):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*',
                                               'Connection': 'close'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def attr(a, k):
    m = re.search(k + r'="([^"]*)"', a)
    return m.group(1) if m else ''


def parse(text):
    items, meta = [], None
    for raw in text.splitlines():
        s = raw.strip().lstrip('\ufeff')
        if not s:
            continue
        if s.startswith('#EXTINF'):
            m = EXTINF.match(s)
            if not m:
                meta = None
                continue
            rest = m.group(2)
            last = rest.rfind(',')
            if last < 0:
                meta = None
                continue
            name = rest[last + 1:].strip()
            if not name:
                meta = None
                continue
            meta = (name, attr(rest[:last], 'group-title'), attr(rest[:last], 'tvg-logo'))
            continue
        if s.startswith('#'):
            continue
        if meta:
            items.append({'name': meta[0], 'group': meta[1], 'logo': meta[2], 'url': s})
            meta = None
    return items


def norm(name):
    n = re.sub(r'[\(\[（【〔].*?[\)\]）】〕]', '', name)
    n = n.replace(' ', '').replace('\u3000', '').lower()
    n = re.sub(r'[·•\-_/]+', '', n)
    n = re.sub(r'(高清|超清|蓝光|标清|4k|8k|hd|sd|fhd|uhd|频道|电视台|一套|二套)$', '', n)
    if n.startswith('cctv') and not n.startswith('cctv-'):
        n = 'cctv-' + n[4:]
    return n


def check(url):
    t0 = time.time()
    r = {'ok': False, 'seg': False, 'lat': 0.0}
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*',
                                                   'Connection': 'close'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_LIST) as resp:
            code, ctype = resp.status, resp.headers.get('Content-Type', '')
            data = resp.read(65536)
        if code != 200 or not data:
            return r
        if data.startswith(b'#EXTM3U') or b'#EXTINF' in data[:8192] or 'mpegurl' in ctype.lower():
            txt = data.decode('utf-8', 'ignore')
            if '#EXTM3U' not in txt:
                return r
            t = None
            for line in txt.splitlines():
                s = line.strip()
                if s and not s.startswith('#'):
                    t = urljoin(url, s)
                    break
            if not t:
                return r
            r['ok'] = True
            r['lat'] = round(time.time() - t0, 3)
            try:
                # SEG_PROBE（仅国内网络）：真拉 384KB 分片，慢 CDN 会在这里暴露（实测有首分片 12s 的线路）
                r['seg'] = len(fetch(t, timeout=TIMEOUT_SEG,
                                     limit=393216 if SEG_PROBE else 32768)) > 0
            except Exception:
                pass
            if SEG_PROBE:
                r['lat'] = round(time.time() - t0, 3)
            return r
        if data[:1] == b'\x47' or b'ftyp' in data[:32]:
            r['ok'] = r['seg'] = True
            r['lat'] = round(time.time() - t0, 3)
        return r
    except urllib.error.HTTPError as e:
        return r
    except Exception:
        return r


def fetch_mirrors(url, limit=8 * 1024 * 1024):
    last = ''
    for m in MIRRORS:
        u = (m + url) if m else url
        try:
            d = fetch(u, timeout=15, limit=limit)
            if d:
                return d
        except Exception as e:
            last = str(e)[:50]
    raise RuntimeError(last)


def main():
    if not os.path.exists(OUT_PATH):
        log('missing %s' % OUT_PATH)
        return 1
    base = parse(io.open(OUT_PATH, encoding='utf-8').read())
    stats = {}
    if os.path.exists(STATS_PATH):
        try:
            stats = json.load(io.open(STATS_PATH, encoding='utf-8'))
        except Exception:
            stats = {}

    # 频道表
    chans, order = {}, []
    for it in base:
        k = norm(it['name'])
        if not k:
            continue
        c = chans.get(k)
        if c is None:
            c = chans[k] = {'name': it['name'], 'group': it['group'], 'logo': it['logo'],
                            'urls': []}
            order.append(k)
        elif not c['group'] and it['group']:
            c['group'] = it['group']
        if it['url'] not in c['urls']:
            c['urls'].append(it['url'])

    log('base   %d chans / %d urls' % (len(order), len({u for c in chans.values() for u in c['urls']})))

    # L1 只测当前清单
    urls = sorted({u for c in chans.values() for u in c['urls']})
    res = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(check, u): u for u in urls}
        for f in as_completed(futs):
            res[futs[f]] = f.result()
    alive = sum(1 for v in res.values() if v['seg'])
    log('L1     alive(seg)=%d/%d' % (alive, len(urls)))

    # L1.5 对失败线路补测一轮，压低瞬时误杀
    retry = [u for u in urls if not res[u]['seg']]
    if retry:
        time.sleep(3)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(check, u): u for u in retry}
            for f in as_completed(futs):
                r = f.result()
                if r['seg']:
                    res[futs[f]] = r
        alive2 = sum(1 for v in res.values() if v['seg'])
        log('L1.5   retried=%d, alive(seg)=%d/%d (+%d)' % (len(retry), alive2, len(urls), alive2 - alive))


    # L2 死线频道 -> 替补池
    dead_chans = [k for k in order if not any(res.get(u, {}).get('seg') for u in chans[k]['urls'])]
    log('L2     dead chans=%d' % len(dead_chans))

    if dead_chans:
        cfg = json.load(io.open(CFG_PATH, encoding='utf-8')) if os.path.exists(CFG_PATH) else {}
        pool = {}
        for src in cfg.get('sources', []):
            if src.get('tier') == 'C':
                continue          # C 级不进替补，省算力
            try:
                txt = fetch_mirrors(src['url']).decode('utf-8', 'ignore')
                items = parse(txt)
                for it in items:
                    pool.setdefault(norm(it['name']), []).append(it['url'])
                log('       pool += %-14s %d urls' % (src['id'], len(items)))
            except Exception as e:
                log('       pool %-14s FAIL %s' % (src['id'], str(e)[:50]))

        cand = []
        for k in dead_chans:
            urls = pool.get(k, [])
            if not urls:
                # 精确名对不上时做一次前缀模糊匹配（CCTV1综合 vs CCTV-1、湖南卫视高清 vs 湖南卫视）
                for pk, pu in pool.items():
                    if len(k) >= 4 and (pk.startswith(k) or k.startswith(pk)):
                        urls = urls + pu
            for u in dict.fromkeys(urls):
                if u not in res:
                    cand.append((k, u))
        log('L2     candidates=%d' % len(cand))
        if cand:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futs = {ex.submit(check, u): (k, u) for k, u in cand}
                for f in as_completed(futs):
                    k, u = futs[f]
                    r = f.result()
                    res[u] = r
                    if r['seg']:
                        chans[k]['urls'].append(u)
                        log('       fill %-14s %.2fs' % (chans[k]['name'], r['lat']))

    # 排序输出
    out = ['#EXTM3U x-tvg-url="https://epg.cdn.loc.cc/e.xml"']
    kept = lines = first_ok = 0
    still_dead = []
    for k in order:
        c = chans[k]
        ranked = []
        for u in c['urls']:
            r = res.get(u)
            if not r or not r['ok']:
                continue
            st = stats.get(u) or {}
            rate = (st.get('ok', 0) + 1.0) / (st.get('ok', 0) + st.get('fail', 0) + 2.0)
            ranked.append((0 if r['seg'] else 1, -rate, r['lat'], u))
        ranked.sort()
        if not ranked:
            # 全死频道：近期还活过的保留 1 条最优历史线路等待复测，否则剔除
            now = time.time()
            recent = [u for u in c['urls']
                      if (stats.get(u) or {}).get('last_ok', 0) > now - KEEP_DEAD_DAYS * 86400]
            if not recent:
                still_dead.append(c['name'])
                continue
            best = max(recent, key=lambda u: ((stats.get(u) or {}).get('last_ok', 0),
                                              -(stats.get(u) or {}).get('ms', 99999)))
            ranked = [(2, 0, 99, best)]
        else:
            kept += 1
            if res[ranked[0][3]]['seg']:
                first_ok += 1
        for seg_i, neg_rate, lat, u in ranked[:MAX_PER_CHANNEL]:
            a = 'tvg-name="%s"' % c['name']
            if c['logo']:
                a += ' tvg-logo="%s"' % c['logo']
            if c['group']:
                a += ' group-title="%s"' % c['group']
            out.append('#EXTINF:-1 %s,%s' % (a, c['name']))
            out.append(u)
            lines += 1

    io.open(OUT_PATH, 'w', encoding='utf-8', newline='\n').write('\n'.join(out) + '\n')
    log('out    %d/%d chans usable, %d lines, first-line ok=%d (%.1f%%), %d bytes'
        % (kept, len(order), lines, first_ok, first_ok * 100.0 / max(len(order), 1),
           os.path.getsize(OUT_PATH)))
    if still_dead:
        log('dead   %d chans: %s' % (len(still_dead), ' '.join(still_dead[:20])))

    # 回写统计
    now = int(time.time())
    new_stats = {}
    for u, r in res.items():
        st = stats.get(u) or {}
        ok, fail, ms, last_ok = st.get('ok', 0), st.get('fail', 0), st.get('ms', 0), st.get('last_ok', 0)
        if r['ok']:
            ok += 1
            last_ok = now
            cur = int(r['lat'] * 1000)
            ms = cur if not ms else int(ms * 0.6 + cur * 0.4)
        else:
            fail += 1
        if fail > 40 and last_ok < now - 7 * 86400:
            continue
        new_stats[u] = {'ok': ok, 'fail': fail, 'last_ok': last_ok, 'ms': ms}
    for u, st in stats.items():
        if u not in new_stats and st.get('last_ok', 0) > now - 30 * 86400:
            new_stats[u] = st
    io.open(STATS_PATH, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(new_stats, ensure_ascii=False, sort_keys=True))
    log('stats  %d entries' % len(new_stats))
    return 0


if __name__ == '__main__':
    sys.exit(main())
