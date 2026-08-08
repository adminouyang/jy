#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV频道源测速工具 - 异步并发优化版
====================================
功能清单：
1. 从网络源获取M3U/TXT格式的频道列表
2. 黑名单机制：加载 freetv/blacklist.txt，命中域名的链接直接跳过
3. 模板匹配：根据 dome.txt 分类主频道和别名
4. 不在模板中的频道统一归入"其它频道"分类
5. 异步并发测速（默认30并发），速度精准（排除TCP慢启动）
6. 打印每个源的：频道名称、链接、速度值、响应时间(TTFB)
7. 只输出速度≥阈值的源，按分类和主频道顺序保存
8. 黑名单只通过配置文件管理，不会自动写入
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
SPEED_THRESHOLD = 600          # KB/s，低于此值不输出
CHECK_TIMEOUT = 5              # 单个请求超时（秒）
MAX_CONCURRENT = 30            # 最大并发数
DEEP_TEST_SIZE = 786432        # 测速数据量（字节），约768KB
STEADY_BYTES = 262144          # 排除前256KB作为爆发期
MIN_TEST_TIME = 1.5            # 最短测速时间（秒）
RETRY_COUNT = 1                # 重试次数
RETRY_DELAY = 0.3              # 重试间隔（秒）
BLACKLIST_FILE = "freetv/blacklist.txt"  # 黑名单文件路径

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
}


# ====================== 黑名单管理 ======================
class Blacklist:
    """黑名单管理：只从文件读取，不自动添加"""

    def __init__(self, filepath=BLACKLIST_FILE):
        self.filepath = filepath
        self.domains = set()
        self._load()

    def _load(self):
        """从文件加载黑名单域名"""
        self.domains.clear()
        if not os.path.exists(self.filepath):
            # 文件不存在则创建空文件
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, 'w', encoding='utf-8') as f:
                f.write("# IPTV黑名单域名列表\n")
                f.write("# 每行一个域名，以#开头的行视为注释\n")
            print(f"[黑名单] 已创建空黑名单文件: {self.filepath}")
            return

        count = 0
        with open(self.filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                self.domains.add(line.lower())
                count += 1
        print(f"[黑名单] 已加载 {count} 个域名（来自 {self.filepath}）")

    def contains(self, url):
        """判断URL的域名是否在黑名单中"""
        try:
            domain = urlparse(url).netloc.lower()
            # 去掉端口号
            domain = domain.split(':')[0]
            return domain in self.domains
        except Exception:
            return False

    def reload(self):
        """重新加载黑名单（运行时可调用）"""
        self._load()


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """
    加载 dome.txt 模板，维护：
    - categories: 分类顺序列表
    - channel_map: 别名 → 主频道
    - main_channels: 主频道 → 分类
    - category_channels: 分类 → 主频道列表
    """

    def __init__(self, template_path):
        self.path = template_path
        self.categories = []
        self.channel_map = {}         # 别名 → 主频道
        self.main_channels = {}       # 主频道 → 分类
        self.category_channels = {}   # 分类 → [主频道列表]
        self.logo_base_url = "https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/"

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

                # 分类行：📡央视频道,#genre# 或 央视频道,#genre#
                if ('📡' in line or line.endswith(',#genre#')) and '#genre#' in line:
                    parts = line.split('#genre#')
                    cat = parts[0].replace('📡', '').strip().strip(',')
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat

                # 频道行：CCTV1,CCTV-1 高清,CCTV1综合
                elif current_cat and ',' in line:
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if items:
                        main = items[0]
                        self.main_channels[main] = current_cat
                        if main not in self.category_channels[current_cat]:
                            self.category_channels[current_cat].append(main)
                        for alias in items:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main

        # 确保"其它频道"分类存在且排在最后
        other_cat = "其它频道"
        if other_cat not in self.categories:
            self.categories.append(other_cat)
            self.category_channels[other_cat] = []
        # 把"其它频道"移到最后
        if other_cat in self.categories:
            self.categories = [c for c in self.categories if c != other_cat] + [other_cat]

        total_main = sum(len(v) for v in self.category_channels.values())
        print(f"[模板] 加载完成：{len(self.categories)} 个分类，{total_main} 个主频道，{len(self.channel_map)} 个别名")
        return True

    def get_main(self, name):
        """别名 → 主频道，若不存在则返回原名"""
        return self.channel_map.get(name, name)

    def get_category(self, name):
        """获取频道所属分类（主频道对应的分类）"""
        main = self.get_main(name)
        return self.main_channels.get(main, "其它频道")

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = main.replace('/', '').replace('\\', '').replace(':', '')
        return f"{self.logo_base_url}{safe}.png"

    def is_known_channel(self, name):
        """判断频道名（或别名）是否在模板中"""
        return name in self.channel_map

    def add_to_other(self, channel_name):
        """将不在模板中的频道添加到'其它频道'分类"""
        if channel_name not in self.main_channels:
            self.main_channels[channel_name] = "其它频道"
        if channel_name not in self.category_channels.get("其它频道", []):
            self.category_channels.setdefault("其它频道", []).append(channel_name)

    def get_all_known_names(self):
        """返回模板中所有已知名称（主频道+别名）"""
        return set(self.channel_map.keys())


# ====================== 频道列表获取（异步） ======================
async def fetch_text(session, url):
    """异步获取URL文本内容"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.text()
    except Exception as e:
        print(f"  [获取失败] {url}: {e}")
        return ''


def clean_m3u_name(raw):
    """清理M3U频道名称，去掉括号和分辨率标识"""
    name = re.sub(r'\([^)]*\)', '', raw)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = name.strip()
    name = re.sub(r'\s+', ' ', name)
    return name


def parse_m3u(text):
    """解析M3U格式文本 → [(频道名, URL)]"""
    channels = []
    lines = text.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            parts = line.split(',')
            if len(parts) >= 2:
                raw_name = parts[-1].strip()
                name = clean_m3u_name(raw_name)
                # 找下一个非注释URL
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
    """解析TXT格式（逗号分隔）→ [(频道名, URL)]"""
    channels = []
    for line in text.split('\n'):
        line = line.strip()
        if '#genre#' in line or not line:
            continue
        if ',' in line and '://' in line:
            try:
                name, url = line.split(',', 1)
                url = url.strip()
                if url.startswith(('http://', 'https://')):
                    name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
                    channels.append((name, url))
            except Exception:
                pass
    return channels


async def fetch_all_channels(source_urls):
    """从多个URL获取频道列表，自动识别M3U/TXT格式"""
    all_channels = []
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        for url in source_urls:
            text = await fetch_text(session, url)
            if not text:
                continue
            if text.strip().startswith('#EXTM3U'):
                chs = parse_m3u(text)
                print(f"  [M3U] {url} → {len(chs)} 个频道")
            else:
                chs = parse_txt(text)
                print(f"  [TXT] {url} → {len(chs)} 个频道")
            all_channels.extend(chs)
    return all_channels


# ====================== 异步测速引擎 ======================
class AsyncSpeedTester:
    """
    异步并发测速引擎
    - 支持黑名单过滤
    - 打印频道名、链接、速度、响应时间
    - 不自动写入黑名单
    """

    def __init__(self, blacklist: Blacklist):
        self.blacklist = blacklist
        self.stats = {
            'total': 0,
            'tested': 0,
            'passed': 0,
            'failed': 0,
            'blacklisted': 0,
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
        测试单个源
        返回: (url, speed_KB/s, ttfb_ms) 或 (url, 0, 0) 表示失败
        """
        self.stats['total'] += 1

        # ---- 黑名单检查 ----
        if self.blacklist.contains(url):
            self.stats['blacklisted'] += 1
            domain = urlparse(url).netloc
            print(f"  ⏭️  跳过黑名单: {channel_name:<25} | {domain}")
            return url, 0, 0

        async with self.semaphore:
            try:
                start = time.time()
                async with self.session.get(url, timeout=CHECK_TIMEOUT) as resp:
                    ttfb = time.time() - start
                    ttfb_ms = ttfb * 1000

                    # TTFB过大直接判定失败
                    if ttfb > 2.5:
                        self.stats['failed'] += 1
                        print(f"  ❌ {channel_name:<25} | {url[:55]:<55} | TTFB超时 {ttfb_ms:6.0f}ms")
                        return url, 0, ttfb_ms

                    downloaded = 0
                    steady_downloaded = 0
                    steady_start = None
                    chunk_speeds = []
                    chunk_start = time.time()
                    first_chunk = True

                    async for chunk in resp.content.iter_chunked(32768):
                        now = time.time()
                        chunk_len = len(chunk)
                        if chunk_len == 0:
                            break

                        elapsed = now - chunk_start
                        if elapsed > 0.001:  # 过滤瞬时完成
                            chunk_speeds.append(chunk_len / elapsed / 1024)
                        chunk_start = now

                        downloaded += chunk_len

                        if downloaded > STEADY_BYTES:
                            if steady_start is None:
                                steady_start = now
                            steady_downloaded += chunk_len

                        # 停止条件
                        if downloaded >= DEEP_TEST_SIZE:
                            break
                        if (now - start) >= MIN_TEST_TIME and downloaded >= 131072:
                            break

                    total_time = time.time() - start
                    if total_time <= 0 or downloaded < 8192:
                        self.stats['failed'] += 1
                        print(f"  ❌ {channel_name:<25} | {url[:55]:<55} | 数据不足")
                        return url, 0, ttfb_ms

                    # ---- 三种速度计算 ----
                    overall_speed = downloaded / total_time / 1024

                    steady_speed = 0
                    if steady_downloaded > 0 and steady_start:
                        steady_elapsed = time.time() - steady_start
                        if steady_elapsed > 0:
                            steady_speed = steady_downloaded / steady_elapsed / 1024

                    median_speed = statistics.median(chunk_speeds) if len(chunk_speeds) >= 3 else overall_speed

                    # 加权融合（稳定期权重最高）
                    final_speed = (0.5 * steady_speed +
                                   0.3 * overall_speed +
                                   0.2 * median_speed)

                    # ---- 更新统计 ----
                    self.stats['tested'] += 1
                    self.stats['speeds'].append(final_speed)
                    self.stats['max'] = max(self.stats['max'], final_speed)
                    self.stats['min'] = min(self.stats['min'], final_speed)

                    if final_speed >= SPEED_THRESHOLD:
                        self.stats['passed'] += 1
                        status = "✅"
                    else:
                        self.stats['failed'] += 1
                        status = "❌"

                    # ---- 打印结果 ----
                    print(f"  {status} {channel_name:<25} | {url[:55]:<55} | "
                          f"速度: {final_speed:7.1f} KB/s | 响应: {ttfb_ms:6.0f} ms")

                    return url, final_speed, ttfb_ms

            except asyncio.TimeoutError:
                self.stats['failed'] += 1
                print(f"  ❌ {channel_name:<25} | {url[:55]:<55} | 超时")
                return url, 0, 0
            except Exception as e:
                self.stats['failed'] += 1
                err = str(e)[:40]
                print(f"  ❌ {channel_name:<25} | {url[:55]:<55} | 错误: {err}")
                return url, 0, 0

    async def batch_test(self, channel_list, template):
        """
        批量并发测速
        channel_list: [(频道名, URL)]
        template: ChannelTemplate实例
        返回: {主频道: [(url, speed)]}, stats
        """
        # 按主频道分组
        groups = {}
        for name, url in channel_list:
            main = template.get_main(name)
            groups.setdefault(main, []).append((name, url))

        results = {}
        total = len(channel_list)
        tested_count = 0

        print(f"\n{'='*100}")
        print(f"开始并发测速：最大并发 {MAX_CONCURRENT}，共 {total} 个源")
        print(f"{'='*100}")

        # 按模板分类顺序处理
        for cat in template.categories:
            mains_in_cat = template.category_channels.get(cat, [])
            for main in mains_in_cat:
                if main not in groups:
                    continue

                entries = groups[main]  # [(name, url), ...]
                # 并发测试该频道所有源
                tasks = [self.test_one(url, name) for name, url in entries]
                results_raw = await asyncio.gather(*tasks)

                # 筛选通过的源
                passed = [(url, sp) for url, sp in zip(
                    [u for _, u in entries],
                    [sp for _, sp in results_raw]
                ) if sp >= SPEED_THRESHOLD]

                passed.sort(key=lambda x: x[1], reverse=True)
                if passed:
                    results[main] = passed

                tested_count += len(entries)
                passed_now = len(passed)
                total_now = len(entries)
                bar = "█" * int(tested_count / total * 30)
                print(f"  📊 进度: {tested_count}/{total} {bar} | "
                      f"通过: {sum(len(v) for v in results.values())} 源")

        return results, self.stats


# ====================== 文件输出 ======================
def save_output(all_channels, template, output_dir='freetv'):
    """保存 freetv.txt 和 freetv.m3u（仅通过的源）"""
    os.makedirs(output_dir, exist_ok=True)

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y%m%d %H:%M:%S')

    epg_url = "https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml"

    # ---- TXT 文件 ----
    txt_path = os.path.join(output_dir, 'freetv.txt')
    txt_lines = ['#genre#', f'更新时间,{time_str}', '']

    # ---- M3U 文件 ----
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

            # TXT格式
            for url, speed in sources:
                txt_lines.append(f'{main},{url}')

            # M3U格式
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
    print(f"\n{'='*60}")
    print(f"📁 输出文件：")
    print(f"   {txt_path} ({total_src} 个源)")
    print(f"   {m3u_path} ({total_src} 个源)")

    # ---- 统计文件 ----
    stats_path = os.path.join(output_dir, 'freetv_stats.txt')
    with open(stats_path, 'w', encoding='utf-8') as f:
        f.write(f"更新时间: {time_str}\n")
        f.write(f"总频道数: {len(all_channels)}\n")
        f.write(f"总源数: {total_src}\n")
        if all_channels:
            avg = total_src / len(all_channels)
            f.write(f"平均每个频道源数: {avg:.1f}\n")
        f.write(f"分类统计:\n")
        for cat in template.categories:
            if cat in template.category_channels:
                total_ch = len(template.category_channels[cat])
                avail_ch = [c for c in template.category_channels[cat] if c in all_channels and all_channels[c]]
                avail_src = sum(len(all_channels[c]) for c in avail_ch)
                f.write(f"  {cat}: {len(avail_ch)}/{total_ch} 频道, {avail_src} 源\n")

    print(f"   {stats_path}")
    print(f"{'='*60}")


# ====================== 主流程 ======================
async def main():
    print("=" * 60)
    print("  IPTV频道源测速工具 (异步并发版)")
    print("  功能: 黑名单过滤 | 模板分类 | 并发测速 | 精准计时")
    print("=" * 60)

    # ---- 1. 加载黑名单 ----
    blacklist = Blacklist(BLACKLIST_FILE)

    # ---- 2. 加载频道模板 ----
    template = ChannelTemplate("freetv/dome.txt")
    if not template.load():
        return

    # ---- 3. 获取频道列表 ----
    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        #"https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
        # 可添加更多源URL
    ]
    print(f"\n从 {len(source_urls)} 个网络源获取频道列表...")
    all_raw = await fetch_all_channels(source_urls)
    print(f"总共获取到 {len(all_raw)} 个频道源")

    if not all_raw:
        print("[错误] 未获取到任何频道源")
        return

    # ---- 4. 分离已知频道和未知频道 ----
    known_names = template.get_all_known_names()
    known_channels = []    # 模板中存在的
    unknown_channels = []   # 模板中不存在的

    for name, url in all_raw:
        if name in known_names:
            known_channels.append((name, url))
        else:
            unknown_channels.append((name, url))

    print(f"\n频道分类统计：")
    print(f"  模板中已知频道: {len(known_channels)} 个源")
    print(f"  模板外未知频道: {len(unknown_channels)} 个源 → 归入「其它频道」")

    # 将未知频道也加入待测试列表，但归入"其它频道"分类
    for name, url in unknown_channels:
        if name not in template.main_channels:
            template.add_to_other(name)

    # 合并所有待测试频道
    all_to_test = known_channels + unknown_channels
    print(f"  合计待测试: {len(all_to_test)} 个源")

    # ---- 5. 标准化名称（别名→主频道）----
    std_list = []
    for name, url in all_to_test:
        main = template.get_main(name)
        std_list.append((main, url))

    # ---- 6. 异步并发测速 ----
    async with AsyncSpeedTester(blacklist) as tester:
        results, stats = await tester.batch_test(std_list, template)

    # ---- 7. 打印统计 ----
    print(f"\n{'='*60}")
    print(f"🎉 测速完成！")
    print(f"{'='*60}")
    print(f"  总源数:       {stats['total']}")
    print(f"  黑名单跳过:   {stats['blacklisted']}")
    print(f"  实际测试:     {stats['tested']}")
    print(f"  通过(≥{SPEED_THRESHOLD}KB/s): {stats['passed']}")
    print(f"  失败:         {stats['failed']}")
    if stats['total'] > 0:
        rate = stats['passed'] / stats['total'] * 100
        print(f"  总通过率:     {rate:.1f}%")
    if stats['speeds']:
        avg = statistics.mean(stats['speeds'])
        print(f"  平均速度:     {avg:.1f} KB/s")
        print(f"  最高速度:     {stats['max']:.1f} KB/s")
        print(f"  最低速度:     {stats['min']:.1f} KB/s")
    print(f"  通过频道数:   {len(results)}")

    # ---- 8. 保存文件 ----
    save_output(results, template)

    # ---- 9. 分类统计 ----
    print(f"\n分类统计：")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in results]
        src_cnt = sum(len(results[m]) for m in avail)
        if mains:
            print(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    # ---- 10. 源最多的频道 TOP10 ----
    if results:
        ranked = sorted(results.items(), key=lambda x: len(x[1]), reverse=True)
        print(f"\n源最多的10个频道：")
        for i, (ch, srcs) in enumerate(ranked[:10], 1):
            cat = template.get_category(ch)
            print(f"  {i:2d}. {ch:<25} {len(srcs):3d} 个源 [{cat}]")

    print(f"\n✅ 全部完成！黑名单文件: {BLACKLIST_FILE}")


if __name__ == '__main__':
    asyncio.run(main())
