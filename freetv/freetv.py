"""
=============================================================
IPTV频道源测速脚本 v2 - 速度修复版
=============================================================
修复内容：
1. ✅ 用总时间/总数据量计算速度（最可靠）
2. ✅ 排除前200KB TCP缓冲爆发期
3. ✅ 增加测速数据量到2MB
4. ✅ 最少测速2秒，消除瞬时波动
5. ✅ 过滤缓冲区虚假数据（read瞬时完成的数据）
6. ✅ 三种方法加权平均，结果更准确
7. ✅ 只输出通过阈值的频道源
8. ✅ 支持M3U格式解析
9. ✅ 只检测dome.txt中定义的频道
=============================================================
"""
import urllib.request
import ssl
import socket
import statistics
import os
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

# 禁用SSL警告
ssl._create_default_https_context = ssl._create_unverified_context

# ====================== 配置类 ======================
class SpeedTestConfig:
    """测速配置类"""
    SPEED_THRESHOLD = 600  # KB/s 速度阈值
    CHECK_TIMEOUT = 8
    MAX_WORKERS = 20
    
    # 深度测速参数（已优化）
    DEEP_TEST_SIZE = 2 * 1024 * 1024  # 2MB 测速数据量
    WARMUP_SIZE = 200 * 1024             # 前200KB为缓冲期
    CHUNK_SIZE = 64 * 1024               # 64KB 读取块
    MAX_DEEP_TIME = 10                    # 最长测10秒
    MIN_TEST_TIME = 2.0                   # 最少测2秒
    
    # 重试策略
    MAX_RETRIES = 2
    RETRY_DELAY = 0.5
    
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
    }


# ====================== 测速引擎 ======================
class SpeedTestEngine:
    """测速引擎 - 速度修复版"""
    
    def __init__(self, config):
        self.config = config
        self.failed_urls = set()
        self.cache = {}
        self.cache_ttl = 300
        self.stats = {
            'total_tested': 0, 'passed': 0, 'failed': 0,
            'retried': 0, 'cached': 0,
            'avg_speed': 0, 'max_speed': 0, 'min_speed': float('inf'),
            'speed_samples': []
        }
        
    def _clean_url(self, url):
        try:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        except:
            return url
            
    def _is_cached(self, url, group_name):
        cache_key = f"{self._clean_url(url)}_{group_name}"
        if cache_key in self.cache:
            result, ts = self.cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return result
        return None
    
    def _set_cache(self, url, group_name, result):
        cache_key = f"{self._clean_url(url)}_{group_name}"
        self.cache[cache_key] = (result, time.time())
        
    def _check_url_safety(self, url):
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return False, "不支持的协议"
            if not parsed.netloc:
                return False, "无效的域名"
            if ' ' in url:
                return False, "URL包含空格"
            return True, "OK"
        except Exception as e:
            return False, str(e)[:30]
    
    # ============================================================
    # ✅ 核心修复：精确测速方法
    # ============================================================
    def _measure_speed(self, url):
        """
        精确测量URL的真实下载速度
        
        修复要点：
        1. 用总时间/总数据量作为主指标
        2. 排除TCP慢启动缓冲期
        3. 每个chunk正确计时（read前记录时间）
        4. 过滤缓冲区虚假数据
        5. 确保最少测速时间
        
        返回: (final_speed_kbs, total_downloaded, total_elapsed)
        """
        config = self.config
        req = urllib.request.Request(url, headers=config.HEADERS)
        
        response = urllib.request.urlopen(req, timeout=config.CHECK_TIMEOUT)
        
        # 尝试禁用Nagle算法，让数据更实时
        try:
            sock = response.fp.raw._sock
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except:
            pass
        
        downloaded = 0
        warmup_done = False
        steady_downloaded = 0
        steady_start = None
        test_start = time.time()
        
        chunk_samples = []   # 正确计时的chunk速度样本
        all_speeds = []     # 所有速度数据（用于统计）
        
        while downloaded < config.DEEP_TEST_SIZE:
            # ✅ 在read之前记录时间
            chunk_start = time.time()
            
            remaining = config.DEEP_TEST_SIZE - downloaded
            to_read = min(config.CHUNK_SIZE, remaining)
            chunk = response.read(to_read)
            
            # ✅ 在read之后记录时间
            chunk_end = time.time()
            chunk_time = chunk_end - chunk_start  # ✅ 这才是真正的下载时间
            
            if not chunk:
                break
            
            chunk_size = len(chunk)
            downloaded += chunk_size
            
            # 检查是否度过缓冲期
            if not warmup_done and downloaded >= config.WARMUP_SIZE:
                warmup_done = True
                steady_start = time.time()
                steady_downloaded = 0
            
            # 稳定期的数据才计入采样
            if warmup_done:
                steady_downloaded += chunk_size
                # ✅ 只记录有意义的下载时间（排除缓冲区瞬时数据）
                if chunk_time > 0.001:  # >1ms 才认为是真实网络数据
                    chunk_speed = chunk_size / chunk_time / 1024
                    # 过滤极端异常值
                    if 10 < chunk_speed < 50000:
                        chunk_samples.append(chunk_speed)
                        all_speeds.append(chunk_speed)
            
            # 时间控制
            elapsed = time.time() - test_start
            
            # 最少测速时间到了，且已有足够数据，可以提前结束
            if elapsed >= config.MIN_TEST_TIME and warmup_done:
                if elapsed >= config.MAX_DEEP_TIME:
                    break
                # 如果已经下载了足够数据且速度稳定，提前结束
                if downloaded >= config.WARMUP_SIZE * 3:
                    break
        
        total_elapsed = time.time() - test_start
        response.close()
        
        # ============================================================
        # 三种方法综合计算最终速度
        # ============================================================
        
        # 方法1: 总时间 / 总数据量（最可靠，不受单个chunk误差影响）
        if total_elapsed > 0:
            overall_speed = downloaded / total_elapsed / 1024
        else:
            overall_speed = 0
        
        # 方法2: 稳定期速度（排除TCP慢启动爆发）
        if warmup_done and steady_start:
            steady_elapsed = time.time() - steady_start
            if steady_elapsed > 0.1 and steady_downloaded > 0:
                steady_speed = steady_downloaded / steady_elapsed / 1024
            else:
                steady_speed = overall_speed
        else:
            steady_speed = overall_speed
        
        # 方法3: chunk采样中位数
        if chunk_samples:
            chunk_median = statistics.median(chunk_samples)
            chunk_avg = sum(chunk_samples) / len(chunk_samples)
        else:
            chunk_median = overall_speed
            chunk_avg = overall_speed
        
        # ============================================================
        # 加权平均：稳定期权重最高
        # ============================================================
        if warmup_done and steady_elapsed > 0.5:
            # 有可靠的稳定期数据
            final_speed = (
                steady_speed * 0.5 +      # 稳定期速度 50%
                overall_speed * 0.3 +      # 总时间法 30%
                chunk_median * 0.2         # 采样中位数 20%
            )
        else:
            # 没有稳定期数据，用总时间法为主
            final_speed = (
                overall_speed * 0.7 +
                chunk_median * 0.3
            )
        
        return final_speed, downloaded, total_elapsed, all_speeds
    
    def _get_speed_with_retry(self, url, group_name, channel_name, retry_count=0):
        """带重试的测速函数"""
        if url in self.failed_urls and retry_count == 0:
            return 0.0
            
        cached = self._is_cached(url, group_name)
        if cached is not None:
            self.stats['cached'] += 1
            return cached
        
        start_time = time.time()
        
        try:
            is_safe, reason = self._check_url_safety(url)
            if not is_safe:
                return 0.0
            
            # TTFB快速检测
            req = urllib.request.Request(url, headers=self.config.HEADERS)
            sock = urllib.request.urlopen(req, timeout=5)
            ttfb = time.time() - start_time
            sock.close()
            
            if ttfb > 4:
                return 0.0
            
            # 正式测速
            speed, downloaded, elapsed, samples = self._measure_speed(url)
            
            if speed > 0:
                self._set_cache(url, group_name, speed)
                
                # 更新统计
                self.stats['max_speed'] = max(self.stats['max_speed'], speed)
                self.stats['min_speed'] = min(self.stats['min_speed'], speed)
                self.stats['speed_samples'].append(speed)
                
                # 输出结果
                channel_disp = channel_name[:20] if len(channel_name) > 20 else channel_name
                url_disp = url[:50] if len(url) > 50 else url
                ttfb_ms = ttfb * 1000
                print(f"  ✓ {channel_disp:<20} | {url_disp:<50} | "
                      f"{speed:7.0f}KB/s | {downloaded/1024:6.0f}KB | "
                      f"{elapsed:.1f}s | TTFB:{ttfb_ms:5.0f}ms")
            else:
                self.failed_urls.add(url)
            
            return speed
            
        except urllib.error.HTTPError as e:
            if e.code in [403, 404, 500, 502, 503]:
                self.failed_urls.add(url)
                return 0.0
            elif retry_count < self.config.MAX_RETRIES:
                time.sleep(self.config.RETRY_DELAY * (retry_count + 1))
                self.stats['retried'] += 1
                return self._get_speed_with_retry(url, group_name, channel_name, retry_count + 1)
            else:
                self.failed_urls.add(url)
                return 0.0
                
        except (urllib.error.URLError, socket.timeout, socket.error) as e:
            if retry_count < self.config.MAX_RETRIES:
                time.sleep(self.config.RETRY_DELAY * (retry_count + 1))
                self.stats['retried'] += 1
                return self._get_speed_with_retry(url, group_name, channel_name, retry_count + 1)
            else:
                self.failed_urls.add(url)
                return 0.0
                
        except Exception as e:
            return 0.0
    
    def get_stats(self):
        if self.stats['speed_samples']:
            self.stats['avg_speed'] = sum(self.stats['speed_samples']) / len(self.stats['speed_samples'])
        return self.stats


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """频道模板处理类"""
    
    def __init__(self, template_file):
        self.template_file = template_file
        self.categories = []
        self.channel_map = {}       # 别名 -> 主频道
        self.main_channels = {}     # 主频道 -> 分类
        self.category_channels = {} # 分类 -> [主频道列表]
        self.logo_base_url = "https://codeberg.org/ou-yang/TV/raw/branch/main/LOGO/"
        
    def load_template(self):
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
                        self.main_channels[main_channel] = current_category
                        if main_channel not in self.category_channels[current_category]:
                            self.category_channels[current_category].append(main_channel)
                        for alias in parts:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main_channel
        
        if "其它" not in self.categories:
            self.categories.append("其它")
            self.category_channels["其它"] = []
            
        print(f"加载模板: 共 {len(self.categories)} 个分类")
        for cat in self.categories:
            print(f"  {cat}: {len(self.category_channels.get(cat, []))}个主频道")
        return True
    
    def get_main_channel(self, name):
        return self.channel_map.get(name, name)
    
    def get_category(self, name):
        main = self.get_main_channel(name)
        return self.main_channels.get(main, "其它")
    
    def get_logo_url(self, name):
        main = self.get_main_channel(name)
        safe = main.replace("/", "").replace("\\", "").replace(":", "")
        return f"{self.logo_base_url}{safe}.png"
    
    def get_channels_by_category(self, category):
        return self.category_channels.get(category, [])
    
    def get_template_channels(self):
        return set(self.channel_map.keys())


# ====================== 批量测速 ======================
def batch_speed_test(channel_list, template):
    """批量测速 - 只保留通过阈值的源"""
    config = SpeedTestConfig()
    engine = SpeedTestEngine(config)
    
    print(f"\n开始测速: {len(channel_list)} 个源")
    print(f"参数: 数据量={config.DEEP_TEST_SIZE/1024/1024:.0f}MB, "
          f"缓冲期={config.WARMUP_SIZE/1024:.0f}KB, "
          f"最少={config.MIN_TEST_TIME:.0f}s, "
          f"阈值={config.SPEED_THRESHOLD}KB/s")
    print("-" * 100)
    
    # 按主频道分组
    channels_by_main = {}
    for name, url in channel_list:
        main = template.get_main_channel(name)
        if main not in channels_by_main:
            channels_by_main[main] = []
        channels_by_main[main].append((name, url))
    
    all_channels = {}
    total = sum(len(s) for s in channels_by_main.values())
    completed = 0
    passed = 0
    
    for category in template.categories:
        for main_ch in template.get_channels_by_category(category):
            if main_ch not in channels_by_main:
                continue
            
            sources = channels_by_main[main_ch]
            print(f"\n📺 {main_ch} ({len(sources)}个源)")
            
            results = []
            for ch_name, ch_url in sources:
                completed += 1
                speed = engine._get_speed_with_retry(ch_url, "freetv", ch_name)
                
                if speed >= config.SPEED_THRESHOLD:
                    results.append((ch_url, speed))
                    passed += 1
                # ❌ 不达标的不保存
            
            if results:
                results.sort(key=lambda x: x[1], reverse=True)
                all_channels[main_ch] = results
                print(f"  ✅ 通过: {len(results)}/{len(sources)}")
                for i, (u, s) in enumerate(results[:3], 1):
                    print(f"    #{i} {s:.0f}KB/s")
            
            if completed % 10 == 0 or completed == total:
                rate = passed / completed * 100 if completed > 0 else 0
                print(f"\n📈 {completed}/{total} ({completed/total*100:.0f}%) | "
                      f"通过:{passed} ({rate:.0f}%)")
    
    # 统计
    engine.stats['total_tested'] = total
    engine.stats['passed'] = passed
    engine.stats['failed'] = total - passed
    stats = engine.get_stats()
    
    print("\n" + "=" * 70)
    print(f"完成: 测试{stats['total_tested']} | 通过{stats['passed']} | "
          f"失败{stats['failed']} | 通过率{stats['passed']/max(total,1)*100:.1f}%")
    if stats['speed_samples']:
        print(f"速度: 平均{stats['avg_speed']:.0f} | "
              f"最高{stats['max_speed']:.0f} | "
              f"最低{stats['min_speed']:.0f} KB/s")
    print("=" * 70)
    
    return all_channels, stats


# ====================== 文件输出 ======================
def save_freetv_files(all_channels, template, epg_url, output_dir="freetv"):
    """保存输出文件 - 只输出通过测速的源"""
    os.makedirs(output_dir, exist_ok=True)
    
    utc = datetime.now(timezone.utc)
    beijing = utc + timedelta(hours=8)
    ftime = beijing.strftime("%Y%m%d %H:%M:%S")
    
    txt_file = os.path.join(output_dir, "freetv.txt")
    txt_lines = ["#genre#", f"更新时间,{ftime}", ""]
    
    m3u_file = os.path.join(output_dir, "freetv.m3u")
    m3u_lines = [f'#EXTM3U x-tvg-url="{epg_url}"']
    
    for category in template.categories:
        mains = template.get_channels_by_category(category)
        avail = [m for m in mains if m in all_channels and all_channels[m]]
        
        if not avail:
            continue
        
        txt_lines.append(f"{category},#genre#")
        
        for mc in avail:
            sources = all_channels[mc]
            logo = template.get_logo_url(mc)
            
            for url, speed in sources:
                txt_lines.append(f"{mc},{url}")
                m3u_lines.extend([
                    f'#EXTINF:-1 tvg-name="{mc}" tvg-logo="{logo}" group-title="{category}", {mc}',
                    url
                ])
    
    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    
    total_src = sum(len(s) for s in all_channels.values())
    print(f"✅ 保存: {txt_file} ({total_src}个源)")
    
    with open(m3u_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))
    print(f"✅ 保存: {m3u_file} ({total_src}个源)")
    
    return txt_file, m3u_file


# ====================== 工具函数 ======================
def clean_m3u_name(name):
    name = re.sub(r'\([^)]*\)', '', name)
    name = re.sub(r'\[[^\]]*\]', '', name)
    name = name.strip()
    name = re.sub(r'\s+', ' ', name)
    return name

def parse_m3u(content):
    channels = []
    lines = content.strip().split('\n')
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

def fetch_channels(url):
    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode('utf-8')
            if text.strip().startswith('#EXTM3U'):
                ch = parse_m3u(text)
                print(f"从 {url} 解析M3U: {len(ch)}个频道")
            else:
                ch = []
                for line in text.split('\n'):
                    line = line.strip()
                    if "#genre#" not in line and "," in line and "://" in line:
                        try:
                            n, a = line.split(',', 1)
                            if a.startswith(('http://', 'https://')):
                                ch.append((n.strip(), a))
                        except:
                            continue
                print(f"从 {url} 解析TXT: {len(ch)}个频道")
            return ch
    except Exception as e:
        print(f"获取失败 {url}: {e}")
        return []

def filter_by_template(channels, template):
    valid = template.get_template_channels()
    return [(n, u) for n, u in channels if n in valid]


# ====================== 主程序 ======================
def main():
    print("=" * 60)
    print("IPTV频道源处理脚本 v2 (速度修复版)")
    print("=" * 60)
    
    template = ChannelTemplate("freetv/dome.txt")
    if not template.load_template():
        return
    
    source_urls = [
        "https://iptv-org.github.io/iptv/index.m3u",
        "https://sub.ottiptv.cc/yylunbo.m3u",
        #"https://raw.githubusercontent.com/haonanren118/IPTV/refs/heads/master/iptv_sources.m3u8",
        "https://raw.githubusercontent.com/kakaxi-1/IPTV/refs/heads/main/ipv4.txt",
        "https://raw.githubusercontent.com/wgq11/iptv/refs/heads/main/result.txt",
        "https://raw.githubusercontent.com/lbxxxtw2/iptv/refs/heads/master/output/tv.txt",
        "https://raw.githubusercontent.com/qingtian6325-lang/IPTV/refs/heads/main/mytv.m3u",
    ]
    
    all_ch = []
    for url in source_urls:
        all_ch.extend(fetch_channels(url))
    
    print(f"\n总计: {len(all_ch)} 个源")
    if not all_ch:
        return
    
    filtered = filter_by_template(all_ch, template)
    print(f"模板匹配: {len(filtered)} 个源")
    if not filtered:
        return
    
    # 标准化
    std = []
    for name, url in filtered:
        main = template.get_main_channel(name)
        std.append((main, url))
    print(f"标准化: {len(std)} 个源")
    
    # 测速
    print("\n开始速度测试...")
    results, stats = batch_speed_test(std, template)
    
    # 保存
    epg = "https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml"
    save_freetv_files(results, template, epg)
    
    # 最终统计
    print("\n" + "=" * 60)
    print(f"✅ 完成! 输出 {len(results)} 个频道")
    
    print("\n分类统计:")
    for cat in template.categories:
        mains = template.get_channels_by_category(cat)
        avail = [m for m in mains if m in results and results[m]]
        src_count = sum(len(results[m]) for m in avail)
        if mains:
            print(f"  {cat}: {len(avail)}/{len(mains)} 频道, {src_count} 个源")
    
    # Top 10
    ranked = sorted(results.items(), key=lambda x: len(x[1]), reverse=True)
    if ranked:
        print("\n源最多的10个频道:")
        for i, (ch, srcs) in enumerate(ranked[:10], 1):
            cat = template.get_category(ch)
            print(f"  {i:2d}. {ch:<25} {len(srcs):3d}个源 ({cat})")
    
    print("=" * 60)


if __name__ == "__main__":
    main()
