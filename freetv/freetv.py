#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 连通性检测工具 v2.0
- 模板解析：支持任意 emoji/符号前缀的分类行（如 📺央视频道,#genre#）
- 检测方法：HEAD 优先，失败降级 GET；仅判断最终状态码是否为 200
- 超时 / 非200 / 连接异常 -> 提取域名写入 freetv/blacklist.txt 并去重
- 输出：通过验证的频道列表（TXT + M3U）
- 打印格式：频道名 | URL(截80) | 耗时ms | 状态
"""

import asyncio
import aiohttp
import ssl
import os
import re
import time
from urllib.parse import urlparse, urldefrag
from datetime import datetime, timedelta, timezone

# ====================== 配置 ======================
CHECK_TIMEOUT = 5          # 单次请求总超时（秒）
CONNECT_TIMEOUT = 5        # TCP/SSL 连接超时
MAX_CONCURRENT = 50        # 并发数
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
    'Referer': 'https://www.google.com/',
}

# emoji/符号清洗：覆盖常用 emoji 平面、杂项符号、变体选择符、零宽连接符
EMOJI_RE = re.compile(
    "[" 
    "\U0001F000-\U0001FAFF"   # 象形/表情/交通/旗/补充
    "\U00002600-\U000027BF"   # 杂项符号+丁巴特
    "\U0001F1E0-\U0001F1FF"   # 国旗
    "\U0000FE00-\U0000FE0F"   # 变体选择符
    "\U0000200D"              # 零宽连接符
    "\U00002190-\U000021FF"   # 箭头
    "\U00002B00-\U00002BFF"   # 符号
    "]+", flags=re.UNICODE
)
WS_RE = re.compile(r'[\u3000\s]+')  # 全角/半角空白


def clean_cat_name(raw: str) -> str:
    s = EMOJI_RE.sub('', raw)
    s = WS_RE.sub('', s)
    # 再去掉可能残留的普通符号前缀，如“4K”保留，“·”保留；“,”已在外部切掉
    return s.strip(' ,，、-')


# ====================== 黑名单 ======================
class Blacklist:
    def __init__(self, path='freetv/blacklist.txt'):
        self.path = path
        self.domains = set()
        self._lock = asyncio.Lock()
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as f:
                f.write("# IPTV黑名单域名列表\n# 每行一个域名，#开头为注释\n")
            print(f"已创建黑名单文件: {self.path}")
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.domains.add(line)
        print(f"黑名单加载完毕: {len(self.domains)} 个域名")

    @staticmethod
    def host_of(url: str) -> str:
        try:
            netloc = urlparse(url).netloc
            # 去用户信息、去端口
            if '@' in netloc:
                netloc = netloc.split('@', 1)[1]
            if ':' in netloc:
                netloc = netloc.split(':', 1)[0]
            return netloc.lower()
        except Exception:
            return ''

    async def add(self, url: str):
        host = self.host_of(url)
        if not host:
            return
        async with self._lock:
            if host in self.domains:
                return
            self.domains.add(host)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(host + '\n')
            self._dedup()

    def _dedup(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        seen = set()
        out = []
        for ln in lines:
            st = ln.strip()
            if st.startswith('#') or st == '':
                out.append(ln if ln.endswith('\n') else ln + '\n')
                continue
            if st in seen:
                continue
            seen.add(st)
            out.append(ln if ln.endswith('\n') else ln + '\n')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.writelines(out)

    def contains(self, url: str) -> bool:
        return self.host_of(url) in self.domains


# ====================== 模板 ======================
class ChannelTemplate:
    def __init__(self, path):
        self.path = path
        self.categories = []
        self.channel_map = {}          # alias -> main
        self.main_channels = {}        # main -> cat
        self.category_channels = {}    # cat -> [main...]

    def load(self):
        if not os.path.exists(self.path):
            print(f"[错误] 模板文件 {self.path} 不存在")
            return False

        current_cat = None
        with open(self.path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 分类行：含 #genre#，取第一个逗号前，清 emoji
                if '#genre#' in line:
                    head = line.split('#genre#', 1)[0]
                    if ',' in head:
                        head = head.split(',', 1)[0]
                    cat = clean_cat_name(head)
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat
                    continue
                # 频道行：至少含一个逗号，且当前有分类
                if current_cat and ',' in line:
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if not items:
                        continue
                    main = items[0]
                    self.main_channels.setdefault(main, current_cat)
                    if main not in self.category_channels[current_cat]:
                        self.category_channels[current_cat].append(main)
                    for alias in items:
                        self.channel_map.setdefault(alias, main)

        # 其它兜底
        other = '其它频道'
        self.categories = [c for c in self.categories if c != other]
        if other not in self.categories:
            self.categories.append(other)
        self.category_channels.setdefault(other, [])

        print(f"模板加载完成：{len(self.categories)} 个分类，{len(self.channel_map)} 个别名")
        return True

    def add_to_other(self, name):
        other = '其它频道'
        self.main_channels.setdefault(name, other)
        if name not in self.category_channels[other]:
            self.category_channels[other].append(name)
        self.channel_map.setdefault(name, name)

    def get_main(self, name):
        return self.channel_map.get(name, name)

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = re.sub(r'[\\/:*?"<>|]', '', main).strip()
        return f"https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/{safe}.png"

    def template_names(self):
        return set(self.channel_map.keys())


# ====================== 抓取源 ======================
async def fetch_text(session, url):
    try:
        async with session.get(url, timeout=10) as resp:
            return await resp.text()
    except Exception as e:
        print(f"获取失败 {url}: {e}")
        return ''


def clean_m3u_name(raw):
    name = re.sub(r'\([^)]*\)', '', raw)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def sanitize_url(raw):
    u = raw.strip()
    u, _ = urldefrag(u)
    if u.startswith(('http://', 'https://')):
        return u
    return None


def parse_m3u(text):
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',', 1)
            if len(parts) >= 2:
                name = clean_m3u_name(parts[1])
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith('#')):
                    j += 1
                if j < len(lines):
                    u = sanitize_url(lines[j])
                    if u and name:
                        out.append((name, u))
                    i = j
        i += 1
    return out


def parse_txt(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if '#genre#' in line or not line or ',' not in line:
            continue
        if '://' not in line:
            continue
        try:
            name, url = line.split(',', 1)
            u = sanitize_url(url)
            name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
            if u and name:
                out.append((name, u))
        except Exception:
            pass
    return out


async def fetch_all(urls):
    res = []
    async with aiohttp.ClientSession(headers=HEADERS) as s:
        for u in urls:
            t = await fetch_text(s, u)
            if not t:
                continue
            if t.lstrip().startswith('#EXTM3U'):
                chs = parse_m3u(t)
                print(f"  M3U源 {u}: {len(chs)} 个频道")
            else:
                chs = parse_txt(t)
                print(f"  TXT源 {u}: {len(chs)} 个频道")
            res.extend(chs)
    return res


# ====================== 连通性测试 ======================
class ConnectivityTester:
    def __init__(self, blacklist: Blacklist):
        self.bl = blacklist
        self.stats = {'total': 0, 'passed': 0, 'failed': 0}
        self.session = None
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def __aenter__(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        conn = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT * 2,
            ttl_dns_cache=600,
            ssl=ssl_ctx,
            force_close=False,
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(
                total=CHECK_TIMEOUT,
                connect=CONNECT_TIMEOUT,
                sock_connect=CONNECT_TIMEOUT,
                sock_read=CHECK_TIMEOUT,
            ),
        )
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def test_one(self, url: str, name: str):
        if self.bl.contains(url):
            print(f"⏭️  黑名单跳过: {name:<12}| {self.bl.host_of(url)}")
            return False, 0

        clean, _ = urldefrag(url)
        if clean != url:
            url = clean

        async with self.sem:
            start = time.time()
            status = None
            try:
                # 1) HEAD
                try:
                    async with self.session.head(url, allow_redirects=True) as r:
                        status = r.status
                        ms = (time.time() - start) * 1000
                except (aiohttp.ClientResponseError, aiohttp.ClientError):
                    # 2) 降级 GET，只读一点
                    async with self.session.get(url, allow_redirects=True) as r:
                        status = r.status
                        await r.content.read(4096)
                        ms = (time.time() - start) * 1000
                except Exception:
                    raise

                if status == 200:
                    self.stats['total'] += 1
                    self.stats['passed'] += 1
                    print(f"✅ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|200")
                    return True, ms
                else:
                    self.stats['total'] += 1
                    self.stats['failed'] += 1
                    print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|{status} -> 写黑名单")
                    await self.bl.add(url)
                    return False, ms

            except asyncio.TimeoutError:
                ms = (time.time() - start) * 1000
                self.stats['total'] += 1
                self.stats['failed'] += 1
                print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|超时 -> 写黑名单")
                await self.bl.add(url)
                return False, ms
            except Exception as e:
                ms = (time.time() - start) * 1000
                self.stats['total'] += 1
                self.stats['failed'] += 1
                print(f"❌ {name:<12}|{url[:80]:<80}|{ms:>7.0f} ms|{str(e)[:25]} -> 写黑名单")
                await self.bl.add(url)
                return False, ms

    async def batch(self, channel_list, tpl: ChannelTemplate):
        groups = {}
        for main, url in channel_list:
            groups.setdefault(main, []).append(url)

        results = {}
        total = len(channel_list)
        done = 0
        print(f"\n开始连通性测试，并发 {MAX_CONCURRENT}，共 {total} 个源")
        print("=" * 140)

        for cat in tpl.categories:
            for main in tpl.category_channels.get(cat, []):
                urls = groups.get(main)
                if not urls:
                    continue
                outs = await asyncio.gather(*[self.test_one(u, main) for u in urls])
                passed = [(u, ms) for u, (ok, ms) in zip(urls, outs) if ok]
                passed.sort(key=lambda x: x[1])
                if passed:
                    results[main] = passed
                done += len(urls)
                ok_n = sum(1 for ok, _ in outs if ok)
                print(f"  {main:<20} 通过 {ok_n}/{len(urls)}  进度 {done}/{total}")
                print("-" * 120)

        return results, self.stats


# ====================== 输出 ======================
def save_output(all_ch, tpl, out_dir='freetv'):
    os.makedirs(out_dir, exist_ok=True)
    bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y%m%d %H:%M:%S')

    txt_path = os.path.join(out_dir, 'freetv.txt')
    m3u_path = os.path.join(out_dir, 'freetv.m3u')
    epg = 'https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml'

    txt_lines = ['#genre#', f'更新时间,{bj}', '']
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg}"']

    for cat in tpl.categories:
        mains = [m for m in tpl.category_channels.get(cat, []) if all_ch.get(m)]
        if not mains:
            continue
        txt_lines.append(f'{cat},#genre#')
        for m in mains:
            for url, ms in all_ch[m]:
                txt_lines.append(f'{m},{url}')
            logo = tpl.get_logo_url(m)
            for url, ms in all_ch[m]:
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{m}" tvg-logo="{logo}" group-title="{cat}", {m}'
                )
                m3u_lines.append(url)

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    n = sum(len(v) for v in all_ch.values())
    print(f"\n输出文件：\n  {txt_path} ({n} 源)\n  {m3u_path} ({n} 源)")


# ====================== 主流程 ======================
async def main():
    print("=" * 92)
    print("IPTV 连通性检测（仅状态码200，自动拉黑无效域名）")
    print("=" * 92)

    bl = Blacklist('freetv/blacklist.txt')
    tpl = ChannelTemplate('freetv/dome.txt')
    if not tpl.load():
        return

    source_urls = [
        "https://sub.ottiptv.cc/yylunbo.m3u",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
    ]

    print("\n从网络源获取频道列表...")
    raw = await fetch_all(source_urls)
    print(f"共获取 {len(raw)} 个源")
    if not raw:
        print("未获取到任何源")
        return

    known, unknown = [], []
    names = tpl.template_names()
    for nm, u in raw:
        if nm in names:
            known.append((nm, u))
        else:
            unknown.append((nm, u))

    for nm, u in unknown:
        tpl.add_to_other(nm)
    print(f"已知频道源 {len(known)}，未知归入其它 {len(unknown)}")

    std = [(tpl.get_main(nm), u) for nm, u in known + unknown]
    print(f"待测源总数: {len(std)}")

    async with ConnectivityTester(bl) as tester:
        results, stats = await tester.batch(std, tpl)

    print("\n" + "=" * 60)
    print("完成统计：")
    print(f"  总检测: {stats['total']}  通过200: {stats['passed']}  失败: {stats['failed']}")
    print(f"  可用频道数: {len(results)}")

    save_output(results, tpl)

    print("\n分类统计：")
    for cat in tpl.categories:
        mains = tpl.category_channels.get(cat, [])
        avail = [m for m in mains if results.get(m)]
        src = sum(len(results[m]) for m in avail)
        print(f"  {cat}: 可用 {len(avail)}/{len(mains)} 频道, {src} 源")


if __name__ == '__main__':
    asyncio.run(main())
