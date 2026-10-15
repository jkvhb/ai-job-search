# -*- coding: utf-8 -*-
"""简历版本库：族谱（母版 → 子版）、母版快照、取回规则。

**边界**：本模块**只**读写 `resumes.json` 与 `resumes/` 下的文件；
绝不碰 `jobs.json` / `jds.json` / `knowledge.json`（那是 `app.py` 的活）。

磁盘布局（**每个族谱最多 2 份内容文件**）：:

    resumes/<名字>.md          ← 该族谱【最新版】内容（保持现有约定：打包分享等照旧）
    resumes/快照/<版本id>.md    ← 【母版】快照（只在上传子版、覆盖前留一份）

两条最容易写歪的规则（写歪了就是"以为留了档其实没留"）：

1. **快照的触发条件**（Task 1.8 定稿；Task 1.9 补了一条**按记录判断**的例外；
   Task 1.10 给这条例外加了"覆盖前先留档"）：
   「快照文件不存在」**或**「**按记录判断**它装的不是这一版的内容（记录的 ``sha256`` 与快照内容不符，
   而盘上那份**恰好就是**这一版）」**且**「``<名字>.md`` 存在且盘上有字节」**且**
   「这一版的原文不是 ``missing``」→ 才写。
   **已存在不重写**（快照一经生成即**不可变**，这是它能当"参照原点"的前提），
   唯一例外就是上面那条"**按记录判断**已过时"：不加它，坏字节会**永久占位**（盘上与记录一致了、
   ``read_version`` 还一直喊 ``mismatch``，真原文也再也补不进去）。
   ⚠️ 记录**没有** ``sha256`` 时**没有依据**判谁对 → 那条例外一律不生效（宁可不动）。
   ⚠️ **证明力全在记录里**：记录被人工改坏（比如把 ``sha256`` 改成盘上那份）时，"过时"这个
   判断本身就是错的 —— 所以覆盖旧快照之前**必须先把它留档**到一个旁挂文件
   （``<id>.md.stale-<旧字节 sha 前 8 位>``：直接写副本，不删除、也不冒充母版），
   否则真快照会被**不可逆**地覆盖掉（Task 1.10 第 2 条，复查实测过）。
   **留档失败 → 放弃本次修正**（抛 ``SnapshotArchiveError``，路由接成 409）：
   宁可这次不修，也绝不把旧快照静默盖掉（Task 1.10b 修 I-1）。同名旁挂只有在
   **内容相同**时才算"已经留过档"；内容不同就换名（``-2`` / ``-3`` …）。
   ⚠️ **旁挂是"留档"，不是"垃圾"**：它会**累积**（同一次覆盖最多多一个），
   是当前可达的最后一手（删掉就真找不回那份旧快照了）；**可手工删**；
   **迁移（`简历迁移.py`）不会带走它**（只按简历文件/记录走）。
   只有 ``purge`` 会顺带清掉**它自己那一版**的旁挂（那一版的快照都被彻底删了，旧副本也没意义）。
   推论：族谱 ``母(v1) → 子(v2) → 孙(v3)`` 里，v3 生成时**不产生新快照**；
   v1 的快照只有在"按记录判断过时"且盘上那份就是 v1 时才被修正（且修正前先留档）。
   存下来的永远是**盘上那份原始字节**（``store.read_bytes``，不是文本读：
   非 UTF-8 解码失败会被兜成空串、CRLF 会被归一成 LF —— 两种都会造出
   "与原文不逐字节相等"的假档）。**存档 ≠ 冒充**（Task 1.8 定的口径）：
   留档留的是「**这一版的原文**」（``<名字>.md`` 里那份）的字节 —— 它核对不通过时
   （``mismatch``：程序外改过 / 非 UTF-8 读不出来）字节**照样留**
   —— 那是用户写的东西，覆盖掉就没地方找了 —— 但**不记 ``mother_file``**、
   即不声称它是母版原文，``read_version`` 之后会如实报 ``mismatch``。
   **不写**只有两种（Task 1.10 第 3 条把主语说清，免得这句话被读成无条件的）：
   ① ``missing``：这一版的原文**从来没保留过**，那时盘上装的是**别人那一版**的内容，
   写进去就是张冠李戴；② 盘上那份**已经不是这一版**（快照已经是好的 ``mother``，
   或快照与盘上那份都不是这一版）—— 那它就不是这一版的原文，同样不该冒充成档。
   另外，延续**必须沿用族谱的名字**：换了名字，"即将被覆盖的 ``<名字>.md``"
   就不是母亲那一份，条件永远不成立 → 这里直接报错（见 ``create_version``）。

2. ``read_version`` 的四种 ``kind``：``latest``（读 ``<名字>.md``）／
   ``mother``（读快照）／
   ``mismatch``（读到内容了，但**内容与记录里的 ``sha256`` 不符**）／
   ``missing``（既不最新也不是母版的历史版 → 原文没保留）。
   **判定顺序（修 3）**：**先看快照，再看"文件主人"**。快照不可变、正是为
   "``<名字>.md`` 已经不是这一版了"留的；先认主人就会在 ``purge`` 掉最新版之后，
   拿文件里残留的**别人/别版的内容**回报这一版（``mismatch``），而它的原文其实完好地
   躺在快照里。只有**没有快照**时才走"文件主人 + sha 核对"那条路（母版同时是最新版、
   且从未延续过 → 没有快照 → 读最新版文件，仍报 ``latest``）。
   **Task 1.9 给这条顺序补的唯一例外**：**按记录判断**快照装的不是这一版（``sha256`` 对不上、
   坏字节进过快照），而盘上那份**恰好就是**（文件主人仍是这一版、内容与记录相符）→
   以盘上那份为准报 ``latest``。不加它，坏字节会永久占位（假红 + 真原文补不进去）。
   记录**没有** ``sha256`` 时这条例外不生效（没有依据，宁可不动）。
   **Task 1.10 第 1 条给读侧补的闸**：**"读不出来"绝不许被当成"文件是空的/盘上那份就是它"**。
   - 快照**有字节却读不出来**（非 UTF-8）时如实报 ``mismatch``，
     **绝不**说成"存过档、内容是空的"（那会让界面显示一份空简历）；
     而且此时若记录里**没有** ``sha256``，**不许**转头去认 ``<名字>.md`` ——
     没有指纹时那是**别人的内容**，报 ``latest`` 就是一句用户可见的假话（实测过）。
   - "``<名字>.md`` **有字节却读不出来**"同理：**不是** ``latest`` + 空文本（那是假空简历），
     而是 ``mismatch``。
   ``missing`` 时 ``text`` 是空串是**事实**，但调用方**必须靠 ``kind`` 分辨**，
   **绝不把空串当成功**（界面据此显示"旧版原文未保留"）。
   ``mismatch`` 与 ``missing`` 是**两回事**：一个"从来没存过"，一个"存过、但磁盘上的
   东西不再是它了"。``mismatch`` 时 ``text`` **照常返回**（让调用方能显示出来对账），
   但 ``kind`` 必须如实说是 ``mismatch``，**不许**谎报 ``latest`` / ``mother``。
   **合法边界（别把它也当成"读不出来"，Task 1.10 第 9 条）**：0 字节 / 纯空白的快照
   （Task 1.8 之前的实现真能造出 0 字节快照）在记录**没有** ``sha256`` 时是**合法空档**
   → 如实报 ``mother`` + 空文本。判"有没有字节"只能按**原始字节**（``strip()`` 后为空
   才算空档）；写成 ``if not text: mismatch`` 就把这批合法空档也诬成"读不出来"了。

3. **正文写入也必须是原子的**（Task 1.11 修 P0-13）：``_write_content`` 旧写法是
   ``open(path, "w")`` —— **先把旧正文截断成 0 字节**再一段段写，中途失败（磁盘满 /
   进程被 kill / 文件名超长）就留下**半截简历**；而**族谱第一家**时没有快照兜底，
   那份半截东西就是用户唯一的一份。现在**内容文件**（正文 / 母版快照 / 旧快照的留档旁挂）
   共用同一条 ``_atomic_write_bytes``（同目录临时文件 + ``os.replace``）：失败 = 目标
   **一个字节都没动** + 一句人话（``SnapshotArchiveError``，路由接成 **409**，不是裸
   ``OSError`` 的 500）。⚠️ 台账 ``resumes.json`` 不走这一条（它是 ``store.write_json``，
   ``store`` 里另一条原子实现）—— 说清范围，别让"只有这一条落盘实现"变成一句空话。
   失败时**还要撤掉这次新建的快照**（``_withdraw_fresh_snapshot``）：一次保存要么整体成立
   （新正文 + 新记录），要么整体不成立；盘上留一份"记录里没有、而 ``read_version``
   按版本 id 照样找得到"的快照就是第三种状态。撤不掉时**照实说**
   （日志 + 那句给用户看的话里明写"没有登记"），绝不假装撤掉了。
   **台账那一跳（``save_resumes``）失败同样不许留半成功**（批 1 复查抓到：正文留在盘上、
   记录里没有、异常还是裸 ``OSError`` → 500）：把正文**回滚**到写之前（族谱第一家就撤掉
   刚写下的那份）、那份新建的快照按"字节至少有一处可达"的顺序处理、失败翻成同一种专用异常；
   理由与顺序见 ``create_version`` 的 docstring。

诚实边界（规格 §8）：

- **旧版原文不保留**（用户已定的取舍：每个族谱只留母版 + 最新版）→ 历史版只有
  ``kind="missing"``。``ai_diff`` 也只是 AI 写的（见下），可能漏，以原文为准。
- ``sha256`` 是**内容指纹**（文本口径，与 ``sha256_of`` / ``_write_content`` 一致）。
  ``read_version`` **自己**用它核对磁盘（快照路径也核 —— 快照不可变，对不上同样是出事了），
  不一致就报 ``mismatch``。**不指望每个调用方都记得比 sha**：一处忘了，就会**静默把别人的
  简历内容当成这一版**返回 —— 这违反"失败绝不静默变成成功"。会不一致的三条真实路径：
  ① 文件被**程序外**改过（手工编辑 / 外部工具 / 别的程序写同名文件）；
  ② ``purge`` 彻底删掉某族谱的最新版后 ``resumes/<名字>.md`` 成**孤儿**，随后
  **另开同名简历**覆盖了它 → 该族谱里仍有记录、仍被 ``_owner_in`` 认作"文件主人"的版本
  会读到**别人的内容**（``kind`` 曾是 ``latest``，sha 却对不上）；
  ③ 手工编辑 ``resumes.json``（记录的 ``sha256`` 与磁盘文件对不上：记录被改了，或文件没跟着改）。
- **调用方约定**：见到 ``mismatch`` 必须显示
  「内容与记录不符（可能被程序外修改过）」，并**把内容与 sha 都亮出来**，
  **绝不静默当正常版本用**（规格 §8.2）。
- 内容文件按 ``newline=""`` 写：磁盘字节 == 内容的 UTF-8 字节（不做 ``\\r\\n`` 转换），
  所以 ``size`` 与 ``sha256`` 严格对应同一份内容，且与仓库里已有的 LF 简历文件一致。

AI 变更比对（规格 §4.4，见 ``ai_diff_of``）：

- **只在"延续"时触发**：``ai_diff_of(新版本id)`` 由**调用方**在"这次是延续"时调用；
  "新开一份独立的"**没有可比对的对象** → 一个 chat 都不调（``create_version`` 自己绝不调模型）。
- 输入 = **母版内容** + **子版内容**（都从 ``resumes.json`` 指到的文件读）。
- **失败不是致命**：网络 / 额度 / 坏 JSON → 版本**照样保存**，``ai_diff_error`` 记原因，
  ``ai_diff`` **保持空对象** —— **绝不写一个空的假 ``ai_diff``**（规格 §8.4）。
  界面据此显示"这次没能生成变更说明"。
"""
import hashlib
import os
import re
import tempfile
import threading
from datetime import datetime

import llm
import log
import store

RESUMES_FILE = "resumes.json"
SNAPSHOT_DIR = "快照"              # resumes/<SNAPSHOT_DIR>/<版本id>.md
REMOVED = "removed"

_AT_FMT = "%Y-%m-%d %H:%M"
_ID_RE = re.compile(r"^rv_(\d+)$")

# 旁挂留档的名字形状：`<版本id>.md.stale-` + 旧字节 sha 前 8 位（**小写**十六进制），
# 重名时再加 `-2` / `-3` …（见 `_archive_old_snapshot`）。
# `purge` 只按这个**精确形状**清旁挂（Task 1.10c 修 E-5）：按前缀一刀切会把
# 手工/别的程序用同一前缀命名的东西（`<id>.md.stale-手工说明`）一起删掉 —— 那是别人的字节。
_SIDE_ARCHIVE_TAIL_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9]+)?$")

# 读 → 改 → 写 必须整体串行：两个并发保存各自「读到同一份旧 doc」再各自写回，
# 后写的那次会把先写的记录整条抹掉，而用户在界面上看不出丢了档。
# 用 RLock 而不是 Lock：create_version 内部还要调 load_resumes / save_resumes（同线程可重入）。
_LOCK = threading.RLock()


class SnapshotArchiveError(RuntimeError):
    """「旧快照没能留档」→ **放弃本次修正**（Task 1.10b 修 I-1 第 3 点）。

    为什么必须是一种**专用**异常（而不是裸 `RuntimeError`）：
    ``app.py`` 的 ``POST /api/resumes`` 只接这一种并回 **409**（冲突：盘上那份东西现在的
    状态让这次保存做不了）。若它用裸 ``RuntimeError``，将来 ``ai_diff_of`` 之类的地方冒出
    任何 ``RuntimeError`` 都会被**误报成"这次保存被中止了"** —— 而那次保存其实**已经成功落盘**
    了。对用户说"没保存"而实际保存了，是本项目最不能犯的那种假话。

    继承 ``RuntimeError`` 只为向后兼容（既有调用方 / 既有测试接的就是 ``RuntimeError``）。

    它现在覆盖**三种**"盘上现在的状态让这次保存做不了"（Task 1.11 加了第 3 种）：
    ① 旧快照没能留档；② 母版原文读不出来（那份东西现在读不了，绝不覆盖它）；
    ③ **正文写不进去**（``_atomic_write_bytes`` 失败：磁盘满 / 文件名超长 / 被占用）——
    三种的共同点是"**这次保存整体没成立**"，所以都该给用户一句人话而不是 500。
    名称里的 Snapshot 是历史包袱：**不要**据此把它当成"只有快照相关才会抛"。
    """

    def __init__(self, message="", cause=None):
        super(SnapshotArchiveError, self).__init__(message)
        self.cause = cause          # 底层 OSError（日志里要用），但**不**并进给用户看的那句话


# 旁挂留档最多试到第几号：`<id>.md.stale-<8位>`、`-2` … `-9`。
# 到顶还给不出不撞的名字 → 当作"没能留档"（宁可这次不修，也绝不静默盖掉旧快照）。
_SIDE_ARCHIVE_TRIES = 9

# "旁挂文件存在、但读不出来"的哨兵 —— 它**不是** None（None = 文件不存在，那一支可以直接写）。
# ⚠️ 全模块**只此一处**定义（Task 1.10c 修 E-3：原先在 `_archive_old_snapshot` 后面又写了一遍，
# 后者盖前者 —— 同一个哨兵两份定义，改一处漏一处就是"读不出来的名字被当成名字空着"）。
_UNREADABLE = object()

# "写正文之前盘上那份**读不出来**"的哨兵（台账写失败要回滚时用）。
# 它与 `None`（此前**没有**这个文件）必须分得开：把"读不出来"当成"不存在"，回滚那一步就会
# 把**别人的**正文当成"这次新建的"删掉 —— 又一个"不知道"被当成"安全"。
_PREV_CONTENT_UNKNOWN = object()


def _archive_old_snapshot(snap_path, old):
    """把即将被覆盖的旧快照字节**留档**到旁挂文件；返回真正写进去的路径（失败抛 ``SnapshotArchiveError``）。

    三条硬规则（每条都对应一次实测过的不可逆丢数据）：

    1. **另写一份副本**（``_write_side``：走 ``_atomic_write_bytes``，同目录临时文件 + ``os.replace``），
       不靠**改名**旧快照：改名（``os.replace(snap_path, side)``）一旦失败，旧快照就还在原地、
       而调用方会接着把它盖掉 —— 那正是复查实测的那条路径。
       （"另写副本"与"原子"不矛盾：副本的字节先落进临时文件、再换名到旁挂名上，
       旧快照在整个过程中**一个字节都没动**。）
    2. **重名不算留过档**：首选名字（``<快照路径>.stale-<旧字节 sha 前 8 位>``）被占用时，
       只有**内容与旧字节相同**才算"已经留过档"；内容不同就**换一个不撞的后缀**
       （``-2`` / ``-3`` …）。"只判存在不判内容"= 重名静默跳过留档 = 旧字节被盖掉。
    3. **留档失败 → 抛异常**（调用方据此**放弃本次修正**）：宁可这次不修，
       也绝不把旧快照静默盖掉（那是不可逆的）。
    """
    room = os.path.dirname(snap_path)
    base = "%s.stale-%s" % (snap_path, hashlib.sha256(old).hexdigest()[:8])
    for n in range(1, _SIDE_ARCHIVE_TRIES + 1):
        side = base if n == 1 else "%s-%d" % (base, n)
        try:
            existing = store.read_bytes(side)
        except OSError:
            existing = _UNREADABLE          # 存在但读不出来 → **不算**"已留过档"，按新名字留着看
        if existing is None or existing == old:
            # 名字空着 → 直接写这一支；同名的内容**与旧字节相同** → 那就是已经留过档了（不重复写）。
            # ⚠️ 判据必须是**内容**：只判"名字在不在"会在重名时静默跳过留档 → 旧字节被盖掉。
            return _write_side(side, old)
    raise SnapshotArchiveError(
        "没能给旧快照留档（旁挂名字试到 -%d 都被占用了），已中止本次修正：宁可这次不修，"
        "也绝不把旧快照静默盖掉（那是不可逆的）。可手工清理 %s 下的 .stale-* 旁挂后再试。"
        % (_SIDE_ARCHIVE_TRIES, room))


def _write_side(side, old):
    """真正落盘那一行（变异取证要打在**这一行**上：留档失败必须冒泡成 ``SnapshotArchiveError``）。

    ⚠️ 这里**必须**放弃本次修正：留档失败还被 ``pass`` 掉的话，调用方会接着把旧快照**盖掉**
    （复查实测：那时旧快照内容在整棵树里再也搜不到）。

    **走的是本模块"内容文件"那条原子写**（正文 / 快照 / 旁挂共用，见 ``_atomic_write_bytes``）——
    "留档旁挂只是旁挂，直写就行"这个想法经不起看：
      - 直写（``store.write_bytes``）半途失败（磁盘满 / 进程被 kill）会留下一个**半截的**
        ``.stale-<8位>`` 文件；而那个名字是**内容寻址**的（声称自己就是那份 sha 的字节），
        内容却只有一半 —— 名与实不符。原子写的半截只可能落在**临时文件**里，由 ``finally``
        删掉，旁挂名上只会出现完整字节。
      - 失败语义**正合适**：``_atomic_write_bytes`` 抛的本来就是 ``SnapshotArchiveError``
        （正是调用方要的那种：放弃本次修正，路由接成 409），临时文件也在 ``finally`` 里清掉，
        不会在快照目录里积垃圾。
      - 目标与临时文件**同目录**（快照目录），``os.replace`` 在这里同样是原子的。
    """
    _atomic_write_bytes(
        side, old, what="旧快照留档", event="resume.snapshot_archive_failed",
        tail="宁可这次不修，也绝不把旧快照静默盖掉（那是不可逆的）。",
        log_fields={"side": os.path.basename(side)})
    return side


def _atomic_write_bytes(path, raw, what, event, tail, log_fields=None):
    """把 ``raw`` 字节**原子地**写到 ``path``（同目录临时文件 + ``os.replace``）。

    **本模块的"内容文件"只有这一条落盘实现**（Task 1.11 修 P0-13：正文 + 母版快照 +
    留档旁挂共用；别在这里之外再写第二套）。⚠️ 说清**范围**，免得又变成"说的和做的不一样"
    （批 1 复查抓到过：旁挂那条当时还是直写）：
      - 走这条的：``<名字>.md``（正文）、``快照/<版本id>.md``（母版快照）、
        ``快照/<版本id>.md.stale-*``（旧快照留档旁挂）；
      - **不走这条的**：台账 ``resumes.json`` —— 那是 ``store.write_json``（``store`` 里另一条
        原子实现，与这里无关），本函数只落**内容字节**。

    为什么必须原子：裸写（``open(path, "wb")``）**先把目标截断成 0 字节**，然后一段一段往里写；
    中途失败（磁盘满 / 进程被 kill / 断电 / 文件名超长）就留下一个**半截文件** ——
    对快照是"版本记录一直指着一个残缺的档"，对正文是"用户唯一的一份成了半截"，
    对留档旁挂则是"名字谎称自己就是那份 sha 的字节、内容却只有一半"。
    临时文件必须与目标**同目录**：``os.replace`` 只在同一文件系统内保证原子（跨盘会退化成复制）。

    参数（**三处**调用各给一句自己的话，函数本身不认识"快照" / "正文" / "旁挂"）：
      - ``what``：失败句里"没能写入的东西"（``母版快照`` / ``简历正文`` / ``旧快照留档``）；
      - ``event``：日志事件名（各种各自一个，事后好分辨是哪一处失败）；
      - ``tail``：那句话里"为什么宁可这次不保存"的那半句（也当日志的 ``note``）；
      - ``log_fields``：该事件特有的字段（比如 ``{"snapshot": ...}`` / ``{"side": ...}``）。

    失败一律翻成 ``SnapshotArchiveError``（一句中文人话，``app.py`` 的 ``POST /api/resumes``
    接成 409）：裸 ``OSError`` 穿到路由就是 **500**，用户只看到「服务器内部错误」，
    对"没写成、这次保存已中止"什么都没说清。
    """
    room = os.path.dirname(path)
    tmp = ""
    try:
        os.makedirs(room, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=room)
        os.close(fd)
        store.write_bytes(tmp, raw)
        os.replace(tmp, path)
        return
    except OSError as e:
        fields = {"dir": room, "error": str(e)}
        fields.update(log_fields or {})
        log.log_event(event, level="warn", note=tail, **fields)
        raise SnapshotArchiveError(
            "没能写入%s（%s），已中止本次保存：%s请先确认 %s 可写、磁盘空间足够。"
            % (what, e, tail, room), cause=e)
    finally:
        # 失败时别在目标目录里留垃圾（成功时 `os.replace` 已经把临时文件挪走了）
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def _write_snapshot_atomically(snap_path, raw):
    """把母版原文**原子地**写成快照；失败抛 ``SnapshotArchiveError``（实现见 ``_atomic_write_bytes``）。

    为什么必须原子（Task 1.10c 修 E-1）：旧写法是裸的 ``store.write_bytes(snap_path, raw)``，
    它等于 ``open(path, "wb")`` —— **先把旧快照截断成 0 字节**，然后一段一段往里写。
    中途失败（磁盘满 / 进程被 kill / 断电）就留下一个**半截快照**，而版本记录仍指着它：
    用户以为"母版原文留过档"，取回来却是残缺的 —— 又一例"以为留了档其实没留"。

    失败一律翻成**专用异常**（一句中文人话，``app.py`` 的 ``POST /api/resumes`` 接成 409）：
    裸 ``OSError`` 穿到路由就是 **500**，用户只看到「服务器内部错误」，对"快照没写成、
    这次保存已中止"什么都没说清（Task 1.10c 第 E-1 条）。
    """
    return _atomic_write_bytes(
        snap_path, raw, what="母版快照", event="resume.snapshot_write_failed",
        tail="宁可这次不保存，也绝不留下一个不完整的快照 —— 版本记录会一直指着它，"
             "以后取回这一版拿到的却是残缺的内容。",
        log_fields={"snapshot": os.path.basename(snap_path)})


def _withdraw_fresh_snapshot(snap):
    """撤掉「**这次调用刚写下、此前不存在**」的快照；返回一句要**照实说**的话（没问题就是空串）。

    ⚠️ ``snap`` 是 ``(路径, 相对路径)``：调用方必须自己保证"此前不存在"（只撤这次新建的），
    这里面**不做**任何判断 —— 传错了就会删掉别人的档。

    为什么选"撤掉"而不是"留着并说明"（P0-13 要求二选一，这里写清理由）：
    一次保存要么**整体成立**（新正文 + 新记录），要么**整体不成立**。盘上留下一份
    "记录里没有、而 ``read_version`` 按版本 id 照样找得到"的快照，就是**第三种状态**：
    用户以为这次保存被中止了，可盘上多了一份谁也说不清来历的档（记录的 ``mother_file``
    是空的，而下次延续又会因为"快照已存在"而不再触发归档 —— 它从此永远说不清是哪来的）。
    撤掉 = 回到**调用前**的盘面。两条例外：
      - **调用前就存在的东西一律不碰**（这里只可能拿到"这次新建"的那一个）；
      - 留档的 ``.stale-*`` 旁挂**不动** —— 它们是 append-only 的留档（见 ``_archive_old_snapshot``），
        是那份旧字节当前可达的最后一手，删了就是真丢字节。
    撤不掉（被别的程序占着）时**绝不假装撤掉了**：返回一句人话，由调用方并进给用户看的那句话里，
    并另记一条日志 —— "盘上留了一份没登记的档"这件事必须说出来。
    """
    snap_path, snap_rel = snap
    try:
        os.remove(snap_path)
        return ""
    except OSError as e:
        log.log_event("resume.snapshot_orphan_left", level="warn",
                      snapshot=os.path.basename(snap_path), dir=os.path.dirname(snap_path),
                      error=str(e),
                      note="这次保存已中止，但刚写下的母版快照没能撤掉：盘上留了一份**没有登记**的档"
                           "（里面是覆盖前盘上那份字节，记录里不会有 mother_file），可手工删。")
        return ("（另外：这次已经写下的母版快照 %s 没能撤掉，它还留在盘上、**没有登记**进版本记录 ——"
                " 里面是覆盖前盘上那份字节，可以手工删。）" % snap_rel)


def _restore_content_before_write(path, prev):
    """把正文恢复成 ``_write_content`` 之前那份 → ``(是否已收回, 一句要照实说的话)``。

    只在"台账（``resumes.json``）没写成"时调用：那一刻盘面是**半成功**的
    （正文已写、记录里没有它），三个分支各说各的实话：

      - ``prev is None``：这次调用之前**没有**这个文件（族谱第一家）→ 撤掉刚写下的那一份
        = 回到调用前的盘面（**绝不**把它当成"留一份给用户看看"：那正是"正文在盘上却没有登记"
        这种没人看得见的状态，`/api/resumes` 的 ``unregistered`` 也只在**新开**时才照得到它）；
      - 有字节：用**同一条**原子写把旧字节写回去（写失败 = 那份没登记的新正文留在盘上，照实说）；
      - ``_PREV_CONTENT_UNKNOWN``：写之前那份**读不出来** → 无从"恢复"，照实说，**绝不动手删**
        （"读不出来"不等于"不存在"，删了就是拿别人的东西冒账）。
    """
    base = os.path.basename(path)
    if prev is _PREV_CONTENT_UNKNOWN:
        log.log_event("resume.ledger_rollback_failed", level="warn", file=base,
                      error="写入之前那份正文读不出来，无从恢复",
                      note="台账没写成要把正文收回来，但写之前那份读不出来 → 只能如实说"
                           "（盘上现在是这次要保存的新内容，且没有登记）")
        return False, ("（盘上的 %s 现在是这次要保存的新内容 —— 写入之前那份读不出来，"
                       "没法恢复到原样。）" % base)
    if prev is None:
        try:
            os.remove(path)
            return True, ""
        except FileNotFoundError:
            return True, ""            # 已经不在了（别的程序删的）→ 结果一样：这次写下的正文不在盘上
        except OSError as e:
            log.log_event("resume.ledger_rollback_failed", level="warn", file=base, error=str(e),
                          note="这次新建的正文没能撤掉 → 盘上留了一份**没有登记**的新正文（必须如实说）")
            return False, ("（这次新建的 %s 没能撤掉，它还留在盘上、**没有登记**进版本记录。）" % base)
    try:
        _atomic_write_bytes(path, prev, what="简历正文（回滚）",
                            event="resume.ledger_rollback_failed",
                            tail="台账（resumes.json）没写成 → 这次保存整体不成立：正文必须回到写之前"
                                 "的样子，绝不留「正文在盘上、版本记录里没有」这种半成功状态。")
        return True, ""
    except SnapshotArchiveError as e:
        return False, ("（这次写下的正文没能收回（%s），盘上现在是这次要保存的新内容、"
                       "**没有登记**进版本记录。）" % e)


def _undo_failed_ledger_write(file_path, prev, fresh_snapshot):
    """台账没写成 → 把盘面收回这次调用之前 → ``(正文是否已收回, 一句要照实说的话)``。

    顺序**不能反**（这是"字节至少要有一处可达"那条底线）：
    先收回正文；**只有**正文真的收回了，那份**这次新建的**母版快照才可以撤 ——
    否则它里面那份"覆盖前盘上那份字节"就是当前唯一的一份，撤掉才是真丢数据。
    """
    restored, note = _restore_content_before_write(file_path, prev)
    if not restored:
        if fresh_snapshot:
            log.log_event("resume.snapshot_orphan_left", level="warn",
                          snapshot=os.path.basename(fresh_snapshot[0]),
                          dir=os.path.dirname(fresh_snapshot[0]),
                          note="台账没写成、正文也没能收回 → 这份**这次新建**的快照**故意不撤**："
                               "里面是覆盖前盘上那份旧字节，是那些字节当前可达的最后一手"
                               "（版本记录里没有登记它）")
            note += ("（这次新建的母版快照 %s **故意没撤**：里面是覆盖前盘上那份旧字节，"
                     "那些字节现在只剩它这一份，但版本记录里没有登记它。）" % fresh_snapshot[1])
        return restored, note
    if fresh_snapshot:
        return restored, _withdraw_fresh_snapshot(fresh_snapshot)
    return restored, note


# ---------- 路径 / 取值小工具 ----------
def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_id(value):
    """版本 id 只能是**单个**文件名段 —— 它会被拼进快照路径，不许带路径分隔符。"""
    v = _text(value)
    if not v or v in (".", "..") or os.path.basename(v) != v:
        return ""
    return v


def _resume_rel(name):
    return "resumes/%s.md" % name


def _resume_path(name):
    return store.p_path("resumes", name + ".md")


def _snapshot_rel(version_id):
    return "resumes/%s/%s.md" % (SNAPSHOT_DIR, version_id)


def _snapshot_path(version_id):
    return store.p_path("resumes", SNAPSHOT_DIR, version_id + ".md")


def _doc_path():
    return store.p_path(RESUMES_FILE)


def _write_content(path, text):
    """写简历正文 —— 与快照共用**原子写**（``_atomic_write_bytes``：临时文件 + ``os.replace``）。

    ``newline=""`` 很关键：默认的换行转换会把 ``\\n`` 写成 ``\\r\\n``（Windows），
    于是"磁盘字节数"与"内容的 UTF-8 字节数"对不上，``size`` / ``sha256`` 也不再是
    同一份内容的指纹。**写侧**因此是保真的 —— 但快照要真的"与原文逐字节相等"，
    还得**读侧**也按字节（快照走 ``store.read_bytes`` / ``write_bytes``：
    文本读会把 CRLF 归一成 LF、把非 UTF-8 兜成空串，见 ``create_version``）。
    ⚠️ 原子写走的还是"文本 → ``encode("utf-8")`` → 写字节"，所以保真口径**没有变**：
    同一份文本在两种写法下落的字节完全相同（已有一条正面断言钉住含 CRLF / 非 ASCII 的情形）。

    P0-13：旧写法是 ``open(path, "w")``（**先把旧正文截断成 0 字节**再一段段写）——
    中途失败（磁盘满 / 被 kill / 文件名超长 ``WinError 123``）就留下**半截简历**，
    而族谱**第一家**时没有快照兜底，那份半截东西就是用户唯一的一份；而且裸 ``OSError``
    穿到路由是 **500**。现在失败 = 目标一个字节都没动 + 一句人话（409）。
    """
    raw = (text if isinstance(text, str) else str(text or "")).encode("utf-8")
    return _atomic_write_bytes(
        path, raw, what="简历正文", event="resume.content_write_failed",
        tail="宁可这次不保存，也绝不留下半截简历 —— 盘上那份仍然是上一次保存的内容，"
             "一个字节都没改。")


def sha256_of(text):
    """内容的 sha256（小写十六进制）。指纹口径 = 文本的 UTF-8 编码（与 ``_write_content`` 一致）。"""
    t = text if isinstance(text, str) else str(text or "")
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _kind_on_disk(text, rec, ok_kind):
    """内容与记录指纹**一致** → 维持 ``ok_kind``（``latest`` / ``mother``）；不一致 → ``mismatch``。

    为什么要在这里核、而不是留给调用方：``sha256`` 只有一个用途 —— 回答"磁盘上这份东西
    还是不是这一版"。把它留给每个调用方各自去比，一处忘了就会**静默把别人的简历内容当成
    这一版**返回（见 ``purge`` 的孤儿文件路径）。所以核对收进库里，``kind`` 如实说话。

    口径与边界：
    - 只比 ``text`` 的 sha（``sha256_of`` 口径 = 文本的 UTF-8 字节）。换行转换不参与判断：
      程序外用 CRLF 重写、而按文本读回来一致的文件**不会**被判 mismatch（``size`` 才会变）。
    - 记录里没有 ``sha256``（人工改坏 / 老记录）→ 没有可比对的依据，只能维持 ``ok_kind``；
      这里的"不知道"绝不冒充"核对通过"，但也绝不凭空喊 mismatch。
    - 记录里没有 ``sha256`` 时，**"文件不见了"不会**变成 mismatch：``store.read_text`` 给空串，
      而"空"与"记录里没有 sha"两条合起来正是"维持 ``ok_kind``"。**文件不存在**这条依据
      不需要 sha（``os.path.exists`` 就能判）→ 由**调用方**（`_read_in` 的"文件主人"支）
      先分三种再调本函数（Task 1.10b 修 I-2）。这就是下面那条 ⚠️ 的另一半：
      本函数**只**能用 sha 说话，"东西在不在 / 读不读得出来"都不归它管。
    - ⚠️ **"读不出来"不归这里管**：记录里**没有** ``sha256`` 时上面那条"维持 ``ok_kind``"
      会把"文件有字节却解不出"（``read_text`` 兜成空串）说成 ``latest`` + 空文本 = 一份**假空简历**。
      那不是 sha 能发现的（没有 sha），只能按**原始字节**判 ——
      调用方（`_read_in` 的两条出口）必须先用 `store.content_bytes_present` 拦一道，
      再调本函数。"读不出来"永远不等于"文件是空的"（Task 1.10 第 1 条）。
    """
    expected = _text(rec.get("sha256"))
    if expected and sha256_of(text) != expected:
        return "mismatch"
    return ok_kind


# ---------- 记录字段的容错读取（不在读/写侧"净化"，否则坏形状会被悄悄修好） ----------
def _name_of(rec):
    return store.safe_name(rec.get("name"))


def _root_of(rec):
    return _clean_id(rec.get("root_id")) or _clean_id(rec.get("id"))


def _rev_of(rec):
    return _as_int(rec.get("rev"), 1)


def _is_removed(rec):
    return bool(rec.get(REMOVED))


def _find(recs, version_id):
    vid = _clean_id(version_id)
    if not vid:
        return None
    for r in recs:
        if _clean_id(r.get("id")) == vid:
            return r
    return None


def _max_rev(recs, root_id):
    top = 0
    for r in recs:
        if _root_of(r) == root_id:
            top = max(top, _rev_of(r))
    return top


def _next_id(recs):
    top = 0
    for r in recs:
        m = _ID_RE.match(_clean_id(r.get("id")))
        if m:
            top = max(top, int(m.group(1)))
    return "rv_%04d" % (top + 1)          # 超过 9999 原样变长：绝不把两个版本撞成同一个 id


def _owner_in(recs, root_id):
    """**当前 ``<名字>.md`` 里装的是谁的内容** —— 该族谱 rev 最大的版本（含已移出的）。

    为什么含已移出的：文件是"最新版容器"，移出列表**不删文件**，所以最近创建的那一版
    的内容仍在文件里。``current_of()``（未移出的 rev 最大者）回答的是另一个问题；
    这里用 owner 才不会把"已经不在文件里的那一版"说成 latest。
    """
    owner = None
    for r in recs:
        if _root_of(r) != root_id:
            continue
        if owner is None or _rev_of(r) >= _rev_of(owner):
            owner = r
    return owner


def _current_in(recs, root_id):
    cur = None
    for r in recs:
        if _is_removed(r) or _root_of(r) != root_id:
            continue
        if cur is None or _rev_of(r) > _rev_of(cur):
            cur = r
    return cur


def _mother_in(recs, root_id):
    for r in recs:
        if not _is_removed(r) and _root_of(r) == root_id and bool(r.get("chosen")):
            return r
    return None


def _read_in(recs, version_id):
    """``read_version`` 的本体（复用已加载的 doc，供锁内调用）。

    **顺序是判定的一部分**（修 3）：**先看母版快照，再看"文件主人"**。
    快照是不可变的、且正是为"``<名字>.md`` 已经不是这一版了"留的；先认主人的话，
    ``purge`` 掉最新版之后（文件里留着那一版的内容、主人退到它身上）就会把**别人的内容**
    当成这一版、并以 ``mismatch`` 回报 —— 而这一版的原文其实完好地躺在快照里。

    **Task 1.9 给这条顺序补的唯一例外**：**按记录判断**快照装的不是这一版的内容
    （``sha256`` 与记录不符：坏字节进过快照 / 被程序外改过），而盘上那份**恰好就是**
    （文件主人仍是这一版、且内容与记录 ``sha256`` 相符）→ 以盘上那份为准，报 ``latest``。
    不加这条例外，坏字节会**永久占位**：磁盘上与记录一致了、这里还一直喊 ``mismatch``（假红），
    真正的母版原文也**再也补不进去** —— `create_version` 的修正条件正是"这里回到 ``latest``"。
    记录**没有** ``sha256`` 时**没有依据**判谁对，这条例外一律不生效（维持快照优先、宁可不动）。
    ⚠️ 这条例外的**全部证据都在记录里**：记录被改坏（`sha256` 被改成盘上那份）时它会指错 ——
    所以 `create_version` 覆盖旧快照前会先把旧字节改名留档（Task 1.10 第 2 条）。

    Task 1.10 第 1 条：**读侧的每一条出口都要过"没有 ``sha256`` 就不认盘上那份"这道闸**。
    快照**有字节却读不出来**、而记录里又没有指纹时**直接**报 ``mismatch``（不许转头去认
    ``<名字>.md`` 里那份**别人的内容**）；``<名字>.md`` 自己**有字节却读不出来**时也不是
    ``latest`` + 空文本，而是 ``mismatch``。"读不出来"永远不等于"文件是空的"。
    """
    rec = _find(recs, version_id)
    if rec is None:
        raise ValueError("找不到版本：%s" % (version_id,))
    vid = _clean_id(rec.get("id"))
    # ① 有快照 → 从快照取（快照同样是磁盘文件：它不可变，sha 对不上说明出事了 → 照样 mismatch）
    snap = _snapshot_path(vid)
    stale_snap = None                 # 「按记录判断，快照里的东西不是这一版」时的兜底答案
    if os.path.exists(snap):
        text = store.read_text(snap, "")
        # 快照**有字节却读不出来**（非 UTF-8 等）时，绝不能报成"存过档、内容是空的" ——
        # 那正是"文件读不出来被当成文件是空的"，只是搬到了快照的读侧。
        # `content_bytes_present` 为真 = 里面有非空白的字节，所以这里只会命中"解码失败"，
        # 合法的空快照（0 字节 / 纯空白）不受影响。
        if not text and store.content_bytes_present(snap):
            # ⚠️ 这里也**必须**过"没有 sha256 就不认盘上那份"这道闸：否则没有指纹时会把
            # `<名字>.md` 里**别人的内容**当成这一版的"最新版原文"报给用户（实测过）。
            if not _text(rec.get("sha256")):
                return "", snap, "mismatch"      # 没有依据 → 快照优先，宁可报不符
            stale_snap = ("", snap, "mismatch")
        else:
            kind = _kind_on_disk(text, rec, "mother")
            if kind == "mother":
                return text, snap, kind          # 快照**就是**这一版 → 照旧快照优先
            stale_snap = (text, snap, kind)      # 走到这里 = 按记录判断它不是这一版
    # ② 认"文件主人"（``<名字>.md`` 里装的是它）
    owner = _owner_in(recs, _root_of(rec))
    if owner is not None and _clean_id(owner.get("id")) == vid:
        path = _resume_path(_name_of(rec))
        text = store.read_text(path, "")
        # 读到了也要**核指纹**：`<名字>.md` 是共享容器，磁盘上的东西可能已经不是这一版了
        # （程序外改过 / purge 留的孤儿被别人覆盖）。不符 → mismatch，绝不谎报 latest。
        # ⚠️ Task 1.10b 修 I-2：**先分三种**，"读不出来"和"东西都没了"都不许混进"空内容"：
        #   ① 文件**不存在** → mismatch。"磁盘上的东西不是它了"包括"**东西都没了**"——
        #      记录里没有 `sha256` 时（`_kind_on_disk` 无从核对）旧实现会报 `latest` + 空文本，
        #      而 `path` 还指着**一个不存在的文件**：界面标题写「最新版原文」、正文空白（实测过）。
        #   ② 文件存在、且按**原始字节**看里面有可丢的字节、却读不出文本（非 UTF-8 / 读失败）
        #      → mismatch（`read_text` 兜出的空串与"文件真的是空的"长得一模一样，
        #      只有按字节看才分得开）。
        #   ③ 文件存在且字节上就是空的（0 字节 / 纯空白）→ **合法内容**，照旧按指纹判（`latest`）。
        if not os.path.exists(path):
            kind = "mismatch"
        elif not text and store.content_bytes_present(path):
            kind = "mismatch"
        else:
            kind = _kind_on_disk(text, rec, "latest")
        # 有快照且按记录判断快照不是这一版时，只有"盘上这份确实是"才改用文件（Task 1.9 那条例外）；
        # 两边都不是这一版 → 照旧把快照那份亮出来对账（快照优先，不因为文件也不符就改口）。
        if kind == "latest" or stale_snap is None:
            return text, path, kind
    if stale_snap is not None:
        return stale_snap
    # ③ 原文确实没保留：text 为空是**事实**，path 也**故意**给空串——
    # 免得调用方拿着一个"名义路径"去读，把最新版当成这一版发出去。
    return "", "", "missing"


# ---------- 读写 ----------
def load_resumes():
    """读版本库。垃圾 / 缺字段 → 结构完整但列表空（绝不抛异常，也绝不因读失败去改盘）。"""
    with _LOCK:
        raw = store.read_json(_doc_path(), None)
        version = 1
        items = []
        if isinstance(raw, dict):
            version = _as_int(raw.get("version"), 1)
            if isinstance(raw.get("resumes"), list):
                items = raw["resumes"]
        recs = []
        for r in items:
            if not isinstance(r, dict):
                continue
            # 没有 id / 名字（含带路径分隔符的 id）的记录无法寻址也无法定位文件 → 不进入视图。
            # 只在内存里丢弃，不动盘上的字节（人工还能救）。
            if not _clean_id(r.get("id")) or not _name_of(r):
                continue
            recs.append(dict(r))
        return {"version": version, "resumes": recs}


def save_resumes(doc):
    """原子写（``store.write_json``）。**不**按字段净化记录：写侧的坏形状必须留在盘上可见。"""
    d = doc if isinstance(doc, dict) else {}
    items = d.get("resumes")
    items = items if isinstance(items, list) else []
    out = {"version": _as_int(d.get("version"), 1) or 1,
           "resumes": [r for r in items if isinstance(r, dict)]}
    with _LOCK:
        store.write_json(_doc_path(), out)


def list_versions(include_removed=False):
    with _LOCK:
        recs = load_resumes()["resumes"]
        return [dict(r) for r in recs if include_removed or not _is_removed(r)]


def by_id(version_id):
    with _LOCK:
        return _find(load_resumes()["resumes"], version_id)


def current_of(root_id):
    """该族谱 rev 最大的**未移出**版本（都不在 → None）。"""
    with _LOCK:
        return _current_in(load_resumes()["resumes"], _clean_id(root_id))


def mother_of(root_id):
    """该族谱 chosen 为真的版本（没有 → None）。已移出的不算（移出=不能继续用）。"""
    with _LOCK:
        return _mother_in(load_resumes()["resumes"], _clean_id(root_id))


# ---------- 写入 ----------
def create_version(name, content, parent_id=None, note=""):
    """写一份简历并登记版本记录。

    新开（``parent_id=None``）：独立根，``rev=1``，本族谱的第一个版本。
    延续（``parent_id`` 非空）：同一族谱，覆盖 ``<名字>.md`` **之前**
    按"快照的触发条件"留下母版原文（见模块开头；Task 1.9 起还包括"修正一份
     **按记录判断已过时**的快照“—— Task 1.10 起，修正前先把旧快照的字节留档，
     见 ``<id>.md.stale-<8位>``；Task 1.10b 起，**留档失败就放弃本次修正**
     （抛专用异常 ``SnapshotArchiveError``））—— 留的是**盘上那份原始字节**
     （核对不通过也留，但那时不记 ``mother_file``：存档 ≠ 冒充）。

     会抛 ``ValueError`` 的情形：名字为空／重名（未移出的）／父版本不存在／
     父版本已移出（移出的族谱不能延续）／延续时改了名字。
     母版原文**读不出来**（被锁 / 权限）**或旧快照没能留档** **或正文写不进去**
     （磁盘满 / 文件名超长 / 被别的程序占用）**或台账写不进去** → 抛
     ``SnapshotArchiveError``（``RuntimeError`` 的子类）并**中止本次保存**
     （绝不覆盖那份读不出来的东西，绝不把旧快照盖掉，绝不留半截正文，也绝不留
     "正文在盘上、版本记录里没有"的半成功状态；消息是人话，
     `app.py` 的路由**只接这一种**并接成 **409** 显示给用户）。

     正文写入是**原子**的（Task 1.11 修 P0-13，与快照共用 ``_atomic_write_bytes``）：
     失败 = ``<名字>.md`` 一个字节都没动 + 这次**不登记**新版本。并且，若这次调用
     **新建**了一份母版快照，失败时它会被**撤掉**（``_withdraw_fresh_snapshot``：
     一次保存要么整体成立、要么整体不成立；撤不掉会在那句话里如实说明）。

     **最后一步写台账失败**（批 1 复查抓到的半成功形态）走同一套口径，**选"回滚正文"而不是
     "留着正文并说明"**，理由三条（都在这个模块里能对上）：
     ① 这里本来就是"整体成立或不成立"的契约（上面那句 + `_withdraw_fresh_snapshot` 的理由都写着
        "第三种状态"不许存在）；"正文在盘上、记录里没有"正是第三种状态 —— 而且是**用户看不见**
        的那种：延续时文件名是**已登记**的，`/api/resumes` 的 `unregistered` 面板根本照不到它，
        可界面上的"最新版"取回来还是旧的（旧正文在快照里），用户完全没法知道自己盘上多了什么；
     ② 留着正文**解决不了**"登记"这件事 —— 台账这一跳刚刚才写失败，我们没有第二个地方可以
        如实登记它，"未登记的版本"在延续这条路上压根没有能识别它的形态；
     ③ 回滚的代价是**可收回的**：这份正文就是这次请求带上来的内容（`content`），调用方手里还有；
        而**不回滚**留下的是"用户以为没保存、盘上却有"的持久状态，只能靠人工发现。
     回滚本身也可能失败（磁盘满时"把旧正文写回去"也会失败）→ 那时**照实说**
     （日志 ``resume.ledger_rollback_failed`` + 那句话里明写"没有登记"），
     并且**那份这次新建的母版快照绝不撤**（覆盖前的旧字节只剩它这一份）。
    """
    text = content if isinstance(content, str) else str(content or "")
    clean = store.safe_name(name)
    if not clean:
        raise ValueError("简历名字不能为空（也不能是 . / ..）")
    pid = _clean_id(parent_id) or None
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        parent = None
        if pid is not None:
            parent = _find(recs, pid)
            if parent is None:
                raise ValueError("找不到父版本：%s" % pid)
            if _is_removed(parent):
                raise ValueError("父版本已移出列表，不能延续（先恢复再延续）")
            if _name_of(parent) != clean:
                raise ValueError("延续时简历名不能改（这一族谱叫「%s」）："
                                 "换了名字，被覆盖的就不是母亲那一份，快照条件永远不成立"
                                 % _name_of(parent))
        new_id = _next_id(recs)
        if parent is not None:
            root_id = _root_of(parent)
            # rev 是**族谱内序号**（规格 §3 从 1 起）：取族谱内最大值 +1，而不是 parent.rev+1。
            # 两者在「母版就是最新版」的常见情形下一致；但族谱里已有 v2 时再从 v1 延续，
            # parent.rev+1 会造出第二个 rev=2，让"谁是文件里那一版"变得不确定。
            rev = _max_rev(recs, root_id) + 1
        else:
            root_id = new_id
            rev = 1
        # name 在**未移出**的简历里必须唯一（绝不静默覆盖别人的文件）；
        # 同族谱的其它版本共用这个名字，不算冲突。
        for r in recs:
            if _is_removed(r) or _name_of(r) != clean:
                continue
            if parent is not None and _root_of(r) == root_id:
                continue
            raise ValueError("已有一份未移出的简历叫「%s」，不能覆盖它（先移出或换个名字）" % clean)
        file_path = _resume_path(clean)
        mother = _mother_in(recs, root_id) if parent is not None else None
        # 这次调用**新建**的（此前盘上没有的）快照 `(路径, 相对路径)`：正文写失败时要撤掉它。
        # 只撤"这次新建的" —— 调用前就存在的快照一律不碰（见 `_withdraw_fresh_snapshot`）。
        fresh_snapshot = None
        if mother is not None:
            mid = _clean_id(mother.get("id"))
            snap_path = _snapshot_path(mid)
            # 已在盘上的快照**原则上**不重写：快照一经生成即**不可变**，这是它能当"参照原点"的前提。
            if os.path.exists(file_path):
                # 只借 `_read_in` 的**结论**（`m_kind`）；它的 `text` 是文本读的产物，
                # 这里**故意不用**：那正是本块的洞（见下）。
                _m_text, _m_path, m_kind = _read_in(recs, mid)
                # 快照**原则上不可变**，唯一的例外是"按记录判断，存着的那份不是这一版的内容"
                # （记录的 sha256 与快照内容不符）、而盘上这份**恰好就是**（`m_kind` 回到 `latest`）。
                # 不加这条例外，坏字节会永久占位：盘上与记录一致了、库还一直喊 mismatch（假红），
                # 真正的母版原文也再也补不进去（实测过）。
                # ⚠️ 证明力**全在记录里**：记录被改坏（比如把 sha 改成盘上那份）时这个"过时"判断
                # 本身就可能是错的 —— 所以下面覆盖前**必须先把旧快照改名留档**（Task 1.10 第 2 条）。
                # ⚠️ 记录**没有** sha256 时**不修** —— 没有依据，宁可不动（那个 `bool(expected)` 就是这道闸）。
                expected = _text(mother.get("sha256"))
                snap_existed_before = os.path.exists(snap_path)
                stale = (bool(expected) and snap_existed_before
                         and m_kind in ("latest", "mother")
                         and sha256_of(store.read_text(snap_path, "")) != expected)
                if (not snap_existed_before or stale) and m_kind != "missing":
                    # 快照必须**按字节**留原文：`_read_in` 走的是文本读（`store.read_text`）。
                    # 非 UTF-8 文件解码失败会被兜成空串（→ `_write_content(snap, "")` 写出
                    # 0 字节"假存档"，而库里还记着 `mother_file` 指向它 = 静默"以为留了档"）；
                    # CRLF 会被归一成 LF（→ 快照与原文不再逐字节相等，而 `_write_content` 的
                    # docstring 明确承诺过"逐字节相等"）。写侧本来就是保真的（`newline=""`），
                    # 所以只用把**读**换成 `store.read_bytes` 即可。
                    #
                    # 口径（Task 1.8 定三条；Task 1.9 把这里的输入收窄成三种）：
                    # **本块只可能拿到 `latest` / `mismatch` / `missing`，拿不到 `mother`** ——
                    # 进得来只有两条路：① `snap_path` 不存在（`_read_in` ②③ 两段，`mother` 只在
                    # 快照分支出现）；② `stale` 为真（它要求快照内容 sha 与记录**不符**，而
                    # `mother` 的定义正是"快照内容与记录 sha **相符**"）→ 两条路都不可能给 `mother`。
                    # - `latest`           → 存字节 **+ 记** `mother_file`（核对通过：它就是母版原文；
                    #   ② 那条路进来的 `latest` 还顺带把被坏字节占位的快照**修正**成真原文）
                    # - `mismatch`         → 存字节、**不记** `mother_file`（**存档 ≠ 冒充**：
                    #   字节是用户写的东西，覆盖掉就没地方找了；但"这是母版原文"这句话不说，
                    #   `read_version` 会如实报 mismatch，界面照旧亮出"内容与记录不符"）
                    # - `missing`          → **不存**（那时 `<名字>.md` 里装的是**别人那一版**
                    #   的东西 —— 这一版的原文根本没保留过；存进去就是张冠李戴，
                    #   `read_version` 会把别人的内容当成这一版的"存档"亮出来）
                    try:
                        raw = store.read_bytes(file_path)
                    except OSError as e:
                        # 读不出母版原文就**不覆盖**它：宁可这次不保存，也绝不把用户那份东西冲掉。
                        # 翻成一句人话：路由（`app.py` 的 `POST /api/resumes`）把它接成 **409**，
                        # 用户看到的就是这一句，而不是兜底那句「服务器内部错误：…」。
                        # Task 1.10b 修 M-9：与"旧快照留不了档"共用**同一个专用异常**
                        # （性质一样：盘上那份东西现在的状态让这次保存做不了），
                        # 而且路由只接这一种 —— 别的 `RuntimeError` 绝不许被说成"保存已中止"。
                        raise SnapshotArchiveError("没能给母版留档（读不出来：%s），已中止本次保存；"
                                                   "请先确认那份文件没有被别的程序占用。" % e, cause=e)
                    # 0 字节没什么可留的：写一个空快照只会让"有快照就走快照"那条顺序
                    # 把这一版说成"存过档"，档里却什么都没有 —— 又一个"以为留了档"。
                    if raw:
                        if stale and snap_existed_before:
                            # 旧快照的字节也**不许静默丢掉** —— 那是盘上出现过的东西，而"过时"
                            # 这个判断的证明力**全在记录里**（记录被改坏时它就是错的，实测过）。
                            # 留档到一个旁挂名字（不删除、也不冒充母版）。
                            # ⚠️ Task 1.10b 修 I-1：留档**失败 → 放弃本次修正**（异常由这里抛出，
                            # 路由接成 409），并且**先读旧字节** —— 读不出来（M-3）同样走这条专用异常，
                            # 绝不让裸 OSError 穿到 app.py 变成 500。
                            try:
                                old = store.read_bytes(snap_path) or b""
                            except OSError as e:
                                raise SnapshotArchiveError(
                                    "没能给旧快照留档（读不出来：%s），已中止本次修正：宁可这次不修，"
                                    "也绝不把旧快照静默盖掉（那是不可逆的）。请先确认那份文件没有被别的"
                                    "程序占用。" % e, cause=e)
                            _archive_old_snapshot(snap_path, old)
                        # Task 1.10c 修 E-1：**原子**写（临时文件 + `os.replace`）。
                        # 旧写法 `store.write_bytes(snap_path, raw)` 会先把旧快照截断成 0 字节，
                        # 中途失败就留下半截快照，而记录仍指着它；而且裸 `OSError` 会变成 500。
                        _write_snapshot_atomically(snap_path, raw)
                        if not snap_existed_before:
                            # P0-13：这份快照是**这次调用新建**的 —— 正文写失败（下面那个 try）时
                            # 要撤掉它。否则盘上会留下一个"记录里没有、而 `read_version` 按版本 id
                            # 照样找得到"的孤儿档（这就是本项要修的第二种伤害）。
                            fresh_snapshot = (snap_path, _snapshot_rel(mid))
                        if m_kind in ("latest", "mother"):
                            mother["mother_file"] = _snapshot_rel(mid)
        # 台账（`resumes.json`）写失败时要把**盘面收回这一步之前** → 先按**原始字节**记下
        # 这份简历此刻盘上是什么（`store.read_bytes`：文件不存在 → None；**读不出来** →
        # `_PREV_CONTENT_UNKNOWN`，绝不冒充"没有"，见 `_restore_content_before_write`）。
        # 位置必须在 `_write_content` **之前**：写完再读，读到的就是刚写下的新正文了。
        try:
            prev_content = store.read_bytes(file_path)
        except OSError:
            prev_content = _PREV_CONTENT_UNKNOWN
        try:
            _write_content(file_path, text)
        except SnapshotArchiveError as e:
            # 正文没写成 → 这次保存**整体**不成立（记录也不会登记）→ 把刚写的快照撤掉，
            # 回到调用前的盘面。撤不掉时**照实说**（并进那句给用户看的话里），绝不假装撤掉了。
            extra = _withdraw_fresh_snapshot(fresh_snapshot) if fresh_snapshot else ""
            if extra:
                raise SnapshotArchiveError(str(e) + extra,
                                           cause=getattr(e, "cause", None)) from None
            raise
        rec = {
            "id": new_id,
            "name": clean,
            "root_id": root_id,
            "parent_id": pid,
            "rev": rev,
            "at": datetime.now().strftime(_AT_FMT),
            "size": os.path.getsize(file_path),
            "sha256": sha256_of(store.read_text(file_path, "")),
            "file": _resume_rel(clean),
            "mother_file": None,
            "chosen": False,
            # 变更说明（Task 4）：这里**只留空壳**，绝不在这里调模型 ——
            # "新开一份独立的"没有可比对的对象，一次 chat 都不该调（规格 §4.4）。
            # 延续时由调用方另调 `ai_diff_of(新版本id)` 填。失败时 `ai_diff_error` 非空、`ai_diff` 留空对象。
            "ai_diff": {},                 # {summary, changes:[{where, what}]}
            "ai_diff_error": "",
            "note": note if isinstance(note, str) else str(note or ""),
            REMOVED: False,
        }
        recs.append(rec)
        try:
            save_resumes(doc)
        except Exception as e:                    # noqa: BLE001 —— 收口的是**盘面**，与异常种类无关
            # 「正文已写、台账没写」= **半成功**：用户以为没保存，盘上却已经有一份记录里没有的
            # 正文；**延续**时连 `/api/resumes` 的 `unregistered` 面板都看不见它（文件名就是已登记
            # 的那个，只是内容换成了新版）。所以这里**先把盘面收回调用之前**，再把失败翻成人话。
            restored, note = _undo_failed_ledger_write(file_path, prev_content, fresh_snapshot)
            log.log_event("resume.ledger_write_failed", level="warn", name=clean, error=str(e),
                          restored=restored,
                          note=note or "盘面已收回本次保存之前的样子（正文收回了、记录里没有新版本）")
            if not isinstance(e, OSError):
                raise                             # 不是写盘失败（程序缺陷）：盘面已收回，异常照原样抛
            raise SnapshotArchiveError(
                "没能把新版本登记进版本记录（%s），已中止本次保存：%s%s请稍后重试。"
                % (e,
                   "刚写下的新正文已收回，盘面回到这次保存之前的样子。"
                   if restored else
                   "这次保存**整体没有成立**（版本记录里没有新版本），但盘上那份新正文"
                   "没能收回、也**没有登记**进版本记录。",
                   note), cause=e) from None
        return dict(rec)


def read_version(version_id):
    """取某一版原文 → ``(text, path, kind)``，``kind ∈ {latest, mother, mismatch, missing}``。

    - ``latest``：这一版就是 ``<名字>.md`` 里那一份（**且内容与记录 sha 一致**）→ 读该文件
      （**没有快照**时走这条；或**按记录判断**快照不是这一版、而盘上这份才是时走这条 ——
      Task 1.9 的例外，见模块开头第 2 条）
    - ``mother``：这一版留有快照（**且快照内容与记录 sha 一致**）→ 读快照。
      **快照优先于"文件主人"**（修 3）：``purge`` 掉更新的版本之后，``<名字>.md`` 里留着的
      是那一版的内容，而这一版的原文仍完好地在快照里 —— 先认主人就会拿别人的内容回报它。
    - ``mismatch``：**读到内容了，但它不是这一版** —— ``sha256_of(text) != record["sha256"]``。
      ``text`` / ``path`` **照常返回**（调用方要能把内容亮出来对账），只有 ``kind`` 如实变红。
      **"读不出来"也算这里**（Task 1.10 第 1 条）：快照或 ``<名字>.md`` **有字节却读不出来**
      （非 UTF-8 / 读失败）时，``text`` 是空串、但 ``kind`` 说 ``mismatch`` ——
      **绝不**说成"存过档、内容是空的"（那会让界面显示一份空简历），也**绝不**在没有
      ``sha256`` 依据时转头去认 ``<名字>.md`` 里**别人的内容**报 ``latest``。
    - ``missing``：原文没保留 → ``text == ""`` 且 ``path == ""``；
      **调用方必须靠 kind 分辨**，绝不把空串当成功

    为什么 ``mismatch`` 必须独立于 ``missing``：``missing`` 是"**从来没存过**"（历史版按设计
    不留原文），``mismatch`` 是"**存过，但磁盘上的东西不再是它了**"。把后者混进前者，用户会
    以为"这版当初就没留档"；混进 ``latest`` / ``mother``，用户会**拿着别人的简历内容当自己
    这一版**用 —— 静默的错比响的错危险得多。

    会走到 ``mismatch`` 的三条真实路径：
    ① 文件被**程序外**改过（手工编辑、外部工具、别的程序写同名文件）；
    ② ``purge`` 彻底删掉某族谱的最新版后 ``resumes/<名字>.md`` 成**孤儿**，随后**另开同名
       简历**覆盖了它 —— 该族谱里仍有记录、仍被认作"文件主人"的版本就会读到**别人的内容**；
    ③ 手工编辑 ``resumes.json``（记录的 ``sha256`` 与磁盘文件对不上：记录被改了，或文件没跟着改）；
    ④ **那个文件被删了**（Task 1.10b 修 I-2）："磁盘上的东西不是它了"包括"**东西都没了**"。
       记录里**没有** ``sha256`` 时旧实现会报 ``latest`` + 空文本（`path` 还指着不存在的文件），
       界面标题就写「最新版原文」+ 空正文 —— 一句用户可见的假话。

    **"文件主人"这一支先分三种**（Task 1.10b 修 I-2，`_read_in` ②）：
    ① 文件**不存在** → ``mismatch``；② 文件存在且 `content_bytes_present` 为真（有字节却解不出）
    → ``mismatch``；③ 文件存在且字节上就是空的 → **合法空内容**，照旧按指纹判（``latest``）。

    两个说不清的边界，如实写在这里：
    - 记录**没有** ``sha256`` 字段时没有可**核对**的依据，"知道的东西是不是这一版"只能维持
      ``latest`` / ``mother``（见 ``_kind_on_disk``）—— "不知道"不冒充"核对通过"。
      但**"读不出来"与"东西在不在"都另有依据**（`content_bytes_present` 看的是**原始字节**、
      `os.path.exists` 看的是文件在不在，两者都不需要 sha）：那时一律 ``mismatch``，
      **不适用**上面那条"维持"（Task 1.10 第 1 条 / Task 1.10b 修 I-2）。
      ⚠️ **Task 1.10c 修 D：调用方必须分清"维持"与"核对过"**。上面这条"维持"只说明
      **没有依据推翻**它，**不等于**"这一版的内容被核对过" —— 记录里**没有** ``sha256`` 时，
      盘上那份完全可能**是别人那一版**（复查枚举出 7 行这种组合），而 ``kind`` 照样是
      ``latest`` / ``mother``。判据只有一条：记录里**有** ``sha256`` 才算核对过。
      界面据此措辞：核对过才说「最新版原文」，没有指纹要换成
      「这一版没有指纹，内容未核对」（``app.py`` 的 ``/api/resumes/file`` 多回的那个
      ``verified`` 就是这条判据；**本函数的三元组 API 故意不动** —— 它只回答 `kind`）。
    - **0 字节 / 纯空白的快照**是**合法**的历史残留（Task 1.8 之前的实现真能写出 0 字节快照）。
      记录里没有 ``sha256`` 时，它是 ``mother`` + 空文本（"这一版当初留下的就是一份空档"），
      **不是** ``mismatch``。判"文件里有没有字节"只认**原始字节**（``strip()`` 后为空才算空档）——
      写成 ``if not text: mismatch`` 就会把这批合法空档诬成"读不出来"（Task 1.10 第 9 条）。
      同理 `<名字>.md` 真的是 0 字节时仍是合法的空简历（``latest`` + 空文本），**不是**"读不出来"；
      但那个文件**不存在**时必须报 ``mismatch``（"没了"和"空的"是两件事）。

    **调用方约定（T2/T3 照此做）**：见到 ``mismatch`` 必须显示
    「内容与记录不符（可能被程序外修改过）」，并**把内容与 sha 都亮出来**（记录的 vs 实测的），
    **绝不静默当正常版本用**。

    未知 id → ``ValueError``。
    """
    with _LOCK:
        return _read_in(load_resumes()["resumes"], version_id)


# ---------- AI 变更比对（规格 §4.4 / Task 4） ----------
#
# 触发条件只有一条：**这次保存是"延续"**（有母版可参照）。"新开一份独立的"没有可比对的对象
# → 一个 chat 都不调（那条路由根本不调 `ai_diff_of`，本模块里也没有别的地方碰模型）。
#
# 调用约定与 `interview.analyze` / `outcome.attribute` **完全一致**：`chat(prompt, cfg)` 两参；
# 默认实现走 `llm.call_openai_compatible`（模块属性在**调用时**现取，不是 import 时绑定 ——
# 所以测试 patch `llm.call_openai_compatible` 就能挡住整条路，绝不会真发网络请求）。
_AI_DIFF_PROMPT = """你在对比一份简历的**两个版本**，写出"新版到底改了哪些地方"。

要求（这几条是硬要求）：
1. **只写确实改了的地方**；没有把握的**宁可不写**，绝不为了凑数编一条。
2. 没改的部分**不要提**。
3. 每条要能对上原文里的具体位置（比如 项目经历 / 技能 / 教育经历 / 自我评价 / 基本信息）。
4. **只输出一个 JSON 对象**，不要解释、不要 markdown 代码块，格式：
{"summary": "一句话总述这次改了什么", "changes": [{"where": "改在哪一节", "what": "改了什么"}]}
5. 如果两版**没有任何实质差别**：summary 写"没有实质变化"，changes 给空数组。

【旧版（母版）内容】
%(mother)s

【新版（这次上传的）内容】
%(child)s
"""


def _clean_diff(value):
    """把模型返回的东西收成 ``{summary, changes}``；**收不出任何内容就返回 None**。

    为什么不"缺字段补空默认值"：``ai_diff`` 只有在**真的写出了东西**时才该非空 ——
    空壳 ``{"summary": "", "changes": []}`` 与"没生成"在界面上长得一样，却会骗过
    "有 ai_diff 就是成功"的判断（规格 §8.4：绝不写假的 ``ai_diff``）。
    """
    if not isinstance(value, dict):
        return None
    summary = value.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    raw = value.get("changes")
    raw = raw if isinstance(raw, list) else []
    changes = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        where = c.get("where")
        what = c.get("what")
        where = where.strip() if isinstance(where, str) else ""
        what = what.strip() if isinstance(what, str) else ("" if what is None else str(what).strip())
        if not (where and what):
            continue
        changes.append({"where": where, "what": what})
    if not summary and not changes:
        return None
    return {"summary": summary, "changes": changes}


def normalize_ai_diff(value):
    """对外口径：永远是 ``{summary, changes}``（给界面/测试一个稳定的形状）。"""
    return _clean_diff(value) or {"summary": "", "changes": []}


def _diff_of(reply):
    """解析模型回复：dict 直接用（已经是对象），字符串走 ``llm.parse_json_reply``。"""
    if isinstance(reply, dict):
        return _clean_diff(reply)
    return _clean_diff(llm.parse_json_reply(reply))


def _call_text_model(prompt, cfg, chat=None):
    """文本模型调用的**唯一**出口（调用约定与 `interview._call_model` 逐字一致）。

    ``chat`` 的注入约定是 **`chat(prompt, cfg)` 两参**（与 `interview.analyze` /
    `outcome.attribute` 一致）；默认实现走 `llm.call_openai_compatible`。
    模型细节（base_url / key / model / temperature）只在这里收口 ——
    **绝不把网络细节漏进"变更比对"的业务逻辑里**，也**绝不自己拼 HTTP**。

    `llm.call_openai_compatible` 是**模块属性、调用时现取**（不是 import 时绑定）：
    测试 patch 它一次就能挡住整条路（`resumelib` 不持有它的旧引用）。
    """
    if chat is not None:
        return chat(prompt, cfg)
    cfg = cfg if isinstance(cfg, dict) else (store.load_config() or {})
    tm = (cfg.get("text_model") or {}) if isinstance(cfg, dict) else {}
    return llm.call_openai_compatible(tm.get("base_url") or "", tm.get("api_key") or "",
                                      tm.get("model") or "",
                                      [{"role": "user", "content": prompt}],
                                      json_mode=True, temperature=tm.get("temperature"))


def _save_diff(version_id, diff=None, error=""):
    """把比对结果（或失败原因）写进那条记录并返回它。**失败时只写 ``ai_diff_error``**。

    只在锁里读改写这一条：`ai_diff` 与 `ai_diff_error` 是**互为反证**的两个字段，
    分开两次写会留下"两个都空"的中间态（界面据此会显示"没留下变更说明"，那不是真的）。
    """
    with _LOCK:
        doc = load_resumes()
        rec = _find(doc["resumes"], version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        if diff is not None:
            rec["ai_diff"] = diff
            rec["ai_diff_error"] = ""
        else:
            rec["ai_diff_error"] = error
        save_resumes(doc)
        return dict(rec)


def ai_diff_of(version_id, chat=None, cfg=None):
    """这一版相比**母版**改了哪些地方（AI 写）。**只在"延续"时由调用方调用**。

    输入是**母版内容**与**子版内容**（都从 ``resumes.json`` 指到的文件读，见 ``read_version``）。
    比成功 → 写进 ``ai_diff``；比失败（没母版 / 网络 / 额度 / 坏 JSON）→ 写 ``ai_diff_error``，
    ``ai_diff`` **保持空对象**，**版本照样在**。返回那条记录（调用方据此看 ``ai_diff``）。

    失败时**抛异常只有两种情况**：版本 id 不存在（``ValueError``，编程错误）。
    其余一律记进 ``ai_diff_error`` 返回 —— 用户在界面上要看到"这次没能生成变更说明 + 原因"，
    而不是一个连版本都没有了的 500（规格 §8.4）。

    ⚠️ **"没有母版"不等于"比对失败"**（§4.4：没有可比对的对象 → 一次都不调）：
    新开的独立简历（``parent_id`` 为空 / 母版记录已不在）时**直接返回、不写任何字段**，
    **绝不**写成 ``ai_diff_error``（那会让界面把"本来就没有可比的东西"说成一次失败）。
    **"只在延续时调模型"这道闸就在下面这行**：调用方（`app.py`）只对延续调本函数，
    本函数自己再挡一次"没有母版"——两道闸都在，任一道被绕开都跑不出比对。

    ``chat`` 注入约定与 ``interview.analyze`` 一致：``chat(prompt, cfg)`` 两参。
    """
    with _LOCK:
        recs = load_resumes()["resumes"]
        rec = _find(recs, version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        pid = _clean_id(rec.get("parent_id"))
        mother = _find(recs, pid) if pid else None
        if mother is None:
            # 没有母版＝这次不是"延续"（新开的独立简历）→ **没有可比对的对象，一次都不调**。
            # 也绝不写成错误：那不是失败，是"本来就没有可比的东西"。
            return dict(rec)
        m_text, _m_path, m_kind = _read_in(recs, pid)
        c_text, _c_path, c_kind = _read_in(recs, _clean_id(rec.get("id")))

    if m_kind not in ("latest", "mother"):
        # 母版原文没保留（或与记录不符）→ 拿不出"母版内容"。**如实报错**，
        # 绝不拿空串/别人的内容去让模型瞎猜一遍（那会写出一份看起来像真的变更说明）。
        reason = ("拿不到母版原文（%s）：旧版原文未保留，或内容与记录不符，"
                  "无法比对这次改了什么" % (m_kind,))
        return _save_diff(version_id, error=reason)
    if c_kind not in ("latest", "mother"):
        reason = "拿不到这一版的原文（%s）：内容与记录不符，无法比对" % (c_kind,)
        return _save_diff(version_id, error=reason)
    if not c_text:
        reason = "这一次的内容是空文件，没有可比的正文"
        return _save_diff(version_id, error=reason)

    prompt = _AI_DIFF_PROMPT % {"mother": m_text, "child": c_text}
    try:
        reply = _call_text_model(prompt, cfg, chat)
        diff = _diff_of(reply)
        if diff is None:
            raise RuntimeError("模型没有写出任何改动的条目（summary 与 changes 都空）")
    except Exception as e:
        # 坏 JSON **不重试**：版本已经保存了，比对失败不是致命（规格 §4.4）。原因要留给用户看。
        return _save_diff(version_id, error=("变更比对失败：%s" % e)[:600])
    return _save_diff(version_id, diff=diff)


def choose(version_id):
    """「选定继续优化」= 标记母简历：同族谱内其它版本一律置 False。"""
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        if _is_removed(rec):
            raise ValueError("已移出列表的版本不能选为母简历（先恢复）")
        root_id = _root_of(rec)
        for r in recs:
            if _root_of(r) == root_id:
                r["chosen"] = r is rec
        save_resumes(doc)
        return dict(rec)


def remove(version_id):
    """移出列表（不是删文件）：文件、母版快照、变更说明全部留在盘上。"""
    with _LOCK:
        doc = load_resumes()
        rec = _find(doc["resumes"], version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        rec[REMOVED] = True
        save_resumes(doc)
        return dict(rec)


def remove_current_of(name):
    """按简历**名字**移出它**当前**那一版（解析与移出在**同一个临界区**里，修 7）。

    语义与"先 ``current_of`` 再 ``remove``"完全一样，但**两步不许分开**：分开时中间插进一次
    并发保存，移出的就是**过时的那一版**（用户看到消失的是另一版）。数据不见得丢，但"移出的是
    哪一版"成了错的 —— 这种事在界面上根本看不出来，所以必须由库自己在锁内一次做完。

    名字口径与 ``create_version`` 记录里的 ``name`` 一致（**不含扩展名**：``母版.md`` → ``母版``），
    与"老前端按文件名删"那条路对得上。这个名字没有任何**未移出**的版本 → ``ValueError``。
    """
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        want = os.path.splitext(store.safe_name(name))[0]
        target = None
        if want:
            for r in recs:
                if _is_removed(r) or _name_of(r) != want:
                    continue
                # 同一族谱里取 rev 最大的**未移出**版本（= 这个名字当前对应的那一版）
                target = _current_in(recs, _root_of(r)) or r
                break
        if target is None:
            raise ValueError("找不到该简历：%s" % (name,))
        target[REMOVED] = True
        save_resumes(doc)
        return dict(target)


def restore(version_id):
    """放回列表。名字被别的未移出简历占用 → ``ValueError``（要求先改名）。"""
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, version_id)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        if _is_removed(rec):
            name, root_id = _name_of(rec), _root_of(rec)
            for r in recs:
                if r is rec or _is_removed(r) or _name_of(r) != name:
                    continue
                if _root_of(r) != root_id:
                    raise ValueError("名字「%s」已被另一份未移出的简历占用，先改名再恢复" % name)
            rec[REMOVED] = False
            save_resumes(doc)
        return dict(rec)


def purge(version_id):
    """彻底删：删记录 + **它自己**的快照文件（**连同它自己的 ``.stale-*`` 留档旁挂**）。
    **绝不碰**别人的文件（反假绿要求）。

    先落盘记录再删文件：万一写盘失败，快照还在（宁可留多，不可留假）。
    注意：``resumes/<名字>.md`` 是**族谱最新版容器**，本函数不删它 ——
    彻底删掉最新版后，那个文件会留在盘上（孤儿文件，名字在索引里空出来）。

    ``.stale-*`` 旁挂（Task 1.10b 修 I-1）**是"留档"不是"垃圾"**：它装的是"即将被覆盖的
    旧快照字节"。它们会**累积**（同一次覆盖最多多一个）、**可手工删**（删掉就真找不回了）、
    **迁移不会带走它**。既然这一版的快照都彻底删了，它自己的留档旁挂也就没有意义了
    （那些字节本来就是"这一版快照的旧副本"）→ 一并清掉，免得目录里越积越多。
    ※ **只清这一版 id 前缀**的旁挂：别人的快照 / 别人的旁挂一律不碰。
    """
    vid = _clean_id(version_id)
    with _LOCK:
        doc = load_resumes()
        recs = doc["resumes"]
        rec = _find(recs, vid)
        if rec is None:
            raise ValueError("找不到版本：%s" % (version_id,))
        recs.remove(rec)
        save_resumes(doc)
        snap = _snapshot_path(vid)
        room = os.path.dirname(snap)
        own = [snap]
        try:
            # 只认**精确形状**的名字：`<这一版id>.md.stale-` + 8 位十六进制（重名再加 `-N`）。
            # ⚠️ Task 1.10c 修 E-5：旧写法只判前缀 `startswith(...)` —— 手工/别的程序恰好用同一
            # 前缀命名时（`<id>.md.stale-手工说明`）会被一起删掉，而本函数承诺"别人的一律不碰"。
            prefix = "%s.md.stale-" % vid
            if os.path.isdir(room):
                own += [os.path.join(room, f) for f in os.listdir(room)
                        if f.startswith(prefix) and _SIDE_ARCHIVE_TAIL_RE.match(f[len(prefix):])]
        except OSError:
            pass
        for p in own:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return dict(rec)
