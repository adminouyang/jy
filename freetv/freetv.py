import aiohttp
import asyncio
import ssl
import socket
import statistics
import os
import re
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
import dns.resolver
from concurrent.futures import ThreadPoolExecutor

# 配置DNS缓存
dns_cache = {}
dns_resolver = dns.resolver.Resolver()

# ====================== 配置类 ======================
class SpeedTestConfig:
    """测速配置类"""
    # 测速阈值
    SPEED_THRESHOLD = 600  # KB/s
    CHECK_TIMEOUT = 5
    MAX_CONCURRENT = 50  # 最大并发数
    
    # 深度测速参数
    DEEP_TEST_SIZE = 512 * 1024  # 512KB（减少测试数据量）
    CHUNK_SIZE = 32 * 1024  # 32KB
    MAX_DEEP_TIME = 0.8  # 减少最大测试时间
    
    # 重试策略
    MAX_RETRIES = 1  # 减少重试次数
    RETRY_DELAY = 0.3
    
    # 连接池参数
    CONNECTOR_LIMIT = 100
    CONNECTOR_TTL = 300
    
    # 头信息
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'close',  # 使用短连接避免连接保持的开销
        'Cache-Control': 'no-cache',
    }


# ====================== 测速引擎 ======================
class SpeedTestEngine:
    """测速引擎类 - 异步版本"""
    
    def __init__(self, config):
        self.config = config
        self.speed_results = {}
        self.failed_urls = set()
        self.cache = {}
        self.cache_ttl = 300
        self.stats = {
            'total_tested': 0,
            'passed': 0,
            'failed': 0,
            'retried': 0,
            'cached': 0,
            'avg_speed': 0,
            'max_speed': 0,
            'min_speed': float('inf'),
            'speed_samples': []
        }
        self.session = None
        self.connector = None
        
    async def __aenter__(self):
        """异步上下文管理器入口"""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        self.connector = aiohttp.TCPConnector(
            limit=self.config.CONNECTOR_LIMIT,
            ttl_dns_cache=self.config.CONNECTOR_TTL,
            ssl=ssl_context,
            force_close=True,  # 关闭连接池保持
            enable_cleanup_closed=True
        )
        
        self.session = aiohttp.ClientSession(
            connector=self.connector,
            headers=self.config.HEADERS,
            timeout=aiohttp.ClientTimeout(
                total=self.config.CHECK_TIMEOUT + 2,
                connect=self.config.CHECK_TIMEOUT,
                sock_read=self.config.CHECK_TIMEOUT
            )
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if self.session:
            await self.session.close()
        if self.connector:
            await self.connector.close()
        
    def _clean_url(self, url):
        """清理URL参数，用于去重"""
        try:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except:
            return url
            
    def _is_cached(self, url, group_name):
        """检查是否有缓存结果"""
        cache_key = f"{self._clean_url(url)}_{group_name}"
        if cache_key in self.cache:
            result, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return result
        return None
    
    def _set_cache(self, url, group_name, result):
        """设置缓存"""
        cache_key = f"{self._clean_url(url)}_{group_name}"
        self.cache[cache_key] = (result, time.time())
        
    def _check_url_safety(self, url):
        """URL安全检查"""
        try:
            parsed = urlparse(url)
            if not parsed.scheme in ('http', 'https'):
                return False, "不支持的协议"
            if not parsed.netloc:
                return False, "无效的域名"
            if ' ' in url:
                return False, "URL包含空格"
            return True, "OK"
        except Exception as e:
            return False, f"URL解析失败: {str(e)[:30]}"
    
    async def _dns_lookup(self, domain):
        """DNS解析（带缓存）"""
        if domain in dns_cache:
            return dns_cache[domain]
        
        try:
            answers = dns_resolver.resolve(domain, 'A')
            ips = [str(r) for r in answers]
            dns_cache[domain] = ips
            return ips
        except:
            dns_cache[domain] = []
            return []
    
    async def _get_speed_with_retry(self, url, group_name, channel_name, retry_count=0):
        """带重试的异步测速函数"""
        if url in self.failed_urls and retry_count == 0:
            return 0.0
            
        cached_result = self._is_cached(url, group_name)
        if cached_result is not None:
            self.stats['cached'] += 1
            return cached_result
        
        # DNS预解析
        try:
            domain = urlparse(url).netloc
            await self._dns_lookup(domain)
        except:
            pass
            
        start_time = time.time()
        
        try:
            is_safe, reason = self._check_url_safety(url)
            if not is_safe:
                return 0.0
            
            # 动态调整超时
            dynamic_timeout = min(self.config.CHECK_TIMEOUT, 3.0)
            
            async with self.session.get(
                url, 
                timeout=aiohttp.ClientTimeout(total=dynamic_timeout),
                allow_redirects=True,
                max_redirects=2
            ) as response:
                ttfb = time.time() - start_time
                
                if ttfb > 2:  # 放宽TTFB阈值
                    return 0.0
                
                result = await self._deep_speed_test(url, response, ttfb, channel_name)
                
                if result > 0:
                    self._set_cache(url, group_name, result)
                    
                return result
                
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if retry_count < self.config.MAX_RETRIES:
                await asyncio.sleep(self.config.RETRY_DELAY)
                self.stats['retried'] += 1
                return await self._get_speed_with_retry(url, group_name, channel_name, retry_count + 1)
            else:
                self.failed_urls.add(url)
                return 0.0
        except Exception as e:
            return 0.0
    
    async def _deep_speed_test(self, url, response, ttfb, channel_name):
        """深度测速实现 - 异步版本"""
        downloaded = 0
        speed_samples = []
        test_start = time.time()
        
        try:
            async for chunk in response.content.iter_chunked(self.config.CHUNK_SIZE):
                if not chunk:
                    break
                    
                chunk_time = time.time() - test_start
                if chunk_time > 0:
                    chunk_speed = len(chunk) / chunk_time / 1024
                    speed_samples.append(chunk_speed)
                
                downloaded += len(chunk)
                
                if downloaded >= self.config.DEEP_TEST_SIZE:
                    break
                    
                if time.time() - test_start > self.config.MAX_DEEP_TIME:
                    break
            
            if not speed_samples or downloaded == 0:
                return 0.0
            
            # 简化速度计算
            if len(speed_samples) >= 3:
                # 取后50%的数据，避免初始连接的影响
                start_idx = max(0, len(speed_samples) // 2)
                valid_speeds = speed_samples[start_idx:]
                if valid_speeds:
                    avg_speed = sum(valid_speeds) / len(valid_speeds)
                else:
                    avg_speed = statistics.median(speed_samples) if speed_samples else 0
            else:
                avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else 0
            
            if avg_speed == 0:
                return 0.0
            
            # 计算稳定性
            if len(speed_samples) > 1:
                try:
                    speed_std = statistics.stdev(speed_samples) if len(speed_samples) >= 2 else 0
                    stability = 1.0 - min(speed_std / avg_speed, 1.0)
                except:
                    stability = 1.0
            else:
                stability = 1.0
            
            final_speed = avg_speed * (0.8 + 0.2 * stability)  # 调整权重
            
            # 更新统计
            self.stats['max_speed'] = max(self.stats['max_speed'], final_speed)
            self.stats['min_speed'] = min(self.stats['min_speed'], final_speed)
            self.stats['speed_samples'].append(final_speed)
            
            ttfb_ms = ttfb * 1000
            
            # 输出结果
            channel_display = channel_name[:20] if len(channel_name) > 20 else channel_name
            url_display = url[:50] if len(url) > 50 else url
            
            print(f"  ✓ {channel_display:<20} | {url_display:<50} | "
                  f"{final_speed:7.1f}KB/s | TTFB: {ttfb_ms:4.0f}ms")
            
            return final_speed
            
        except Exception as e:
            return 0.0
    
    def get_stats(self):
        """获取统计信息"""
        if self.stats['speed_samples']:
            self.stats['avg_speed'] = sum(self.stats['speed_samples']) / len(self.stats['speed_samples'])
        return self.stats


# ====================== 批量测速函数 ======================
async def batch_speed_test_optimized(channel_list, template):
    """优化的批量测速函数 - 异步版本"""
    config = SpeedTestConfig()
    
    print(f"开始对 {len(channel_list)} 个频道源进行速度测试...")
    print("-" * 100)
    
    # 按主频道分组
    channels_by_main = {}
    for channel_name, channel_url in channel_list:
        main_channel = template.get_main_channel(channel_name)
        if main_channel not in channels_by_main:
            channels_by_main[main_channel] = []
        channels_by_main[main_channel].append((channel_name, channel_url))
    
    all_channels = {}  # 主频道 -> 列表[(URL, 速度)]
    total_to_test = sum(len(sources) for sources in channels_by_main.values())
    completed = 0
    passed_count = 0
    
    # 创建测速引擎
    async with SpeedTestEngine(config) as engine:
        # 按照模板中的分类和主频道顺序进行测试
        for category in template.categories:
            main_channels_in_category = template.get_channels_by_category(category)
            
            for main_channel in main_channels_in_category:
                if main_channel not in channels_by_main:
                    continue
                    
                sources = channels_by_main[main_channel]
                
                print(f"\n📺 测试频道: {main_channel} ({len(sources)} 个源)")
                print("-" * 100)
                
                # 并发测试这个频道的所有源
                tasks = []
                for channel_name, channel_url in sources:
                    task = engine._get_speed_with_retry(channel_url, "freetv", channel_name)
                    tasks.append((task, channel_url))
                
                # 限制并发数
                batch_size = min(config.MAX_CONCURRENT, len(tasks))
                source_results = []
                
                for i in range(0, len(tasks), batch_size):
                    batch = tasks[i:i + batch_size]
                    batch_tasks = [task for task, _ in batch]
                    batch_urls = [url for _, url in batch]
                    
                    # 执行批量测试
                    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                    
                    for j, (result, url) in enumerate(zip(batch_results, batch_urls)):
                        completed += 1
                        
                        if isinstance(result, Exception) or result == 0.0:
                            speed = 0.0
                        else:
                            speed = result
                        
                        if speed >= config.SPEED_THRESHOLD:
                            source_results.append((url, speed))
                            passed_count += 1
                        
                        # 进度显示
                        if completed % 10 == 0 or completed == total_to_test:
                            pass_rate = (passed_count / completed * 100) if completed > 0 else 0
                            print(f"\n📈 进度: {completed}/{total_to_test} 个源 ({completed/total_to_test*100:.1f}%) | "
                                  f"通过: {passed_count} 个源 ({pass_rate:.1f}%)")
                            print("-" * 100)
                
                # 按速度从大到小排序
                if source_results:
                    source_results.sort(key=lambda x: x[1], reverse=True)
                    all_channels[main_channel] = source_results
                    
                    # 显示这个频道的测试结果摘要
                    print(f"\n  📊 {main_channel} 测试结果: 通过 {len(source_results)}/{len(sources)} 个源")
                    for i, (url, speed) in enumerate(source_results[:3], 1):  # 只显示前3个
                        print(f"    ✅ 第{i}名: {speed:7.1f}KB/s")
        
        # 计算最终统计
        engine.stats['total_tested'] = total_to_test
        engine.stats['passed'] = passed_count
        engine.stats['failed'] = total_to_test - passed_count
        stats = engine.get_stats()
        
        print("\n" + "=" * 100)
        print("🎉 速度测试完成!")
        print("=" * 100)
        print(f"📊 统计信息:")
        print(f"  总计测试: {stats['total_tested']} 个频道源")
        print(f"  通过测试: {stats['passed']} 个源 (速度 ≥ {config.SPEED_THRESHOLD} KB/s)")
        print(f"  失败: {stats['failed']} 个源")
        
        if stats['total_tested'] > 0:
            pass_rate = stats['passed'] / stats['total_tested'] * 100
            print(f"  通过率: {pass_rate:.1f}%")
            print(f"  平均速度: {stats['avg_speed']:.1f} KB/s")
            print(f"  最高速度: {stats['max_speed']:.1f} KB/s")
            print(f"  最低速度: {stats['min_speed']:.1f} KB/s" if stats['min_speed'] != float('inf') else "  最低速度: 0.0 KB/s")
        
        print("=" * 100)
        
        return all_channels, stats


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """频道模板处理类"""
    
    def __init__(self, template_file):
        self.template_file = template_file
        self.categories = []
        self.channel_map = {}
        self.main_channels = {}
        self.category_channels = {}
        self.logo_base_url = "https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/"
        
    def load_template(self):
        """加载频道模板文件"""
        if not os.path.exists(self.template_file):
            print(f"错误: 模板文件 {self.template_file} 不存在")
            return False
            
        current_category = None
        
        with open(self.template_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                    
                if "📡" in line and "#genre#" in line:
                    parts = line.split('#genre#')
                    if len(parts) > 0:
                        current_category = parts[0].replace("📡", "").strip()
                        if current_category and current_category not in self.categories:
                            self.categories.append(current_category)
                            self.category_channels[current_category] = []
                elif current_category and "," in line:
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) > 0:
                        main_channel = parts[0]
                        aliases = parts
                        
                        self.main_channels[main_channel] = current_category
                        
                        if main_channel not in self.category_channels[current_category]:
                            self.category_channels[current_category].append(main_channel)
                        
                        for alias in aliases:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main_channel
        
        if "其它" not in self.categories:
            self.categories.append("其它")
            self.category_channels["其它"] = []
            
        print(f"加载模板: 共 {len(self.categories)} 个分类")
        for category in self.categories:
            print(f"  {category}: {len(self.category_channels.get(category, []))} 个主频道")
        
        return True
    
    def get_main_channel(self, channel_name):
        """根据别名获取主频道名称"""
        return self.channel_map.get(channel_name, channel_name)
    
    def get_category(self, channel_name):
        """根据频道名称获取分类"""
        main_channel = self.get_main_channel(channel_name)
        return self.main_channels.get(main_channel, "其它")
    
    def get_logo_url(self, channel_name):
        """获取频道台标URL"""
        main_channel = self.get_main_channel(channel_name)
        safe_name = main_channel.replace("/", "").replace("\\", "").replace(":", "")
        return f"{self.logo_base_url}{safe_name}.png"
    
    def get_all_main_channels(self):
        """获取所有主频道（按分类和顺序）"""
        result = []
        for category in self.categories:
            if category in self.category_channels:
                result.extend(self.category_channels[category])
        return result
    
    def get_channels_by_category(self, category):
        """获取指定分类下的所有主频道"""
        return self.category_channels.get(category, [])
    
    def get_template_channels(self):
        """获取模板中的所有频道名称（主频道+别名）"""
        return set(self.channel_map.keys())


# ====================== 文件输出 ======================
def save_freetv_files(all_channels, template, epg_url, output_dir="freetv"):
    """保存输出文件 - 每个频道输出多个源，按速度排序"""
    
    os.makedirs(output_dir, exist_ok=True)
    
    utc_time = datetime.now(timezone.utc)
    beijing_time = utc_time + timedelta(hours=8)
    formatted_time = beijing_time.strftime("%Y%m%d %H:%M:%S")
    
    # 保存freetv.txt文件
    txt_file = os.path.join(output_dir, "freetv.txt")
    txt_lines = ["#genre#", f"更新时间,{formatted_time}", ""]
    
    # 保存freetv.m3u文件
    m3u_file = os.path.join(output_dir, "freetv.m3u")
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg_url}"']
    
    # 按分类和模板顺序输出
    for category in template.categories:
        main_channels_in_category = template.get_channels_by_category(category)
        
        available_channels_in_category = []
        for main_channel in main_channels_in_category:
            if main_channel in all_channels and all_channels[main_channel]:
                available_channels_in_category.append(main_channel)
        
        if not available_channels_in_category:
            continue
        
        txt_lines.append(f"{category} #genre#")
        
        for main_channel in available_channels_in_category:
            sources = all_channels[main_channel]
            
            for url, speed in sources:
                txt_lines.append(f"{main_channel},{url}")
            
            logo_url = template.get_logo_url(main_channel)
            for url, speed in sources:
                m3u_lines.extend([
                    f'#EXTINF:-1 tvg-name="{main_channel}" tvg-logo="{logo_url}" group-title="{category}",{main_channel}',
                    url
                ])
    
    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    
    total_sources = sum(len(sources) for sources in all_channels.values())
    print(f"已保存: {txt_file} ({total_sources} 个频道源)")
    
    with open(m3u_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))
    
    print(f"已保存: {m3u_file} ({total_sources} 个频道源)")
    
    return txt_file, m3u_file


# ====================== 主程序 ======================
def clean_m3u_channel_name(name):
    """清理M3U格式的频道名称，去除括号和分辨率信息"""
    cleaned = re.sub(r'\([^)]*\)', '', name)
    cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)
    cleaned = cleaned.strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned

def parse_m3u_content(content):
    """解析M3U格式的内容"""
    channels = []
    lines = content.strip().split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        if not line or (line.startswith('#') and not line.startswith('#EXTINF')):
            i += 1
            continue
            
        if line.startswith('#EXTINF'):
            parts = line.split(',')
            if len(parts) >= 2:
                channel_name = parts[-1].strip()
                channel_name = clean_m3u_channel_name(channel_name)
                
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or lines[j].startswith('#')):
                    j += 1
                    
                if j < len(lines):
                    url = lines[j].strip()
                    if url and (url.startswith('http://') or url.startswith('https://')):
                        channels.append((channel_name, url))
                        i = j
        i += 1
    
    return channels

async def fetch_channel_list_from_url(url, session):
    """从单个URL获取频道列表，异步版本"""
    try:
        async with session.get(url, timeout=10) as response:
            text = await response.text()
            
            if text.strip().startswith('#EXTM3U'):
                channels = parse_m3u_content(text)
                print(f"从 {url} 解析M3U格式: {len(channels)} 个有效频道")
            else:
                channels = []
                for line in text.split('\n'):
                    line = line.strip()
                    if "#genre#" not in line and "," in line and "://" in line:
                        try:
                            channel_name, channel_address = line.split(',', 1)
                            if channel_address.startswith(('http://', 'https://')):
                                clean_name = re.sub(r'^\[[A-Z0-9]+\]\s*', '', channel_name).strip()
                                channels.append((clean_name, channel_address))
                        except:
                            continue
                print(f"从 {url} 解析TXT格式: {len(channels)} 个有效频道")
                
            return channels
            
    except Exception as e:
        print(f"获取频道列表失败 {url}: {e}")
        return []

def filter_channels_by_template(channels, template):
    """过滤频道，只保留模板中存在的频道（主频道或别名）"""
    template_channels = template.get_template_channels()
    filtered_channels = []
    
    for channel_name, channel_url in channels:
        if channel_name in template_channels:
            filtered_channels.append((channel_name, channel_url))
    
    return filtered_channels

async def main_async():
    """主程序 - 异步版本"""
    config = SpeedTestConfig()
    
    print("=" * 60)
    print("IPTV频道源处理脚本 (异步优化版本)")
    print("=" * 60)
    
    template = ChannelTemplate("freetv/dome.txt")
    if not template.load_template():
        return
    
    print("\n从多个网络源获取频道列表...")
    
    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        "https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt"
        # "https://freetv.fun/test_channels_original_new.txt"
        # 可以继续添加更多源URL
    ]
    
    all_channels = []
    
    # 使用异步session获取频道列表
    async with aiohttp.ClientSession() as session:
        for url in source_urls:
            channels = await fetch_channel_list_from_url(url, session)
            all_channels.extend(channels)
    
    print(f"合并后总共有 {len(all_channels)} 个频道源")
    
    if not all_channels:
        print("错误: 没有获取到任何频道源")
        return
    
    print("\n过滤频道，只保留dome.txt中存在的频道...")
    filtered_channels = filter_channels_by_template(all_channels, template)
    
    print(f"过滤后保留: {len(filtered_channels)} 个频道源")
    
    if not filtered_channels:
        print("错误: 没有找到模板中存在的频道源")
        return
    
    print("\n使用模板标准化频道名称...")
    standardized_channels = []
    
    for channel_name, channel_url in filtered_channels:
        main_channel = template.get_main_channel(channel_name)
        standardized_channels.append((main_channel, channel_url))
    
    print(f"标准化后频道: {len(standardized_channels)} 个")
    
    print("\n开始速度测试...")
    all_channels_with_speeds, stats = await batch_speed_test_optimized(standardized_channels, template)
    
    print("\n生成输出文件...")
    epg_url = "https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml"
    txt_file, m3u_file = save_freetv_files(all_channels_with_speeds, template, epg_url)
    
    print("\n" + "=" * 60)
    print("处理完成!")
    print("=" * 60)
    
    channel_source_counts = []
    for main_channel, sources in all_channels_with_speeds.items():
        channel_source_counts.append((main_channel, len(sources)))
    
    channel_source_counts.sort(key=lambda x: x[1], reverse=True)
    
    print(f"原始源数: {len(all_channels)}")
    print(f"模板匹配源数: {len(filtered_channels)}")
    print(f"通过测速的频道数: {len(all_channels_with_speeds)}")
    print(f"通过测速的源数: {sum(len(sources) for sources in all_channels_with_speeds.values())}")
    
    if len(all_channels) > 0:
        match_rate = len(filtered_channels) / len(all_channels) * 100
        print(f"模板匹配率: {match_rate:.1f}%")
    
    if len(filtered_channels) > 0:
        pass_rate = sum(len(sources) for sources in all_channels_with_speeds.values()) / len(filtered_channels) * 100
        print(f"通过率: {pass_rate:.1f}%")
    
    print("\n分类统计:")
    for category in template.categories:
        if category in template.category_channels:
            total_channels = len(template.category_channels[category])
            available_channels = [c for c in template.category_channels[category] if c in all_channels_with_speeds and all_channels_with_speeds[c]]
            available_sources = sum(len(all_channels_with_speeds[c]) for c in available_channels)
            if total_channels > 0:
                print(f"  {category}: {len(available_channels)}/{total_channels} 个频道, {available_sources} 个源")
    
    if channel_source_counts:
        print("\n源最多的10个频道:")
        for i, (channel, count) in enumerate(channel_source_counts[:10], 1):
            category = template.get_category(channel)
            print(f"  {i:2d}. {channel[:30]:<30} - {count:3d} 个源 ({category})")
    
    print(f"\n输出文件:")
    print(f"  {txt_file}")
    print(f"  {m3u_file}")
    print("=" * 60)


def main():
    """主程序入口"""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
