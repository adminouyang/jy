#!/usr/bin/env python3
"""
IPTV 播放列表自动更新脚本（精简版）
从远程 API 抓取 IPTV 源（测速选优），生成 M3U8/TXT 文件
"""

import requests
import os
import re
import time
from datetime import datetime, timezone, timedelta
import gc
from urllib.parse import quote, urlparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

# ==================== 配置 ====================
EPG_URL = os.environ.get("EPG_URL", "https://epg.112114.xyz/pp.xml")
LOGO_BASE_URL = "https://ghfast.top/https://raw.githubusercontent.com/Jarrey/iptv_logo/main/tv/"

# 输出文件路径（可通过环境变量自定义，默认为当前目录下的文件）
OUTPUT_M3U8 = os.environ.get("OUTPUT_M3U8", "/Hotel/Remote Access/output.m3u8")
OUTPUT_TXT = os.environ.get("OUTPUT_TXT", "/Hotel/Remote Access/output.txt")

# 远程源配置
API_URL = "https://iptvs.pes.im"
TOP_N = 5
MAX_WORKERS = 20
HOST_SPEED_TEST_TIMEOUT = 15
SPEED_TEST_BATCH_SIZE = 60
ZHGXTV_INTERFACE = "/ZHGXTV/Public/json/live_interface.txt"
HSMD_ADDRESS_LIST_FILE = "/Hotel/Remote Access/hsmd_address_list.txt"
HSMDTV_TEST_URI = "/newlive/live/hls/1/live.m3u8"
LOG_FILE = os.environ.get("LOG_FILE", "/Hotel/Remote Access/logs/cron.log")

# 分组定义（按显示顺序）
GROUP_ORDER = [
    "央视频道", "卫视频道", "4K频道", "数字频道", "港澳台频道", "少儿频道",
    "安徽频道", "北京频道", "上海频道", "湖南频道", "湖北频道",
    "河南频道", "河北频道", "山东频道", "山西频道", "广东频道",
    "广西频道", "四川频道", "陕西频道", "浙江频道", "江西频道",
    "福建频道", "海南频道", "贵州频道", "黑龙江频道", "吉林频道",
    "辽宁频道", "内蒙古频道", "新疆频道",
    "其他频道",
]

# ==================== 频道分类 & 名称映射模板 ====================
CHANNEL_CATEGORIES = {
    "央视频道": [
         "CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV4欧洲", "CCTV4美洲", "CCTV5", "CCTV5+", "CCTV6", "CCTV7",
         "CCTV8", "CCTV9", "CCTV10", "CCTV11", "CCTV12", "CCTV13", "CCTV14", "CCTV15", "CCTV16", "CCTV17",
         "兵器科技", "风云音乐", "风云足球", "风云剧场", "怀旧剧场", "第一剧场", "女性时尚", "世界地理", "央视台球", "高尔夫网球",
         "央视文化精品", "卫生健康", "电视指南", "老故事", "中学生", "发现之旅", "书法频道", "国学频道", "环球奇观","CCTV4K",
         "CETV1", "CETV2", "CETV3", "CETV4", "早期教育","CGTN","CGTN纪录","CGTN俄语","CGTN英语",
   ],
   "卫视频道": [
       "重温经典","湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "深圳卫视", "北京卫视", "广东卫视", "广西卫视", "东南卫视","海峡卫视","海南卫视",
       "河北卫视", "河南卫视", "湖北卫视", "江西卫视", "四川卫视", "重庆卫视", "贵州卫视", "云南卫视", "天津卫视", "安徽卫视", "厦门卫视",
       "山东卫视", "山东教育卫视","辽宁卫视", "黑龙江卫视", "吉林卫视", "内蒙古卫视", "宁夏卫视", "山西卫视", "陕西卫视", "甘肃卫视", "青海卫视",
       "新疆卫视", "西藏卫视", "三沙卫视", "兵团卫视", "延边卫视", "安多卫视", "康巴卫视", "农林卫视", "大湾区卫视",
  ],
    "4K频道": [
       "北京卫视4K","河南卫视4K",
  ],
  "数字频道": [
      "CHC动作电影", "CHC家庭影院", "CHC影迷电影", "淘电影", "淘精彩", "淘剧场", "淘4K", "淘娱乐","书画频道","新视觉",
      "4K电影","海看大片", "华数热播剧场","华数谍战剧场", "IPTV戏曲","IPTV经典电影", "IPTV喜剧影院", "华数动作影院", "精品剧场","IPTV抗战剧场","华数精选","IPTV武侠剧场","华数星影","华数家庭影院",
      "求索纪录", "求索科学","求索生活", "求索动物", "纪实人文", "纪实科教", "睛彩青少", "睛彩竞技", "睛彩篮球", "睛彩广场舞", "魅力足球", "五星体育", "体育赛事","收视指南",
      "劲爆体育", "快乐垂钓", "四海钓鱼", "茶频道", "先锋乒羽", "天元围棋", "汽摩", "车迷频道",
      "欢笑剧场","IPTV综艺","IPTV体育","IPTV电影","IPTV国防军事","IPTV电视剧","IPTV科教","IPTV社会与法","IPTV音乐",
      "中国交通", "中国天气", "网络棋牌","EETV生态环境",
 ],
 "港澳台频道": [
     "凤凰中文台", "凤凰资讯台", "凤凰香港台", "凤凰电影台", "龙祥时代", "星空卫视", "CHANNEL[V]", "澳门莲花", "TVB星河", "东森财经新闻",
     "东森综合", "私人影院", "DMAX", "动物星球", "ANIMAX","翡翠台","明珠台",
 ],
 "少儿频道": [
      "淘BABY","淘萌宠", "金色学堂", "动漫秀场", "金鹰卡通", "优漫卡通", "哈哈炫动", "嘉佳卡通","优优宝贝","华数少儿动画","黑莓动画",
 ],
 "安徽频道": [
     "安徽影视", "安徽经济生活", "安徽公共", "安徽综艺体育", "安徽农业科教", "阜阳生活频道", "马鞍山新闻综合", "马鞍山公共", "宣城综合", "广德新闻综合", "", "环球奇观",
     "临泉一台", "临泉三台", "", "", "", "", "", "",
     "", "", "", "", "", "", "", "", "", "", "",
 ],
 "北京频道": [
      "北京新闻","北京影视","北京文艺","北京体育休闲","北京财经","北京纪实科教","北京卡酷少儿", 
 ],
 "上海频道": [
     "新闻综合", "都市频道","都市剧场", "东方影视", "纪实人文", "第一财经", "五星体育", "东方财经", "ICS频道", "上海教育台", "七彩戏剧",
     "欢笑剧场4K", "生活时尚", "法治天地", "上海纪实", "乐游", "", "",
     "", "", "", "", "", "", "", "", "", "", "",
 ],
 "湖南频道": [
     "湖南导视","湖南国际", "湖南电影", "湖南电视剧", "湖南经视", "湖南娱乐", "湖南公共", "湖南都市","湖南教育台","湖南爱晚", "芒果互娱", "长沙新闻综合", "长沙文旅频道",
     "长沙政法频道", "长沙影视", "长沙女性","永州新闻综合","永州经济生活", "抗战剧场", "古装剧场", "高清院线", "先锋兵羽", "望城综合", "花鼓戏", "南县新闻","湘潭新闻综合","湘西新闻综合",
     "株洲新闻综合","怀化新闻综合","怀化经济生活","湘西文化旅游", "溆浦综合","益阳新闻综合", "芷江电视台", "衡阳新闻综合", "衡阳文旅法治", "辰溪新闻综合", "邵阳新闻综合", "邵阳文旅民生",
     "郴州综合", "郴州文旅", "靖州综合","张家界新闻综合","张家界公共", "常德新闻综合","岳阳新闻综合","岳阳文旅都市","宁乡戏曲","娄底综合","望城综合","游戏风云","金鹰纪实","武冈综合",
     "邵阳县","隆回电视台","武冈综合","新邵新闻综合","城步电视","洞口综合频道","新宁新闻综合","邵东电视台","凤凰时政"
 ],
 "湖北频道": [
     "湖北综合", "湖北影视", "湖北生活","湖北公共新闻", "湖北教育", "湖北经视", "荆州新闻综合", "荆州垄上","湖北垄上","十堰新闻综合","十堰经济旅游",
 ],
 "河南频道": [
     "河南都市频道", "河南民生频道","河南法治频道", "河南电视剧频道", "河南新闻频道","河南公共频道","河南国际频道","河南乡村频道","梨园频道", "文物宝库","武术世界", "安阳新闻综合","安阳文旅",
     "广平新闻频道","漯河新闻综合","漯河文旅频道","爱大剧","河南移动电视","驻马店2公共频道","驻马店1新闻综合频道","驻马店3科教频道",
 ],
 "河北频道": [
      "河北影视剧", "河北都市","河北经济生活", "河北文旅公共", "河北少儿科教","河北三农","河北杂技","睛彩河北","邯郸新闻综合","邯郸科技教育","邯郸公共", "衡水新闻", "衡水公共","荆州新闻综合","魏县综合新闻",         
      "广平新闻频道",
 ],
 "山东频道": [
      "山东新闻","山东国际","山东综艺","山东影视", "山东齐鲁", "山东农科","山东体育休闲","山东生活", "山东少儿","山东教育","济南文旅体育","烟台新闻","烟台公共","QTV-1","QTV-2","QTV-3","QTV-4",
     "临沂导视", "临沂图文", "临沂综合", "临沂农科",
     "兰陵导视", "兰陵公共", "兰陵综合","东营新闻综合","东营公共",
 ],
 "山西频道": [
      "山西影视","山西文体生活","山西经济与科技","山西黄河","山西社会与法治","太原-1","吕梁-1","吕梁-2","中阳电视台",
 ],
 "广东频道": [
      "广东影视", "广东少儿", "广东民生", "广东新闻", "广东经济科教", "广东体育","广东珠江","广东岭南戏曲","广东现代教育", "广州台","广州新闻", "广州影视","广州经济","嘉佳卡通",
      "深圳都市频道", "深圳少儿","珠海",  
      "茂名综合","南山有线","汕头综合","汕尾新闻综合","梅州综合","梅州客家生活","梅州导视","惠州-1","揭阳综合","揭阳生活","揭西综合频道","深圳龙岗频道","云浮综合",
 ],
 "广西频道": [
     "广西影视", "广西综艺旅游", "广西都市", "广西新闻", "广西移动", "广西科技", "精彩影视", "平南台",  "南宁新闻综合","南宁影视娱乐", "南宁公共", "玉林新闻综合","玉林娱乐","图文信息",
     "兴业综合","北海经济科教","强盛城市海岸","凤山综合频道","河池新闻综合","河池教育频道","河池资讯",

 ],
 "四川频道": [
     "四川新闻", "四川文化旅游", "四川影视文艺","四川科教","四川经济","四川妇儿","四川乡村", "峨眉电影", "熊猫影院", "广元综合", "广元公共", "四川卫视-乡村公共", "蓬安电视台",
     "金熊猫卡通","BesTV华语影院","BesTV宝宝动画","BesTV动漫专区","BesTV电竞天堂","乐山文旅生活","四川星空购物"
 ],
 "陕西频道": [
     "陕西新闻资讯","陕西都市青春","陕西银龄","陕西秦腔","西安新闻综合","西安都市频道","三门峡新闻综合", "灵宝新闻综合",
 ],    
 "浙江频道": [
     "浙江新闻", "杭州影视"
 ], 
 "江西频道": [
     "江西新闻","江西都市","江西经济生活","江西少儿","江西指南","江西公共农业","抚州综合","抚州公共","赣州新闻综合","赣州经济生活","赣州教育","章贡综合","石城综合", "石城影视",  
 ],    
 "福建频道": [
     "福建新闻","福建综合", "福建教育","乡村振兴公共频道","福建文旅体育","厦门-1"
 ], 
 "海南频道": [
     "海南自贸","海南新闻","海南社会与法","海南文旅",
 ], 
 "贵州频道": [
     "贵州-2", "贵州-4","贵州-5","贵阳",
 ],
 "黑龙江频道": [
     "黑龙江影视","黑龙江文体","黑龙江都市","黑龙江新闻法治","黑龙江公共","黑龙江农业科教","黑龙江少儿","精彩影视"
 ],
 "吉林频道": [
     "吉林影视", "吉林都市", "吉林乡村", "吉林教育", "吉林综艺文化", "吉林生活", "长影频道", "松原生活","松原","通化新闻综合","辉南综合",
 ],    
 "辽宁频道": [
     "辽宁都市","辽宁影视剧","辽宁生活","辽宁教育青少","辽宁北方","辽宁公共","辽宁经济","辽宁移动电视","辽宁体育休闲",
 ], 
 "内蒙古频道": [
     "内蒙古新闻综合", "内蒙古文体娱乐", "内蒙古经济生活", "内蒙古农牧频道", "内蒙古少儿频道", "内蒙古蒙语频道", "内蒙古蒙语频道II", "包头新闻综合","包头生活服务",
     "包头经济生活",
 ],
 "新疆频道": [
     "新疆-2", "新疆-3", "新疆-4", "新疆-5", "新疆-6", "新疆-7", "新疆-8", "新疆-9","广告测试",
     "24KZ","Рика ТВ","Хабар","Manas TV","Новое телевидение","qazaqstantv","Abai TV","MTKR","Qazsport",
 ],
    "其他频道": []
}


# 频道名称映射
CHANNEL_MAPPING = {
        "CCTV1": ["CCTV1", "CCTV-1", "CCTV1综合","CCTV-1综合", "CCTV1高清","CCTV1-高清", "CCTV1HD","CCTV1-综合HD", "cctv1","中央1台","sCCTV1-综合","CCTV01","CCTV1-综合",
                  "CCTV1-综合高清","CCTV1 高清","CCTV1-综合y","CCTV1综合y"],
        "CCTV2": ["CCTV2", "CCTV-2", "CCTV2财经", "CCTV-2财经","CCTV2高清","CCTV2-高清", "CCTV2HD","CCTV2-HD", "cctv2","中央2台","aCCTV2","sCCTV2-财经","CCTV02","CCTV2-财经高清","CCTV2-财经","CCTV2 高清"],
        "CCTV3": ["CCTV3", "CCTV-3", "CCTV3综艺","CCTV-3综艺", "CCTV3高清","CCTV3-高清", "CCTV3HD", "cctv3","中央3台","acctv3","sCCTV3-综艺","CCTV03","CCTV3-综艺","CCTV3-综艺高清",
                  "CCTV3 高清","CCTV3-综艺HD","*CCTV3综艺"],
        "CCTV4": ["CCTV4", "CCTV-4", "CCTV4中文国际","CCTV-4中文国际", "CCTV4高清","CCTV4-高清", "CCTV4HD", "cctv4","中央4台","aCCTV4","sCCTV4-国际","CCTV04","CCTV4-国际","CCTV4-国际高清",
                  "CCTV4国际","CCTV4 高清"],
        "CCTV5": ["CCTV5", "CCTV-5", "CCTV5体育","CCTV-5体育", "CCTV5高清","CCTV5-高清", "CCTV-5高清","CCTV5HD","CCTV5-HD", "cctv5","中央5台","sCCTV5-体育","CCTV05","CCTV5-体育","CCTV5-体育高清",
                  "CCTV5 高清","CCTV5-体育HD","*CCTV5体育"],
        "CCTV5+": ["CCTV5+", "CCTV-5+", "CCTV5+体育赛事", "CCTV5+高清", "CCTV5+HD", "cctv5+", "CCTV5plus","CCTV5+体育赛事高清","CCTV-5+体育赛事","CCTV5+体育赛事HD","CCTV5+体育","CCTV5+ 高清"],
        "CCTV6": ["CCTV6", "CCTV-6", "CCTV6电影","CCTV-6电影", "CCTV6高清","CCTV6-高清","CCTV-6高清", "CCTV6HD","CCTV6-HD", "cctv6","中央6台","sCCTV6-电影","CCTV06","CCTV6-电影","CCTV6-电影高清",
                  "CCTV6 高清","CCTV6-电影HD","*CCTV6电影"],
        "CCTV7": ["CCTV7", "CCTV-7", "CCTV7军事","CCTV-7国防军事", "CCTV7高清","CCTV7-高清", "CCTV7HD", "cctv7","中央7台","CCTV07","CCTV7-军农高清","CCTV7-军农","CCTV7军事农业","CCTV7 高清","CCTV7-国防军事",
                  "CCTV7军农","CCTV7国防军事"],
        "CCTV8": ["CCTV8", "CCTV-8", "CCTV8电视剧","CCTV-8电视剧", "CCTV8-电视剧","CCTV8高清","CCTV8-高清", "CCTV8HD","CCTV8-电视剧HD", "cctv8","中央8台","sCCTV8-电视剧","CCTV08","CCTV8-电视剧高清","CCTV8 高清"],
        "CCTV9": ["CCTV9", "CCTV-9", "CCTV9纪录","CCTV-9纪录", "CCTV9-纪录","CCTV9高清", "CCTV9-高清","CCTV9HD", "cctv9","中央9台","sCCTV9-纪录","CCTV09","CCTV9-纪录高清","CCTV9记录","CCTV9-纪录HD",
                  "CCTV 9","CCTV9 高清","纪录"],
        "CCTV10": ["CCTV10", "CCTV-10", "CCTV10科教","CCTV-10科教","CCTV10-科教", "CCTV10高清","CCTV10-高清", "CCTV10HD","CCTV10-HD", "cctv10","中央10台","sCCTV10-科教","CCTV10-科教高清",
                   "CCTV 10","CCTV10 高清"],
        "CCTV11": ["CCTV11", "CCTV-11", "CCTV11戏曲","CCTV-11戏曲", "CCTV11高清","CCTV11-高清", "CCTV11HD","CCTV11-戏曲HD", "cctv11", "中央11台","sCCTV11-戏曲","CCTV11-戏曲","CCTV11-戏曲高清",
                   "CCTV 11","CCTV11 戏曲"],
        "CCTV12": ["CCTV12", "CCTV-12", "CCTV12社会与法","CCTV-12社会与法", "CCTV12高清","CCTV12-高清", "CCTV12HD","CCTV12-社会与法HD", "cctv12","中央12台","sCCTV12-社会与法","CCTV12-社会与法","CCTV12社会与法制",
                   "CCTV12-社会与法高清","CCTV 12","CCTV12 高清"],
        "CCTV13": ["CCTV13", "CCTV-13", "CCTV13新闻","CCTV-13新闻","CCTV13-新闻", "CCTV13高清","CCTV13-高清", "CCTV13HD", "cctv13","中央13台","sCCTV13-新闻","CCTV-新闻","CCTV13-新闻高清",
                   "测试频道08","CCTV13新闻","CCTV 13","CCTV新闻"],
        "CCTV14": ["CCTV14", "CCTV-14", "CCTV14少儿","CCTV-14少儿","CCTV14-少儿", "CCTV14高清","CCTV14-高清", "CCTV14HD","CCTV14-少儿HD", "cctv14","中央14台","sCCTV14-少儿","CCTV-少儿高清",
                   "CCTV-少儿","CCTV14-少儿高清","CCTV 14","CCTV14少儿 高清","CCTV少儿"],
        "CCTV15": ["CCTV15", "CCTV-15", "CCTV15音乐","CCTV-15音乐", "CCTV15高清","CCTV15-高清", "CCTV15HD", "cctv15","中央15台","sCCTV15-音乐","CCTV-音乐","CCTV15-音乐","CCTV 15","CCTV15 音乐",
                  "CCTV音乐"],
        "CCTV16": ["CCTV16", "CCTV-16", "CCTV16奥林匹克", "CCTV16高清","CCTV16-高清", "CCTV16HD", "cctv16","中央16台",	"CCTV-16 奥林匹克","CCTV 16","CCTV16 奥林匹克 高清"],
        "CCTV17": ["CCTV17", "CCTV-17", "CCTV17农业农村","CCTV17农村农业","CCTV-17农业农村", "CCTV17高清","CCTV17-高清", "CCTV17HD", "cctv17","中央17台","CCTV 17","CCTV17 农业农村 高清","农业农村"],

        "CCTV4欧洲": ["CCTV4欧洲", "CCTV-4欧洲", "CCTV4欧洲高清", "CCTV4欧洲HD"],
        "CCTV4美洲": ["CCTV4美洲", "CCTV-4美洲", "CCTV4美洲高清", "CCTV4美洲HD"],

        "兵器科技": ["兵器科技", "CCTV兵器科技", "兵器科技频道","兵器科技HD","国防军事"],
        "风云音乐": ["风云音乐", "CCTV风云音乐","风云音乐高清","CCTV风云音乐高清"],
        "第一剧场": ["第一剧场", "CCTV第一剧场","第一剧场HD","第一剧场高清","CCTV第一剧场高清"],
        "风云足球": ["风云足球", "CCTV风云足球","风云足球HD","风云足球高清","CCTV风云足球高清"],
        "风云剧场": ["风云剧场", "CCTV风云剧场","风云剧场HD","风云剧场高清","CCTV风云剧场高清"],
        "怀旧剧场": ["怀旧剧场", "CCTV怀旧剧场","怀旧剧场HD","怀旧剧场高清","CCTV怀旧剧场高清"],
        "发现之旅": ["CCTV发现之旅"],
        "女性时尚": ["女性时尚", "CCTV女性时尚","CCTV-女性时尚"],
        "世界地理": ["地理世界", "CCTV世界地理","世界地理高清"],
        "央视台球": ["央视台球", "CCTV央视台球","CCTV-央视台球","央视台球HD","CCTV央视台球高清"],
        "高尔夫网球": ["高尔夫网球", "央视高网", "CCTV高尔夫网球", "高尔夫","高尔夫·网球HD","CCTV高尔夫网球高清"],
        "央视文化精品": ["央视文化精品", "CCTV央视文化精品","央视精品","CCTV精品"],
        "老故事": ["CCTV老故事"],
        "卫生健康": ["卫生健康", "CCTV卫生健康"],
        "电视指南": ["电视指南高清", "CCTV电视指南"],
        "中国天气": ["中国气象"],
        "安多卫视": ["1020"],
        "重温经典": ["重温经典高清","测试频道23","重温金典"],
        "安徽卫视": ["安徽卫视高清","安徽卫视 高清","安徽卫视HD"],
        "北京卫视": ["北京卫视HD","北京卫视高清","北京卫视 高清","BTV北京卫视"],
        "东南卫视": ["福建东南", "福建东南卫视","东南卫视高清","东南卫视HD","东南卫视 高清","福建卫视"],
        "海峡卫视": ["海峡卫视高清"],
        "东方卫视": ["上海卫视", "东方卫视高清","SBN","上海卫视高清","东方卫视HD","上海东方卫视","东方卫视 高清","上海卫视 高清","上海东方卫视高清","东方卫视(上海)","东方卫视上海"],
        "农林卫视": ["陕西农林卫视", "农林卫视"],
        "江苏卫视": ["江苏卫视HD","江苏卫视高清","江苏卫视 高清"],
        "江西卫视": ["江西卫视高清","江西卫视 标清","江西卫视HD"],
        "黑龙江卫视": ["黑龙江卫视高清","黑龙江卫视HD","黑龙江卫视 高清"],
        "吉林卫视": ["吉林卫视","吉林卫视高清","吉林卫视 标清"],
        "辽宁卫视": ["辽宁卫视HD","辽宁卫视 高清","辽宁卫视高清"],
        "甘肃卫视": ["甘肃卫视","甘肃卫视高清","甘肃卫视 标清"],
        "湖南卫视": ["湖南卫视HD", "湖南电视","湖南卫视高清","湖南卫视 高清"],
        "河南卫视": ["河南卫视HD","河南卫视高清","河南卫视高清清","河南卫视 高清"],
        "河北卫视": ["河北卫视","河北卫视高清","河北卫视 高清","河北卫视HD"],
        "湖北卫视": ["湖北卫视","湖北卫视高清","湖北卫视 高清"],
        "海南卫视": ["旅游卫视", "海南卫视HD","海南高清卫视","海南卫视高清"],
        "三沙卫视": ["三沙卫视高清","三沙卫视 标清"],
        "厦门卫视": ["厦门卫视","厦门卫视高清","厦门卫视 标清"],
        "重庆卫视": ["重庆卫视HD","重庆卫视高清","重庆卫视 高清"],
        "深圳卫视": ["深圳卫视高清", "深圳卫视HD","深圳卫视 高清","深圳"],
        "广东卫视": ["广东卫视","广东卫视高清","广东卫视 高清","广东卫视HD"],
        "广西卫视": ["广西卫视","广西卫视高清","广西卫视 标清","广西卫视HD"],
        "天津卫视": ["天津卫视","天津卫视高清","天津卫视HD","天津卫视 高清"],
        "山东卫视": ["山东高清","山东卫视高清","山东卫视HD","山东卫视 高清"],
        "山西卫视": ["山西卫视高清","山西卫视 高清"],
        "陕西卫视": ["陕西卫视高清","陕西卫视 标清","陕西卫视HD"],
        "四川卫视": ["四川卫视","四川卫视高清","四川卫视 高清","四川卫视HD"],
        "浙江卫视": ["浙江卫视高清","浙江卫视HD","浙江卫视 高清"],
        "云南卫视": ["云南卫视 标清","云南卫视高清"],
        "贵州卫视": ["贵州卫视","贵州卫视高清","贵州卫视-1","贵州卫视 高清"],
        "宁夏卫视": ["宁夏卫视高清","宁夏卫视 标清"],
        "内蒙古卫视": ["内蒙古卫视高清", "内蒙古", "内蒙卫视","内蒙古卫视 标清"],
        "康巴卫视": ["康巴卫视高清"],
        "山东教育卫视": ["山东教育","山东教育卫视"],
        "大湾区卫视": ["南方卫视高清","南方卫视","广东南方卫视",],
        "新疆卫视": ["新疆卫视", "新疆1","新疆卫视 标清"],
        "兵团卫视": ["兵团卫", "兵团卫视高清"],
        "青海卫视": ["青海卫视高清","青海卫视 标清","青海卫视HD"],
        "西藏卫视": ["XZTV2","西藏卫视高清","西藏卫视HD","西藏卫视 标清"],

        "CETV1": ["中国教育1台", "中国教育一台", "中国教育一套高清", "教育一套" ,"CETV-1高清","中国教育","CETV1 标清","CETV-1","中国教育-1","CETV1 高清","中央教育"],
        "CETV2": ["中国教育2台", "中国教育二台", "中国教育二套高清","CETV2 标清","CETV-2"],
        "CETV3": ["中国教育3台", "中国教育三台", "中国教育三套高清","CETV3 标清","CETV-3"],
        "CETV4": ["中国教育4台", "中国教育四台", "中国教育四套高清","CETV4 标清","CETV-4","中国教育-4"],
        "CGTN": ["CCTVnews","CCTVNews"],
        "CGTN英语": ["CGTN 英语高清"],
        "CHC动作电影": ["动作电影","CHC 动作电影","CHC动作电影高清"],
        "CHC家庭影院": ["家庭影院","CHC 家庭影院","CHC家庭影院高清"],
        "CHC影迷电影": ["高清电影","CHC 高清电影","高清影院","CHC高清电影","中国电影","CHC电影"],

        "淘电影": ["淘电影", "IPTV淘电影"],
        "淘精彩": ["淘精彩", "IPTV淘精彩"],
        "淘剧场": ["淘剧场", "IPTV淘剧场"],
        "淘4K": ["淘4K", "IPTV淘4K"],
        "淘娱乐": ["淘娱乐", "IPTV淘娱乐"],
        "淘BABY": ["淘BABY", "IPTV淘BABY", "淘baby"],
        "淘萌宠": ["淘萌宠", "IPTV淘萌宠"],
        "IPTV戏曲": ["相声小品",],
        "IPTV综艺": ["IPTV相声","IPTV3+","iptv3+",],
        "IPTV体育": ["iptv5+","IPTV5+"],
        "IPTV电影": ["iptv6+","IPTV6+"],
        "IPTV国防军事": ["军事","IPTV7+"],
        "IPTV电视剧": ["iptv8+","IPTV8+"],
        "IPTV科教": ["野外","IPTV10+"],
        "IPTV社会与法": ["IPTV法制","法制","法治","IPT11+"],
        "IPTV音乐": ["音乐现场",],
        "华数热播剧场": ["IPTV-热播剧场","热播剧场","IPTV热播剧场"],
        "华数谍战剧场": ["IPTV-谍战剧场","谍战剧场"],
        "华数少儿动画": ["IPTV-少儿动画","少儿动画","早教"],
        "收视指南": ["收视指南"],
        "IPTV经典电影": ["经典电影", "IPTV-经典电影"],
        "IPTV喜剧影院": ["喜剧影院", "IPTV-喜剧影院"],
        "华数动作影院": ["动作影院", "IPTV-动作影院"],
        "华数家庭影院": ["家庭影院"],
        "IPTV抗战剧场": ["测试频道15","抗战剧场","IPTV-抗战剧场"],
        "IPTV武侠剧场": ["武侠剧场"],
        "华数精选": ["精选"],
        "华数星影": ["星影"],
        "精品剧场": ["精品剧场", "IPTV精品剧场"],
        "精彩影视": ["精彩影视", "IPTV-精彩影视"],
        "书画频道": ["书法频道", "书法书画","书法"],
        "环球奇观": ["环球奇观", "环球旅游", "安广网络"],
        "中学生": ["中学生频道", "中学生课堂"],

        "魅力足球": ["魅力足球", "上海魅力足球"],
        "睛彩青少": ["睛彩青少", "睛彩羽毛球"],
        "求索纪录": ["求索纪录", "求索记录"],
        "": ["",],
        "EETV生态环境": ["生态环境"],

        "星空卫视": ["星空卫视", "星空衛視","测试频道05","XF星空卫视"],
        "CHANNEL[V]": ["Channel[V]", "CHANNEL[V]"],
        "凤凰中文台": ["凤凰卫视中文台", "凤凰中文", "凤凰卫视","凤凰中文台","测试1","测试01","测试","测试一"],
        "凤凰香港台": ["凤凰卫视香港台", "凤凰香港"],
        "凤凰资讯台": ["凤凰卫视资讯台", "凤凰资讯", "凤凰咨询","凤凰咨讯","测试2","测试02","测试二"],
        "凤凰电影台": ["凤凰卫视电影台", "凤凰电影", "鳳凰衛視電影台","凤凰影视"],
        "澳门莲花": ["测试频道19"],
        "TVB星河": ["测试频道01","星河台"],
        "翡翠台": ["翡翠台","香港翡翠"],
        "明珠台": ["明珠台","香港明珠"],
        "经典港剧": ["测试频道02"],
        "龙祥时代": ["测试频道0"],
        "东森财经新闻": ["测试频道07"],
        "东森综合": ["测试频道13"],
        "私人影院": ["测试频道10"],
        "DMAX": ["测试频道11"],
        "动物星球": ["测试频道12"],
        "ANIMAX": ["测试频道14"],
        "抗战剧场": ["测试频道15"],

        "安徽影视": ["安徽影视"], 
        "安徽经济生活": ["安徽经济"], 
        "安徽公共": ["安徽影视"], 
        "安徽综艺体育": ["安徽综艺体育", "安徽综艺"],
        "安徽农业科教": ["安徽农业科教", "安徽科教"],
        "阜阳生活频道": ["阜阳公共频道"], 
        "马鞍山新闻综合": ["马鞍山新闻综合", "马鞍山新闻"],
        "马鞍山公共": ["马鞍山公共"],
        "宣城综合": ["宣城公共"],
        "广德新闻综合": ["广德一套","广德一套HD"],
    
    "北京新闻": ["BTV新闻"],
    "北京影视": ["BTV影视"],
    "北京文艺": ["BTV文艺"],
    "北京体育休闲": ["BTV体育"],
    "北京财经": ["BTV财经"],
        "北京纪实科教": ["BTV高清","纪实科教","北京科教","北京冬奥纪实高清"],
        "北京卡酷少儿": ["卡酷","卡酷少儿", "卡酷动画", "卡酷动漫", "北京卡酷","北京少儿","卡通卫视","北京卡通","卡酷卡通","卡酷少儿 标清","卡酷少儿HD","BTV卡酷少儿"], 

        "乐游": ["乐游高清", "乐游频道", "乐游纪实","全纪实","全纪实乐游高清"],
        "欢笑剧场": ["上海欢笑剧场","欢笑剧场4K"],
        "生活时尚": ["生活时尚", "SiTV生活时尚", "上海生活时尚","生活时尚HD","生活时尚高清"],
        "都市剧场": ["都市剧场高清", "SiTV都市剧场", "上海都市剧场", "都市时尚","都市剧场HD","海看剧场"],
        "上海纪实": ["上海纪实高清"],
        "东方财经": ["东方财经高清"],
        
        "河南都市频道": ["河南都市"],
        "河南民生频道": ["河南民生","民生频道"],
        "河南法治频道": ["河南法治","河南法制频道","河南法制","法制频道"],
        "河南电视剧频道": ["河南电视剧","电视剧频道",],
        "河南新闻频道": ["河南新闻","新闻频道"],
        "河南公共频道": ["河南公共","公共频道"],
        "河南乡村频道": ["新农村频道","河南新农村频道","河南新农村","乡村"],
        "梨园频道": ["河南戏曲", "梨园", "河南梨园","河南梨园频道","梨园频道高清"],
        "文物宝库": ["文物频道", "河南文物宝库","文物宝库高清","文物"],
        "武术世界": ["武术世界", "河南武术世界","武术世界高清"],
        "河南移动电视": ["河南移动电视"],
        "安阳新闻综合": ["安阳新闻综合"],
        "安阳文旅": ["安阳科教频道"],
        "漯河新闻综合": ["漯河综合频道"],
        "漯河文旅频道": ["漯河公共频道"],
        "驻马店2公共频道": ["驻马店公共频道2","驻马店公共2"],
        "驻马店1新闻综合频道": ["驻马店新闻综合频道1","驻马店新闻综合1"],
        "驻马店3科教频道": ["驻马店科技频道3","驻马店科技3"],

        "河北影视剧": ["河北影视"],
        "河北都市": ["河北都市"],
        "河北三农": ["河北农民"],
        "河北文旅公共": ["河北公共"],
        "河北经济生活": ["河北经济"],
        "河北少儿科教": ["少儿科教"],
        "河北三农": ["河北农民"], 
        "睛彩天下": ["睛彩河北"],
        "邯郸新闻综合": ["邯郸新闻综合"],
        "邯郸科技教育": ["邯郸科教"],
        "邯郸公共": ["邯郸公共"],
        "荆州新闻综合": ["荆州新闻高清","荆州综合"],
        "魏县综合新闻": ["魏县电视台"],
        "广平新闻频道": ["广平电视台"],

        "湖南电影": ["湖南电影高清", "湖南电影HD","潇湘电影"],
        "湖南电视剧": ["湖南电视剧HD"],
        "湖南娱乐": ["湖南娱乐高清", "湖南娱乐HD"],
        "湖南都市": ["湖南都市高清", "湖南都市HD"],
        "湖南国际": ["湖南国际高清", "湖南国际HD"],
        "湖南公共": ["湖南公共HD"],
        "湖南爱晚": ["湖南公共高清"],
        "湖南教育台": ["湖南教育HD","湖南教育"],
        "湖南经视": ["湖南经视HD"],
        "金鹰卡通": ["金鹰卡通高清", "湖南金鹰卡通","金鹰卡通HD","金鹰卡通 标清","金鹰卡通2"],
        "长沙新闻综合": ["长沙综合","长沙新闻","长沙新闻HD","长沙"],
        "长沙文旅频道": ["文旅频道"],
        "长沙政法频道": ["长沙政法HD","长沙政法"],
        "湘潭新闻综合": ["湘潭台","湘潭综合"],
        "湘西文化旅游": ["湘西公共"],
        "湘西新闻综合": ["湘西台","湘西综合"],
        "湘西文化旅游": ["湘西公共"],
        "溆浦综合": ["溆浦时政"],
        "益阳新闻综合": ["益阳台","益阳公共","益阳综合"],
        "芷江电视台": ["芷江综合"],
        "衡阳新闻综合": ["衡阳台","衡阳综合"],
        "衡阳文旅法治": ["衡阳公共"],
        "辰溪新闻综合": ["辰溪综合"],
        "邵阳新闻综合": ["邵阳台","邵阳综合"],
        "邵阳文旅民生": ["邵阳公共"],
        "邵阳县": ["邵阳县综合"],
        "郴州综合": ["郴州台","株洲综合"],
        "郴州文旅": ["郴州公共"],
        "株洲新闻综合": ["株洲台"],
        "怀化新闻综合": ["怀化综合"],
        "怀化经济生活": ["怀化公共"],
        "永州新闻综合": ["永州台","永州综合"],
        "永州经济生活": ["永州公共"],
        "岳阳新闻综合": ["岳阳台","岳阳综合"],
        "岳阳文旅都市": ["岳阳公共"],
        "娄底综合": ["娄底台"],
        "常德新闻综合": ["常德台","常德综合"],
        "张家界新闻综合": ["张家界","张家界综合"],
        "张家界公共": ["张家界公共"],
        "望城综合": ["望城台"],
        "茶频道": ["茶频道HD", "湖南茶频道","茶频道高清",],
        "快乐垂钓": ["快乐垂钓HD","快乐垂钓高清",],
        "先锋乒羽": ["先锋乒羽"],
        "天元围棋": ["天元围棋"],
        "金鹰纪实": ["金鹰纪实","金鹰纪视高清","金鹰记实","金鹰纪实高清","金鹰纪实HD"],
        "武冈综合": ["武冈时政"],
        "新邵新闻综合": ["新邵综合"],
        "隆回电视台": ["隆回综合"],
        "城步电视": ["城步综合"],
        "洞口综合频道": ["洞口综合"],
        "新宁新闻综合": ["新宁综合"],
        "邵东电视台": ["邵东综合"],
        "凤凰时政": ["凤凰时政","测试三"],

        "湖北影视": ["湖北影视高清"],
        "湖北公共新闻": ["湖北公共"],
        "湖北经视": ["湖北经视高清"],
        "湖北生活": ["湖北生活高清"],
        "湖北综合": ["湖北综合","湖北电视台"],
        "湖北教育": ["湖北教育",],
        "湖北垄上": ["湖北龚上",],
        "十堰新闻综合": ["十堰新闻"],
        "十堰经济旅游": ["十堰经济旅游"],

        "山东国际": ["山东国际",],
        "山东影视": ["山东影视",],
        "山东综艺": ["山东综艺",],
        "山东文旅": ["山东文旅",],
        "山东新闻": ["山东公共",],
        "山东生活": ["山东生活"],
        "山东农科": ["山东农科",],
        "山东齐鲁": ["山东齐鲁","齐鲁卫视","齐鲁"],
        "山东少儿": ["山东少儿",],
        "山东体育休闲": ["山东体育",],
        "QTV-1": ["QTV1",],
        "QTV-2": ["QTV2",],
        "QTV-3": ["QTV3",],
        "QTV-4": ["QTV4",],
        "烟台新闻": ["烟台新闻",],
        "烟台公共": ["烟台公共"],
        "山东教育": ["山东教育",], 
        "临沂导视": ["临沂导视",], 
        "临沂图文": ["临沂图文",], 
        "临沂综合": ["临沂综合",], 
        "临沂农科": ["临沂农科",], 
        "兰陵导视": ["兰陵导视",], 
        "兰陵公共": ["兰陵公共",], 
        "兰陵综合": ["兰陵综合",],
        "济南文旅体育": ["SDETV",],
        "东营公共": ["东营公共"],
        "东营新闻综合": ["东营新闻综合"],

        "山西影视": ["山西影视","山西影视 高清"],
        "山西经济与科技": ["山西经济与科技","经济与科技 高清"],
        "山西社会与法治": ["山西社会与法制","社会与法治 高清","山西科教"],
        "山西文体生活": ["山西公共","文体生活 高清"],
        "山西黄河": ["山西黄河 高清","黄河卫视"],
        "太原-1": ["太原1套"],
        "吕梁-1": ["吕梁-1高清","吕梁1"],
        "吕梁-2": ["吕梁-2高清","吕梁2"],
        "中阳电视台": ["中阳1","中阳-1"],

        "陕西新闻资讯": ["陕西一套"],
        "陕西都市青春": ["陕西二套"],
        "陕西银龄": ["陕西三套"],
        "陕西秦腔": ["陕西五套"],
        "陕西四套": ["陕西四套"],
        "西安新闻综合": ["西安一套"],
        "西安都市频道": ["西安二套"],

        "广东影视": ["广东影视-1","测试频道22","广东影视高清",],
        "广东少儿": ["南方五","广东少儿高清",],
        "广东民生": ["广东民生","广东公共",],
        "广东新闻": ["广东新闻","广东新闻高清","广州综合"],
        "广东经济科教": ["广东科技","广东经济科教高清",],
        "广东体育": ["广东体育++","测试频道09","广东体育高清"],
        "广东珠江": ["珠江台","珠江台BM","珠江频道","珠江台++","测试频道16","珠江台高清","广东珠江高清"],
        "广东岭南戏曲": ["岭南戏剧","岭南戏曲"],
        "广东现代教育": ["现代教育"],
        "广州影视": ["广州影视"],
        "广州经济": ["广州经济"],
        "深圳都市频道": ["深圳都市频道"],
        "深圳龙岗频道": ["深圳龙岗频道"],
        "南山有线": ["南山有线"],
        "梅县综合": ["梅县1"],
        "嘉佳卡通": ["嘉佳卡通", "广东嘉佳卡通", "佳佳卡通","嘉佳卡通高清",],
        "茂名综合": ["茂名综合", "茂名综合高清"],
        "汕头综合":["汕头综合",],
        "汕尾新闻综合": ["汕尾-1"],
        "珠海": ["珠海-1"],
        "梅州综合": ["梅州1"],
        "梅州客家生活": ["梅州"],
        "梅州导视": ["天映"],
        "惠州-1": ["HZTV-1",],
        "揭阳综合" : ["揭阳综合",],
        "揭阳生活": ["揭阳公共",],
        "揭西综合频道": ["揭西电视台"],
        "云浮综合": ["云浮综合"],

        "福建新闻": ["福建新闻高清"],
        "福建文旅体育": ["福建旅游"],
        "福建教育": ["福建教育高清"],
        "乡村振兴公共频道": ["乡村振兴公共"],
        "厦门-1": ["厦门综合高清"],
        
        "海南自贸": ["海南综合"],
        "海南新闻": ["海南新闻"],
        "海南社会与法": ["海南公共"],
        "海南文旅": ["海南影视"],

        "贵州-2": ["贵州卫视-2"],
        "贵州-4": ["贵州卫视-4"],
        "贵州-5": ["贵州卫视-5"],
        "贵阳": ["贵阳-1"],
        "峨眉电影": ["峨眉电影高清"],
        "四川影视文艺": ["SCTV-5","SCTV5","SCTV-5高清","SCTV-5(四川影视）","四川文艺"],
        "四川文化旅游": ["四川文旅","文化旅游","SCTV3","SCTV-3","SCTV-3高清"],
        "四川新闻": ["SCTV-4","SCTV4","SCTV-4高清"],
        "四川经济": ["SCTV-2","SCTV2","SCTV-2高清"],
        "四川妇儿": ["SCTV-7","SCTV7","SCTV-7高清","四川七台"],
        "四川科教": ["SCTV科教"],
    "四川乡村": ["四川乡村"],
        "四川星空购物": ["SCTV-6","SCTV6","SCTV-6高清"],
        "BesTV华语影院": ["华语影院"],
        "BesTV宝宝动画": ["宝宝动画"],
        "BesTV动漫专区": ["动漫专区"],
        "BesTV电竞天堂": ["电竞天堂"],
        "乐山文旅生活": ["乐山文旅"],

        "广西综艺旅游": ["广西广播电视台综艺频道","广西综艺","广西综艺-B"],
        "广西都市": ["广西广播电视台都市频道"],
        "广西新闻": ["广西广播电视台新闻频道"],
        "广西影视": ["广西广播电视台影视频道","广西影视-J"],
        "南宁新闻综合": ["南宁新闻 高清"],
        "南宁影视娱乐": ["南宁影视 高清","南宁影视"],
        "南宁公共": ["南宁公共 高清"],
        "玉林新闻综合": ["玉林新闻 高清", "XF玉林台"],
        "玉林娱乐": ["玉林娱乐"],
        "图文信息": ["玉林信息"],
        "兴业综合": ["兴业台"],
        "北海经济科教": ["北海经济科教"],
        "强盛城市海岸": ["强盛城市海岸"],
        "凤山综合频道": ["凤山综合频道"],
        "河池新闻综合": ["河池新闻综合"],
        "河池教育频道": ["河池教育频道"],
        "河池资讯": ["河池资讯"],

        "江西都市": ["JXTV-2","江西2"],
        "江西经济生活": ["JXTV-3","江西3"],
    "江西公共农业": ["江西5"],
        "江西少儿": ["JXTV-6","江西少儿高清","江西6"],
        "江西新闻": ["江西新闻频道高清","JXTV-7","江西7"],
        "赣州新闻综合": ["赣州TV-1"],
        "赣州经济生活": ["赣州TV-公共"],
        "赣州教育": ["赣州TV-教育"],
        "章贡综合": ["章贡TV"],
        "石城影视": ["石城影视",],
        "石城综合": ["石城综合"],
    "抚州公共": ["抚州1","抚州一套"],

        "黑龙江影视": ["黑龙江视影视高清"],
        "黑龙江文体": ["黑龙江文体","黑龙江文体高清"],
        "黑龙江都市": ["黑龙江都市高清"],
        "黑龙江新闻法治": ["黑龙江新闻法治高清"],
        "黑龙江公共": ["黑龙江公共高清"],
        "黑龙江农业科教": ["黑龙江农业科教高清"],
        "黑龙江少儿": ["黑龙江少儿","黑龙江农少儿高清"],
        "精彩影视": ["精彩影视高清"],

        "吉林影视": ["吉林影视","吉视影视"],
        "吉林都市": ["吉视都市"],
        "吉林综艺文化": ["吉林综艺"],
        "吉林生活": ["吉视生活"],
        "吉林乡村": ["吉视乡村"],
        "松原生活": ["松原公共"],
        "辉南综合": ["辉南"],
        "通化新闻综合": ["通化1"],

        "辽宁都市": ["辽宁都市"],
        "辽宁影视剧": ["辽宁影视剧"],
        "辽宁生活": ["辽宁生活"],
        "辽宁教育青少": ["辽宁教育青少"],
        "辽宁北方": ["辽宁北方"],
        "辽宁公共": ["辽宁公共"],
        "辽宁经济": ["辽宁经济"],
        "辽宁移动电视": ["辽宁移动电视"],
        "辽宁体育休闲": ["辽宁体育"],

        "内蒙古新闻综合": ["内蒙古新闻综合"], 
        "内蒙古文体娱乐": ["内蒙古文体娱乐"], 
        "内蒙古经济生活": ["内蒙古经济生活"], 
        "内蒙古农牧频道": ["内蒙古农牧频道"], 
        "内蒙古少儿频道": ["内蒙古少儿频道"], 
        "内蒙古蒙语频道": ["内蒙古蒙语频道"], 
        "内蒙古蒙语频道II": ["内蒙古蒙语频道II"], 
        "包头新闻综合": ["包头新闻综合频道"],
        "包头生活服务": ["包头生活服务频道"],
        "包头经济生活": ["包头经济生活频道"],

        "新疆-3": ["新疆卫视-3","新疆3"],
        "新疆-5": ["新疆卫视-5","新疆5"],
        "新疆-2": ["新疆2"],

        "法治天地": ["法治天地高清","法制天地"],

        "龙祥时代": ["龙祥时代", "XF有线电影"],
        "汽摩": ["汽摩", "汽摩频道", "重庆汽摩"],

        "劲爆体育": ["劲爆体育高清"],
        "游戏风云": ["SiTV游戏风云", "上海游戏风云","游戏风云高清"],
        "金色学堂": ["金色学堂高清", "SiTV金色学堂", "上海金色学堂"],
        "动漫秀场": ["动漫秀场", "SiTV动漫秀场", "上海动漫秀场","动漫剧场","新动漫","动漫秀场高清"],
        "哈哈炫动": ["哈哈炫动", "炫动卡通"],
        "优漫卡通": ["优漫卡通", "优漫漫画"],
        "中国交通": ["中国交通", "中国交通频道"],
        "中国天气": ["中国气象", "中国天气频道","中央气象","中国天气高清"],
        "网络棋牌": ["网络棋牌", "IPTV网络棋牌"],
    
}



def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ==================== 远程源抓取逻辑 ====================

def _get_remaining_timeout(deadline, fallback_timeout):
    if deadline is None:
        return fallback_timeout
    remaining = deadline - time.time()
    return min(fallback_timeout, remaining) if remaining > 0 else 0


def fetch_api_data():
    """从远程 API 获取 IPTV 源列表，带重试"""
    for attempt in range(3):
        try:
            log(f"抓取远程源 (第{attempt+1}次)...")
            response = requests.get(API_URL, timeout=10)
            if response.status_code == 200:
                data = response.json()
                log(f"✅ 远程源获取成功，共 {len(data.get('results', []))} 个源")
                return data
        except Exception as e:
            log(f"⚠️ 远程源获取失败: {e}")
        time.sleep(3)
    return None


def get_ts_url(m3u8_url, deadline=None):
    """解析 m3u8 获取第一个 TS 分片 URL"""
    try:
        request_timeout = _get_remaining_timeout(deadline, 5)
        if request_timeout <= 0:
            return None
        response = requests.get(m3u8_url, timeout=request_timeout)
        if response.status_code != 200:
            return None
        for line in response.text.strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                if line.startswith('http'):
                    return line
                elif line.startswith('/'):
                    base = m3u8_url.split('/')[0] + "//" + m3u8_url.split('/')[2]
                    return base + line
                else:
                    return m3u8_url.rsplit('/', 1)[0] + "/" + line
        return None
    except Exception:
        return None


def get_download_speed(url, deadline=None):
    """测量下载速度 (MB/s)，失败返回 -1"""
    try:
        request_timeout = _get_remaining_timeout(deadline, 10)
        if request_timeout <= 0:
            return -1
        start_time = time.time()
        with requests.get(url, stream=True, timeout=request_timeout) as r:
            r.raise_for_status()
            size = 0
            chunk_size = 8192
            limit_size = 10 * 1024 * 1024
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    size += len(chunk)
                if size > limit_size:
                    break
                if time.time() - start_time > 8:
                    break
                if deadline is not None and time.time() > deadline:
                    break
        duration = time.time() - start_time
        if duration == 0:
            duration = 0.001
        return (size / 1024 / 1024) / duration
    except Exception:
        return -1


def test_host_speed(item, fetch_channels=False):
    """测试单个源的速度，返回 (speed, channels)"""
    host = item.get('host')
    match_type = item.get('matchType')
    if not host:
        return -1, []

    speed = -1
    channels = []
    deadline = time.time() + HOST_SPEED_TEST_TIMEOUT

    def timed_out():
        return time.time() > deadline

    try:
        if match_type == 'txiptv':
            if timed_out():
                return -1, channels
            json_url = f"http://{host}/iptv/live/1000.json?key=txiptv"
            try:
                request_timeout = _get_remaining_timeout(deadline, 2)
                if request_timeout <= 0:
                    return -1, channels
                response = requests.get(json_url, timeout=request_timeout)
                if response.status_code == 200:
                    json_data = response.json()
                    valid_channel_url = None
                    if 'data' in json_data:
                        for it in json_data['data']:
                            if isinstance(it, dict):
                                name = it.get('name')
                                urlx = it.get('url')
                                if not name or not urlx or ',' in urlx:
                                    continue
                                full_url = urlx if 'http' in urlx else (
                                    f"http://{host}{urlx}" if urlx.startswith('/') else f"http://{host}/{urlx}"
                                )
                                # 过滤 udp/rtp 链接
                                if 'udp' in full_url.lower() or 'rtp' in full_url.lower() or 'PLTV' in full_url or 'AHBKLIVE' in full_url:
                                    continue
                                if fetch_channels:
                                    channels.append({'name': name, 'url': full_url})
                                if not valid_channel_url:
                                    valid_channel_url = full_url
                    if valid_channel_url and not timed_out():
                        ts_url = get_ts_url(valid_channel_url, deadline=deadline)
                        if ts_url:
                            speed = get_download_speed(ts_url, deadline=deadline)
            except Exception:
                speed = -1
                
        elif match_type == 'hsmdtv':
            if timed_out():
                return -1, channels
            test_url = f"http://{host}{HSMDTV_TEST_URI}"
            ts_url = get_ts_url(test_url, deadline=deadline)
            if ts_url:
                speed = get_download_speed(ts_url, deadline=deadline)
                
        elif match_type == 'jsmpeg':
            if timed_out():
                return -1, channels
            json_url = f"http://{host}/streamer/list"
            try:
                request_timeout = _get_remaining_timeout(deadline, 2)
                if request_timeout <= 0:
                    return -1, channels
                response = requests.get(json_url, timeout=request_timeout)
                if response.status_code == 200:
                    json_data = response.json()
                    valid_channel_url = None
                    for it in json_data:
                        name = it.get('name', '').strip()
                        key = it.get('key', '').strip()
                        if not name or not key:
                            continue
                        full_url = f"http://{host}/hls/{key}/index.m3u8"
                        if 'udp' in full_url.lower() or 'rtp' in full_url.lower() or 'PLTV' in full_url or 'AHBKLIVE' in full_url:
                            continue
                        if fetch_channels:
                            channels.append({'name': name, 'url': full_url})
                        if not valid_channel_url:
                            valid_channel_url = full_url
                    if valid_channel_url and not timed_out():
                        ts_url = get_ts_url(valid_channel_url, deadline=deadline)
                        if ts_url:
                            speed = get_download_speed(ts_url, deadline=deadline)
            except Exception:
                speed = -1

        elif match_type == 'zhgxtv':
            if timed_out():
                return -1, channels
            interface_url = f"http://{host}{ZHGXTV_INTERFACE}"
            request_timeout = _get_remaining_timeout(deadline, 5)
            if request_timeout <= 0:
                return -1, channels
            target_response = requests.get(interface_url, timeout=request_timeout)
            if target_response.status_code == 200:
                content = target_response.content.decode('utf-8', errors='ignore')
                valid_channel_url = None
                for line in content.split('\n'):
                    line = line.strip()
                    if ',' in line:
                        parts = line.split(',', 2)
                        if len(parts) >= 2:
                            name = parts[0].strip()
                            url_part = parts[1].strip()
                            try:
                                if url_part.startswith("http"):
                                    p = urlparse(url_part)
                                    full_url = f"{p.scheme}://{host}{p.path}"
                                    if p.query:
                                        full_url += f"?{p.query}"
                                elif url_part.startswith("/"):
                                    full_url = f"http://{host}{url_part}"
                                else:
                                    full_url = f"http://{host}/{url_part}"
                                if 'udp' in full_url.lower() or 'rtp' in full_url.lower() or 'PLTV' in full_url or 'AHBKLIVE' in full_url:
                                    continue
                                if fetch_channels:
                                    channels.append({'name': name, 'url': full_url})
                                if not valid_channel_url:
                                    valid_channel_url = full_url
                            except Exception:
                                continue
                if valid_channel_url and not timed_out():
                    ts_url = get_ts_url(valid_channel_url, deadline=deadline)
                    if ts_url:
                        speed = get_download_speed(ts_url, deadline=deadline)
    except Exception:
        speed = -1

    return speed, channels


def fetch_channels_for_source(source):
    """获取指定源的所有频道"""
    match_type = source.get('matchType')
    if match_type in ('txiptv', 'jsmpeg', 'zhgxtv'):
        _, channels = test_host_speed(
            {'host': source.get('host'), 'matchType': match_type},
            fetch_channels=True
        )
        source['channels'] = channels or []


def normalize_channel_name(name):
    """按 CHANNEL_MAPPING 把抓到的原始名归一到标准名"""
    # 1) 基础清洗（保留你原来有用的部分，但去掉 name_map）
    name = name.replace("cctv", "CCTV").replace("中央", "CCTV").replace("央视", "CCTV")
    for rep in ["高清", "超高", "HD", "标清", "频道", "-", " ", "PLUS", "＋", "(", ")"]:
        name = name.replace(rep, "" if rep not in ("PLUS", "＋") else "+")
    name = re.sub(r"CCTV(\d+)台", r"CCTV\1", name)

    # 2) 反查 CHANNEL_MAPPING：看清洗后的名是否落在某个标准名的别名列表里
    for std_name, aliases in CHANNEL_MAPPING.items():
        if not std_name:          # 跳过空 key
            continue
        if name in aliases:
            return std_name
    return name    # 没命中就返回清洗后的原名


# ==================== 分组 & 排序 & 生成 ====================

def get_channel_group(name):
    """按 CHANNEL_CATEGORIES 反查分组，'其他频道' 兜底"""
    for grp, members in CHANNEL_CATEGORIES.items():
        if not grp:
            continue
        if name in members:
            return grp
    return "其他频道"


def channel_sort_key(name):
    """频道排序：CCTV 数字优先，然后卫视，然后其他"""
    name_upper = name.upper()
    if "CCTV" in name_upper:
        match = re.search(r"CCTV(\d+)", name_upper)
        if match:
            return (0, int(match.group(1)))
        if "5+" in name_upper or "5PLUS" in name_upper:
            return (0, 5.5)
        return (0, 999)
    if "CGTN" in name_upper:
        return (1, name)
    if "卫视" in name:
        return (2, name)
    return (3, name)


def build_logo_url(name):
    return f"{LOGO_BASE_URL}{quote(name, safe='')}.png"


def build_m3u8_entry(name, url, group_title=None):
    if group_title is None:
        group_title = get_channel_group(name)
    logo_url = build_logo_url(name)
    return f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{logo_url}" group-title="{group_title}",{name}\n{url}'


# ==================== 远程源：抓取 + 测速 + 生成 ====================

def fetch_remote_sources():
    """
    从远程 API 抓取 IPTV 源，测速选优，返回 (m3u8_content, txt_content)
    失败返回 None
    """
    # 初始化返回值，防止意外未定义
    m3u8_content = None
    txt_content = None

    data = fetch_api_data()
    if not data or not isinstance(data, dict) or "results" not in data:
        return None

    result = data["results"]
    if not result:
        return None

    # 并行测速
    results_with_speed = []
    total_hosts = len(result)
    completed_hosts = 0
    valid_hosts = 0
    log(f"开始测速 {total_hosts} 个源...")

    for i in range(0, len(result), SPEED_TEST_BATCH_SIZE):
        batch = result[i:i + SPEED_TEST_BATCH_SIZE]
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            future_to_item = {executor.submit(test_host_speed, item): item for item in batch}
            future_start_time = {future: time.time() for future in future_to_item}
            pending = set(future_to_item.keys())

            while pending:
                done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    pending.discard(future)
                    item = future_to_item[future]
                    try:
                        speed, _ = future.result()
                        if speed > 0:
                            valid_hosts += 1
                            results_with_speed.append({
                                'host': item['host'],
                                'matchType': item['matchType'],
                                'source': item.get('source', 'N/A'),
                                'speed': speed,
                                'channels': []
                            })
                    except Exception:
                        pass
                    finally:
                        completed_hosts += 1
                        if completed_hosts % 10 == 0 or completed_hosts == total_hosts:
                            log(f"测速进度: {completed_hosts}/{total_hosts} (有效: {valid_hosts})")

                now = time.time()
                timed_out_futures = [
                    f for f in list(pending)
                    if now - future_start_time.get(f, now) > HOST_SPEED_TEST_TIMEOUT
                ]
                for f in timed_out_futures:
                    pending.discard(f)
                    f.cancel()
                    completed_hosts += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    log(f"测速完成: {valid_hosts}/{total_hosts} 个有效源")

    # 筛选速度 > 0.5 MB/s 的源（降低阈值以适应 GitHub Actions 环境）
    valid_results = [r for r in results_with_speed if r['speed'] > 1]
    valid_results.sort(key=lambda x: x['speed'], reverse=True)

    # 确保每种类型至少选一个（排除已移除的 hsmdtv）
    final_sources = []
    selected_hosts = set()
    for m in ['txiptv', 'zhgxtv', 'jsmpeg', 'hsmdtv']:
        for res in valid_results:
            if res['matchType'] == m and res['host'] not in selected_hosts:
                final_sources.append(res)
                selected_hosts.add(res['host'])
                break

    # 填充剩余（按速度排序）
    for res in valid_results:
        if len(final_sources) >= TOP_N:
            break
        if res['host'] not in selected_hosts:
            final_sources.append(res)
            selected_hosts.add(res['host'])

    final_sources.sort(key=lambda x: x['speed'], reverse=True)

    if len(final_sources) < 1:  # 放宽最小源数量要求
        log(f"⚠️ 有效源不足 ({len(final_sources)} < 1)，放弃远程源")
        return None

    log(f"选中 Top {len(final_sources)} 源:")
    for idx, src in enumerate(final_sources):
        log(f"  源{idx+1}: {src['host']} ({src['matchType']}) {src['speed']:.2f}MB/s")

    # 抓取频道
    all_entries = []
    for idx, source in enumerate(final_sources):
        log(f"抓取频道: 源{idx+1} {source['host']} ({source['matchType']})...")
        fetch_channels_for_source(source)

        match_type = source['matchType']
        channels = source.get('channels', [])

        if match_type == 'hsmdtv':
            # hsmdtv 需要从 hsmd_address_list.txt 生成频道
            entries = process_hsmdtv_channels(source['host'], idx)
        if channels:
            for ch in channels:
                name = normalize_channel_name(ch['name'])
                group = get_channel_group(name)
                all_entries.append({
                    'name': name,
                    'url': ch['url'],
                    'group': group,
                    'content': build_m3u8_entry(name, ch['url'], group),
                    'index': idx,
                    'speed': source['speed']  # 记录源速度用于排序
                })

    if not all_entries:
        log("⚠️ 远程源未抓取到任何频道")
        return None

    log(f"远程源共抓取到 {len(all_entries)} 条频道记录")

    # 按频道名分组（同名频道保留多个源）
    grouped = {}
    for entry in all_entries:
        name = entry['name']
        if name not in grouped:
            grouped[name] = []
        grouped[name].append(entry)

    # 排序频道名
    unique_names = sorted(grouped.keys(), key=channel_sort_key)

    # 生成 M3U8（按分组顺序，同一频道按速度降序排列）
    beijing_tz = timezone(timedelta(hours=8))
    beijing_now = datetime.now(beijing_tz)
    update_time = beijing_now.strftime("%y/%m/%d %H:%M:%S")
    m3u8_lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"', f"#EXT-X-UPDATED: {update_time}"]

    # 按分组整理频道
    grouped_by_group = {}
    for entry in all_entries:
        grp = entry['group']
        if grp not in grouped_by_group:
            grouped_by_group[grp] = {}
        name = entry['name']
        if name not in grouped_by_group[grp]:
            grouped_by_group[grp][name] = []
        grouped_by_group[grp][name].append(entry)

    # 按 GROUP_ORDER 顺序输出
    seen_groups = set()
    for grp in GROUP_ORDER:
        if grp in grouped_by_group:
            seen_groups.add(grp)
            # 分组内频道按 channel_sort_key 排序
            names = sorted(grouped_by_group[grp].keys(), key=channel_sort_key)
            for name in names:
                entries_list = sorted(grouped_by_group[grp][name], key=lambda x: x['speed'], reverse=True)
                for entry in entries_list:
                    m3u8_lines.append(entry['content'])

    # 处理不在 GROUP_ORDER 中的分组（如果有的话）
    for grp in grouped_by_group:
        if grp not in seen_groups:
            names = sorted(grouped_by_group[grp].keys(), key=channel_sort_key)
            for name in names:
                entries_list = sorted(grouped_by_group[grp][name], key=lambda x: x['speed'], reverse=True)
                for entry in entries_list:
                    m3u8_lines.append(entry['content'])

    m3u8_content = "\n".join(m3u8_lines)

    # 生成 TXT（按分组输出，同一频道按速度降序排列）
    # 先按分组整理
    grouped_by_group = {}
    for entry in all_entries:
        grp = entry['group']
        if grp not in grouped_by_group:
            grouped_by_group[grp] = {}
        name = entry['name']
        if name not in grouped_by_group[grp]:
            grouped_by_group[grp][name] = []
        grouped_by_group[grp][name].append((entry['url'], entry['speed']))

    txt_lines = []
    # 按 GROUP_ORDER 顺序输出
    seen_groups = set()
    for grp in GROUP_ORDER:
        if grp in grouped_by_group:
            seen_groups.add(grp)
            txt_lines.append(f"{grp},#genre#")
            # 频道名称排序
            names = sorted(grouped_by_group[grp].keys(), key=channel_sort_key)
            for name in names:
                # 对同一频道的多个URL按速度降序排列
                urls_sorted = sorted(grouped_by_group[grp][name], key=lambda x: x[1], reverse=True)
                for url, _ in urls_sorted:
                    txt_lines.append(f"{name},{url}")
            txt_lines.append("")  # 分组间空行

    # 输出不在 GROUP_ORDER 中的分组
    for grp in grouped_by_group:
        if grp not in seen_groups:
            txt_lines.append(f"{grp},#genre#")
            names = sorted(grouped_by_group[grp].keys(), key=channel_sort_key)
            for name in names:
                urls_sorted = sorted(grouped_by_group[grp][name], key=lambda x: x[1], reverse=True)
                for url, _ in urls_sorted:
                    txt_lines.append(f"{name},{url}")
            txt_lines.append("")

    txt_content = "\n".join(txt_lines).strip()

    del results_with_speed, valid_results, final_sources, all_entries, grouped
    gc.collect()

    return m3u8_content, txt_content

def process_hsmdtv_channels(host, source_index):
    """处理 hsmdtv 源频道"""
    entries = []
    try:
        if not os.path.exists(HSMD_ADDRESS_LIST_FILE):
            log(f"⚠️ {HSMD_ADDRESS_LIST_FILE} 不存在，跳过 hsmdtv 源")
            return entries
        with open(HSMD_ADDRESS_LIST_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = re.search(r'(http://[^\s]+)', line)
            if match:
                url_in_file = match.group(1)
                part_before_url = line.split(url_in_file)[0]
                name = re.sub(r'^\s*\d+\s+', '', part_before_url).strip()
                name = name.replace("（默认频道）", "").strip()
                name = clean_channel_name(name)
                parsed = urlparse(url_in_file)
                new_url = f"http://{host}{parsed.path}"
                group = get_channel_group(name)
                entries.append({
                    'name': name, 'url': new_url, 'group': group,
                    'content': build_m3u8_entry(name, new_url, group),  #, group
                    'index': source_index
                })
    except Exception as e:
        log(f"⚠️ 处理 hsmdtv 频道失败: {e}")
    return entries

# ==================== 主流程 ====================

def save_output(m3u8_content, txt_content):
    """将生成的内容保存到本地文件"""
    try:
        os.makedirs(os.path.dirname(OUTPUT_M3U8) or '.', exist_ok=True)
        with open(OUTPUT_M3U8, 'w', encoding='utf-8') as f:
            f.write(m3u8_content)
        log(f"✅ M3U8 已保存到 {OUTPUT_M3U8} ({len(m3u8_content)} 字节)")

        os.makedirs(os.path.dirname(OUTPUT_TXT) or '.', exist_ok=True)
        with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
            f.write(txt_content)
        log(f"✅ TXT 已保存到 {OUTPUT_TXT} ({len(txt_content)} 字节)")
        return True
    except Exception as e:
        log(f"❌ 保存文件异常: {e}")
        return False


def main():
    log("=" * 50)
    log("IPTV 播放列表自动更新（远程源抓取 + 本地保存）")
    log("=" * 50)

    m3u8_content = None
    txt_content = None

    # 尝试远程抓取
    try:
        result = fetch_remote_sources()
        if result:
            m3u8_content, txt_content = result
    except Exception as e:
        log(f"❌ 远程抓取异常: {e}")

    if not m3u8_content:
        log("❌ 远程源获取失败，本次更新跳过")
        return False

    log(f"📊 数据来源: 远程源")
    log(f"   M3U8: {len(m3u8_content)} 字节, TXT: {len(txt_content)} 字节")

    # 保存到本地文件
    success = save_output(m3u8_content, txt_content)

    if success:
        log("🎉 更新完成!")
    else:
        log("💔 保存失败")

    return success


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
