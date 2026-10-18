# -*- coding: utf-8 -*-
"""JD 稳定身份：结构化 ID 的生成 / 校验 / 手改 + JD 库（`jds.json`）。

**本模块修的是根因。** 以前岗位身份 = **报告文件名**（`20261003_语音文本评测_75分`），
文件名里嵌了**日期与分数**，于是「换个简历重分析」必然产生一个新身份 →
知识库 `from_jds` 越叠越多 → `multi_jd`（被 N 个岗位提到，复习优先级的**乘性**因子）
算错。用户真实数据里 6 个概念的 `multi_jd` **全部**来自同一份 JD 的两次分析。
阶段 3.6 用模糊指纹做了补救（治标），本模块把**身份本身**改对（治本）：

    本科_【未知单位】_标注质检_文本_北京_13-26K_0001
    └学历 └单位        └职责   └领域 └城市 └薪资    └编号（全局递增 4 位）

**分隔符是 `_`，不是 `-`**：薪资本身就是 `13-26K`，用 `-` 会分不清字段边界。

**铁律**：
  1. `jdid` 可以 import `store` / `kb` / `log`；**绝不 import `jobdup`**（指纹判定是任务 2 的
     接线，本模块只做身份）。
  2. `kb.py` **绝不能** import `jdid`（kb 是纯数据模块，方向不能反）。
  3. 写 `knowledge.json` 必须持 `kb._KB_LOCK`（本项目真实发生过并发丢更新 / 写坏文件）；
     写 `jds.json` 持本模块的 `_JD_LOCK`。**两把锁的获取顺序恒定为 `_JD_LOCK` → `_KB_LOCK`**，
     绝不反向，否则会死锁。
  4. `rename_jd` 是「算 → 写」两段：**全部改动先在内存里算出来、校验通过后再落盘**；
     落盘中途任一步抛错就用内存里的原像回滚，绝不留半改状态（jds.json 改了、知识库没改，
     等于把用户的引用全部指丢）。
  5. 已有的报告文件**永不重命名**（它们只是产物，台账通过 `jobs.json.report` 找到它们）。
"""
import re
import threading

import kb
import log
import store

import jdrefs

# ---------- 常量（来自规格 §3/§4，别拍） ----------
SEP = "_"                       # 分隔符用 _，因为薪资里本身有 -（13-26K）
UNKNOWN = "【未知%s】"           # 【未知单位】/【未知学历】/【未知职责】/【未知城市】/【未知薪资】/【未知领域】
BAD_CHARS = r'\/:*?"<>|'        # 文件名非法字符（必须清洗，ID 会进文件名）
DOMAIN_RULES = [("多模态", "多模态"), ("语音", "语音"), ("视频", "视频"),
                ("图片", "图片"), ("图像", "图片"), ("文本", "文本"), ("Agent", "Agent")]
SEGMENTS = ("education", "company", "duty", "domain", "city", "salary", "seq")
SEG_COUNT = len(SEGMENTS)       # 7 段

# 每段的占位符名字（拼成 UNKNOWN 里的那个词）
_UNKNOWN_WORD = {"education": "学历", "company": "单位", "duty": "职责",
                 "domain": "领域", "city": "城市", "salary": "薪资"}

# 清洗用：非法字符 + 所有空白（ID 会进文件名，空格会带来引号地狱）
_BAD_RE = re.compile(r"[\\/:*?\"<>|\s]+")

# 城市提取：先认这些已知城市名（省会 / 直辖市），认不出再退到「第一个 区/县/市… 之前」
_KNOWN_CITIES = (
    "北京", "上海", "天津", "重庆", "广州", "深圳", "成都", "杭州", "南京", "武汉",
    "西安", "苏州", "长沙", "郑州", "合肥", "青岛", "厦门", "福州", "济南", "大连",
    "沈阳", "哈尔滨", "长春", "昆明", "贵阳", "南宁", "太原", "石家庄", "乌鲁木齐",
    "兰州", "西宁", "银川", "呼和浩特", "海口", "三亚", "珠海", "东莞", "佛山", "无锡",
    "宁波", "常州", "南昌", "温州", "嘉兴", "泉州", "烟台", "徐州", "潍坊", "保定",
)
_CITY_MAX = 6

# ---------- 字段归一化（ID 要短到能当文件名、能在台账里一眼读）----------
# 为什么必须归一化：`REPORT_DATA` 里的原始字段是**给人读的说明**，不是身份。
# 用真实数据举例，不归一化的话 ID 是 70 个字符：
#   统招公办本科及以上_四川金沙人力资源开发管理有限公司（金沙人力）_数据标注质检_视频_成都_6-8K（入职五险，提及一金7k+）_0001
# 而用户要的形态是 30 个字符：本科_金沙人力_数据标注质检_视频_成都_6-8K_0001。
# 归一化只做「三段」（学历 / 单位 / 薪资）—— 职责、领域、城市本来就是短的。
_EDU_TIERS = ("博士", "硕士", "本科", "大专", "中专", "高中", "不限")
# 学历档位按「高 → 低」排：取**出现的最高档**（「统招公办本科及以上」是本科，
# 「本科及以上，硕士优先」按最高档算硕士）。`不限` 垫底。
_COMPANY_MAX = 12
# 单位截断上限。真实数据里最长的那条（`四川金沙人力资源开发管理有限公司`，16 字）
# 截到 12 字后整条 ID 是 38 字，仍在「≤40 字」这条可读性底线内。
_SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—－~～至到]\s*(\d+(?:\.\d+)?)\s*[kK]")
# 只认 `x-yK` 区间；破折号把半角/全角/波浪/「至」「到」都算上
# （真实数据里 `13–26K` 用的是**全角破折号** U+2013，不归一化会和 `13-26K` 变成两个身份）。
_BRACKET_RE = re.compile(r"[（(]([^（）()]*)[）)]")
_BRACKET_OPEN_RE = re.compile(r"[（(]")
# 法定组织形式后缀，**必须按长度降序**：顺序反了 `集团有限公司` 会先被 `公司` 咬掉一截
# → `某某集团有限`（半个后缀，正是这条规则要避免的东西）。
# `分公司` 也在表里：中缀括号那条路会得到 `某公司分公司`，若只认 `公司` 会剥成 `某公司分`
# （吊着一个 `分`）。它比 `公司` 长，所以排在前面。
_LEGAL_SUFFIXES = ("股份有限公司", "有限责任公司", "集团有限公司", "有限公司",
                   "分公司", "公司", "集团")

# 括号里的**纯限定词**：它们不是公司身份的一部分，出现在括号里时无论位置都要忽略。
# 为什么必须有这张表：`阿里巴巴（中国）` 与 `腾讯（中国）` 若按「取更短一侧」走会双双变成
# `中国` → 两个不同公司同一段 → 撞 ID → 用户看到莫名其妙的「ID 已被占用」。
# 只收**地理限定词**（全国性 + 大区 + 城市）——行业词/部门词不收：那会开始误伤真实简称。
_QUALIFIER_WORDS = ("中国",)
_REGION_WORDS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北")

# 本模块的整体互斥：`upsert_jd` / `rename_jd` 都是「读 jds.json → 改内存 → 写盘」多步写，
# 多步之间必须串行（`rename_jd` 还要再改知识库/面试记录/台账，跨度更大）。
# 用 RLock 而不是 Lock：写路径之间将来可能互相调用，普通 Lock 会直接死锁。
# 风格照 kb._KB_LOCK / jobdup._MERGE_LOCK。
_JD_LOCK = threading.RLock()


# ---------- 清洗 / 取值小工具 ----------
def _clean(value):
    """清洗成可以进文件名的片段：去掉文件名非法字符与所有空白。

    非字符串一律先 `str()` 再清洗（**绝不抛异常**，报表 JSON 是用户可手改的）；
    None / 布尔值不算内容 → 空串。
    """
    if value is None or isinstance(value, bool):
        return ""
    text = value if isinstance(value, str) else str(value)
    return _BAD_RE.sub("", text).strip()


def _text(value):
    """取一个字段的字符串值：非字符串（含 None / 数字 / 列表）一律当缺失 → None。"""
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _first_text(*values):
    """第一个「非空字符串」的值。"""
    for v in values:
        t = _text(v)
        if t:
            return t
    return None


def _as_int(value, default=0):
    """容错取整数：'3' → 3；None / 'abc' / bool → default（绝不抛异常）。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bad_chars_in(text):
    """返回 text 里出现的**文件名非法字符**（去重、按 BAD_CHARS 顺序），没有则空串。

    只认 `BAD_CHARS` 这九个 —— 空白不在这里报，它由下面的「清洗后是否原样」那道关兜住，
    错误说明才能分别说清「你打了 `/`」和「你打了空格」。
    """
    return "".join(ch for ch in BAD_CHARS if ch in text)


# ---------- 字段派生 ----------
def _education_of(data):
    """学历：`education`，空则看 `hard_gates` 里 `name=学历` 那条的 `jd`。"""
    edu = _text(data.get("education"))
    if edu:
        return edu
    gates = data.get("hard_gates")
    if isinstance(gates, list):
        for g in gates:
            if not isinstance(g, dict):
                continue
            if _text(g.get("name")) == "学历":
                got = _first_text(g.get("jd"), g.get("value"), g.get("requirement"))
                if got:
                    return got
    return None


def _city_from_address(addr):
    """从详细地址里取城市：「成都武侯区瑞鑫时代大厦-A座」→「成都」。

    先认**已知城市名**（省会/直辖市，用户真实数据里就是「北京 / 成都 / 上海」这几个），
    认不出再退到「第一个 区/县/市/路… 之前，最多 6 个字」。

    为什么不能只用「砍到第一个 区」：`成都武侯区…` 会砍成 `成都武侯`（武侯是区名），
    而直辖市地址（`北京朝阳区…`）后面根本没有 市。已知城市名这一步两种都能对。
    """
    s = _clean(addr)
    if not s:
        return None
    for city in _KNOWN_CITIES:
        if s.startswith(city):
            return city
    stops = [i for i in (s.find(c) for c in "市区县路街镇号") if i > 0]
    if stops:
        s = s[:min(stops)]
    return (s[:_CITY_MAX] or None)


def _domain_of(data):
    """领域：**没有现成字段**，用 `DOMAIN_RULES` 从 标题 / 关键词 / JD 原文 里推。

    推不出返回 None（由 `build_id` 填 `【未知领域】`）—— 绝不猜。
    规则表按顺序命中（「多模态」排最前，因为它最具体）。
    """
    parts = []
    for key in ("job_title", "title", "job_category", "jd_text", "jd_raw", "description"):
        v = data.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    for key in ("keywords", "tags"):
        v = data.get(key)
        if isinstance(v, list):
            parts.extend([x for x in v if isinstance(x, str)])
    jd = data.get("jd")
    if jd is None:
        # 报告里「岗位/JD 原始信息」的键名不止一种（jd / job），两种都认
        jd = data.get("job")
    if isinstance(jd, dict):
        for key in ("text", "raw", "jd_text", "content", "job_title", "title"):
            v = jd.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
        for key in ("keywords", "tags"):
            v = jd.get(key)
            if isinstance(v, list):
                parts.extend([x for x in v if isinstance(x, str)])
    elif isinstance(jd, str) and jd:
        parts.append(jd)
    haystack = "\n".join(parts)
    if not haystack:
        return None
    for keyword, domain in DOMAIN_RULES:
        if keyword in haystack:
            return domain
    return None


def normalize_fields(fields):
    """把派生出来的字段归一化成「能进 ID 的简短身份值」。只动**学历 / 单位 / 薪资**三段。

    归一化后的 ID 从 70 字缩到 30 字左右（见文件里 `_EDU_TIERS` 上面那段注释），
    所以 `derive_fields` 末尾会直接调它 —— `build_id(derive_fields(data), seq)` 拿到的就是短 ID。

    职责 / 领域 / 城市原样带出：它们本来就是短的，`build_id` 只负责清洗非法字符。
    归一化不出的值一律 **None**（由 `build_id` 填 `【未知xx】`），绝不猜。
    """
    data = fields if isinstance(fields, dict) else {}
    out = dict(data)
    out["education"] = _normalize_education(data.get("education"))
    out["company"] = _normalize_company(data.get("company"))
    out["salary"] = _normalize_salary(data.get("salary"))
    return out


def _normalize_education(value):
    """冗长的学历表述 → 一个短档位：`统招公办本科及以上` → `本科`。

    取**出现的最高档**（`_EDU_TIERS` 已是高→低序）：`本科及以上` 是本科；
    `大专及以上` 是大专；提不出任何档位 → None（绝不把整句话塞进 ID）。
    """
    text = _text(value)
    if not text:
        return None
    for tier in _EDU_TIERS:
        if tier in text:
            return tier
    return None


def _normalize_salary(value):
    """只留 `x-yK` 区间：`6-8K（入职五险，提及一金7k+）` → `6-8K`。

    破折号统一成半角 `-`（真实数据里 `13–26K` 是全角破折号，不统一就会与 `13-26K`
    变成两个不同的身份 —— 正是本模块要根除的那类「同一件事两处不同写法」）。
    提不出区间 → None（由 `build_id` 填 `【未知薪资】`）。
    """
    text = _text(value)
    if not text:
        return None
    m = _SALARY_RE.search(text)
    if not m:
        return None
    return "%s-%sK" % (m.group(1), m.group(2))


def _strip_legal_suffix(text):
    """剥掉结尾的**法定组织形式后缀**（`有限公司` / `集团` 之类）。

    为什么：`北京字节跳动科技有限公司`（12 字）截断到 12 字正好切出 `北京字节跳动科技有限`
    —— 半个 `有限公司`，用户每天要看这个身份串，一眼就是坏的。先剥后缀再截断得到
    `北京字节跳动科技`（8 字），完整又干净。

    **按长度降序只剥一个、匹配到就收手**：顺序反了的话 `集团有限公司` 会先被 `公司` 咬掉一截
    → `某某集团有限`。也**不能继续往下试更短的后缀**：整串就叫 `有限公司` 时，
    继续试 `公司` 会得到 `有限` —— 那正是「半个后缀」，本函数要避免的东西。

    **整串就是后缀时退回不剥**：公司名本身就叫 `有限公司` 的话，剥光会得到一个空段
    （`build_id` 只好填 `【未知单位】`，等于把用户填过的信息丢掉）。
    """
    for suffix in _LEGAL_SUFFIXES:
        if text.endswith(suffix):
            if len(text) == len(suffix):
                return text                 # 整串就是后缀 → 退回不剥，绝不留空段
            return text[:-len(suffix)]
    return text


def _is_qualifier(inner):
    """括号里是不是**纯地理限定词**（`中国` / `华东` / `北京`）。空 → False。"""
    text = inner.strip()
    return bool(text) and (text in _QUALIFIER_WORDS or text in _REGION_WORDS
                           or text in _KNOWN_CITIES)


def _normalize_company(value):
    """单位取「简短身份」。三种形态：

    **① 括号里是纯地理限定词** → 无论括号在不在结尾都**忽略括号内容**，剩下的当全名剥后缀：

        "阿里巴巴（中国）有限公司"                        -> "阿里巴巴"
        "腾讯（中国）有限公司"                            -> "腾讯"
        "阿里巴巴（中国）"                               -> "阿里巴巴"
        "腾讯（中国）"                                   -> "腾讯"
        "某公司（北京）分公司"                            -> "某公司"
        "阿里巴巴（华东）"                               -> "阿里巴巴"

    为什么必须单列这一条（**这是修掉的 bug**）：`阿里巴巴（中国）` 与 `腾讯（中国）` 若按
    下面的「取更短一侧」走，会**双双归成 `中国`** —— 两个不同的公司变成同一段，其余段再相同
    就撞 ID，用户会看到莫名其妙的「ID 已被占用」。
    **空段守卫**：括号外什么都不剩时（公司名就叫 `（北京）`）退回不忽略，绝不产生空段。

    **② 括号在结尾、里面是简称 / 注解** → 取括号内与括号前**更短的一侧**：

        "四川金沙人力资源开发管理有限公司（金沙人力）"      -> "金沙人力"
        "金沙人力（四川金沙人力资源开发管理有…）"           -> "金沙人力"
        "某 AI 科技公司（示例）"                          -> "示例"

    **③ 括号后面还有字、但括号里不是限定词**（说明性中缀括号）→ 括号整段去掉，
    剩下的当全名剥后缀：

        "某公司（北京）分公司（简称）"                      -> "某公司"

    **没有括号** → 剥法定后缀再用全名（真实数据那条剥完正好 12 字，与不剥时相同）：

        "四川金沙人力资源开发管理有限公司"                 -> "四川金沙人力资源开发管理"
        "北京字节跳动网络技术有限公司"                     -> "北京字节跳动网络技术"

    截断时按 `_BRACKET_OPEN_RE` 砍掉半个括号（不能留 `（` 半截）。空 → None（`【未知单位】`）。
    """
    text = _text(value)
    if not text:
        return None
    m = _BRACKET_RE.search(text)
    outside = _BRACKET_RE.sub("", text).strip()     # 去掉全部括号组之后剩下的字
    strip_suffix = True                             # 只有「括号在结尾的简称」那条不剥后缀
    if m and outside and _is_qualifier(m.group(1)):
        # ① 限定词：忽略括号内容。`outside` 非空是**空段守卫** ——
        # `（北京）` 这种整个公司名就是括号的，忽略后会变成空段 → 必须退回不忽略
        text = outside
    elif m:
        if text[m.end():].strip():
            text = outside                           # ③ 中缀说明括号：整段去掉
        else:
            # ② 括号在结尾：括号里是简称/注解 → 取更短一侧（选出来就是简称，不再剥后缀）
            strip_suffix = False
            inner = m.group(1).strip()
            prefix = text[:m.start()].strip()
            cands = [c for c in (inner, prefix) if c]
            if cands:
                text = min(cands, key=len)   # 同长时保留括号内那条（"优先括号里的简称"）
    if strip_suffix:
        text = _strip_legal_suffix(text)
    text = text.strip()
    if len(text) > _COMPANY_MAX:
        text = _BRACKET_OPEN_RE.split(text[:_COMPANY_MAX])[0].rstrip("）) ")
    return text or None


def derive_fields(report_data):
    """从一份 `REPORT_DATA` 派生 ID 的六个字段。**缺的字段值为 None**（由 `build_id` 填占位符）。

    为什么可以零提示词改动就拿到结构化 ID：报告的 `REPORT_DATA` 里本来就有
    `education` / `company` / `job_category` / `location`(`address`) / `salary`，
    **只有「领域」没有现成字段**，用关键词规则推。

    这里**刻意不清洗**（职责原样带出，例如 `数据标注/质检`）——清洗是 `build_id` 的活，
    这样 `derive_fields` 的结果仍然可以拿去展示/手改，不会把 `/` 静默吃掉。

    **末尾会过一遍 `normalize_fields`**：学历 / 单位 / 薪资 归一化成简短身份值
    （原始的长表述会让 ID 长到 70 字，没法当文件名）。
    注意：归一化会**丢掉原始表述**（`6-8K（入职五险，提及一金7k+）` → `6-8K`）。
    要把原始值存进 JD 记录（`jds.json`）的话，得另外从 `REPORT_DATA` 读，
    或者直接调下面的 `normalize_fields` 之前那份原始 dict。
    """
    data = report_data if isinstance(report_data, dict) else {}
    company = _text(data.get("company"))
    duty = _text(data.get("job_category")) or _text(data.get("duty"))
    city = _text(data.get("location"))
    if not city:
        addr = _text(data.get("address"))
        if addr:
            city = _city_from_address(addr)
    return normalize_fields({
        "education": _education_of(data),
        "company": company,
        "duty": duty,
        "domain": _domain_of(data),
        "city": city,
        "salary": _text(data.get("salary")),
    })


# ---------- 拼接 / 解析 / 校验 ----------
def build_id(fields, seq):
    """七段拼接：`字段1_…_字段6_编号`。

    缺失 / 空 → `【未知xx】`；每段清洗掉 `BAD_CHARS` 与空白；编号补零到 4 位
    （超过 4 位**原样输出**，不截断 —— 宁可长一位也不能把两个岗位撞成同一个 ID；
    负数 / 非数字一律归 0，绝不把 `-003` 这种带 `-` 的东西塞进 ID）。
    """
    data = fields if isinstance(fields, dict) else {}
    out = []
    for name in SEGMENTS[:-1]:
        value = _clean(data.get(name))
        out.append(value or (UNKNOWN % _UNKNOWN_WORD[name]))
    out.append("%04d" % max(0, _as_int(seq)))
    return SEP.join(out)


def parse_id(text):
    """`ID → (字段 dict, 编号 int)`。段数不对 / 段空 / 编号不是数字 → `ValueError`。"""
    raw = ""
    if isinstance(text, str):
        raw = text.strip()
    if not raw:
        raise ValueError("ID 不能为空")
    parts = raw.split(SEP)
    if len(parts) != SEG_COUNT:
        raise ValueError("岗位 ID 必须是 %d 段（%s），现在有 %d 段：%s"
                         % (SEG_COUNT, SEP.join(SEGMENTS), len(parts), raw))
    for i, p in enumerate(parts):
        if not p.strip():
            raise ValueError("岗位 ID 的第 %d 段是空的（%s 不能有空段）：%s" % (i + 1, SEP, raw))
    tail = parts[-1]
    if not tail.isdigit():
        raise ValueError("岗位 ID 的最后一段必须是编号（数字），现在是 %r：%s" % (tail, raw))
    fields = {name: parts[i] for i, name in enumerate(SEGMENTS[:-1])}
    return fields, int(tail)


def validate_id(text, jds, exclude_id=None):
    """校验手改的 ID。合法返回 `None`，不合法返回**给用户看的错误说明**（规格 §4）。

    - 段数必须 7 段、每段非空、无文件名非法字符 `\\ / : * ? " < > |`、无空白
    - **全库查重**：与其它 JD 的 `id` 完全相同 → 返回「已被占用」（调用方据此回 409）
    - 允许把某段改成 `【未知xx】`
    - `exclude_id`：改自己时传自己，否则「没改动就保存」会被自己占用挡掉

    **判据是「清洗后是否原样」而不是白名单**：`build_id` 生成什么，这里就必须接受什么。
    用白名单（只允许中英文/数字/`-`/`_`/【】）会把真实公司名里的全角括号
    （`四川金沙人力资源开发管理有限公司（金沙人力）`）判成非法 —— 于是
    「自动生成的 ID 存不进去」，任务 2 的接线会在真实数据上直接崩。

    **只读不写**（本项目已被「校验顺手改库」坑过）：本函数一个字节都不落盘。
    """
    # 字符层面的检查放在切段**之前**：`…_6-8K_ 0001` 这种「末尾段带空格」的写法，
    # 先切段只会报「最后一段必须是数字」，用户根本看不出问题是那个空格。
    raw = text.strip() if isinstance(text, str) else ""
    bad = _bad_chars_in(raw)
    if bad:
        return "岗位 ID 里有文件名非法字符：%s（这些字符不能进文件名，请去掉）" % bad
    if _clean(raw) != raw:
        # 空白（含全角空格 / 制表符）会被 build_id 清洗掉。**不在这里拦下来**，
        # 台账核对时就会出现「显示的是 A、存进去的是 B」——用户会以为自己改失败了。
        return "岗位 ID 里有空白字符（空格 / 制表符 / 全角空格），请去掉：%r" % raw
    try:
        parse_id(text)
    except ValueError as e:
        return str(e)
    uid = _clean(raw)
    ex = _text(exclude_id)
    if isinstance(jds, dict):            # 容错：误传整份 {"jds": [...]} 也认
        jds = jds.get("jds")
    for j in (jds if isinstance(jds, list) else []):
        if not isinstance(j, dict):
            continue
        other = _text(j.get("id"))
        if not other or other == ex:
            continue
        if _clean(other) == uid:
            return "这个 ID 已被占用：%s（请换一段信息或换个编号）" % other
    return None


# ---------- JD 库（jds.json） ----------
def _jds_path(key=None):
    return store.p_path("jds.json", key=key)


def load_jds(key=None):
    """读 JD 库，永远返回 `{"version","seq","jds"}` 形状的字典，**绝不抛异常**。

    容错：文件缺失 / JSON 坏了 / `jds` 不是列表 → 空列表；`seq` 不是数字 → 0；
    `jds` 里不是 dict 或没有非空字符串 id 的项**丢掉**（用户可手改这个文件）。
    """
    d = store.read_json(_jds_path(key), None)
    if not isinstance(d, dict):
        return {"version": 1, "seq": 0, "jds": []}
    raw = d.get("jds")
    out = []
    for r in (raw if isinstance(raw, list) else []):
        if isinstance(r, dict) and _text(r.get("id")):
            out.append(r)
    return {"version": _as_int(d.get("version"), 1) or 1,
            "seq": max(0, _as_int(d.get("seq"), 0)),
            "jds": out}


def save_jds(data, key=None):
    """原子写 JD 库（走 `store.write_json`）。**读不出来时拒绝**（理由见 `store.jds_safe_to_write`），
    覆盖前留一份 `.bak-<时间戳>`。非 dict 一律当空库写，绝不写坏结构。

    ⚠️ 判据 + 留档 + 写盘整段在 `_JD_LOCK` 里（与 `store.save_jobs` 同一做法）：判据与写盘之间
    若挤进另一个写者，留下来的就不是"覆盖前"那一份了。

    为什么必须拒绝：`load_jds()` 的契约是"绝不抛异常"（读失败 → 空库），而本函数是**整份覆盖**
    —— 于是"一次读失败"就等于整个 JD 库被内存里那份空库顶掉。**实测**：GBK 的 2 条记录库上
    只剩新那一条 / `next_seq` 把它清成空库，且盘上没有任何副本。

    **异常类型选 `RuntimeError`**（三个写入口一致）：全仓库「前置条件不满足就中止」用的都是它
    （见 `store.ProfileUnreadable` 的说明），`ValueError` 在本模块留给**入参 / 校验**类错误
    （"没有这个 ID"、"ID 已被占用"）—— 把"文件读不出来"混进校验类错误，用户会以为是自己
    填错了东西，跑去改 ID 而不是修文件。
    """
    with _JD_LOCK:
        p = _jds_path(key)
        # 判据问的就是**这个** key 的路径（不是默认 profile 的那一份），见 store.jds_safe_to_write。
        ok, err = store.jds_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝写入**：`save_jds` 是**整份覆盖**，而现在内存里那份是从读不出来的文件"
                "兜出来的空库（`load_jds()` 永不抛异常）—— 写下去就是整个 JD 库一次性没掉，"
                "盘上没有任何副本。请先把 `jds.json` 修好（或从别处恢复、把它移开）再保存。" % err)
        if not isinstance(data, dict):
            data = {}
        raw = data.get("jds")
        out = {"version": _as_int(data.get("version"), 1) or 1,
               "seq": max(0, _as_int(data.get("seq"), 0)),
               "jds": [r for r in (raw if isinstance(raw, list) else [])
                       if isinstance(r, dict) and _text(r.get("id"))]}
        store.snapshot_before_overwrite(p)      # 覆盖前留档（自带 `.bak-` 轮转，只留最新 3 份）
        store.write_json(p, out)
        return out


def find_jd(jd_id, key=None):
    """按 id 找一条 JD 记录。找不到（或 id 空）→ None。"""
    want = _text(jd_id)
    if not want:
        return None
    for r in load_jds(key)["jds"]:
        if r.get("id") == want:
            return r
    return None


def upsert_jd(record, key=None):
    """插入或按 id 更新一条 JD 记录。**不动 seq**（编号只由 `next_seq` 推进）。

    记录里没有 `created_at` 时补一个；返回落盘后的记录（便于调用方接着填字段）。

    **读不出来时拒绝**（判据与 `save_jds` 同一份 `store.jds_safe_to_write`）：
    `load_jds()` 读失败时给的是空库，拿它去 upsert 就是把整个 JD 库换成这一条
    （**实测**：2 条记录 → 只剩新那一条，`.bak-` 0 份）。
    """
    if not isinstance(record, dict) or not _text(record.get("id")):
        raise ValueError("JD 记录必须有非空的 id")
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _JD_LOCK:
        p = _jds_path(key)
        # ⚠️ 先判"能不能写"再读：读不出来时 `load_jds()` 给的是空库。
        ok, err = store.jds_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝写入**：这次 `upsert_jd` 会把整个 JD 库覆盖成「这一条 + 读出来的"
                "空库」，旧记录会在盘上消失且没有副本。请先把 `jds.json` 修好"
                "（或从别处恢复、把它移开）再抓这个岗位。" % err)
        data = load_jds(key)
        new = dict(record)
        new.setdefault("id_history", [])
        if not _text(new.get("created_at")):
            new["created_at"] = now
        new["updated_at"] = now
        for i, r in enumerate(data["jds"]):
            if r.get("id") == new["id"]:
                merged = dict(r)
                merged.update(new)
                # id_history 不能因为「这次没带」就被抹掉
                if not merged.get("id_history"):
                    merged["id_history"] = r.get("id_history") or []
                data["jds"][i] = merged
                save_jds(data, key)
                return merged
        data["jds"].append(new)
        save_jds(data, key)
        return new


def next_seq(key=None):
    """`seq += 1` 并落盘，返回新的编号（全局递增，ID 的编号段用它）。

    **读不出来时拒绝**（与 `upsert_jd` 同一份判据）：`load_jds()` 读失败时给的是空库，
    `seq` 会从 0 重新开始 —— 于是编号复用、ID 撞车；**实测**更严重：整个 JD 库被清成
    `{"version":1,"seq":1,"jds":[]}`（299 → 47 字节），且没有副本。
    """
    with _JD_LOCK:
        p = _jds_path(key)
        ok, err = store.jds_safe_to_write(p)
        if not ok:
            raise RuntimeError(
                "%s。**拒绝写入**：`next_seq` 要把 `seq` 写回盘上，而现在内存里那份是从读不出来"
                "的文件兜出来的空库 —— 写下去就是整个 JD 库被清空（编号还会从 1 重新开始，"
                "跟已有的 ID 撞车）。请先把 `jds.json` 修好（或从别处恢复、把它移开）再继续。"
                % err)
        data = load_jds(key)
        data["seq"] = max(0, _as_int(data.get("seq"), 0)) + 1
        save_jds(data, key)
        return data["seq"]


# ---------- 分析记录（jobs.json 那一侧，只读/改名） ----------
def jd_id_of_analysis(analysis):
    """从 `jobs.json` 的一条分析记录取它挂在哪个 JD 下。没有 → 空串。"""
    if not isinstance(analysis, dict):
        return ""
    return _text(analysis.get("jd_id")) or ""


def analyses_of(jd_id, key=None):
    """该 JD 下的全部分析记录，按 `created_at` 升序（没有 created_at 的排最前）。"""
    want = _text(jd_id)
    if not want:
        return []
    out = [j for j in store.load_jobs(key)
           if isinstance(j, dict) and _text(j.get("jd_id")) == want]
    return sorted(out, key=lambda j: (str(j.get("created_at") or ""), str(j.get("id") or "")))


# ---------- 改 ID ----------
def _interview_status_paths(old_id):
    """扫 `interviews/<面试id>/status.json`，返回 `(job_id == old_id 的文件路径, 原因)`。

    **原因非空 = 有一份 status.json 读不出来**，调用方（`rename_jd`）必须据此**拒绝整次改名**。

    为什么不能再用 `store.read_json(path, None)` 把"读不出来"当成"它没引用我"：
    面试记录不像知识库那样只有一个集中入口 —— 它是**每个面试目录一个文件**，
    所以漏掉一份没有任何集中信号。被跳过的那一份，改完 ID 之后 `job_id` 会**静默**留着旧
    ID（同一个岗位两种身份），用户看不到任何提示。**"读不出来"就是"不知道它指向谁"**，
    而"不知道"不许当成"安全"（本项目丢数据的习惯）。
    0 字节 / 文件不存在**不算**读不出来：里面一个字节都没有，也就没有 `job_id` 可指。
    """
    import glob
    out = []
    err = ""
    for path in sorted(glob.glob(store.interview_file("*", "status.json"))):
        st = store.read_json(path, None)
        if isinstance(st, dict):
            if st.get("job_id") == old_id:
                out.append(path)
            continue
        if store.file_has_bytes_to_lose(path):
            err = err or ("面试记录 `%s` 读不出来（拿不到它的 `job_id`）" % path)
    return out, err


def _bad(msg, **kw):
    log.log_event("jdid.rename_rejected", level="warn", reason=msg, **kw)
    return ValueError(msg)


def rename_jd(old_id, new_id, key=None):
    """改一个 JD 的 ID，并把**所有引用**同步改过去。成功返回新 id 字符串。

    改哪些地方（规格 §4「改 ID 的影响」）：**清单只有一份**（批 7.1）——
    `jdrefs.refs_of_jd(old)` 说了算（`jdrefs.KINDS`：`jds.json` 本体 / 台账 `jd_id` /
    知识库 `from_jds` / 面试 `status.json` / 报告勾选 `report_state` / 反哺 `feedback.json`），
    逐项重写交给 `jdrefs.rewrite_jd_refs`。本模块**不再自己列清单**（原先自己列三处 ⇒
    漏了 `report_state/<报告id>.json` 与 `interviews/*/feedback.json`，后者一旦漏，改完名它
    的键就指向一个不存在的岗位，下一次反哺重试会把同一次面试重复计进复习队列）。
    **已有的报告文件不重命名**（它们只是产物；台账通过 `jobs.json.report` 找到它们）。

    **台账那一段的判据是 `jd_id or id`**（`jdrefs` 的口径，与删除预演同一把尺子）：迁移前
    形态的记录（身份就是 `id`、没有 `jd_id`）也会跟上改名 —— 只认 `jd_id` 的话它们改完还是
    `refs_of_jd(旧)` 的命中（旧 id 悬着）。这是本步唯一一处判据变化，属于"清单与改写同一把
    尺子"的必然结果。

    **失败语义全部在 `jdrefs`**：它写之前重新过每一处的 `*_safe_to_write`，任何一处读不出来/
    结构不对就**整次拒绝**（一个字节都不写）；落盘中途失败则用**原始字节**原像逐字节回滚
    ——绝不留「jds.json 改了、引用没改」。本模块保留的是**前面的闸门**（JD 库读不出来、
    知识库读不出来、任何一份面试 `status.json` 读不出来、以及 `refs_of_jd` 自己报出来的
    "读不出来" ⇒ 一律 `RuntimeError` 拒绝、一个字节都不动），那句话是给用户看的。

    **知识库读不出来时拒绝整次改名**（`RuntimeError`）：判据是 `store.knowledge_safe_to_write()`
    —— 与 `kb.repoint_jd_refs` 用的是**同一份**（"读不出来 ≠ 空"只有一个实现）。为什么必须拒绝：
    `load_knowledge()` 会把坏文件静默归成 `{"nodes": []}`，于是"没有卡引用它"与"库读不出来"
    **不可区分**，照改就会留下「jds.json 指向新 ID、知识库里的引用仍指向旧 ID」的两种身份
    （回归测试 `tests/test_jdid.py::RenameJdTest::test_an_unreadable_knowledge_base_refuses_the_rename_and_changes_nothing`）。
    0 字节 / 根本不存在的知识库**不算**读不出来（那是合法的"第一次"，没有可丢的字节）。

    **JD 库读不出来时同样拒绝**（`RuntimeError`，判据 `store.jds_safe_to_write()`）：老实现会让
    `load_jds()` 把坏文件静默归成空库，然后报「JD 库里没有这个 ID：X」—— 而那条记录其实就在
    盘上（只是读不出来），误导用户去新建一个同岗位的第二种身份。

    **任何一份面试 `status.json` 读不出来也拒绝整次改名**（`RuntimeError`）：面试记录是每个
    面试目录一个文件，漏一份没有集中信号；老实现把"读不出来"当成"这份没引用我"跳过，
    改完 ID 后那份的 `job_id` 静默留着旧 ID（回归测试
    `RenameJdUnreadableInterviewStatusTest`）。

    **原像按原始字节保存**：**读不出来**（被锁 / 权限 / 任何异常）绝不能被当成"本来不存在"、
    更不许被兜成空内容 —— 回滚会反过来把用户的文件写成 `{}`（回归测试见
    `tests/test_jdid.py::RenameJdTest::test_a_read_failure_at_snapshot_time_leaves_the_bytes_untouched`）；
    换行也不许被文本模式换掉 —— 回滚的语义是"和动手之前一模一样"。
    """
    old = _text(old_id)
    new = _text(new_id)
    if not old or not new:
        raise _bad("旧 ID 与新 ID 都要给", old=old or "", new=new or "")
    if old == new:
        # 幂等：本来就一样 → 一个文件都不动
        return old

    with _JD_LOCK:
        jds_path = _jds_path(key)
        # ---- 需求二①：`jds.json` 读不出来 → **如实说"读不出来"**，不许说成"库里没有这个 ID" ----
        # 老行为：`load_jds()` 把坏文件静默归成空库 → 走到下面那句 `_bad("JD 库里没有这个 ID")`
        # —— 而那条记录其实**就在盘上**（只是读不出来）。用户照着"没有这个 ID"去查只会更糊涂，
        # 还很容易顺手"那就新建一个吧"，于是同一个岗位又多出一种身份。
        # 判据用 `store.jds_safe_to_write`（= 严格读 + "盘上有没有字节"），与三个写入口同一份；
        # 0 字节 / 不存在**不算**读不出来（那是合法的"第一次"，照旧报"库里没有这个 ID"）。
        jds_ok, jds_err = store.jds_safe_to_write(jds_path)
        if not jds_ok:
            msg = ("%s。**已拒绝这次改 ID**：读不出来就无法确认 %s 在不在库里，"
                   "也无从把引用改过去（把「读不出来」说成「没有这个 ID」正是要修的误导）。"
                   "这次改名一个字节都没动 —— 请先把 JD 库修好（或从别处恢复）再改。"
                   % (jds_err, old))
            log.log_event("jdid.rename_rejected", level="warn", reason=msg, old=old, new=new)
            raise RuntimeError(msg)
        data = load_jds(key)
        target = None
        for r in data["jds"]:
            if r.get("id") == old:
                target = r
                break
        if target is None:
            raise _bad("JD 库里没有这个 ID：%s" % old, old=old, new=new)
        err = validate_id(new, data["jds"], exclude_id=old)
        if err:
            raise _bad("新 ID 不接受：%s" % err, old=old, new=new)
        taken = any(_clean(r.get("id") or "") == _clean(new) for r in data["jds"]
                    if r.get("id") != old)
        if taken:
            raise _bad("这个 ID 已被占用：%s（请换一段信息或换个编号）" % new, old=old, new=new)

        # 锁顺序恒为 _JD_LOCK → _KB_LOCK（见文件头铁律 3）
        with kb._KB_LOCK:
            # ---- P0-8：动手之前先确认知识库**读得出来**（判据只准有一份）----
            # 不装这道闸门的后果（本块修的就是它）：`load_knowledge()` 把坏文件静默归成
            # `{"nodes": []}` → 重指命中 0 → **一字不写**，而本函数照样成功返回 ——
            # 于是 jds.json / 台账 / 面试记录都改指新 ID，知识库里那些卡的 `from_jds`
            # 仍指向旧 ID（同一个岗位两种身份，用户修好库的那一刻就看得见）。
            # 这是"会串档"里最隐蔽的一条：**"没有卡引用它"与"库读不出来"不可区分**。
            # 判据只准有一份（`store.knowledge_safe_to_write`）：读得出来 **或** 真的没有一个字节
            # 才放行；0 字节 / 不存在是合法的"第一次"，不算读不出来。
            #
            # 这道闸门**不删**（虽然 `kb.repoint_jd_refs` 自己也会拒绝坏库）：它要在**任何**
            # 落盘之前就拒绝整次改名，而且那句话是给用户看的（说清"这次改名一个字节都没动"）。
            kb_ok, kb_reason = store.knowledge_safe_to_write(store.knowledge_path(key))
            if not kb_ok:
                msg = ("%s。**已拒绝这次改 ID**：读不出来就无法确认哪些卡引用了 %s，"
                       "改完会留下「jds.json 指向新 ID、知识库里的引用还指向旧 ID」的两种身份。"
                       "这次改名一个字节都没动 —— 请先把知识库修好（或从别处恢复）再改。"
                       % (kb_reason, old))
                log.log_event("jdid.rename_rejected", level="warn", reason=msg, old=old, new=new)
                raise RuntimeError(msg)

            # ---- 需求二②：有面试 status.json 读不出来 → 拒绝整次改名（"读不出来"就是"不知道"）----
            # 面试记录是**每个面试目录一个文件**，漏掉一份不会有任何集中信号：老实现把它当成
            # "这份没引用我"跳过，改完 ID 后那份的 `job_id` 静默留着旧 ID（同一个岗位两种身份）。
            _paths, iv_err = _interview_status_paths(old)
            if iv_err:
                msg = ("%s。**已拒绝这次改 ID**：读不出来就无法确认哪些面试记录挂在 %s 下面，"
                       "改完会有一条面试记录的 `job_id` 静默留着旧 ID（同一个岗位两种身份）。"
                       "这次改名一个字节都没动 —— 请先把它修好（或从别处恢复、把它移开）再改。"
                       % (iv_err, old))
                log.log_event("jdid.rename_rejected", level="warn", reason=msg, old=old, new=new)
                raise RuntimeError(msg)

            # ---- 6) 引用清单只有一份（`jdrefs.KINDS`）：**纯读**，先把"读不出来"拒掉 ----
            # 批 7.1 第 3 步：改名原先自己 repoint 三处 ⇒ 漏了 `report_state/<报告id>.json`
            # 与 `interviews/*/feedback.json`（后者**键就是旧 JD id**，不改就会指向一个不存在的
            # 岗位，下一次反哺重试会把同一次面试重复计进复习队列）。现在只问 `jdrefs`。
            # ⚠️ 它自己**不取**任何外层锁（本线程正持着 `_JD_LOCK` + `_KB_LOCK`），
            # 写 knowledge.json 时只重入 `kb._with_kb_write` 的 `_KB_LOCK` ⇒ 锁序逐边不变。
            refs = jdrefs.refs_of_jd(old, key)
            bad_refs = jdrefs.unreadable_refs(refs)
            if bad_refs:
                # **"读不出来"就是"不知道它指向谁"** ⇒ 拒绝整次改名（与上面两道闸门同一规格）。
                # 老实现只拦 `status.json`；`feedback.json` 坏掉时它会照改并报成功 ——
                # 改完那个坏文件里的旧键还指着已经不存在的岗位（同岗位两种身份，全程静默）。
                msg = ("%s。**已拒绝这次改 ID**：读不出来就无法确认还有哪些引用挂在 %s 下面，"
                       "改完会留下「jds.json 指向新 ID、那些引用还指着旧 ID」的两种身份。"
                       "这次改名一个字节都没动 —— 请先把它修好（或从别处恢复、把它移开）再改。"
                       % ("；".join(r.err for r in bad_refs), old))
                log.log_event("jdid.rename_rejected", level="warn", reason=msg, old=old, new=new)
                raise RuntimeError(msg)

            # ---- 7) 落盘：逐项重写 + 原像逐字节回滚**都由 `jdrefs` 负责** ----
            # 它写之前会重新过一遍每一处的 `*_safe_to_write`，任何一处写不得就**整次拒绝**
            # （一个字节都不写）；落盘中途失败则用原像逐字节还原，绝不留「jds 改了、引用没改」。
            # 原像必须按**原始字节**取（`read_bytes`）：文本模式会把 LF 换成 CRLF，那样"逐字节
            # 还原"就是假的（回归：`test_a_read_failure_at_snapshot_time_leaves_the_bytes_untouched`
            # 与 `RenameRollbackByteExactTest`）。
            try:
                jdrefs.rewrite_jd_refs(old, new, refs=refs, key=key)
            except (jdrefs.JdRefsRefused, jdrefs.JdRefsWriteFailed) as e:
                # 拒绝（坏文件）与中途失败（已回滚）都是"这次改名没成"：原样给用户看 `jdrefs`
                # 那句说清"哪个文件、为什么"的话 —— 重新包装一遍只会丢掉细节。
                log.log_exc("jdid.rename_failed", e, old=old, new=new)
                raise _bad("改 ID 失败：%s" % e, old=old, new=new)
            except Exception as e:
                # 原像都取不出来（文件被锁 / 权限 / IO）：`jdrefs` 在**任何落盘之前**就抛了，
                # 一个字节都没写。这里换成 `ValueError`（本模块对外的既有约定），
                # 绝不把"读失败"兜成"这个文件本来不存在"再去写一份假的。
                log.log_exc("jdid.rename_failed", e, old=old, new=new)
                raise _bad("改 ID 失败：%s（本次改名一个字节都没动）" % e, old=old, new=new)

        # 计数只用于日志（口径由 `jdrefs` 的 `Ref.count` 给出：kb = 卡片数、jobs = 记录数、
        # 面试 = 逐文件的条数），不再自己另算一遍。
        per_kind = {}
        for r in refs:
            per_kind[r.kind] = per_kind.get(r.kind, 0) + max(r.count, 0)
        log.log_event("jdid.rename_done", old=old, new=new,
                      kb_nodes=per_kind.get("kb", 0),
                      interviews=per_kind.get("iv_status", 0),
                      analyses=per_kind.get("jobs", 0))
        return new
