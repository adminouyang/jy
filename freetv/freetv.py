import urllib.request
import ssl
import socket
import statistics
import os
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
import concurrent.futures
import time

# 禁用SSL警告
ssl._create_default_https_context = ssl._create_unverified_context

# ====================== 配置类 ======================
class SpeedTestConfig:
    """测速配置类"""
    # 测速阈值
    SPEED_THRESHOLD = 100  # KB/s
    CHECK_TIMEOUT = 5
    MAX_WORKERS = 20
    
    # 深度测速参数
    DEEP_TEST_SIZE = 1024 * 1024  # 1MB
    CHUNK_SIZE = 64 * 1024  # 64KB
    MAX_DEEP_TIME = 1.2
    
    # 重试策略
    MAX_RETRIES = 2
    RETRY_DELAY = 0.5
    
    # 头信息
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
    }


# ====================== 测速引擎 ======================
class SpeedTestEngine:
    """测速引擎类"""
    
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
            
    def _get_speed_with_retry(self, url, group_name, retry_count=0):
        """带重试的测速函数"""
        if url in self.failed_urls and retry_count == 0:
            return 0.0
            
        cached_result = self._is_cached(url, group_name)
        if cached_result is not None:
            self.stats['cached'] += 1
            return cached_result
            
        is_deep = True  # 所有频道都进行深度测速
        start_time = time.time()
        
        try:
            is_safe, reason = self._check_url_safety(url)
            if not is_safe:
                return 0.0
                
            req = urllib.request.Request(url, headers=self.config.HEADERS)
            response = urllib.request.urlopen(req, timeout=self.config.CHECK_TIMEOUT)
            
            ttfb = time.time() - start_time
            
            if ttfb > 3:
                response.close()
                return 0.0
                
            result = self._deep_speed_test(url, response, ttfb)
            
            if result > 0:
                self._set_cache(url, group_name, result)
                
            return result
            
        except urllib.error.HTTPError as e:
            if e.code in [403, 404, 500, 502, 503]:
                self.failed_urls.add(url)
                return 0.0
            elif retry_count < self.config.MAX_RETRIES:
                time.sleep(self.config.RETRY_DELAY * (retry_count + 1))
                self.stats['retried'] += 1
                return self._get_speed_with_retry(url, group_name, retry_count + 1)
            else:
                return 0.0
                
        except (urllib.error.URLError, socket.timeout) as e:
            if retry_count < self.config.MAX_RETRIES:
                time.sleep(self.config.RETRY_DELAY * (retry_count + 1))
                self.stats['retried'] += 1
                return self._get_speed_with_retry(url, group_name, retry_count + 1)
            else:
                self.failed_urls.add(url)
                return 0.0
                
        except Exception as e:
            return 0.0
                
    def _deep_speed_test(self, url, response, ttfb):
        """深度测速实现"""
        downloaded = 0
        speed_samples = []
        test_start = time.time()
        
        try:
            while downloaded < self.config.DEEP_TEST_SIZE:
                chunk_start = time.time()
                chunk = response.read(min(self.config.CHUNK_SIZE, 
                                        self.config.DEEP_TEST_SIZE - downloaded))
                if not chunk:
                    break
                    
                chunk_time = time.time() - chunk_start
                if chunk_time > 0:
                    chunk_speed = len(chunk) / chunk_time / 1024
                    speed_samples.append(chunk_speed)
                
                downloaded += len(chunk)
                
                if time.time() - test_start > self.config.MAX_DEEP_TIME:
                    break
                    
            response.close()
            
            if not speed_samples or downloaded == 0:
                return 0.0
                
            if len(speed_samples) >= 5:
                median_speed = statistics.median(speed_samples)
                filtered_speeds = [s for s in speed_samples if 0.5 <= s/median_speed <= 2.0]
                if filtered_speeds:
                    avg_speed = sum(filtered_speeds) / len(filtered_speeds)
                else:
                    avg_speed = median_speed
            else:
                avg_speed = sum(speed_samples) / len(speed_samples)
                
            if len(speed_samples) > 1:
                try:
                    speed_std = statistics.stdev(speed_samples) if len(speed_samples) >= 2 else 0
                    stability = 1.0 - min(speed_std / avg_speed, 1.0) if avg_speed > 0 else 0
                except:
                    stability = 1.0
            else:
                stability = 1.0
                
            final_speed = avg_speed * (0.7 + 0.3 * stability)
            
            self.stats['max_speed'] = max(self.stats['max_speed'], final_speed)
            self.stats['min_speed'] = min(self.stats['min_speed'], final_speed)
            self.stats['speed_samples'].append(final_speed)
            
            ttfb_ms = ttfb * 1000
            print(f"  ✓ 下载: {downloaded/1024:6.1f}KB | 速度: {final_speed:7.1f}KB/s | TTFB: {ttfb_ms:5.1f}ms")
                  
            return final_speed
            
        except Exception as e:
            try:
                response.close()
            except:
                pass
            return 0.0
        
    def get_stats(self):
        """获取统计信息"""
        if self.stats['speed_samples']:
            self.stats['avg_speed'] = sum(self.stats['speed_samples']) / len(self.stats['speed_samples'])
        return self.stats


# ====================== 频道模板处理 ======================
class ChannelTemplate:
    """频道模板处理类"""
    
    def __init__(self, template_file):
        self.template_file = template_file
        self.categories = []  # 保存分类顺序
        self.channel_map = {}  # 别名到主频道名的映射
        self.main_channels = {}  # 主频道到分类的映射
        self.category_channels = {}  # 分类下的主频道列表（按顺序）
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
                    # 处理分类行
                    parts = line.split('#genre#')
                    if len(parts) > 0:
                        current_category = parts[0].replace("📡", "").strip()
                        if current_category and current_category not in self.categories:
                            self.categories.append(current_category)
                            self.category_channels[current_category] = []
                elif current_category and "," in line:
                    # 处理频道行
                    parts = [p.strip() for p in line.split(",") if p.strip()]
                    if len(parts) > 0:
                        main_channel = parts[0]
                        aliases = parts
                        
                        # 记录主频道分类
                        self.main_channels[main_channel] = current_category
                        
                        # 添加到分类下的频道列表
                        if main_channel not in self.category_channels[current_category]:
                            self.category_channels[current_category].append(main_channel)
                        
                        # 创建别名映射
                        for alias in aliases:
                            if alias not in self.channel_map:
                                self.channel_map[alias] = main_channel
        
        # 添加"其它"分类
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
        # 清理频道名中的特殊字符
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


# ====================== 批量测速函数 ======================
def batch_speed_test_optimized(channel_list, template):
    """优化的批量测速函数"""
    config = SpeedTestConfig()
    engine = SpeedTestEngine(config)
    
    print(f"开始对 {len(channel_list)} 个频道进行速度测试...")
    print("-" * 80)
    
    # 按主频道分组，每个主频道可能有多个源
    channels_by_main = {}
    for channel_name, channel_url in channel_list:
        main_channel = template.get_main_channel(channel_name)
        if main_channel not in channels_by_main:
            channels_by_main[main_channel] = []
        channels_by_main[main_channel].append((channel_name, channel_url))
    
    # 测试每个主频道的最快源
    fast_channels = {}  # 主频道 -> (最快的URL, 速度)
    total_to_test = len(channels_by_main)
    completed = 0
    
    for main_channel, sources in channels_by_main.items():
        completed += 1
        print(f"[{completed}/{total_to_test}] 测试 {main_channel}: {len(sources)} 个源")
        
        best_speed = 0
        best_url = None
        
        for channel_name, channel_url in sources:
            print(f"  源: {channel_name[:40]:<40}")
            speed = engine._get_speed_with_retry(channel_url, "freetv")
            
            if speed >= config.SPEED_THRESHOLD and speed > best_speed:
                best_speed = speed
                best_url = channel_url
        
        if best_url:
            fast_channels[main_channel] = (best_url, best_speed)
            print(f"  ✅ 通过 | 速度: {best_speed:7.1f}KB/s")
        else:
            print(f"  ❌ 失败 | 所有源速度不足")
        
        # 进度显示
        if completed % 5 == 0 or completed == total_to_test:
            current_passed = len(fast_channels)
            pass_rate = (current_passed / completed * 100) if completed > 0 else 0
            print(f"\n进度: {completed}/{total_to_test} | 通过: {current_passed} ({pass_rate:.1f}%)")
            print("-" * 80)
    
    engine.stats['total_tested'] = total_to_test
    engine.stats['passed'] = len(fast_channels)
    engine.stats['failed'] = total_to_test - len(fast_channels)
    
    # 计算最终统计
    stats = engine.get_stats()
    
    print("\n" + "=" * 80)
    print(f"速度测试完成!")
    print(f"总计测试: {stats['total_tested']} 个主频道")
    print(f"通过测试: {stats['passed']} 个 (速度 ≥ {config.SPEED_THRESHOLD} KB/s)")
    print(f"失败: {stats['failed']} 个")
    
    if stats['total_tested'] > 0:
        pass_rate = stats['passed'] / stats['total_tested'] * 100
        print(f"通过率: {pass_rate:.1f}%")
        print(f"平均速度: {stats['avg_speed']:.1f} KB/s")
    
    print("=" * 80)
    
    return fast_channels, stats


# ====================== 文件输出 ======================
def save_freetv_files(fast_channels, template, epg_url, output_dir="freetv"):
    """保存输出文件"""
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 获取当前时间
    utc_time = datetime.now(timezone.utc)
    beijing_time = utc_time + timedelta(hours=8)
    formatted_time = beijing_time.strftime("%Y%m%d %H:%M:%S")
    
    # 1. 保存freetv.txt文件
    txt_file = os.path.join(output_dir, "freetv.txt")
    txt_lines = ["#genre#", f"更新时间,{formatted_time}", ""]
    
    # 2. 保存freetv.m3u文件
    m3u_file = os.path.join(output_dir, "freetv.m3u")
    m3u_lines = [
        "#EXTM3U",
        f'x-tvg-url="{epg_url}"',
        ""
    ]
    
    # 按分类和模板顺序输出
    for category in template.categories:
        # 获取该分类下的主频道（按模板顺序）
        main_channels_in_category = template.get_channels_by_category(category)
        
        # 过滤出已通过测速的频道
        available_channels = []
        for main_channel in main_channels_in_category:
            if main_channel in fast_channels:
                channel_url, speed = fast_channels[main_channel]
                available_channels.append((main_channel, channel_url, speed))
        
        if not available_channels:
            continue
        
        # 在txt文件中添加分类标题
        txt_lines.append(f"{category},#genre#")
        
        # 输出该分类下的所有频道
        for main_channel, channel_url, speed in available_channels:
            # txt格式
            txt_lines.append(f"{main_channel},{channel_url}")
            
            # m3u格式
            logo_url = template.get_logo_url(main_channel)
            m3u_lines.extend([
                f'#EXTINF:-1 tvg-name="{main_channel}" tvg-logo="{logo_url}" group-title="{category}", {main_channel}',
                channel_url
            ])
    
    # 保存txt文件
    with open(txt_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt_lines))
    print(f"已保存: {txt_file} ({len(txt_lines)-3} 个频道)")
    
    # 保存m3u文件
    with open(m3u_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(m3u_lines))
    print(f"已保存: {m3u_file} ({len(available_channels)} 个频道)")
    
    # 保存统计信息
    stats_file = os.path.join(output_dir, "freetv_stats.txt")
    with open(stats_file, 'w', encoding='utf-8') as f:
        f.write(f"更新时间: {formatted_time}\n")
        f.write(f"总频道数: {len(fast_channels)}\n")
        f.write(f"分类统计:\n")
        for category in template.categories:
            if category in template.category_channels:
                total = len(template.category_channels[category])
                passed = sum(1 for c in template.category_channels[category] if c in fast_channels)
                f.write(f"  {category}: {passed}/{total} 个\n")
    
    return txt_file, m3u_file


# ====================== 主程序 ======================
def main():
    # 初始化配置
    config = SpeedTestConfig()
    
    # 1. 加载频道模板
    print("=" * 60)
    print("IPTV频道源处理脚本")
    print("=" * 60)
    
    template = ChannelTemplate("freetv/dome.txt")
    if not template.load_template():
        return
    
    # 2. 从URL获取频道列表
    print("\n从网络获取频道列表...")
    
    source_url = "https://freetv.fun/test_channels_original_new.txt"
    freetv_lines = []
    
    try:
        req = urllib.request.Request(source_url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3')

        with urllib.request.urlopen(req) as response:
            data = response.read()
            text = data.decode('utf-8')
            lines = text.split('\n')
            print(f"从URL获取到 {len(lines)} 行数据")
            
            for line in lines:
                line = line.strip()
                if "#genre#" not in line and "," in line and "://" in line:
                    try:
                        channel_name, channel_address = line.split(',', 1)
                        if channel_address.startswith(('http://', 'https://')):
                            freetv_lines.append((channel_name, channel_address))
                    except:
                        continue
                        
            print(f"解析到 {len(freetv_lines)} 个有效频道")
            
    except Exception as e:
        print(f"获取频道列表失败: {e}")
        return
    
    if not freetv_lines:
        print("错误: 没有获取到任何频道")
        return
    
    # 3. 使用模板标准化频道名称
    print("\n使用模板标准化频道名称...")
    standardized_channels = []
    unmatched_channels = []
    
    for channel_name, channel_url in freetv_lines:
        main_channel = template.get_main_channel(channel_name)
        if main_channel in template.main_channels or main_channel == channel_name:
            standardized_channels.append((main_channel, channel_url))
        else:
            unmatched_channels.append((channel_name, channel_url))
    
    print(f"标准化后频道: {len(standardized_channels)} 个")
    print(f"未匹配频道: {len(unmatched_channels)} 个")
    
    # 4. 合并未匹配频道到"其它"分类
    for channel_name, channel_url in unmatched_channels:
        standardized_channels.append((channel_name, channel_url))
        if channel_name not in template.main_channels:
            template.main_channels[channel_name] = "其它"
            if channel_name not in template.category_channels["其它"]:
                template.category_channels["其它"].append(channel_name)
    
    # 5. 进行速度测试
    print("\n开始速度测试...")
    fast_channels, stats = batch_speed_test_optimized(standardized_channels, template)
    
    # 6. 保存输出文件
    print("\n生成输出文件...")
    epg_url = "https://gh-proxy.com/https://raw.githubusercontent.com/adminouyang/231006/refs/heads/main/py/TV/EPG/epg.xml"
    txt_file, m3u_file = save_freetv_files(fast_channels, template, epg_url)
    
    # 7. 输出统计信息
    print("\n" + "=" * 60)
    print("处理完成!")
    print("=" * 60)
    print(f"原始频道数: {len(freetv_lines)}")
    print(f"通过测速: {len(fast_channels)}")
    print(f"通过率: {len(fast_channels)/len(freetv_lines)*100:.1f}%")
    
    # 按分类统计
    print("\n分类统计:")
    for category in template.categories:
        if category in template.category_channels:
            total = len(template.category_channels[category])
            passed = sum(1 for c in template.category_channels[category] if c in fast_channels)
            if total > 0:
                print(f"  {category}: {passed}/{total} 个 ({passed/total*100:.1f}%)")
    
    print(f"\n输出文件:")
    print(f"  {txt_file}")
    print(f"  {m3u_file}")
    print("=" * 60)


if __name__ == "__main__":
    main()
