/*
 * ============================================================================
 *  ysp.js  ——  央视频 / 央视影音 直播直链解析脚本
 *  适配：ouTV 及其它内置 Rhino + HttpBridge(req) 的播放器
 *
 *  原理：本地构造 cKey 签名（TEA + XOR + cksum + 自定义 base64），
 *        向 bkliveinfo.ysp.cctv.cn 发起 GET，取回官方 m3u8 直链。
 *        全程只用到 req()，不依赖任何原生方法。
 *
 *  用法（二选一）：
 *     ① CCTV-1,local://ysp.js            ← 播 CH 指定的台
 *     ② CCTV-5,local://ysp.js/cctv5      ← 用 URL 尾巴指定台（需 App 支持传参）
 *        写频道键名(cctv5)、中文名(CCTV-5)、或数字ID(600001859 / 2024078201) 都能认
 *  换台：改下面 CH（可选值见 CHANNELS 表）
 *  画质：DEFN = "fhd" 高清 / "shd" 标清（高清失败会自动降级）
 *
 *  说明：第一/风云/怀旧剧场是付费频道，高清需登录 VIP，
 *        标清匿名可取，故表中第 4 位固定写 "shd"。
 * ============================================================================
 */

var CH   = "cctv1";   /* ← 要播的频道，改这一行 */
var DEFN = "fhd";     /* ← 首选画质 */

/*
 * 频道表：[显示名, cnlid, livepid, 首选画质(可留空), 分组]
 * 分组仅用于生成播放列表，脚本本身不依赖。
 */
var CHANNELS = {
    /* ---------------- 央视开路频道 ---------------- */
    cctv1:    ["CCTV-1 综合",      "2024078201", "600001859", "", "央视"],
    cctv2:    ["CCTV-2 财经",      "2024075401", "600001800", "", "央视"],
    cctv3:    ["CCTV-3 综艺",      "2024068501", "600001801", "", "央视"],
    cctv4:    ["CCTV-4 中文国际",  "2029797101", "600001814", "", "央视"],
    cctv5:    ["CCTV-5 体育",      "2024078401", "600001818", "", "央视"],
    cctv5p:   ["CCTV-5+ 体育赛事", "2024078001", "600001817", "", "央视"],
    cctv6:    ["CCTV-6 电影",      "2013693901", "600108442", "", "央视"],
    cctv7:    ["CCTV-7 国防军事",  "2024072001", "600004092", "", "央视"],
    cctv8:    ["CCTV-8 电视剧",    "2029793001", "600001803", "", "央视"],
    cctv9:    ["CCTV-9 纪录",      "2024078601", "600004078", "", "央视"],
    cctv10:   ["CCTV-10 科教",     "2024078701", "600001805", "", "央视"],
    cctv11:   ["CCTV-11 戏曲",     "2027248701", "600001806", "", "央视"],
    cctv12:   ["CCTV-12 社会与法", "2027248801", "600001807", "", "央视"],
    cctv13:   ["CCTV-13 新闻",     "2029797201", "600001811", "", "央视"],
    cctv14:   ["CCTV-14 少儿",     "2027248901", "600001809", "", "央视"],
    cctv15:   ["CCTV-15 音乐",     "2027249001", "600001815", "", "央视"],
    cctv16:   ["CCTV-16 奥林匹克", "2027249101", "600098637", "", "央视"],
    cctv17:   ["CCTV-17 农业农村", "2027249401", "600001810", "", "央视"],
    cctv4k:   ["CCTV-4K 超高清",   "2029810301", "600002264", "", "央视"],
    cctv8k:   ["CCTV-8K 超高清",   "2026774101", "600156816", "", "央视"],
    cctv164k:   ["CCTV-16 4K",    "2027249301", "600099502", "fhd", "央视"],
    cgtn:     ["CGTN",             "2024181701", "600014550", "", "央视"],
    cgtnfr:   ["CGTN 法语",        "2024181801","600084704","fhd", "央视"],
    cgtnru:   ["CGTN 俄语",        "2024181901","600084758","fhd", "央视"],
    cgtnar:   ["CGTN 阿拉伯语",    "2024182001","600084782","fhd", "央视"],
    cgtnes:   ["CGTN 西班牙语",    "2024182101","600084744","fhd", "央视"],
    cgtndoc:   ["CGTN 纪录",        "2024182301","600084781","fhd", "央视"],
    dyjc:     ["CCTV-第一剧场",    "2026874203", "600099655", "shd", "央视"],
    fyjc:     ["CCTV-风云剧场",    "2025637103", "600099658", "shd", "央视"],
    hjjc:     ["CCTV-怀旧剧场",    "2026874303", "600099620", "shd", "央视"],
    cetv1:    ["CETV-1 中国教育1台", "2022823801", "600171827", "", "央视"],
    guoxue:   ["国学频道",    "2029360403", "600213139", "fhd", "央视"],

    /* ---------------- 卫视频道 ---------------- */
    bjws:     ["北京卫视",   "2024052703", "600002309", "", "卫视"],
    dfws:     ["东方卫视",   "2024054503", "600002483", "", "卫视"],
    jsws:     ["江苏卫视",   "2024171103", "600002521", "", "卫视"],
    zjws:     ["浙江卫视",   "2024054703", "600002520", "", "卫视"],
    hunws:    ["湖南卫视",   "2024054803", "600002475", "", "卫视"],
    hbws:     ["湖北卫视",   "2024171203", "600002508", "", "卫视"],
    gdws:     ["广东卫视",   "2024060903", "600002485", "", "卫视"],
    gxws:     ["广西卫视",   "2024060703", "600002509", "", "卫视"],
    hljws:    ["黑龙江卫视", "2029797003", "600002498", "", "卫视"],
    haiws:    ["海南卫视",   "2024055603", "600002506", "", "卫视"],
    cqws:     ["重庆卫视",   "2024061103", "600002531", "", "卫视"],
    szws:     ["深圳卫视",   "2024061303", "600002481", "", "卫视"],
    scws:     ["四川卫视",   "2024061403", "600002516", "", "卫视"],
    henanws:  ["河南卫视",   "2029797303", "600002525", "", "卫视"],
    dnws:     ["东南卫视",   "2024061503", "600002484", "", "卫视"],
    gzws:     ["贵州卫视",   "2024061603", "600002490", "", "卫视"],
    jxws:     ["江西卫视",   "2024061703", "600002503", "", "卫视"],
    lnws:     ["辽宁卫视",   "2024171303", "600002505", "", "卫视"],
    ahws:     ["安徽卫视",   "2024171403", "600002532", "", "卫视"],
    hebws:    ["河北卫视",   "2024171503", "600002493", "", "卫视"],
    sdws:     ["山东卫视",   "2029787903", "600002513", "", "卫视"],
    tjws:     ["天津卫视",   "2019927003", "600152137", "", "卫视"],
    shxws:    ["陕西卫视",   "2029795103", "600190400", "", "卫视"],
    nmgws:    ["内蒙古卫视", "2025561203", "600190401", "", "卫视"],
    gsws:     ["甘肃卫视",   "2025561703", "600190408", "", "卫视"],
    nxws:     ["宁夏卫视",   "2025608503", "600190737", "", "卫视"],
    sxws:     ["山西卫视",   "2025560803", "600190407", "", "卫视"],
    ynws:     ["云南卫视",   "2025561303", "600190402", "", "卫视"],
    jlws:     ["吉林卫视",   "2025561503", "600190405", "", "卫视"],
    qhws:     ["青海卫视",   "2025559103", "600190406", "", "卫视"],
    xzws:     ["西藏卫视",   "2025558003", "600190403", "", "卫视"],
    xjws:     ["新疆卫视",   "2019927403", "600152138", "", "卫视"],
    btws:     ["兵团卫视",   "2025990501", "600193252", "", "卫视"]
};

/* ============================ 基础工具 ============================ */

var HEX = "0123456789abcdef";
var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

function u32(v) { return v >>> 0; }

function rndBytes(n) {
    var a = [], i;
    for (i = 0; i < n; i++) a.push((Math.random() * 256) | 0);
    return a;
}

function hexOf(bytes, upper) {
    var s = "", h = upper ? "0123456789ABCDEF" : HEX, i;
    for (i = 0; i < bytes.length; i++) {
        s += h.charAt((bytes[i] >> 4) & 15) + h.charAt(bytes[i] & 15);
    }
    return s;
}

/* 字符串 → UTF-8 字节数组 */
function utf8Of(s) {
    var out = [], i, c, c2;
    for (i = 0; i < s.length; i++) {
        c = s.charCodeAt(i);
        if (c < 0x80) {
            out.push(c);
        } else if (c < 0x800) {
            out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
        } else if (c >= 0xd800 && c < 0xdc00) {
            c2 = s.charCodeAt(++i);
            c = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
            out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63),
                     0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
        } else {
            out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
        }
    }
    return out;
}

function be16(v) { return [(v >>> 8) & 255, v & 255]; }

function be32(v) {
    v = u32(v);
    return [(v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255];
}

/* 2 字节大端长度 + 内容 */
function lp(s) {
    var b = (typeof s === "string") ? utf8Of(s) : s;
    var out = be16(b.length), i;
    for (i = 0; i < b.length; i++) out.push(b[i]);
    return out;
}

function pushAll(dst, src) {
    var i;
    for (i = 0; i < src.length; i++) dst.push(src[i]);
}

/* 自定义 base64：+/ 换成 _-，去掉填充的 = */
function b64url(bytes) {
    var s = "", n = bytes.length, i, b0, b1, b2;
    for (i = 0; i < n; i += 3) {
        b0 = bytes[i];
        b1 = (i + 1 < n) ? bytes[i + 1] : 0;
        b2 = (i + 2 < n) ? bytes[i + 2] : 0;
        s += B64.charAt(b0 >> 2);
        s += B64.charAt(((b0 & 3) << 4) | (b1 >> 4));
        if (i + 1 < n) s += B64.charAt(((b1 & 15) << 2) | (b2 >> 6));
        if (i + 2 < n) s += B64.charAt(b2 & 63);
    }
    return s.replace(/\+/g, "_").replace(/\//g, "-");
}

/* 校验和：v = (0x83 * v + b) & 0x7fffffff */
function cksum(bytes) {
    var v = 0, i;
    for (i = 0; i < bytes.length; i++) v = (0x83 * v + bytes[i]) & 0x7fffffff;
    return v;
}

/* ============================ TEA 加密 ============================ */

var TEA_KEY  = [0x59b2f7cf, 0x725ef43c, 0x34fdd7c1, 0x23411ed3];
var GTEA_KEY = [0x110dbec1, 0x0c23e7d2, 0xe56a1cad, 0x6914ef1b];
var XOR  = [0x84, 0x2e, 0xed, 0x08, 0xf0, 0x66, 0xe6, 0xea,
            0x48, 0xb4, 0xca, 0xa9, 0x91, 0xed, 0x6f, 0xf3];
var GXOR = [0xb3, 0xc9, 0x53, 0xa0, 0x69, 0x13, 0xad, 0x4d];

/* 单块 TEA（16 轮），输入 8 字节，输出 8 字节 */
function teaBlock(b, off, k) {
    var y = ((b[off] << 24) | (b[off + 1] << 16) | (b[off + 2] << 8) | b[off + 3]) >>> 0;
    var z = ((b[off + 4] << 24) | (b[off + 5] << 16) | (b[off + 6] << 8) | b[off + 7]) >>> 0;
    var s = 0, i, t;
    for (i = 0; i < 16; i++) {
        s = u32(s + 0x9e3779b9);
        t = u32(u32(u32(z << 4) + k[0]) ^ u32(z + s) ^ u32(u32(z >>> 5) + k[1]));
        y = u32(y + t);
        t = u32(u32(u32(y << 4) + k[2]) ^ u32(y + s) ^ u32(u32(y >>> 5) + k[3]));
        z = u32(z + t);
    }
    return [(y >>> 24) & 255, (y >>> 16) & 255, (y >>> 8) & 255, y & 255,
            (z >>> 24) & 255, (z >>> 16) & 255, (z >>> 8) & 255, z & 255];
}

/* TEA + 类 CBC 链式混淆（对应原算法的 _tea_pkt） */
function teaPkt(data, key) {
    var pad = (8 - ((data.length + 10) % 8)) % 8;
    var plain = [], i, j, mixed, enc, cipher;

    plain.push((((Math.random() * 256) | 0) & 0xf8) | pad);
    pushAll(plain, rndBytes(pad + 2));
    pushAll(plain, data);
    for (i = 0; i < 7; i++) plain.push(0);

    var out = [], pp = [0, 0, 0, 0, 0, 0, 0, 0], pc = [0, 0, 0, 0, 0, 0, 0, 0];
    for (i = 0; i < plain.length; i += 8) {
        mixed = [];
        for (j = 0; j < 8; j++) mixed.push((plain[i + j] ^ pc[j]) & 255);
        enc = teaBlock(mixed, 0, key);
        cipher = [];
        for (j = 0; j < 8; j++) {
            cipher.push((enc[j] ^ pp[j]) & 255);
            out.push(cipher[j]);
        }
        pp = mixed;
        pc = cipher;
    }
    return out;
}

function xorWith(bytes, key) {
    var out = [], i;
    for (i = 0; i < bytes.length; i++) out.push((bytes[i] ^ key[i % key.length]) & 255);
    return out;
}

/* ============================ cKey 签名 ============================ */

var PLATFORM = 4330403;
var APPVER   = "V8.22.1035.3031";

function tail5(v) {
    var t = String(v);
    return t.length >= 5 ? t.substring(t.length - 5) : "";
}

function ckGuard(ts, guid) {
    var body = be32(ts), i;
    pushAll(body, lp(tail5(guid)));
    pushAll(body, lp(tail5("null")));
    pushAll(body, lp(tail5("null")));
    pushAll(body, lp(tail5("-1")));

    var plain = lp(body);
    var enc = teaPkt(plain, GTEA_KEY);
    pushAll(enc, be32(cksum(plain)));
    return hexOf(xorWith(enc, GXOR), true);
}

function buildCKey(channelId) {
    var ts = Math.floor(new Date().getTime() / 1000);
    var guid = hexOf(rndBytes(16), false);
    var guard = ckGuard(ts, guid);
    var uid = hexOf(rndBytes(4), true);
    var i;

    var body = [0x00, 0x00, 0x00, 0x42, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x04, 0xd2];
    pushAll(body, be32(PLATFORM));
    pushAll(body, be32(0));
    pushAll(body, be32(ts));
    pushAll(body, lp("dcgh"));
    pushAll(body, lp("_zj1A5Gh6QYcxWjIUGos2w=="));
    pushAll(body, lp(APPVER));
    pushAll(body, lp(String(channelId)));
    pushAll(body, lp(guid));
    pushAll(body, be32(1));
    pushAll(body, be32(1));
    pushAll(body, lp(uid));
    pushAll(body, lp("nil"));
    pushAll(body, lp("57eab0c4-2c58-44c6-8ae9-dd2757525dc5"));
    pushAll(body, lp("nil"));
    pushAll(body, lp("v0.1.000"));
    pushAll(body, lp("com.cctv.yangshipin.app.iphone"));
    pushAll(body, lp(String(PLATFORM)));
    pushAll(body, lp("ex_json_bus"));
    pushAll(body, lp("ex_json_vs"));
    pushAll(body, lp(guard));

    var pkt = be16(body.length);
    pushAll(pkt, body);

    var sum = be32(cksum(pkt));
    pkt[18] = sum[0]; pkt[19] = sum[1]; pkt[20] = sum[2]; pkt[21] = sum[3];

    var enc = teaPkt(pkt, TEA_KEY);
    pushAll(enc, be32(cksum(pkt)));
    var raw = xorWith(enc, XOR);

    return {
        cKey:   "--01" + b64url(raw),
        guid:   guid,
        ts:     ts,
        flowId: hexOf(rndBytes(16), true) + "_" + PLATFORM
    };
}

/* ============================ 接口请求 ============================ */

function qs(params) {
    var out = [], k;
    for (k in params) {
        if (params.hasOwnProperty(k)) {
            out.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(params[k])));
        }
    }
    return out.join("&");
}

/* 取回该频道的候选 m3u8 地址（已按稳定性排序） */
function fetchPlayUrls(channelId, livePid, defn) {
    var t = buildCKey(channelId);

    var params = {
        atime: "120",
        livepid: livePid,
        cnlid: channelId,
        appVer: APPVER,
        app_version: "300090",
        caplv: "1",
        cmd: "2",
        defn: defn || "fhd",
        device: "iPhone",
        encryptVer: "4.2",
        getpreviewinfo: "0",
        hevclv: "0",
        lang: "zh-Hans_CN",
        livequeue: "0",
        logintype: "1",
        nettype: "1",
        newnettype: "1",
        newplatform: String(PLATFORM),
        platform: String(PLATFORM),
        sdtfrom: "v3021",
        spacode: "23",
        spaudio: "1",
        spdemuxer: "6",
        spdrm: "2",
        spdynamicrange: "1",
        spflv: "1",
        spflvaudio: "1",
        sphdrfps: "60",
        sphttps: "1",
        spvcode: "SCgzMDoxMDgwLDYwOjEwODB8MzA6MTA4MCw2MDoxMDgwKQ==",
        spvideo: "4",
        stream: "1",
        system: "1",
        sysver: "ios18.2.1",
        uhd_flag: "0",
        cKey: t.cKey,
        guid: t.guid,
        fntick: String(t.ts),
        flowid: t.flowId,
        playbacktime: "0"
    };

    var resp = req("https://bkliveinfo.ysp.cctv.cn/?" + qs(params), {
        method: "GET",
        headers: { "User-Agent": "qqlive", "Accept": "application/json" }
    });

    var res;
    try {
        res = JSON.parse(resp);
    } catch (e) {
        throw new Error("bkliveinfo 返回非 JSON：" + String(resp).substring(0, 100));
    }

    var code = parseInt(res.iretcode, 10);
    if (code !== 0) {
        var why = res.errinfo || "";
        if (code === 25)      why = "需登录（付费频道，高清锁 VIP，标清一般免费）";
        else if (code === 8)  why = "节目已结束（该频道 ID 可能已过期）";
        else if (code === 14) why = "URI 参数不合法";
        throw new Error("接口拒绝 iretcode=" + code + " " + why);
    }

    var urls = [], i, u;
    if (res.playurl) urls.push(res.playurl);

    var bu = res.backurl_list || res.backurlList || res.backurl;
    if (bu && bu.length !== undefined) {
        for (i = 0; i < bu.length; i++) {
            u = bu[i];
            urls.push(typeof u === "string" ? u : (u.url || u.playurl || ""));
        }
    } else if (typeof bu === "string") {
        var sp = bu.split(/[;,]/);
        for (i = 0; i < sp.length; i++) urls.push(sp[i]);
    }

    var out = [], seen = {};
    for (i = 0; i < urls.length; i++) {
        u = urls[i];
        if (!u || u.indexOf(".cctv.") < 0 || seen[u]) continue;
        seen[u] = 1;
        out.push(u);
    }
    if (!out.length) throw new Error("没有解析到 m3u8 地址");

    /* 保持接口给的顺序：playurl 在前（官方首选），backurl_list 里的 bk 备份在后。
       不再按 bklive- 重排 —— 两条都是独立签发的地址，交给列表里的 ?l=1 去选。 */
    return out;
}

/* ===== 故意删除「预取验证」=====
 * 旧版有个 pickPlayable()：先把候选 m3u8 各取一遍、确认能出 #EXTM3U 才返回。
 * 这在央视频 CDN 上是致命的：同一条直链 ~30 秒内只放行 1~2 次，
 * 验证用掉的那次就是播放器要用的那次，播放器随后拿到 403 → 黑屏。
 * 结论：宁可把可能失效的地址直接交给播放器（失败后 App 会自动换线路），
 *       也绝不提前请求。此处若将来要加「测速」，请改用接口层 iretcode 判断。
 */

/* ============================ 入口 ============================ */

/*
 * 解析入口参数。摸鱼TV 调 main() 时会传一个参数（URL 里 .js 之后那一段，可能是空的），
 * 支持写法：
 *     cctv1 / CCTV-5 / 600001859 / 2024078201
 *     cctv1?l=1&q=shd      （l=选第几条候选线路，q=画质）
 *     cctv1/1              （同上，简写）
 * 认不出来就退回 CH，绝不因此报错。
 */
function parseTail(p) {
    var out = { key: CH, line: 0, qual: "" };
    if (p === undefined || p === null) return out;
    var s = String(p).replace(/^\s+|\s+$/g, "");
    if (!s) return out;

    /* 万一传进来的是整条 URL，先截掉 .js 之后的部分 */
    var m = s.match(/\.js[/?](.*)$/);
    if (m && m[1]) s = m[1];

    s = s.replace(/^[?&/]+/, "");

    /* 拆出 ?a=b&c=d 形式的参数 */
    var q = "", cut = s.search(/[?#]/);
    if (cut >= 0) { q = s.substring(cut + 1); s = s.substring(0, cut); }

    /* 拆出 cctv1/1 形式的线路号 */
    if (s.indexOf("/") > 0) {
        var parts = s.split("/");
        s = parts[0];
        if (/^\d+$/.test(parts[parts.length - 1])) out.line = parseInt(parts[parts.length - 1], 10);
    }

    s = s.replace(/^ch=/, "").replace(/^\s+|\s+$/g, "");

    if (q) {
        var kv = q.split(/[&;]/), i, eq, k, v;
        for (i = 0; i < kv.length; i++) {
            eq = kv[i].indexOf("=");
            if (eq < 0) continue;
            k = kv[i].substring(0, eq).toLowerCase();
            v = kv[i].substring(eq + 1);
            if ((k === "l" || k === "line") && /^\d+$/.test(v)) {
                out.line = parseInt(v, 10);
            } else if (k === "q" || k === "defn") {
                if (v === "fhd" || v === "shd" || v === "hd") out.qual = v;
            }
        }
    }

    if (!s) return out;

    if (CHANNELS[s]) { out.key = s; return out; }

    var low = s.toLowerCase();
    if (CHANNELS[low]) { out.key = low; return out; }

    var k1;
    for (k1 in CHANNELS) {
        if (CHANNELS[k1][1] === s || CHANNELS[k1][2] === s) { out.key = k1; return out; }
    }
    /* 按中文名 / 简名反查："CCTV-5" 能命中 "CCTV-5 体育" */
    for (k1 in CHANNELS) {
        var full = CHANNELS[k1][0];
        if (full === s || full.split(" ")[0] === s) { out.key = k1; return out; }
    }

    console.log("未知频道参数 " + s + "，回落到 " + CH);
    return out;
}

function nowMs() { return new Date().getTime(); }

function main(param) {
    var t0 = nowMs();
    var p = parseTail(param);
    var info = CHANNELS[p.key];
    if (!info) throw new Error("未知频道：" + p.key);

    /* 第 4 位指定了画质（如付费频道的 shd）就不再试高清 */
    var first = p.qual || info[3] || DEFN;
    var defs = [first];
    if (first !== "shd") defs.push("shd");

    var lastErr = "", d, list, url, code;
    for (d = 0; d < defs.length; d++) {
        try {
            list = fetchPlayUrls(info[1], info[2], defs[d]);
        } catch (e) {
            lastErr = String((e && e.message) || e);
            /* 只有「画质不可用 / 需 VIP / 参数不合法」才值得换一档再试；
               网络异常、超时一律立刻放弃，重试只会把 App 的 30 秒耗尽。 */
            var m = lastErr.match(/iretcode=(\d+)/);
            code = m ? parseInt(m[1], 10) : -1;
            if (code === 25 || code === 14 || code === 8 || code === 400 ||
                lastErr.indexOf("没有解析到") >= 0) continue;
            break;
        }

        url = list[p.line] || list[0];
        console.log("[" + info[0] + "] " + defs[d] + " 候选 " + list.length +
                    " 个 线路 " + (p.line + 1) + " 用时 " + (nowMs() - t0) + "ms → " +
                    url.substring(0, 56));
        /* 直接返回，绝不再请求 —— 详见上方「故意删除预取验证」 */
        return url;
    }

    throw new Error(info[0] + " 解析失败（" + (nowMs() - t0) + "ms）：" + lastErr);
}
