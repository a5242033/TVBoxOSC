# -*- coding: utf-8 -*-
"""三镜像一致性检查：raw / ghfast / gh-proxy 的 jsm.json 是否一致。

用法：
    python scripts/check_mirrors.py --wait 60
退出码：0 一致；1 不一致或拉取失败（重试 --wait 秒后仍不一致）。
"""
import argparse, hashlib, json, random, re, ssl, sys, time, urllib.parse, urllib.request

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=CTX))

REL = 'a5242033/TVBoxOSC/master/config/jsm.json'
MIRRORS = {
    'raw': 'https://raw.githubusercontent.com/' + REL,
    'ghfast': 'https://ghfast.top/https://raw.githubusercontent.com/' + REL,
    'gh-proxy': 'https://gh-proxy.com/https://raw.githubusercontent.com/' + REL,
}


def fetch(url, timeout=25):
    u = urllib.parse.quote(url + ('&' if '?' in url else '?') + 'v=%d' % random.randint(1, 10 ** 9),
                           safe=":/?&=%#+;,@!$'()*~[]|")
    req = urllib.request.Request(u, headers={'User-Agent': 'okhttp/3.8.1', 'Connection': 'close'})
    with OP.open(req, timeout=timeout) as r:
        return r.read()


def signature(body):
    """用站点集合 + spider 做语义指纹（忽略缩进/顺序差异里的非实质变化）。"""
    d = json.loads(body.decode('utf-8-sig'))
    apis = sorted({(s.get('name') or '') + '|' + (s.get('api') or '') for s in d.get('sites', [])})
    return {
        'sites': len(d.get('sites', [])),
        'urls': len(d.get('urls', [])),
        'spider': bool(d.get('spider')),
        'hash': hashlib.sha256(('\n'.join(apis) + str(d.get('spider'))).encode()).hexdigest()[:16],
        'sample': apis[:3],
    }


def once():
    out, errs = {}, {}
    for name, url in MIRRORS.items():
        try:
            out[name] = signature(fetch(url))
        except Exception as e:
            errs[name] = '%s: %s' % (type(e).__name__, str(e)[:60])
    return out, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wait', type=int, default=0, help='最多等待多少秒（每 30s 重试）')
    args = ap.parse_args()

    deadline = time.time() + args.wait
    attempt = 0
    while True:
        attempt += 1
        res, errs = once()
        print('attempt %d  %s' % (attempt, {k: (v['sites'], v['hash']) for k, v in res.items()}))
        for k, v in errs.items():
            print('  ERR %-9s %s' % (k, v))

        hashes = {v['hash'] for v in res.values()}
        # raw 在国内常被重置，允许缺席：只要仍有两个以上镜像且哈希一致就算过
        ok = len(res) >= 2 and len(hashes) == 1
        if ok:
            ref = res.get('raw') or next(iter(res.values()))
            print('OK 镜像一致（%d/%d 可达）：sites=%d urls=%d spider=%s hash=%s'
                  % (len(res), len(MIRRORS), ref['sites'], ref['urls'], ref['spider'], ref['hash']))
            return 0
        if time.time() >= deadline:
            print('FAIL 不一致或缺失：')
            for k in MIRRORS:
                print('  %-9s %s' % (k, res.get(k, errs.get(k))))
            return 1
        time.sleep(30)


if __name__ == '__main__':
    sys.exit(main())
