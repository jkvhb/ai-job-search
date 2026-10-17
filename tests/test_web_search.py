# -*- coding: utf-8 -*-
"""批 6.1：岗位台账搜索框**防抖** + 行内统计请求**限量**（判据全部在 Node 桩里真跑出来）。

体检 §6 实测的最严重前端性能问题：

  · 搜索框**没有节流** —— 每敲一个键都直接 `renderJobs()`（300 个岗位 ≈ 1.47 MB HTML、桩内 66 ms）；
  · 行内统计是"**每个岗位一个请求**"，而且是 `Promise.all`：300 个岗位 ⇒ **一瞬间 300 个请求**
    （`ThreadingHTTPServer` ⇒ 一瞬间 300 条线程）；连敲 N 个搜索键更是 N×300 个请求
    （`miss` 只看 `IV_CACHE`/`IV_FAILED`、**不看"正在飞"**）。

判据**全部是可数量**的（渲染次数 / 请求数 / 同时在飞数），没有一条拿墙钟当判据：

  ① 连敲 5 次搜索键 ⇒ `#jobList` 的 innerHTML 只被赋值 **1** 次（待触发的定时器也只有 1 个）；
  ② 300 个岗位时，统计请求**同时在飞 ≤ 8**（`IV_STAT_MAX_INFLIGHT`），且 300 个岗位一个不少地取回来；
  ③ 对照组：只敲 1 次 ⇒ 该渲染 1 次、该发的请求数与**直接调 `renderJobs()`** 完全一致（防抖不吞单次操作）；
  ④ 在途去重：第一趟还没落地就再渲染一次 ⇒ 请求数**不再翻倍**（改前 `miss` 只看缓存 ⇒ 又来一遍 300）。

这些数字必须在 Node 桩里真跑出来，所以用了 `tests/web_probe.py` 的两个**新增**钩子
（批 6.1 纯增量，`run_index` / `index_script` 的既有 API 一个字没改）：
`__fire(sel,type,ev)` 把页面**真注册过**的监听器叫起来（以前 `addEventListener` 是空函数，
"敲一下搜索框"在桩里根本发生不了）、`__htmlSets(sel)` 数那个元素被赋过几次 innerHTML。
桩里的 `setTimeout` 换成手动"走表"的假定时器 —— 判据是**次数**，不是时间。

本文件只用 Node 探针，不碰任何真实数据；仍然继承 `IsolatedCase`
（项目约定：碰 store/log 的测试必须隔离；这里连 store/log 都不 import，纯属多一道保险）。
"""
import os
import re
import unittest

from tests import web_probe
from tests.base import IsolatedCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_INDEX = os.path.join(ROOT, "web", "index.html")

JOBS_N = 300
# 同时在飞的统计请求上限（与 web/index.html 里的 `IV_STAT_MAX_INFLIGHT` 一致）。
# 这里**写死 8**：它就是本批的契约，不跟着页面里的常量跑 —— 否则把常量改成 300 也照样绿。
MAX_INFLIGHT = 8


def _need_node():
    """缺 Node 是**响亮失败**（不是跳过）：跳过等于把这批前端断言静默删掉。"""
    if web_probe.NODE is None:
        raise AssertionError("本环境缺 Node（`node` 不在 PATH 上）：跑不了页面探针，"
                             "这批前端渲染断言不能静默跳过 —— 装上 Node 再重跑")


def _job(i):
    """台账行（形状对齐 `/api/jds` 的 jds 行：卡片上要用的字段 + 一份带逐维评分的分析）。"""
    jid = "本科_公司%03d_评测_AI_北京_13-26K_%03d" % (i, i)
    return {
        "id": jid,
        "job_title": "岗位%03d" % i,          # 每一行都含「岗位」：对照组要用它当搜索词
        "job_category": "AI",
        "status": "未投递",
        "created_at": "2026-10-%02d 09:00" % (i % 28 + 1),
        "company": "公司%03d" % i,
        "hr_name": "HR%03d" % i,
        "phone": "1380000%04d" % i,
        "address": "北京",
        "location": "北京",
        "salary": "13-26K",
        "source_type": "截图",
        "notes": "",
        "source_link": "",
        "report": "",
        "analyses": [{
            "id": "a%03d" % i,
            "resume": "简历A",
            "resume_id": "v1",
            "report": "r%03d.html" % i,
            "score": 66.0,
            "created_at": "2026-10-01 09:00",
            "scores": [{"dimension": "团队管理", "weight": 25, "score": 5}],
            "scores_error": "",
        }],
    }


def _fixture(n=JOBS_N):
    return {"jobs": [_job(i) for i in range(n)]}


# ---------------------------------------------------------------------------
# 场景 JS 的公共前奏：假定时器（手动走表）+ 统计请求观测台
# ---------------------------------------------------------------------------
_PRELUDE = """
/* ---- 假定时器：判据是"渲染了几次 / 待触发几个定时器"（可数量），不是墙钟 ---- */
var __timers = [];
var __realSet = setTimeout, __realClear = clearTimeout;
setTimeout = function(fn, ms){ __timers.push({fn: fn, ms: ms}); return __timers.length; };
clearTimeout = function(id){ if(id > 0 && id <= __timers.length) __timers[id-1] = null; };
function __pending(){ return __timers.filter(function(t){ return !!t; }).length; }
function __tick(){
  var due = __timers.slice(); __timers = [];
  due.forEach(function(t){ if(t && typeof t.fn === 'function') t.fn(); });
  return due.filter(function(t){ return !!t; }).length;
}

/* ---- 统计请求观测台：sent / 同时在飞 / 峰值在飞 ---- */
var __sent = 0, __inFlight = 0, __maxInFlight = 0;
function __resetStat(){ __sent = 0; __inFlight = 0; __maxInFlight = 0; }
function __installFetch(){
  fetch = function(url){
    var u = String(url);
    if(u.indexOf('/api/interview/list') !== 0)
      return Promise.resolve({ok:true, json:function(){ return Promise.resolve({ok:true, jds:[]}); }});
    __sent++; __inFlight++;
    if(__inFlight > __maxInFlight) __maxInFlight = __inFlight;
    /* 下一个宏任务才回包：这样"同时在飞"才真的堆得起来。
       立刻 resolve 的话，并发这件事根本观察不到 —— 那种判据是假的。 */
    return new Promise(function(resolve){
      __realSet(function(){
        __inFlight--;
        resolve({ok:true, json:function(){ return Promise.resolve({ok:true, interviews:[]}); }});
      }, 0);
    });
  };
}
/* 把页面自己排的定时器和在途请求都跑完。返回跑了多少轮；-1 = 排不空（用例据此判红）。 */
async function __drain(){
  for(var i = 0; i < 20000; i++){
    __tick();
    await new Promise(function(r){ __realSet(r, 0); });
    if(!__pending() && __inFlight === 0) return i;
  }
  return -1;
}
"""

# ① 连敲 5 次搜索键
_CASE_FIVE_KEYS = _PRELUDE + """
__installFetch();
JOBS = __fixture.jobs;
document.querySelector('#jobList').innerHTML = '';      // 先清一次，下面数**增量**
var before = __htmlSets('#jobList');
__resetStat();
var fired = 0;
['岗','岗位','岗位0','岗位01','岗位012'].forEach(function(v){
  document.querySelector('#q').value = v;
  fired += __fire('#q', 'input', {});
});
out.fired = fired;                                      // 5 次敲键真的叫到了 5 个监听器
out.pendingTimers = __pending();                        // 只该留下 1 个待触发的防抖定时器
__tick();                                               // "走表"一次：防抖定时器到点
out.drained = await __drain();
out.htmlSets = __htmlSets('#jobList') - before;         // 渲染了几次
out.sent = __sent;
out.cached = Object.keys(IV_CACHE).length;
out.n = JOBS.length;
"""

# ② 300 个岗位：同时在飞的统计请求
_CASE_300_JOBS = _PRELUDE + """
__installFetch();
JOBS = __fixture.jobs;
document.querySelector('#q').value = '';
document.querySelector('#jobList').innerHTML = '';
__resetStat();
renderJobs();
out.inFlightAtStart = __inFlight;
out.drained = await __drain();
out.sent = __sent;
out.maxInFlight = __maxInFlight;
out.cached = Object.keys(IV_CACHE).length;
out.failed = Object.keys(IV_FAILED).length;
/* 用 typeof 兜住：改前的页面里根本没有这个常量 —— 那也要红成一条**断言**，
   而不是 Node 侧一句 ReferenceError（后者看不出"上限是多少"）。 */
out.limit = (typeof IV_STAT_MAX_INFLIGHT === 'undefined') ? null : IV_STAT_MAX_INFLIGHT;
out.n = JOBS.length;
"""

# ③ 对照组：只敲 1 次
_CASE_SINGLE_KEY = _PRELUDE + """
__installFetch();
JOBS = __fixture.jobs;
document.querySelector('#q').value = '岗位';            // 每一行都含它 → 过滤后仍是 N 个
document.querySelector('#jobList').innerHTML = '';

__resetStat();
var before1 = __htmlSets('#jobList');
var fired = __fire('#q', 'input', {});
__tick();
out.drained1 = await __drain();
out.byEvent = {htmlSets: __htmlSets('#jobList') - before1, sent: __sent, fired: fired};

/* 直接调 renderJobs()（完全不经过防抖）：渲染次数与"该发的请求数"必须一致 */
ivCacheClear();
__resetStat();
var before2 = __htmlSets('#jobList');
renderJobs();
out.drained2 = await __drain();
out.byDirect = {htmlSets: __htmlSets('#jobList') - before2, sent: __sent};
out.n = JOBS.length;
"""

# ④ 在途去重：第一趟还没落地就再渲染一次
_CASE_INFLIGHT_DEDUPE = _PRELUDE + """
__installFetch();
JOBS = __fixture.jobs;
document.querySelector('#q').value = '';
document.querySelector('#jobList').innerHTML = '';
__resetStat();
renderJobs();
var afterFirst = __sent;                                // 第一趟：8 个在飞 + 292 个排在队列里
renderJobs();                                           // 立刻再渲染一次（改前：miss 又是 N 个）
var afterSecond = __sent;
out.afterFirst = afterFirst;
out.afterSecond = afterSecond;
out.drained = await __drain();
out.sent = __sent;
out.maxInFlight = __maxInFlight;
out.cached = Object.keys(IV_CACHE).length;
out.n = JOBS.length;
"""


class JobsSearchDebounceTest(IsolatedCase):
    """① / ③：搜索框防抖（连敲 5 次只渲染 1 次；单次操作不许被吞掉）。"""

    def test_five_search_keystrokes_render_the_ledger_once(self):
        """① 连敲 5 次 ⇒ `#jobList` 的 innerHTML 只赋值 **1** 次。

        改前：监听器直接就是 `renderJobs` ⇒ 渲染 **5** 次（`out.htmlSets == 5`），
        而且 5 个键之间一个定时器都没有（`out.pendingTimers == 0`）。
        """
        _need_node()
        out = web_probe.run_index(_CASE_FIVE_KEYS, _fixture())
        self.assertEqual(out["n"], JOBS_N)
        self.assertEqual(out["fired"], 5,
                         "5 次敲键没有真的叫到 5 个监听器（桩没接上？）：%r" % (out,))
        self.assertGreaterEqual(out["drained"], 0, "定时器/请求排不空：%r" % (out,))
        self.assertEqual(out["htmlSets"], 1,
                         "连敲 5 次搜索键应当只渲染 1 次，实际渲染 %d 次" % out["htmlSets"])
        self.assertEqual(out["pendingTimers"], 1,
                         "连敲 5 次后待触发的防抖定时器应当**只有 1 个**（改前是 0 个）：%r" % (out,))
        # 一次渲染该发的统计请求一个不少（限量 ≠ 少算）
        self.assertEqual(out["sent"], JOBS_N, out)
        self.assertEqual(out["cached"], JOBS_N, out)

    def test_a_single_search_keystroke_still_renders_and_requests_the_same(self):
        """③ 对照组：只敲 1 次 ⇒ 渲染 1 次，请求数与**直接调 `renderJobs()`** 完全一致。

        防抖只该合并"连续敲键"，不该把单次操作也吞掉。
        """
        _need_node()
        out = web_probe.run_index(_CASE_SINGLE_KEY, _fixture())
        self.assertEqual(out["n"], JOBS_N)
        self.assertEqual(out["byEvent"]["fired"], 1)
        self.assertEqual(out["byEvent"]["htmlSets"], 1, out)
        self.assertEqual(out["byDirect"]["htmlSets"], 1, out)
        self.assertEqual(out["byEvent"]["sent"], JOBS_N, out)
        self.assertEqual(out["byDirect"]["sent"], out["byEvent"]["sent"],
                         "敲一下搜索键与直接 renderJobs() 该发的请求数必须一致：%r" % (out,))
        self.assertGreaterEqual(out["drained1"], 0, out)
        self.assertGreaterEqual(out["drained2"], 0, out)


class JobsStatsRequestLimitTest(IsolatedCase):
    """② / ④：整块渲染时统计请求限量（在途去重 + 在飞 ≤ 8）。"""

    def test_300_jobs_keep_in_flight_stats_requests_at_or_below_the_cap(self):
        """② 300 个岗位 ⇒ 同时在飞 ≤ 8（改前是 `Promise.all` ⇒ 一瞬间 300 个），
        而且 300 个岗位**一个不少**地取回来（限量不是少算）。"""
        _need_node()
        out = web_probe.run_index(_CASE_300_JOBS, _fixture())
        self.assertEqual(out["n"], JOBS_N)
        self.assertGreaterEqual(out["drained"], 0, "定时器/请求排不空：%r" % (out,))
        self.assertLessEqual(out["inFlightAtStart"], MAX_INFLIGHT,
                             "renderJobs() 一返回就已经有 %d 个请求在飞（上限 %d）"
                             % (out["inFlightAtStart"], MAX_INFLIGHT))
        self.assertLessEqual(out["maxInFlight"], MAX_INFLIGHT,
                             "300 个岗位时同时在飞的统计请求到了 %d 个（上限 %d）"
                             % (out["maxInFlight"], MAX_INFLIGHT))
        self.assertGreaterEqual(out["maxInFlight"], 2,
                                "限量退化成了串行（峰值在飞 %d）—— 上限不是 1" % out["maxInFlight"])
        # 限量 ≠ 少算：每个岗位都发了、都回来了
        self.assertEqual(out["sent"], JOBS_N, out)
        self.assertEqual(out["cached"], JOBS_N, out)
        self.assertEqual(out["failed"], 0, out)
        self.assertEqual(out["limit"], MAX_INFLIGHT,
                         "页面的在飞上限变了（本批的契约是 8）：%r" % (out,))

    def test_a_second_render_while_stats_are_in_flight_does_not_refire_them(self):
        """④ 在途去重：第一趟还没落地就再渲染一次 ⇒ 请求数不翻倍。

        改前 `miss` 只看 `IV_CACHE`/`IV_FAILED`，第一趟刚发出去、缓存还是空的，
        第二次渲染会**再来一遍 300 个请求**（连敲 5 个搜索键就是 5×300）。
        """
        _need_node()
        out = web_probe.run_index(_CASE_INFLIGHT_DEDUPE, _fixture())
        self.assertEqual(out["n"], JOBS_N)
        self.assertLessEqual(out["afterFirst"], MAX_INFLIGHT,
                             "第一趟一上来就发了 %d 个请求（上限 %d）"
                             % (out["afterFirst"], MAX_INFLIGHT))
        self.assertEqual(out["afterSecond"], out["afterFirst"],
                         "第一趟还在飞时再渲染一次，请求数从 %d 涨到了 %d（在途去重没生效）"
                         % (out["afterFirst"], out["afterSecond"]))
        self.assertGreaterEqual(out["drained"], 0, out)
        self.assertEqual(out["sent"], JOBS_N, out)
        self.assertLessEqual(out["maxInFlight"], MAX_INFLIGHT, out)
        self.assertEqual(out["cached"], JOBS_N, out)


class JobsSearchDebounceIsAlignedWithKbTest(unittest.TestCase):
    """防抖写法与 `#kbQ` 对齐（同一个 250ms）—— 风格检查，不判时间。

    仓库里已有先例：知识库搜索框 `#kbQ` 是 `clearTimeout(kbT); kbT = setTimeout(kbLoad, 250)`。
    台账搜索框照它的写法来，别自创一种节流风格（比如 300ms 或 requestAnimationFrame 版）。
    """

    def test_the_ledger_search_box_uses_the_same_250ms_style_as_kbq(self):
        with open(WEB_INDEX, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(src, r"clearTimeout\(kbT\);\s*kbT = setTimeout\(kbLoad, 250\)")
        self.assertRegex(src, r"clearTimeout\(jqT\);\s*jqT = setTimeout\(renderJobs, 250\)")


if __name__ == "__main__":
    unittest.main()
