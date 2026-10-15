# -*- coding: utf-8 -*-
"""`web/index.html` 的 **Node 探针**：真跑页面里的那段 JS，再断言**渲染出来的**东西。

为什么要真跑：这一页的前端断言最容易**假绿**——
「源码里有 `<option value="`」这件事在**占位选项**（`'<option value="">（无简历，请先新建）'`）
里也成立，所以「删掉真选项的 value」照样能通过（上一位实现者真踩过）。
本模块把页面的真 JS 丢进 `vm`（垫一层最小 DOM），只把**渲染结果**回传给 Python 去断言。

三条纪律（都写进代码里，不靠自觉）：

1. **切不到就报错**：函数体/脚本按锚点切，锚点没了立刻 `fatal`，绝不静默空跑；
2. **页面为空就报错**：调用方拿到空结果必须 `assert`（见 `run_index` 的返回值与各用例）；
3. **`<select>` 要照浏览器演**：重写 `innerHTML` 之后，原先选中的 option 不在了就回到第一项 ——
   这正是「重渲染清掉用户已选简历」那条 bug 的机制。垫片不这么演，那条用例就会**假绿**。

探针脚本从 **stdin** 读 JSON（`{js, scenario, fixture}`），结果 base64 回传（避免 Windows 控制台
编码把中文吃掉 —— 那种失败看起来像「文案不对」）。`scenario` 是一段**跑在页面作用域里**的 JS
（能直接调 `renderResumePicker` / `rsAskContinue` / `loadResumes` 这些真函数），
返回值放进 `out` 对象（`out.xxx = ...`）回传。
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_INDEX = os.path.join(ROOT, "web", "index.html")
NODE = shutil.which("node")

# `/* init */` 之后是页面的 boot（`ivInit(); loadResumes(); loadConfig(); loadJobs();`）：
# 本探针不需要它，也**不该**由它去发真实请求 —— 只取它之前的那一大段（含全部函数定义）。
BOOT_ANCHOR = "/* init */"


def index_script():
    """页面里那一段 JS（切不到就**立刻失败**，别静默切出半截）。"""
    with open(WEB_INDEX, "r", encoding="utf-8") as f:
        src = f.read()
    start = src.index("<script>")
    start = src.index(">", start) + 1
    end = src.index(BOOT_ANCHOR, start)
    js = src[start:end]
    if len(js) < 1000:
        raise AssertionError("切出来的脚本太短（锚点变了？）：%d 字符" % len(js))
    return js


PROBE = r"""
const fs = require('fs');
const vm = require('vm');

function bail(obj){
  process.stdout.write(Buffer.from(JSON.stringify(obj == null ? null : obj), 'utf8').toString('base64'));
  process.exit(0);
}

let payload;
try { payload = JSON.parse(fs.readFileSync(0, 'utf8')); }
catch (e) { bail({ fatal: '探针拿不到输入：' + String(e && e.message || e) }); }

const js = String(payload.js || '');
const scenario = String(payload.scenario || '');
const fixture = payload.fixture === undefined ? null : payload.fixture;

// 切错了就别往下跑：空跑出来的"绿"是假绿
if (js.indexOf('function renderResumePicker(r)') < 0)
  bail({ fatal: '探针拿到的不是 web/index.html 的脚本（锚点切错了）' });

const unexpected = [];
process.on('unhandledRejection', r => unexpected.push(String((r && r.stack) || (r && r.message) || r)));
process.on('uncaughtException', e => unexpected.push('uncaught: ' + String((e && e.stack) || e)));

/* ---- 最小 DOM 垫片 ----
   同一个选择器永远拿到同一个元素（`$('#x')` 前后一致），元素自动创建 ——
   页面 boot 里的 `$('#x').addEventListener(...)` 因此不会炸，而本探针只碰它关心的那几个。 */
const els = {};

// `<select>` 的浏览器语义：重写 innerHTML 后，原先选中的 option 不在了 → 回到第一项。
// 不照浏览器演，修 2 那条用例就会**假绿**（"保留已选" 去掉也照样通过）。
const SELECTS = new Set(['#resumeSel']);

function firstOptionValue(html){
  const m = /<option\b[^>]*\bvalue="([^"]*)"/.exec(html);
  return m ? m[1] : '';
}

/* ---- 委派按钮：让 `box.querySelectorAll('[data-x]')` 真的返回**渲染出来的**按钮 ----
   不实现这个，`renderJobs()` 里那一串
   `box.querySelectorAll('[data-jddel]').forEach(b=>b.addEventListener('click',...))`
   就永远是**空循环** —— 于是"按钮渲染出来了、但绑定是死的"在测试里看不出来：
   页面里有个 btn、源码里也有那行字符串、测试全绿，点下去什么都不会发生。
   这正是本项目出过两次的事故类型（后端全绿、前端悄悄坏掉），所以垫片必须把按钮造出来，
   并且 `click()` 要能调到真正注册的监听器。 */
function camelOf(attr){
  return attr.replace(/^data-/, '').replace(/-([a-z])/g, (_s, c) => c.toUpperCase());
}
function buttonsIn(html, sel){
  const m = /^\[(data-[a-z0-9-]+)\]$/.exec(String(sel || '').trim());
  if (!m) return [];
  const attr = m[1], out = [];
  const re = /<button\b([^>]*)>/g;
  let hit;
  while ((hit = re.exec(String(html == null ? '' : html)))) {
    const d = new RegExp('\\b' + attr + '="([^"]*)"').exec(hit[1]);
    if (!d) continue;
    const handlers = {};
    out.push({
      dataset: { [camelOf(attr)]: d[1] },
      _handlers: handlers,
      addEventListener(type, fn){ (handlers[type] = handlers[type] || []).push(fn); },
      removeEventListener(){}, setAttribute(){}, getAttribute(){ return null; },
      focus(){}, click(){ (handlers.click || []).slice().forEach(fn => fn()); }
    });
  }
  return out;
}

function mkEl(key){
  /* 按钮缓存：**同一个 innerHTML 下，反复 `querySelectorAll` 必须拿到同一批对象**。
     真实 DOM 就是这样：`renderJobs()` 先拿到按钮、注册监听器，测试再拿一次、`click()` ——
     拿到的是**同一个**按钮，所以点得响。若每次新建对象，"注册的那批"和"点的那批"不是一批，
     点击永远是空的（我第一版垫片就栽在这：断言全过、click 什么都不发生）。
     `innerHTML` 一换就作废旧缓存 —— 这也正是真实 DOM 的语义（换 innerHTML 即丢监听器），
     `renderJobs` 的注释明确依赖这一点。 */
  const btnCache = {};
  const el = {
    _html: '', _htmlRev: 0, value: '', textContent: '', hidden: false, className: '',
    src: '', files: null,
    dataset: {}, style: {}, options: [], selectedIndex: 0,
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){}, removeEventListener(){}, appendChild(){}, removeChild(){},
    insertBefore(){}, focus(){}, click(){}, setAttribute(){}, getAttribute(){ return null; },
    querySelector(){ return null; },
    querySelectorAll(sel){
      const k = String(sel) + '@' + el._htmlRev;
      if (!btnCache[k]) btnCache[k] = buttonsIn(el._html, sel);
      return btnCache[k];
    },
    getContext(){ return null; },
    add(){}, remove(){}
  };
  Object.defineProperty(el, 'innerHTML', {
    get(){ return el._html; },
    set(v){
      el._html = String(v == null ? '' : v);
      el._htmlRev++;
      if (SELECTS.has(key)) el.value = firstOptionValue(el._html);
    }
  });
  return el;
}
function elOf(sel){ const k = String(sel); return els[k] || (els[k] = mkEl(k)); }

const document = {
  querySelector: sel => elOf(sel),
  getElementById: id => elOf('#' + id),
  querySelectorAll: () => [],
  createElement: () => mkEl('@new'),
  addEventListener(){}, body: mkEl('body'), documentElement: mkEl('html'), title: ''
};
const window = {
  devicePixelRatio: 1, scrollY: 0, innerWidth: 1200, __tt: null,
  addEventListener(){}, removeEventListener(){}, scrollTo(){}, print(){},
  open(){ return mkEl('@popup'); }
};
window.window = window;
const navigator = { clipboard: { writeText: () => Promise.resolve() }, userAgent: 'probe' };
const location = { href: 'http://127.0.0.1/probe', origin: 'http://127.0.0.1', reload(){} };

const ctx = {
  document, window, navigator, location, console, Promise, JSON, Math, Object, Array, String,
  Number, Date, Boolean, RegExp, Error, TypeError, isNaN, parseInt, parseFloat,
  encodeURIComponent, decodeURIComponent, setTimeout, clearTimeout, setInterval, clearInterval,
  queueMicrotask, requestAnimationFrame: () => 0, cancelAnimationFrame(){}, reportError(){},
  __alerts: [], __confirms: [], __confirmQueue: [], __fetchJson: { ok: true },
  /* 从**渲染出来的** innerHTML 里解析 option（不碰垫片的内部状态）：
     调用方据此断言"那一条选项"到底是哪个 value。 */
  __opts: sel => {
    const html = String((sel && sel.innerHTML) || '');
    const out = [];
    const re = /<option\b([^>]*)>([\s\S]*?)<\/option>/g;
    let m;
    while ((m = re.exec(html))) {
      const a = /\bvalue="([^"]*)"/.exec(m[1]);
      out.push({ value: a ? a[1] : null, text: m[2] });
    }
    return out;
  }
};
ctx.confirm = msg => {
  ctx.__confirms.push(String(msg));
  return ctx.__confirmQueue.length ? ctx.__confirmQueue.shift() === true : false;
};
ctx.alert = msg => { ctx.__alerts.push(String(msg)); };
ctx.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(ctx.__fetchJson) });

const epilogue = '\n;globalThis.__fixture = ' + JSON.stringify(fixture) + ';\n' +
  '(async () => {\n  const out = {};\n  try {\n' + scenario +
  '\n  } catch (e) { out.error = String((e && e.stack) || e); }\n  return out;\n})()';

(async () => {
  let result;
  try {
    const p = vm.runInNewContext(js + epilogue, ctx);
    result = await p;
  } catch (e) {
    bail({ fatal: '跑页面 JS 失败：' + String((e && e.stack) || e) });
  }
  if (!result || typeof result !== 'object') result = { fatal: 'scenario 没有回传结果：' + String(result) };
  // 让**未 await 的调用**产生的那条 rejected promise 浮出来（修 6③ 要的正是它）
  await new Promise(r => setTimeout(r, 30));
  result.unexpected = unexpected;
  bail(result);
})().catch(e => bail({ fatal: '探针自身崩了：' + String((e && e.stack) || e) }));
"""


def run_index(scenario, fixture=None, timeout=60):
    """跑一段场景（真 JS），回传探针的 `out` 对象。

    失败会抛 `AssertionError`（切错脚本 / 探针崩了 / 缺 Node），**绝不静默返回空**。
    缺 Node 时是**响亮失败**（不是跳过）：跳过会让这批断言在无 Node 的机器上静默消失。
    """
    if NODE is None:
        # 缺 Node 是**响亮失败**（1.10③）：静默跳过等于把「真跑页面 JS」这批断言删掉，
        # 而全量测试照样绿。调用方（tests/test_app.py 的 `_need_node`）也会先报同一条。
        raise AssertionError("本环境缺 Node（`node` 不在 PATH 上）：跑不了页面探针，"
                             "这批前端渲染断言不能静默跳过 —— 装上 Node 再重跑")
    js = index_script()
    payload = json.dumps({"js": js, "scenario": scenario,
                          "fixture": fixture if fixture is not None else {}},
                         ensure_ascii=False)
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        script = os.path.join(tmp, "probe.js")
        with open(script, "w", encoding="utf-8", newline="") as f:
            f.write(PROBE)
        out = subprocess.run([NODE, script], input=payload.encode("utf-8"),
                             capture_output=True, timeout=timeout)
        if out.returncode != 0:
            raise AssertionError("Node 探针失败（exit=%s）：%s"
                                 % (out.returncode, out.stderr.decode("utf-8", "replace")[-2000:]))
        got = json.loads(base64.b64decode(out.stdout.decode("ascii").strip()).decode("utf-8"))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if not isinstance(got, dict):
        raise AssertionError("探针回传的不是对象：%r" % (got,))
    if got.get("fatal"):
        raise AssertionError("探针跑不起来：%s" % got["fatal"])
    return got
