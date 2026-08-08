#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道源测速工具 - 异步并发优化完整版
功能：
  1. 从网络源获取M3U/TXT格式的频道列表
  2. 根据模板(dome.txt)过滤频道，只测试模板中存在的频道
  3. 读取黑名单(freetv/blacklist.txt)，跳过黑名单域名的链接
  4. 异步并发测速，大幅提升效率
  5. 修复速度虚高问题：排除TCP慢启动爆发期，采用总时间+稳定期+中位数加权
  6. 只输出速度≥阈值的源，按模板分类和主频道顺序保存
  7. 测试失败的域名自动追加写入黑名单
"""

import asyncio
import aiohttp
import ssl
import statistics
import os
import re
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

# ====================== 全局配置 ======================
SPEED_THRESHOLD  = 600          # KB/s，低于此值视为失败
CHECK_TIMEOUT    = 5            # 单个请求超时（秒）
MAX_CONCURRENT   = 30           # 最大并发数
DEEP_TEST_SIZE   = 786432       # 测速数据量（字节），约768KB
STEADY_BYTES     = 262144       # 排除前256KB作为TCP慢启动爆发期
MIN_TEST_TIME    = 1.5          # 最短测速时间（秒）
RETRY_COUNT      = 1            # 重试次数
RETRY_DELAY      = 0.3          # 重试间隔（秒）
BLACKLIST_FILE   = "freetv/blacklist.txt"  # 黑名单文件路径

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
}


# ====================== 黑名单管理 ======================
class Blacklist:
    """黑名单管理：加载、查询、追加"""

    def __init__(self, filepath=BLACKLIST_FILE):
        self.filepath = filepath
        self.domains = set()   # 当前内存中的黑名单域名集合
        self._ensure_dir()
        self.load()

    def _ensure_dir(self):
        """确保目录存在"""
        d = os.path.dirname(self.filepath)
        if d:
            os.makedirs(d, exist_ok=True)

    def load(self):
        """从文件加载黑名单"""
        if not os.path.exists(self.filepath):
            print(f"[黑名单] 文件不存在，将创建新文件: {self.filepath}")
            # 创建空文件
            with open(self.filepath, 'w', encoding='utf-8') as f:
                pass
            return

        with open(self.filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.domains.add(line)

        print(f"[黑名单] 已加载 {len(self.domains)} 个域名")

    def is_blocked(self, url):
        """判断某个URL的域名是否在黑名单中"""
        try:
            domain = urlparse(url).netloc
            # 去掉端口号
            if ':' in domain:
                domain = domain.split(':')[0]
            return domain in self.domains
        except Exception:
            return False

    def add(self, url):
        """将URL的域名追加写入黑名单（内存+文件）"""
        try:
            domain = urlparse(url).netloc
            if ':' in domain:
                domain = domain.split(':')[0]
            if not domain or domain in self.domains:
                return
            self.domains.add(domain)
            with open(self.filepath, 'a', encoding='utf-8') as f:
                f.write(domain + '\n')
        except Exception as e:
            print(f"  [警告] 写入黑名单失败: {e}")

    def size(self):
        return len(self.domains)


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """加载dome.txt模板，维护分类、主频道、别名映射"""

    def __init__(self, template_path):
        self.path = template_path
        self.categories = []                  # 分类顺序
        self.channel_map = {}                 # 别名 → 主频道
        self.main_channels = {}               # 主频道 → 分类
        self.category_channels = {}           # 分类 → 主频道列表

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
                if '📡' in line and '#genre#' in line:
                    # 分类行: "📡分类名,#genre#" 或 "📡分类名,备注,#genre#"
                    # 在 "#genre#" 上 split，取第0段，再按逗号取第0段
                    pre = line.split('#genre#')[0]
                    cat = pre.split(',')[0].replace('📡', '').strip()
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat
                elif current_cat:
                    # 频道行：支持带逗号（主频道,别名1,别名2）和不带逗号（单名称）
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if items:
                        main = items[0]
                        self.main_channels[main] = current_cat
                        if main not in self.category_channels[current_cat]:
                            self.category_channels[current_cat].append(main)
                        for alias in items:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main

        # 确保有"其它"分类
        if '其它' not in self.categories:
            self.categories.append('其它')
            self.category_channels['其它'] = []

        print(f"[模板] 加载完成: {len(self.categories)} 个分类, "
              f"{len(self.channel_map)} 个别名, "
              f"{sum(len(v) for v in self.category_channels.values())} 个主频道")
        return True

    def get_main(self, name):
        """别名 → 主频道，不存在则返回原名"""
        return self.channel_map.get(name, name)

    def get_category(self, name):
        main = self.get_main(name)
        return self.main_channels.get(main, '其它')

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = main.replace('/', '').replace('\\', '').replace(':', '')
        return f"https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/{safe}.png"

    def get_template_names(self):
        """返回模板中所有已知名称（主频道+别名）"""
        return set(self.channel_map.keys())


# ====================== 频道列表获取（异步） ======================
async def fetch_text(session, url):
    """异步获取文本内容"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.text()
    except Exception as e:
        print(f"  [获取失败] {url}: {e}")
        return ''


def clean_m3u_name(raw):
    """清理M3U频道名称，去掉括号和分辨率信息"""
    name = re.sub(r'\([^)]*\)', '', raw)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = name.strip()
    name = re.sub(r'\s+', ' ', name)
    return name


def parse_m3u(text):
    """解析M3U文本，返回 [(名称, URL)]"""
    channels = []
    lines = text.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',')
            if len(parts) >= 2:
                name = clean_m3u_name(parts[-1].strip())
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                    j += 1
                if j < len(lines):
                    url = lines[j].strip()
                    if url.startswith(('http://', 'https://')):
                        channels.append((name, url))
                        i = j
        i += 1
    return channels


def parse_txt(text):
    """解析TXT格式（逗号分隔: 名称,URL）"""
    channels = []
    for line in text.split('\n'):
        line = line.strip()
        if '#genre#' in line or not line:
            continue
        if ',' in line and '://' in line:
            try:
                name, url = line.split(',', 1)
                if url.startswith(('http://', 'https://')):
                    name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
                    channels.append((name, url))
            except Exception:
                pass
    return channels


async def fetch_channels_from_urls(urls):
    """从多个URL获取频道列表（自动识别M3U/TXT）"""
    all_channels = []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for url in urls:
            text = await fetch_text(session, url)
            if not text:
                continue
            if text.strip().startswith('#EXTM3U'):
                chs = parse_m3u(text)
                print(f"  [M3U] {url}: {len(chs)} 个频道")
            else:
                chs = parse_txt(text)
                print(f"  [TXT] {url}: {len(chs)} 个频道")
            all_channels.extend(chs)
    return all_channels


# ====================== 异步测速引擎 ======================
class AsyncSpeedTester:
    """异步并发测速引擎"""

    def __init__(self, blacklist: Blacklist):
        self.blacklist = blacklist
        self.stats = {
            'total': 0,
            'tested': 0,        # 实际发起测试的数量（排除黑名单）
            'passed': 0,
            'failed': 0,
            'blacklisted': 0,   # 被黑名单跳过的
            'speeds': [],
            'max': 0,
            'min': float('inf'),
        }
        self.session = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)

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
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT + 2),
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def test_one(self, url, channel_name):
        """
        测试单个源：
          1. 先检查黑名单，命中则跳过
          2. 测速，返回速度KB/s；失败返回0
          3. 失败时将域名加入黑名单
        """
        self.stats['total'] += 1

        # ---- 黑名单检查 ----
        if self.blacklist.is_blocked(url):
            self.stats['blacklisted'] += 1
            return 0.0

        async with self.semaphore:
            try:
                start = time.time()
                async with self.session.get(url, timeout=CHECK_TIMEOUT) as resp:
                    ttfb = time.time() - start
                    if ttfb > 2.5:
                        # TTFB过高，直接判定失败
                        self.blacklist.add(url)
                        self.stats['failed'] += 1
                        return 0.0

                    downloaded = 0
                    steady_downloaded = 0
                    steady_start = None
                    chunk_speeds = []
                    chunk_start = time.time()

                    async for chunk in resp.content.iter_chunked(32768):
                        now = time.time()
                        chunk_len = len(chunk)
                        if chunk_len == 0:
                            break

                        # 正确计时：本次chunk的下载耗时
                        elapsed = now - chunk_start
                        if elapsed > 0.001:   # 过滤瞬时完成（缓冲数据）
                            chunk_speeds.append(chunk_len / elapsed / 1024)
                        chunk_start = now

                        downloaded += chunk_len

                        # 稳定期数据（超过STEADY_BYTES后）
                        if downloaded > STEADY_BYTES:
                            if steady_start is None:
                                steady_start = now
                            steady_downloaded += chunk_len

                        # 满足条件即停止
                        if downloaded >= DEEP_TEST_SIZE:
                            break
                        if (now - start) >= MIN_TEST_TIME and downloaded >= 131072:
                            break

                    total_time = time.time() - start

                    # 数据量太小，视为失败
                    if total_time <= 0 or downloaded < 8192:
                        self.blacklist.add(url)
                        self.stats['failed'] += 1
                        return 0.0

                    # ---- 三种速度计算方法 ----
                    # 方法1：总时间法（最可靠）
                    overall_speed = downloaded / total_time / 1024
                    # 方法2：稳定期法
                    steady_speed = 0.0
                    if steady_downloaded > 0 and steady_start:
                        se = time.time() - steady_start
                        if se > 0:
                            steady_speed = steady_downloaded / se / 1024
                    # 方法3：中位数法
                    median_speed = statistics.median(chunk_speeds) if len(chunk_speeds) >= 3 else overall_speed

                    # 加权融合（稳定期权重最高）
                    final_speed = (0.5 * steady_speed +
                                   0.3 * overall_speed +
                                   0.2 * median_speed)

                    # 低于阈值 → 失败 → 加入黑名单
                    self.stats['tested'] += 1
                    if final_speed >= SPEED_THRESHOLD:
                        self.stats['passed'] += 1
                    else:
                        self.stats['failed'] += 1
                        self.blacklist.add(url)

                    # 更新统计
                    self.stats['speeds'].append(final_speed)
                    self.stats['max'] = max(self.stats['max'], final_speed)
                    self.stats['min'] = min(self.stats['min'], final_speed)

                    return final_speed

            except Exception:
                self.stats['failed'] += 1
                self.blacklist.add(url)
                return 0.0

    async def batch_test(self, channel_list, template):
        """
        批量并发测速
        channel_list: [(主频道名, URL)]
        返回: {主频道: [(url, speed)]}
        """
        # 按主频道分组
        groups = {}
        for main, url in channel_list:
            groups.setdefault(main, []).append(url)

        results = {}
        total = len(channel_list)
        tested_count = 0

        print(f"\n{'='*70}")
        print(f"开始并发测速 | 最大并发: {MAX_CONCURRENT} | 阈值: ≥{SPEED_THRESHOLD}KB/s")
        print(f"总源数: {total} | 黑名单域名数: {self.blacklist.size()}")
        print(f"{'='*70}")

        for cat in template.categories:
            mains_in_cat = template.category_channels.get(cat, [])
            for main in mains_in_cat:
                if main not in groups:
                    continue
                urls = groups[main]

                # 并发测试该频道的所有源
                tasks = [self.test_one(url, main) for url in urls]
                speeds = await asyncio.gather(*tasks)

                # 只收集通过的源
                passed = [(url, sp) for url, sp in zip(urls, speeds) if sp >= SPEED_THRESHOLD]
                passed.sort(key=lambda x: x[1], reverse=True)
                if passed:
                    results[main] = passed

                tested_count += len(urls)
                passed_now = sum(1 for sp in speeds if sp >= SPEED_THRESHOLD)
                blocked_now = sum(1 for u in urls if self.blacklist.is_blocked(u))

                # 打印进度
                bar_len = 30
                filled = int(bar_len * tested_count / total) if total > 0 else 0
                bar = '█' * filled + '░' * (bar_len - filled)
                print(f"  [{bar}] {tested_count}/{total}  "
                      f"{main:<20} 通过:{passed_now}/{len(urls)}  "
                      f"黑名单跳过:{blocked_now}")

        return results, self.stats


# ====================== 文件输出 ======================
def save_output(all_channels, template, output_dir='freetv'):
    """保存 freetv.txt 和 freetv.m3u（仅通过阈值的源）"""
    os.makedirs(output_dir, exist_ok=True)

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y%m%d %H:%M:%S')

    epg_url = 'https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml'

    # ---- TXT ----
    txt_path = os.path.join(output_dir, 'freetv.txt')
    txt_lines = ['#genre#', f'更新时间,{time_str}', '']

    # ---- M3U ----
    m3u_path = os.path.join(output_dir, 'freetv.m3u')
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg_url}"']

    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in all_channels and all_channels[m]]
        if not avail:
            continue

        txt_lines.append(f'{cat},#genre#')
        for main in avail:
            sources = all_channels[main]
            for url, speed in sources:
                txt_lines.append(f'{main},{url}')
            logo = template.get_logo_url(main)
            for url, speed in sources:
                m3u_lines.append(
                    f'#EXTINF:-1 tvg-name="{main}" tvg-logo="{logo}" group-title="{cat}", {main}'
                )
                m3u_lines.append(url)

    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    total_src = sum(len(v) for v in all_channels.values())
    print(f"\n[输出] {txt_path} ({total_src} 个源)")
    print(f"[输出] {m3u_path} ({total_src} 个源)")
    return txt_path, m3u_path


def save_stats(stats, all_channels, template, output_dir='freetv'):
    """保存统计信息"""
    os.makedirs(output_dir, exist_ok=True)
    stats_path = os.path.join(output_dir, 'freetv_stats.txt')

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y-%m-%d %H:%M:%S')

    lines = []
    lines.append(f"更新时间: {time_str}")
    lines.append(f"总源数(含黑名单): {stats['total']}")
    lines.append(f"实际测试: {stats['tested']}")
    lines.append(f"黑名单跳过: {stats['blacklisted']}")
    lines.append(f"通过(≥{SPEED_THRESHOLD}KB/s): {stats['passed']}")
    lines.append(f"失败: {stats['failed']}")
    if stats['speeds']:
        lines.append(f"平均速度: {statistics.mean(stats['speeds']):.1f} KB/s")
        lines.append(f"最高速度: {stats['max']:.1f} KB/s")
        lines.append(f"最低速度: {stats['min']:.1f} KB/s")
    lines.append(f"输出频道数: {len(all_channels)}")
    lines.append(f"输出源数: {sum(len(v) for v in all_channels.values())}")
    lines.append("")
    lines.append("分类统计:")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in all_channels and all_channels[m]]
        src_cnt = sum(len(all_channels[m]) for m in avail)
        lines.append(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"[输出] {stats_path}")


# ====================== 主流程 ======================
async def main():
    print("=" * 60)
    print("  IPTV频道源测速工具 (异步并发 + 黑名单版)")
    print("=" * 60)

    # 0. 初始化黑名单
    blacklist = Blacklist(BLACKLIST_FILE)

    # 1. 加载模板
    template = ChannelTemplate('freetv/dome.txt')
    if not template.load():
        return

    # 2. 获取频道列表
    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        #"https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
        # 可在此添加更多源URL
    ]
    print(f"\n[步骤1] 从 {len(source_urls)} 个网络源获取频道列表...")
    all_raw = await fetch_channels_from_urls(source_urls)
    print(f"[步骤1] 共获取到 {len(all_raw)} 个频道源")

    if not all_raw:
        print("[错误] 未获取到任何频道源")
        return

    # 3. 过滤：只保留模板中存在的频道
    known = template.get_template_names()
    filtered = [(n, u) for n, u in all_raw if n in known]
    print(f"[步骤2] 模板过滤后保留: {len(filtered)} 个")

    if not filtered:
        print("[错误] 没有找到模板中存在的频道")
        return

    # 4. 标准化名称：别名 → 主频道
    std_list = []
    seen = set()
    for name, url in filtered:
        main = template.get_main(name)
        # 去重：同一主频道+同一URL只保留一个
        key = (main, url)
        if key in seen:
            continue
        seen.add(key)
        std_list.append((main, url))

    print(f"[步骤3] 标准化后共 {len(std_list)} 个待测源")

    # 5. 预过滤黑名单（统计一下会被跳过多少）
    pre_blocked = sum(1 for _, url in std_list if blacklist.is_blocked(url))
    if pre_blocked > 0:
        print(f"[步骤4] 其中 {pre_blocked} 个源的域名在黑名单中，将被跳过")

    # 6. 异步并发测速
    print(f"\n[步骤5] 开始并发测速...")
    async with AsyncSpeedTester(blacklist) as tester:
        results, stats = await tester.batch_test(std_list, template)

    # 7. 输出文件
    print(f"\n[步骤6] 生成输出文件...")
    save_output(results, template)
    save_stats(stats, results, template)

    # 8. 最终统计
    print("\n" + "=" * 60)
    print("  测速完成！")
    print("=" * 60)
    print(f"  总源数:       {stats['total']}")
    print(f"  黑名单跳过:   {stats['blacklisted']}")
    print(f"  实际测试:     {stats['tested']}")
    print(f"  通过(≥{SPEED_THRESHOLD}KB/s): {stats['passed']}")
    print(f"  失败:         {stats['failed']}")
    if stats['total'] > 0:
        pass_rate = stats['passed'] / stats['total'] * 100
        print(f"  总通过率:     {pass_rate:.1f}%")
    print(f"  黑名单域名数: {blacklist.size()}")
    print(f"  输出频道数:   {len(results)}")
    print(f"  输出源数:     {sum(len(v) for v in results.values())}")

    # 分类统计
    print(f"\n  分类统计:")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in results and results[m]]
        src_cnt = sum(len(results[m]) for m in avail)
        if len(mains) > 0:
            print(f"    {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    print("\n" + "=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
