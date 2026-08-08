#!/usr/bin/env python3
"""
IPTV频道源测速工具 - 异步并发优化版
功能：
- 从网络源获取M3U/TXT格式的频道列表
- 根据模板(dome.txt)过滤频道，只测试模板中存在的频道
- 异步并发测速，大幅提升效率
- 修复速度虚高问题：排除TCP慢启动爆发期，采用总时间+稳定期+中位数加权
- 只输出速度≥600KB/s的源，按模板分类和主频道顺序保存
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

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Connection': 'keep-alive',
}

# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """加载dome.txt模板，维护分类、主频道、别名映射"""
    def __init__(self, template_path):
        self.path = template_path
        self.categories = []                     # 分类顺序
        self.channel_map = {}                    # 别名→主频道
        self.main_channels = {}                  # 主频道→分类
        self.category_channels = {}              # 分类→主频道列表

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
                    # 分类行
                    parts = line.split('#genre#')
                    cat = parts[0].replace('📡', '').strip()
                    if cat and cat not in self.categories:
                        self.categories.append(cat)
                        self.category_channels[cat] = []
                    current_cat = cat
                elif current_cat and ',' in line:
                    # 频道行
                    items = [x.strip() for x in line.split(',') if x.strip()]
                    if items:
                        main = items[0]
                        self.main_channels[main] = current_cat
                        if main not in self.category_channels[current_cat]:
                            self.category_channels[current_cat].append(main)
                        for alias in items:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main

        # 确保有“其它”分类
        if '其它' not in self.categories:
            self.categories.append('其它')
            self.category_channels['其它'] = []

        print(f"模板加载完成：{len(self.categories)} 个分类，{len(self.channel_map)} 个别名")
        return True

    def get_main(self, name):
        """别名→主频道，若不存在则返回原名"""
        return self.channel_map.get(name, name)

    def get_category(self, name):
        main = self.get_main(name)
        return self.main_channels.get(main, '其它')

    def get_logo_url(self, name):
        main = self.get_main(name)
        safe = main.replace('/', '').replace('\\', '').replace(':', '')
        return f"https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/{safe}.png"

    def get_all_main_channels(self):
        """按分类顺序返回所有主频道"""
        result = []
        for cat in self.categories:
            result.extend(self.category_channels.get(cat, []))
        return result

    def get_template_names(self):
        """返回模板中所有已知的名称（主频道+别名）"""
        return set(self.channel_map.keys())

# ====================== 频道列表获取（异步） ======================
async def fetch_text(session, url):
    """异步获取文本内容"""
    try:
        async with session.get(url, timeout=10) as resp:
            return await resp.text()
    except Exception as e:
        print(f"获取失败 {url}: {e}")
        return ''

def clean_m3u_name(raw):
    """清理M3U频道名称，去掉括号和分辨率"""
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
                # 找下一行非注释URL
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
    """解析TXT格式（逗号分隔）"""
    channels = []
    for line in text.split('\n'):
        line = line.strip()
        if '#genre#' in line or not line:
            continue
        if ',' in line and '://' in line:
            try:
                name, url = line.split(',', 1)
                if url.startswith(('http://', 'https://')):
                    # 清理名称前的[BD]等
                    name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', name).strip()
                    channels.append((name, url))
            except:
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
                print(f"  M3U源 {url}: {len(chs)} 个频道")
            else:
                chs = parse_txt(text)
                print(f"  TXT源 {url}: {len(chs)} 个频道")
            all_channels.extend(chs)
    return all_channels

# ====================== 异步测速引擎 ======================
class AsyncSpeedTester:
    def __init__(self):
        self.stats = {'total': 0, 'passed': 0, 'failed': 0,
                      'speeds': [], 'max': 0, 'min': float('inf')}
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
            force_close=False
        )
        self.session = aiohttp.ClientSession(
            connector=conn,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT + 2)
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def test_one(self, url, channel_name):
        """测试单个源，返回速度KB/s，失败返回0"""
        async with self.semaphore:
            try:
                start = time.time()
                async with self.session.get(url, timeout=CHECK_TIMEOUT) as resp:
                    ttfb = time.time() - start
                    if ttfb > 2.5:
                        return 0.0

                    downloaded = 0
                    steady_downloaded = 0
                    steady_start = None
                    chunk_speeds = []
                    chunk_start = time.time()  # 第一个chunk的开始时间

                    async for chunk in resp.content.iter_chunked(32768):
                        now = time.time()
                        chunk_len = len(chunk)
                        if chunk_len == 0:
                            break

                        # 正确计时：本次chunk的下载时间
                        elapsed = now - chunk_start
                        if elapsed > 0.001:  # 过滤瞬时完成
                            chunk_speeds.append(chunk_len / elapsed / 1024)
                        chunk_start = now

                        downloaded += chunk_len

                        # 记录稳定期（超过STEADY_BYTES后的数据）
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
                    if total_time <= 0 or downloaded < 8192:
                        return 0.0

                    # 方法1：总时间法
                    overall_speed = downloaded / total_time / 1024
                    # 方法2：稳定期法
                    steady_speed = 0
                    if steady_downloaded > 0 and steady_start:
                        steady_elapsed = time.time() - steady_start
                        if steady_elapsed > 0:
                            steady_speed = steady_downloaded / steady_elapsed / 1024
                    # 方法3：中位数法
                    median_speed = statistics.median(chunk_speeds) if len(chunk_speeds) >= 3 else overall_speed

                    # 加权融合
                    final_speed = (0.5 * steady_speed +
                                   0.3 * overall_speed +
                                   0.2 * median_speed)

                    # 更新统计
                    self.stats['total'] += 1
                    if final_speed >= SPEED_THRESHOLD:
                        self.stats['passed'] += 1
                    else:
                        self.stats['failed'] += 1
                    self.stats['speeds'].append(final_speed)
                    self.stats['max'] = max(self.stats['max'], final_speed)
                    self.stats['min'] = min(self.stats['min'], final_speed)

                    return final_speed

            except Exception:
                self.stats['failed'] += 1
                return 0.0

    async def batch_test(self, channel_list, template):
        """
        批量并发测速
        channel_list: [(主频道名, URL)]
        template: ChannelTemplate实例
        返回: {主频道: [(url, speed)]} , stats
        """
        # 按主频道分组
        groups = {}
        for main, url in channel_list:
            groups.setdefault(main, []).append(url)

        results = {}
        total_sources = len(channel_list)
        tested = 0

        print(f"\n开始并发测速，最大并发 {MAX_CONCURRENT}，共 {total_sources} 个源")
        print("=" * 70)

        # 按模板分类顺序处理
        for cat in template.categories:
            mains_in_cat = template.category_channels.get(cat, [])
            for main in mains_in_cat:
                if main not in groups:
                    continue
                urls = groups[main]
                # 并发测试该频道所有源
                tasks = [self.test_one(url, main) for url in urls]
                speeds = await asyncio.gather(*tasks)

                # 收集通过的结果
                passed = [(url, sp) for url, sp in zip(urls, speeds) if sp >= SPEED_THRESHOLD]
                passed.sort(key=lambda x: x[1], reverse=True)
                if passed:
                    results[main] = passed

                tested += len(urls)
                passed_now = sum(1 for sp in speeds if sp >= SPEED_THRESHOLD)
                print(f"  {main:<20} 通过 {passed_now}/{len(urls)}  进度 {tested}/{total_sources}")

        return results, self.stats

# ====================== 文件输出 ======================
def save_output(all_channels, template, output_dir='freetv'):
    """保存freetv.txt和freetv.m3u"""
    os.makedirs(output_dir, exist_ok=True)

    utc_now = datetime.now(timezone.utc)
    bj_time = utc_now + timedelta(hours=8)
    time_str = bj_time.strftime('%Y%m%d %H:%M:%S')

    # TXT文件
    txt_path = os.path.join(output_dir, 'freetv.txt')
    txt_lines = ['#genre#', f'更新时间,{time_str}', '']

    # M3U文件
    m3u_path = os.path.join(output_dir, 'freetv.m3u')
    epg_url = 'https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml'
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

    # 写入文件
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))

    total_src = sum(len(v) for v in all_channels.values())
    print(f"\n输出文件：")
    print(f"  {txt_path} ({total_src} 个源)")
    print(f"  {m3u_path} ({total_src} 个源)")

# ====================== 主流程 ======================
async def main():
    print("=" * 55)
    print("IPTV频道源测速工具 (异步并发优化版)")
    print("=" * 55)

    # 1. 加载模板
    template = ChannelTemplate('freetv/dome.txt')
    if not template.load():
        return

    # 2. 获取频道列表（支持多个URL）
    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        #"https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
        # 可添加更多源
    ]
    print("\n从网络源获取频道列表...")
    all_raw = await fetch_channels_from_urls(source_urls)
    print(f"总共获取到 {len(all_raw)} 个频道源")

    if not all_raw:
        print("错误：未获取到任何频道源")
        return

    # 3. 过滤：只保留模板中存在的频道
    known_names = template.get_template_names()
    filtered = [(name, url) for name, url in all_raw if name in known_names]
    print(f"过滤后保留 {len(filtered)} 个（仅模板中存在）")

    if not filtered:
        print("错误：没有找到模板中存在的频道")
        return

    # 4. 标准化名称：别名→主频道
    std_list = [(template.get_main(name), url) for name, url in filtered]
    print(f"标准化后共 {len(std_list)} 个待测源")

    # 5. 异步并发测速
    async with AsyncSpeedTester() as tester:
        results, stats = await tester.batch_test(std_list, template)

    # 6. 输出结果
    print("\n" + "=" * 55)
    print("测速完成！")
    print(f"  总测试源数: {stats['total']}")
    print(f"  通过(≥{SPEED_THRESHOLD}KB/s): {stats['passed']}")
    print(f"  失败: {stats['failed']}")
    if stats['speeds']:
        print(f"  平均速度: {statistics.mean(stats['speeds']):.1f} KB/s")
        print(f"  最高速度: {stats['max']:.1f} KB/s")
        print(f"  最低速度: {stats['min']:.1f} KB/s")
    print(f"  通过频道数: {len(results)}")

    save_output(results, template)

    # 7. 分类统计
    print("\n分类统计：")
    for cat in template.categories:
        mains = template.category_channels.get(cat, [])
        avail = [m for m in mains if m in results]
        src_cnt = sum(len(results[m]) for m in avail)
        print(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_cnt} 源")

    print("\n完成！")

if __name__ == '__main__':
    asyncio.run(main())
