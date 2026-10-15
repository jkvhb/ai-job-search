# -*- coding: utf-8 -*-
"""resumelib（简历版本库：族谱 + 母版快照 + 取回规则）的测试。

继承 IsolatedCase：store.DATA_ROOT 与 log.LOG_FILE 同时被重定向到临时目录，
绝不在真实 data/ 上跑。
"""
import hashlib
import json
import os
import shutil
import threading
from datetime import datetime
from unittest import mock

import llm
import log
import resumelib
import store

from tests.base import IsolatedCase


def _stale_side_name(snap_path, raw):
    """旁挂留档的**基本**名字（`<快照路径>.stale-<旧字节 sha 前 8 位>`，与 `resumelib` 同口径）。"""
    return "%s.stale-%s" % (snap_path, hashlib.sha256(raw).hexdigest()[:8])


def _events_text():
    """隔离根里的事件日志全文（`IsolatedCase` 把 `log.LOG_FILE` 也重定向进临时目录）。"""
    try:
        with open(log.LOG_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


class CreateVersionTest(IsolatedCase):
    def test_first_version_is_its_own_root_and_mother_is_absent(self):
        rec = resumelib.create_version("母版", "第一版内容")
        self.assertEqual(rec["rev"], 1)
        self.assertIsNone(rec["parent_id"])
        self.assertEqual(rec["root_id"], rec["id"])
        self.assertIsNone(resumelib.mother_of(rec["root_id"]))
        self.assertIsNone(rec["mother_file"])            # 没有子版 → 不写快照

    def test_duplicate_name_in_live_versions_is_rejected(self):
        resumelib.create_version("母版", "内容A")
        with self.assertRaises(ValueError):
            resumelib.create_version("母版", "内容B")     # 绝不静默覆盖别人的文件

    def test_continue_makes_a_child_and_snapshots_the_mother_before_overwriting(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        self.assertEqual((v2["rev"], v2["parent_id"], v2["root_id"]), (2, v1["id"], v1["id"]))
        again = resumelib.by_id(v1["id"])
        self.assertTrue(again["mother_file"])                                    # 快照已留
        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual((text, kind), ("第一版内容", "mother"))                  # **逐字**是覆盖前的内容

    def test_the_mother_snapshot_is_never_rewritten(self):
        v1 = resumelib.create_version("母版", "第一版内容"); resumelib.choose(v1["id"])
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = resumelib.by_id(v1["id"])["mother_file"]
        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])
        self.assertEqual(store.read_text(store.p_path(snap)), "第一版内容")       # 仍是第一版

    def test_read_version_of_a_forgotten_revision_reports_missing_not_empty(self):
        # 真实的「母→子→孙」三版：v2 既不是最新(v3)也不是母版(v1) → 它的原文没被保留
        v1 = resumelib.create_version("母版", "第一版内容"); resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        v3 = resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])
        text_latest, _, kind_latest = resumelib.read_version(v3["id"])
        self.assertEqual((text_latest, kind_latest), ("第三版内容", "latest"))
        text_mother, _, kind_mother = resumelib.read_version(v1["id"])
        self.assertEqual((text_mother, kind_mother), ("第一版内容", "mother"))
        text_mid, path_mid, kind_mid = resumelib.read_version(v2["id"])       # ← 中间那一版
        # 原文没保留 → text 为空是**事实**；调用方必须靠 kind 分辨，
        # **绝不把空串当成功**（界面据此显示"旧版原文未保留"）。
        self.assertEqual(kind_mid, "missing")
        self.assertEqual(text_mid, "")


class ChooseRemovePurgeTest(IsolatedCase):
    def test_choose_is_unique_within_a_lineage(self):
        v1 = resumelib.create_version("母版", "A"); v2 = resumelib.create_version("母版", "B", parent_id=v1["id"])
        resumelib.choose(v1["id"]); resumelib.choose(v2["id"])
        self.assertFalse(resumelib.by_id(v1["id"])["chosen"])                     # 同族谱只能一个
        self.assertTrue(resumelib.by_id(v2["id"])["chosen"])

    def test_remove_keeps_every_file_on_disk(self):
        v1 = resumelib.create_version("母版", "A"); resumelib.choose(v1["id"])
        resumelib.create_version("母版", "B", parent_id=v1["id"])
        resumelib.remove(v1["id"])
        self.assertTrue(os.path.exists(store.p_path("resumes/母版.md")))           # 最新版文件还在
        self.assertTrue(os.path.exists(store.p_path(resumelib.by_id(v1["id"])["mother_file"])))  # 快照还在

    def test_purge_also_clears_this_versions_own_stale_archives(self):
        """M-5：彻底删某一版时，**它自己的** ``.stale-*`` 留档旁挂也一并清掉（别人的一律不碰）。

        旁挂是"留档"（装的是即将被覆盖的旧快照字节），会累积、可手工删。
        既然这一版的快照都被彻底删了，它自己的旧副本留着只会让目录越积越多 —— 但**只清
        这一版 id 前缀**的那些：别人的快照 / 别人的旁挂必须逐字未变（反假绿）。
        """
        v1 = resumelib.create_version("甲", "A1")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("甲", "A2", parent_id=v1["id"])
        room = store.p_path("resumes", resumelib.SNAPSHOT_DIR)
        snap = os.path.join(room, v1["id"] + ".md")
        mine = os.path.join(room, "%s.md.stale-00000000" % v1["id"])
        mine_bytes = "v1 的旧快照副本".encode("utf-8")
        store.write_bytes(mine, mine_bytes)
        other = resumelib.create_version("乙", "B1")
        resumelib.choose(other["id"])
        resumelib.create_version("乙", "B2", parent_id=other["id"])
        theirs = os.path.join(room, "%s.md.stale-00000000" % other["id"])
        theirs_bytes = "乙的旧快照副本".encode("utf-8")
        store.write_bytes(theirs, theirs_bytes)

        resumelib.purge(v1["id"])

        self.assertFalse(os.path.exists(mine), "这一版自己的留档旁挂要跟着清掉")
        self.assertFalse(os.path.exists(snap), "自己的快照照旧删掉")
        self.assertTrue(os.path.exists(theirs), "别人的留档旁挂一个字节都不许动")
        self.assertEqual(store.read_bytes(theirs), theirs_bytes)
        self.assertTrue(os.path.exists(store.p_path("resumes", "甲.md")),
                        "族谱主文件是「最新版容器」，purge 不删它（老口径不变）")
        self.assertEqual(resumelib.by_id(v2["id"])["name"], "甲")   # v2 还在（只删了 v1）

    def test_purge_only_clears_side_names_that_really_look_like_its_own_archives(self):
        """E-5（Task 1.10c）：`purge` 清旁挂必须按**精确形状**认名字，不许按前缀一刀切。

        机器写的旁挂只有一种形状：`<版本id>.md.stale-` + **8 位十六进制**（重名时再加 `-N`）。
        旧实现只判 `f.startswith("<版本id>.md.stale-")`：手工把别的快照命名成
        `<版本id>.md.stale-手工说明`（或别的程序恰好用了这个前缀）时，`purge` 会把**不是留档的东西**
        一起删掉 —— 那是别人的字节，本函数对自己承诺过"别人的一律不碰"。

        变异取证：把形状判断放宽回"前缀匹配"，下面两条 `assertTrue` 立刻红。
        """
        v1 = resumelib.create_version("甲", "A1")
        room = store.p_path("resumes", resumelib.SNAPSHOT_DIR)
        os.makedirs(room, exist_ok=True)
        own_real = os.path.join(room, "%s.md.stale-0a1b2c3d" % v1["id"])
        own_numbered = os.path.join(room, "%s.md.stale-0a1b2c3d-2" % v1["id"])
        handmade = os.path.join(room, "%s.md.stale-手工说明" % v1["id"])
        other_shape = os.path.join(room, "%s.md.stale-0a1b2c3dZZ" % v1["id"])
        for p in (own_real, own_numbered, handmade, other_shape):
            store.write_bytes(p, ("内容：%s" % os.path.basename(p)).encode("utf-8"))
        handmade_bytes = store.read_bytes(handmade)
        other_shape_bytes = store.read_bytes(other_shape)

        resumelib.purge(v1["id"])

        self.assertFalse(os.path.exists(own_real), "这一版自己的留档旁挂要跟着清掉（8 位十六进制那支）")
        self.assertFalse(os.path.exists(own_numbered), "重名换号的那支（`-2`）也是留档，要一起清掉")
        self.assertTrue(os.path.exists(handmade),
                        "名字不是留档形状（`.stale-手工说明`）→ 不是机器留的档，绝不许删：%r"
                        % (sorted(os.listdir(room)),))
        self.assertEqual(store.read_bytes(handmade), handmade_bytes, "那些字节不是留档，一个字节都不许动")
        self.assertTrue(os.path.exists(other_shape),
                        "`.stale-<8位>ZZ` 不是留档形状（多了尾巴）→ 不许删")
        self.assertEqual(store.read_bytes(other_shape), other_shape_bytes)

    def test_purge_removes_only_its_own_snapshot(self):
        v1 = resumelib.create_version("甲", "A1"); resumelib.choose(v1["id"])
        resumelib.create_version("甲", "A2", parent_id=v1["id"])
        other = resumelib.create_version("乙", "B1")
        before = store.read_text(store.p_path("resumes/乙.md"))
        resumelib.purge(v1["id"])
        self.assertIsNone(resumelib.by_id(v1["id"]))
        self.assertEqual(store.read_text(store.p_path("resumes/乙.md")), before)   # 别人的文件逐字未变
        self.assertIsNotNone(resumelib.by_id(other["id"]))


# ---------------- 以下为补充（计划的测试之外，针对同一批硬规则） ----------------

class SnapshotTriggerTest(IsolatedCase):
    """快照的**唯一**触发条件与"不可变"：这两条写歪了，就是"以为留了档其实没留"。"""

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def test_snapshot_bytes_are_byte_identical_to_the_overwritten_content(self):
        v1 = resumelib.create_version("母版", "第一行\n第二行 中文"); resumelib.choose(v1["id"])
        resumelib.create_version("母版", "新内容", parent_id=v1["id"])
        with open(self._snap_path(v1["id"]), "rb") as f:
            self.assertEqual(f.read(), "第一行\n第二行 中文".encode("utf-8"))      # 逐字节，不做 CRLF 转换

    def test_no_snapshot_for_a_child_without_a_mother(self):
        v1 = resumelib.create_version("母版", "第一版内容")                        # 没有 choose
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        self.assertIsNone(resumelib.by_id(v1["id"])["mother_file"])              # 没有母版 → 不留快照
        self.assertEqual(v2["rev"], 2)
        self.assertEqual(resumelib.list_versions()[0]["mother_file"], None)

    def test_existing_snapshot_is_not_rewritten_even_when_the_trigger_holds(self):
        v1 = resumelib.create_version("母版", "第一版内容"); resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        old = 1000000000                      # 2001-09-09：把 mtime 钉在过去，重写必然改变它
        os.utime(snap, (old, old))
        before = os.stat(snap).st_mtime_ns
        recorded = resumelib.by_id(v1["id"])["mother_file"]
        # 手工把文件改回母版内容 → 此刻「母版内容 == 即将被覆盖的内容」成立、且快照文件**已存在**
        store.write_text(store.p_path("resumes", "母版.md"), "第一版内容")
        v3 = resumelib.create_version("母版", "第四版内容", parent_id=v1["id"])
        self.assertEqual(v3["rev"], 3)
        self.assertEqual(os.stat(snap).st_mtime_ns, before)                      # 一个字节都没重写
        with open(snap, "rb") as f:
            self.assertEqual(f.read(), "第一版内容".encode("utf-8"))
        self.assertEqual(resumelib.by_id(v1["id"])["mother_file"], recorded)     # 记录也没被动过

    def test_snapshot_is_written_before_the_file_is_overwritten(self):
        """母版是「当前最新版」时延续：快照必须等于**覆盖前**那份，而不是覆盖后那份。"""
        v1 = resumelib.create_version("母版", "覆盖前"); resumelib.choose(v1["id"])
        resumelib.create_version("母版", "覆盖后", parent_id=v1["id"])
        text, _path, kind = resumelib.read_version(v1["id"])
        self.assertEqual((text, kind), ("覆盖前", "mother"))                      # 不是"覆盖后"

    def test_mother_that_is_also_latest_reads_the_latest_file(self):
        v1 = resumelib.create_version("独苗", "唯一内容"); resumelib.choose(v1["id"])
        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(text, "唯一内容")
        self.assertEqual(kind, "latest")                                         # 同时是最新版 → 读最新版文件
        self.assertEqual(path, store.p_path("resumes", "独苗.md"))
        self.assertFalse(os.path.exists(self._snap_path(v1["id"])))              # 不写"一件事两处存"

    def test_missing_version_returns_no_path_so_a_careless_caller_cannot_serve_the_wrong_file(self):
        v1 = resumelib.create_version("母版", "第一版内容"); resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])
        text, path, kind = resumelib.read_version(v2["id"])
        self.assertEqual((text, path, kind), ("", "", "missing"))


class DocAndBoundaryTest(IsolatedCase):
    def test_missing_file_yields_a_complete_empty_doc(self):
        self.assertEqual(resumelib.load_resumes(), {"version": 1, "resumes": []})

    def test_garbage_file_yields_a_complete_empty_doc(self):
        store.write_text(store.p_path(resumelib.RESUMES_FILE), "{ 这不是 json")
        self.assertEqual(resumelib.load_resumes(), {"version": 1, "resumes": []})

    def test_doc_without_a_resumes_list_yields_a_complete_empty_doc(self):
        store.write_json(store.p_path(resumelib.RESUMES_FILE), {"version": 1})
        self.assertEqual(resumelib.load_resumes(), {"version": 1, "resumes": []})

    def test_record_fingerprint_describes_the_file_on_disk(self):
        content = "第一行\n第二行 中文"
        rec = resumelib.create_version("指纹", content)
        path = store.p_path("resumes", "指纹.md")
        self.assertEqual(rec["size"], os.path.getsize(path))                     # 真实磁盘字节数
        self.assertEqual(rec["size"], len(content.encode("utf-8")))              # 换行不做 CRLF 转换
        self.assertEqual(rec["sha256"], resumelib.sha256_of(store.read_text(path)))
        self.assertEqual(rec["at"], datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(rec["file"], "resumes/指纹.md")
        self.assertEqual(rec["ai_diff"], {})
        self.assertEqual(rec["ai_diff_error"], "")
        self.assertEqual(rec["note"], "")
        self.assertFalse(rec["chosen"])
        self.assertFalse(rec[resumelib.REMOVED])

    def test_name_is_sanitized_so_it_cannot_escape_the_resumes_dir(self):
        rec = resumelib.create_version("../../坏名字", "x")
        self.assertEqual(rec["name"], "坏名字")
        self.assertTrue(os.path.exists(store.p_path("resumes", "坏名字.md")))
        self.assertFalse(os.path.exists(os.path.join(store.profile_dir(), "坏名字.md")))

    def test_empty_name_is_rejected(self):
        for bad in ("", "   ", "..", ".", None):
            with self.assertRaises(ValueError):
                resumelib.create_version(bad, "x")

    def test_continuing_a_removed_lineage_is_rejected(self):
        v1 = resumelib.create_version("母版", "A")
        resumelib.remove(v1["id"])
        with self.assertRaises(ValueError):
            resumelib.create_version("母版", "B", parent_id=v1["id"])

    def test_continuation_cannot_change_the_name(self):
        v1 = resumelib.create_version("母版", "A"); resumelib.choose(v1["id"])
        with self.assertRaises(ValueError):
            resumelib.create_version("另一个名字", "B", parent_id=v1["id"])

    def test_remove_is_idempotent_and_restore_puts_it_back_in_the_live_list(self):
        v1 = resumelib.create_version("甲", "A")
        resumelib.remove(v1["id"]); resumelib.remove(v1["id"])
        self.assertEqual(resumelib.list_versions(), [])
        self.assertEqual(len(resumelib.list_versions(include_removed=True)), 1)
        resumelib.restore(v1["id"])
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]])
        self.assertIsNotNone(resumelib.current_of(v1["root_id"]))

    def test_restore_rejects_a_name_taken_by_another_live_resume(self):
        v1 = resumelib.create_version("甲", "A")
        resumelib.remove(v1["id"])
        resumelib.create_version("甲", "B")            # 移出后名字空出来了，另开一份同名的
        with self.assertRaises(ValueError):
            resumelib.restore(v1["id"])                # 名字被占用 → 要求先改名

    def test_unknown_ids_are_reported_loudly(self):
        self.assertIsNone(resumelib.by_id("rv_9999"))
        self.assertIsNone(resumelib.current_of("rv_9999"))
        self.assertIsNone(resumelib.mother_of("rv_9999"))
        for call in (resumelib.read_version, resumelib.choose,
                     resumelib.remove, resumelib.restore, resumelib.purge):
            with self.assertRaises(ValueError):
                call("rv_9999")

    def test_purge_keeps_other_lineages_snapshots_byte_identical(self):
        a1 = resumelib.create_version("甲", "A1"); resumelib.choose(a1["id"])
        resumelib.create_version("甲", "A2", parent_id=a1["id"])
        b1 = resumelib.create_version("乙", "B1"); resumelib.choose(b1["id"])
        resumelib.create_version("乙", "B2", parent_id=b1["id"])
        snap_b = store.p_path("resumes", resumelib.SNAPSHOT_DIR, b1["id"] + ".md")
        with open(snap_b, "rb") as f:
            before = f.read()
        resumelib.purge(a1["id"])
        self.assertTrue(os.path.exists(snap_b))
        with open(snap_b, "rb") as f:
            self.assertEqual(f.read(), before)        # 别人那一族谱的快照逐字未变

    def test_library_writes_only_resumes_json_and_files_under_resumes(self):
        v1 = resumelib.create_version("母版", "A"); resumelib.choose(v1["id"])
        resumelib.create_version("母版", "B", parent_id=v1["id"])
        resumelib.remove(v1["id"]); resumelib.restore(v1["id"])
        for other in ("jobs.json", "jds.json", "knowledge.json"):
            self.assertFalse(os.path.exists(store.p_path(other)), other)         # 绝不碰别人的数据
        got = set()
        for root, _dirs, names in os.walk(store.profile_dir()):
            for n in names:
                rel = os.path.relpath(os.path.join(root, n), store.profile_dir())
                got.add(rel.replace("\\", "/"))
        self.assertEqual(got, {"resumes.json", "resumes/母版.md", "resumes/快照/rv_0001.md"})


class ConcurrencyTest(IsolatedCase):
    def test_concurrent_creates_do_not_lose_records(self):
        """读→改→写必须整体持锁：否则并发新建互相把记录覆盖掉（当事人看不出丢档）。"""
        errors = []

        def work(i):
            try:
                resumelib.create_version("简历%d" % i, "内容%d" % i)
            except Exception as e:                # noqa: BLE001 —— 收集起来一起断言
                errors.append(repr(e))

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        recs = resumelib.list_versions()
        self.assertEqual(len(recs), 8)
        self.assertEqual(sorted(r["name"] for r in recs), sorted("简历%d" % i for i in range(8)))
        self.assertEqual(len({r["id"] for r in recs}), 8)


class MismatchTest(IsolatedCase):
    """内容与记录不符必须如实报 ``mismatch``：绝不把"磁盘上的东西不是这一版"说成 latest/mother。

    这些用例覆盖三条实际会发生的路径：
    ① 文件被**程序外**改过；② ``purge`` 留下孤儿文件后被同名简历覆盖；③ 快照被改。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _write_outside_resumelib(self, rel_path, text):
        """模拟"程序外"改文件：绕开 resumelib，只用裸 open（字节与内容严格一致）。"""
        with open(store.p_path(rel_path), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def test_a_tampered_live_file_is_reported_as_mismatch_not_latest(self):
        v1 = resumelib.create_version("母版", "原始内容")
        # 程序外改文件（绕开 resumelib）
        self._write_outside_resumelib("resumes/母版.md", "别人手改的内容")
        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch")                      # 不许谎报 latest
        self.assertEqual(text, "别人手改的内容")                 # 内容照常给，便于对账
        self.assertEqual(path, store.p_path("resumes", "母版.md"))
        self.assertNotEqual(resumelib.sha256_of(text), v1["sha256"])   # 证据

    def test_a_tampered_mother_snapshot_is_also_mismatch(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        self.assertEqual(v2["rev"], 2)
        snap = resumelib.by_id(v1["id"])["mother_file"]
        self.assertEqual(snap, "resumes/%s/%s.md" % (resumelib.SNAPSHOT_DIR, v1["id"]))
        self._write_outside_resumelib(snap, "快照被改了")
        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch")                      # 快照不可变 → 对不上就是出事了
        self.assertEqual(text, "快照被改了")
        self.assertEqual(path, self._snap_path(v1["id"]))
        self.assertNotEqual(resumelib.sha256_of(text), v1["sha256"])

    def test_an_untampered_snapshot_still_reports_mother(self):
        """反例对照：没被动过就必须还是 mother（别把 mismatch 撒得到处都是）。"""
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        text, _path, kind = resumelib.read_version(v1["id"])
        self.assertEqual((text, kind), ("第一版内容", "mother"))

    def test_a_record_without_a_sha256_field_is_not_falsely_flagged_as_mismatch(self):
        """人工把 resumes.json 里的 ``sha256`` 删掉 → 没有比对依据，不许凭空喊 mismatch。"""
        v1 = resumelib.create_version("母版", "原始内容")
        doc = resumelib.load_resumes()
        doc["resumes"][0].pop("sha256")
        resumelib.save_resumes(doc)
        self.assertIsNone(resumelib.by_id(v1["id"]).get("sha256"))
        text, _path, kind = resumelib.read_version(v1["id"])
        self.assertEqual((text, kind), ("原始内容", "latest"))     # 读得到、也说得清"没法核"

    def test_the_purge_orphan_scenario_no_longer_silently_returns_a_stranger(self):
        """**真实可达**的"别人的简历"路径（用公开接口走通，不靠手工改盘）：

        ① 甲族谱 a1(母) → a2(母) → a3(最新，其内容成了 ``<名字>.md`` 里的孤儿)；
        ② ``purge(a3)``：记录没了，孤儿文件留着；
        ③ ``remove(a2)``/``remove(a1)``：名字空出来（``read_version`` 仍可寻址它们）；
        ④ 另开同名新简历（乙）→ **覆盖**那个孤儿文件；
        ⑤ 读 a2 —— a2 在甲族谱里 rev 最高，``_owner_in`` 认定"``<名字>.md`` 里是它"，于是旧代码
           把**乙的全新简历**当成 a2 的原文、以 ``kind="latest"`` 静默返回。

        ⚠️ 修 3 之后这一步走**快照**（a2 自己是母版、快照一直在）：拿回的是 a2 的原文，
        ``kind="mother"``。要钉住的不变量没变 —— **绝不把乙的内容当成 a2 的原文**。
        """
        a1 = resumelib.create_version("同名", "甲的第一版")
        resumelib.choose(a1["id"])
        a2 = resumelib.create_version("同名", "甲的第二版", parent_id=a1["id"])
        resumelib.choose(a2["id"])
        a3 = resumelib.create_version("同名", "甲的第三版", parent_id=a2["id"])
        resumelib.purge(a3["id"])                                # 最新版记录没了 → 孤儿文件 = "甲的第三版"
        self.assertEqual(store.read_text(store.p_path("resumes", "同名.md")), "甲的第三版")
        resumelib.remove(a2["id"])                               # 甲族谱里 rev 最高的残留版本
        resumelib.remove(a1["id"])                               # 名字空出来（哪怕母版还留着一份快照）
        stranger = resumelib.create_version("同名", "乙的全新简历")   # 覆盖孤儿文件
        self.assertEqual(stranger["root_id"], stranger["id"])
        text, path, kind = resumelib.read_version(a2["id"])
        self.assertEqual(kind, "mother")                         # ← 旧代码这里是 "latest"（静默给别人的内容）
        self.assertEqual(text, "甲的第二版")                      # ← **是 a2 自己的原文**，不是乙的
        self.assertEqual(path, self._snap_path(a2["id"]))         # 从快照取（不可变的那份）
        self.assertNotEqual(text, "乙的全新简历")
        self.assertEqual(resumelib.sha256_of(text), a2["sha256"])              # 逐字就是 a2 那一份
        self.assertNotEqual(resumelib.sha256_of(text), stranger["sha256"])     # 与乙的记录不符（证据）

    def test_a_live_version_whose_file_was_orphaned_by_purge_reads_its_mother_snapshot(self):
        """**全是未移出记录**的孤儿路径（界面真会读到的那种）：

        purge 掉最新版 a3 后，``<名字>.md`` 里留下的是 a3 的内容；此时 a2 既是母版又是
        "文件主人"，旧代码会以 ``kind="latest"`` 把 **a3 的内容**当成 a2 的原文返回 ——
        记录里的 sha 明明对不上。修 3 之后**先看快照**：a2 的原文完好地在它自己的快照里，
        于是如实回 ``mother`` + a2 的原文（而不是拿被彻底删掉的那一版来冒充）。
        """
        a1 = resumelib.create_version("同名", "甲的第一版")
        resumelib.choose(a1["id"])
        a2 = resumelib.create_version("同名", "甲的第二版", parent_id=a1["id"])
        resumelib.choose(a2["id"])
        a3 = resumelib.create_version("同名", "甲的第三版", parent_id=a2["id"])
        resumelib.purge(a3["id"])
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [a1["id"], a2["id"]])   # 全是活的
        # 事实：`<名字>.md` 里确实还是 a3 的内容（孤儿文件）
        self.assertEqual(store.read_text(store.p_path("resumes", "同名.md")), "甲的第三版")
        text, path, kind = resumelib.read_version(a2["id"])
        self.assertEqual(kind, "mother")                          # ← 旧代码这里是 "latest"
        self.assertEqual(text, "甲的第二版")                       # ← 逐字是 a2 的原文，不是 a3 的
        self.assertEqual(path, self._snap_path(a2["id"]))
        self.assertEqual(resumelib.sha256_of(text), resumelib.by_id(a2["id"])["sha256"])   # 对得上
        # a2 的原文没丢：就在它自己的快照里（记录也如实写了 mother_file）
        self.assertEqual(store.read_text(self._snap_path(a2["id"])), "甲的第二版")
        self.assertEqual(resumelib.by_id(a2["id"])["mother_file"],
                         "resumes/%s/%s.md" % (resumelib.SNAPSHOT_DIR, a2["id"]))


class SnapshotCertaintyAfterCleanupTest(IsolatedCase):
    """清理掉那半条**恒真**的空守卫之后的行为对照（Task 2 的第 4 项）。

    被删掉的是 `create_version` 里 `m_text == store.read_text(file_path, "")` 这半条：
    它把**同一个文件读两遍**，恒真，且让「必须确认母版原文」这句真话的责任分散在两处。
    确认的责任现在**只在** `_read_in` / `_kind_on_disk`（也就是 `m_kind`）：

    ① **正常延续仍然写快照**（清理不许把功能删没）——逐字等于覆盖前那份；
    ② 母版原文**核对不通过**（`mismatch`）时：盘上那份字节**照样存**（Task 1.8 的口径：
       那是用户写的东西，覆盖掉就没地方找了），但**绝不记 `mother_file`** ——
       不声称它是母版原文，`read_version` 仍如实报 `mismatch`。**存档 ≠ 冒充**。
       ② 是 `m_kind in ("latest", "mother")` 这道闸的反向对照：
       把它改成恒真（变异⑤）→ ②必红。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _write_outside_resumelib(self, rel_path, text):
        """模拟"程序外"改文件：绕开 resumelib，只用裸 open。"""
        with open(store.p_path(rel_path), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def test_a_normal_continuation_still_writes_the_mother_snapshot(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = resumelib.by_id(v1["id"])["mother_file"]
        self.assertEqual(snap, "resumes/%s/%s.md" % (resumelib.SNAPSHOT_DIR, v1["id"]))
        with open(self._snap_path(v1["id"]), "rb") as f:
            self.assertEqual(f.read(), "第一版内容".encode("utf-8"))

    def test_a_tampered_mother_is_archived_without_claiming_to_be_the_mother(self):
        """口径（Task 1.8）：被程序外改过的内容**存字节但不冒充**母版原文。

        老断言（"不确定就一个快照都不写"）已被新口径推翻：不写就等于把用户盘上那份东西
        直接丢掉，而"不冒充"这件事**只需要不记 `mother_file`** —— 记录才是那句话的载体。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        self._write_outside_resumelib("resumes/母版.md", "别人手改的内容")
        self.assertEqual(resumelib.read_version(v1["id"])[2], "mismatch")     # 前提：确认失败了
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        with open(self._snap_path(v1["id"]), "rb") as f:
            self.assertEqual(f.read(), "别人手改的内容".encode("utf-8"),
                             "覆盖前必须先把盘上那份字节存下来")
        self.assertIsNone(resumelib.by_id(v1["id"])["mother_file"])           # 记录没谎报留了档
        self.assertEqual(resumelib.read_version(v1["id"])[2], "mismatch")     # 也没有冒充成母版原文

    def test_a_mother_whose_text_was_never_kept_is_not_snapshotted_as_an_empty_file(self):
        """`missing` 是"存字节"那条口径**唯一的例外**：盘上装的是**别人那一版**的东西。

        这一版的原文从来没保留过（`read_version` 如实报 `missing`）。此时若把
        ``<名字>.md`` 里那份字节写进它的快照，`read_version` 就会改口报 `mismatch`
        并把**别人那一版的内容**当成这一版的"存档"亮给用户 —— 张冠李戴。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        resumelib.create_version("母版", "第三版内容", parent_id=v2["id"])
        resumelib.choose(v2["id"])                       # 母版 = v2（历史版，原文没保留）
        self.assertEqual(resumelib.read_version(v2["id"])[2], "missing")
        resumelib.create_version("母版", "第四版内容", parent_id=v1["id"])
        self.assertFalse(os.path.exists(self._snap_path(v2["id"])),
                         "把「别人那一版」的内容记到这一版名下（missing 会被说成 mismatch）")
        self.assertIsNone(resumelib.by_id(v2["id"])["mother_file"])
        self.assertEqual(resumelib.read_version(v2["id"])[2], "missing")   # 仍如实说"没保留"


class MotherSnapshotPrecedenceTest(IsolatedCase):
    """修 3：**有母版快照时优先从快照取**（快照不可变，正是为"最新版被彻底删掉"留的）。

    探针（修之前）：``v1(chosen) + v2 → purge(v2) → read_version(v1)`` 回 ``mismatch``、
    ``text`` 是 **v2 的内容**，而 v1 的原文一直躺在 ``resumes/快照/rv_0001.md`` 里、一个字节
    都没动 —— 界面据此对用户说「可能被程序外修改过」，是**假话**。根因是判定顺序：先认
    「文件主人」（purge 后退到 v1）→ 内容与记录 sha 不符 → mismatch，**根本没看快照**。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def test_after_purging_the_newer_version_the_mother_comes_back_from_its_snapshot(self):
        v1 = resumelib.create_version("母版", "v1 的原文\n第二行")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "v2 的内容", parent_id=v1["id"])
        resumelib.purge(v2["id"])
        # 前提（两个"事实"都摆出来）：最新版容器里是 v2 的内容，快照里是 v1 的原文
        self.assertEqual(store.read_text(store.p_path("resumes", "母版.md")), "v2 的内容")
        self.assertEqual(store.read_text(self._snap_path(v1["id"])), "v1 的原文\n第二行")

        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mother")                        # ← 修之前是 "mismatch"
        self.assertEqual(text, "v1 的原文\n第二行")              # ← 逐字；修之前是 v2 的内容
        self.assertEqual(path, self._snap_path(v1["id"]))
        self.assertNotEqual(text, "v2 的内容")

    def test_a_tampered_snapshot_is_still_reported_as_mismatch(self):
        """快照也不可用时（被程序外改过）仍必须如实 ``mismatch``：改了顺序，sha 核对一步没少。"""
        v1 = resumelib.create_version("母版", "v1 的原文")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "v2 的内容", parent_id=v1["id"])
        resumelib.purge(v2["id"])
        with open(self._snap_path(v1["id"]), "w", encoding="utf-8", newline="") as f:
            f.write("别人手改的快照")
        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch")
        self.assertEqual(text, "别人手改的快照")
        self.assertEqual(path, self._snap_path(v1["id"]))


PART_NAME = "周汉钦-A"
MOTHER_TEXT = ("候选人简历正文（母版）\n"
               "项目经历：语音质检平台\n"
               "技能：Python / 数据标注")
CHILD_TEXT = ("候选人简历正文（母版）\n"
              "项目经历：语音质检平台；新增语音Agent项目\n"
              "技能：Python / 数据标注")


class AiDiffTest(IsolatedCase):
    """Task 4 / 规格 §4.4：AI 变更比对**只在延续时触发**，且走 `llm.chat(prompt, cfg)` 两参约定。

    注入点与 `interview.analyze` / `outcome.attribute` 完全相同：替身**实现** `llm.call_openai_compatible`
    （模块属性在调用时现取，不是 import 时绑定）—— 所以这里 patch 一次就够，
    **绝不真调模型**、也不需要给 `resumelib` 开一条只在测试里存在的参数。
    """

    def _fake_chat(self, reply):
        prompts = []

        def fake(base_url, api_key, model, messages, json_mode=False, **kw):
            prompts.append(messages[0]["content"])
            return reply

        self._patch(llm, "call_openai_compatible", fake)
        return prompts

    def _patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _lineage(self):
        v1 = resumelib.create_version(PART_NAME, MOTHER_TEXT)
        resumelib.choose(v1["id"])
        return v1

    def test_a_continuation_calls_the_model_once_with_both_versions_verbatim(self):
        v1 = self._lineage()
        prompts = self._fake_chat(json.dumps({
            "summary": "把语音Agent项目补进了项目经历",
            "changes": [{"where": "项目经历", "what": "新增语音Agent项目"},
                        {"where": "自我评价", "what": "强调数据标注经验"}]},
            ensure_ascii=False))
        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        resumelib.ai_diff_of(v2["id"])

        self.assertEqual(len(prompts), 1, "延续应当**恰好**调用一次模型：%r" % (prompts,))
        prompt = prompts[0]
        # 逐字断言"输入确实是母版内容 + 子版内容"（两段内容都出现在同一条提示词里）
        self.assertIn(MOTHER_TEXT, prompt, "提示词里没有母版内容")
        self.assertIn(CHILD_TEXT, prompt, "提示词里没有子版内容")
        self.assertLess(prompt.index(MOTHER_TEXT), prompt.index(CHILD_TEXT),
                        "母版与子版的先后顺序反了（模型会读反「这一版改了什么」）")

        rec = resumelib.by_id(v2["id"])
        self.assertEqual(rec["ai_diff_error"], "")
        self.assertEqual(rec["ai_diff"], {
            "summary": "把语音Agent项目补进了项目经历",
            "changes": [{"where": "项目经历", "what": "新增语音Agent项目"},
                        {"where": "自我评价", "what": "强调数据标注经验"}]})
        # 永久可查：重新读库仍是这一份（不是只活在内存里）
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"]["summary"],
                         "把语音Agent项目补进了项目经历")

    def test_a_crashing_model_still_saves_the_version_and_never_writes_an_empty_fake_diff(self):
        v1 = self._lineage()
        prompts = self._fake_chat(None)

        def boom(base_url, api_key, model, messages, json_mode=False, **kw):
            prompts.append(messages[0]["content"])
            raise RuntimeError("模型接口返回 429：余额不足（注入）")

        self._patch(llm, "call_openai_compatible", boom)
        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        rec = resumelib.ai_diff_of(v2["id"])

        # ① 版本照样在（比对失败不是致命：规格 §8.4）
        self.assertEqual(resumelib.read_version(v2["id"])[0], CHILD_TEXT)
        self.assertIn(v2["id"], [r["id"] for r in resumelib.list_versions()])
        # ② 原因非空、且说得出是什么错
        self.assertIn("429", rec["ai_diff_error"])
        # ③ **绝不写一个空的假 ai_diff**：空对象/空壳都算"假装成功"
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"], {},
                         "比对失败却写了一个空的假 ai_diff")
        self.assertEqual(store.read_json(store.p_path("resumes.json"))["resumes"][-1]["ai_diff"], {})

    def test_a_broken_json_reply_is_not_retried_and_not_faked(self):
        v1 = self._lineage()
        prompts = self._fake_chat("这不是 JSON，模型随口说的话")
        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        rec = resumelib.ai_diff_of(v2["id"])

        self.assertEqual(len(prompts), 1, "坏 JSON **不重试**（版本已保存，比对失败不是致命）")
        self.assertEqual(resumelib.read_version(v2["id"])[0], CHILD_TEXT)   # 版本仍在
        self.assertIn("JSON", rec["ai_diff_error"])
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"], {})

    def test_a_new_independent_resume_never_calls_the_model(self):
        prompts = self._fake_chat('{"summary":"不该被调用","changes":[]}')
        resumelib.create_version(PART_NAME, MOTHER_TEXT)          # 新开一份独立的
        self.assertEqual(prompts, [], "新开一份独立的**没有可比对的对象**，一次 chat 都不该调")
        # 记录里也不该有假的变更说明
        rec = resumelib.list_versions()[0]
        self.assertEqual((rec["ai_diff"], rec["ai_diff_error"]), ({}, ""))

    def test_a_mother_whose_text_was_not_kept_is_an_honest_error_not_an_empty_call(self):
        """母版原文没保留 → **明确报错**（不发两条内容去让模型瞎猜），也不写假 ai_diff。"""
        v1 = resumelib.create_version(PART_NAME, MOTHER_TEXT)
        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        resumelib.create_version(PART_NAME, "第三版内容", parent_id=v2["id"])
        resumelib.choose(v2["id"])                                 # 母版 = v2（历史版，原文没保留）
        prompts = self._fake_chat('{"summary":"瞎写","changes":[]}')
        v4 = resumelib.create_version(PART_NAME, "第四版内容", parent_id=v1["id"])
        rec = resumelib.ai_diff_of(v4["id"])

        self.assertEqual(prompts, [], "母版原文都没有，却还是调了模型（只能靠猜）")
        self.assertIn("旧版原文未保留", rec["ai_diff_error"])
        self.assertEqual(resumelib.by_id(v4["id"])["ai_diff"], {})

    def test_the_model_reply_is_cleaned_field_by_field(self):
        """模型返回的形状不保证：空的 since（where）丢掉、非字符串压成字符串、缺的补空。"""
        clean = resumelib.normalize_ai_diff({
            "summary": "  加了两个项目  ",
            "changes": [{"where": " 项目经历 ", "what": " 新增语音Agent "},
                        {"where": "", "what": "没说是哪儿改的"},     # 缺 where → 不算一条
                        {"what": "只有 where 的空字符串"},
                        "根本不是对象",
                        {"where": "技能", "what": 123}],
        })
        self.assertEqual(clean["summary"], "加了两个项目")
        self.assertEqual(clean["changes"], [{"where": "项目经历", "what": "新增语音Agent"},
                                            {"where": "技能", "what": "123"}])
        self.assertEqual(resumelib.normalize_ai_diff({"changes": "不是数组"}),
                         {"summary": "", "changes": []})

    def test_a_reply_that_is_already_a_dict_is_used_as_is(self):
        v1 = self._lineage()

        def fake(base_url, api_key, model, messages, json_mode=False, **kw):
            return {"summary": "直接给了对象", "changes": [{"where": "教育经历", "what": "补了学校"}]}

        self._patch(llm, "call_openai_compatible", fake)
        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        resumelib.ai_diff_of(v2["id"])
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"],
                         {"summary": "直接给了对象",
                          "changes": [{"where": "教育经历", "what": "补了学校"}]})

    def test_an_injected_two_argument_chat_is_used_as_is(self):
        """注入约定就是 **`chat(prompt, cfg)` 两参**（与 interview.analyze / outcome.attribute 一致）。

        这条用**两参可调用对象**走一遍：既钉住"能注入"，也钉住"提示词里同时有母版与子版"。
        """
        v1 = self._lineage()
        seen = []

        def chat(prompt, cfg):
            seen.append(prompt)
            return '{"summary":"补了项目","changes":[{"where":"项目经历","what":"新增语音Agent项目"}]}'

        v2 = resumelib.create_version(PART_NAME, CHILD_TEXT, parent_id=v1["id"])
        resumelib.ai_diff_of(v2["id"], chat=chat, cfg={"text_model": {"model": "假模型"}})

        self.assertEqual(len(seen), 1)
        self.assertIn(MOTHER_TEXT, seen[0])
        self.assertIn(CHILD_TEXT, seen[0])
        self.assertEqual(resumelib.by_id(v2["id"])["ai_diff"],
                         {"summary": "补了项目",
                          "changes": [{"where": "项目经历", "what": "新增语音Agent项目"}]})


class SnapshotByteFidelityTest(IsolatedCase):
    """快照必须**逐字节**等于被覆盖掉的那份原文。

    洞在读侧：`_read_in` 走 `store.read_text` —— 非 UTF-8 解码失败被兜成空串（写出 0 字节"假存档"），
    CRLF 被归一成 LF（快照与原文不再逐字节相等）。`_write_content` 反而是保真的（`newline=""`）。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _drop_sha(self, version_id):
        """抹掉记录里的 sha256（模拟"老记录 / 人工改坏"），让 `_kind_on_disk` 只能维持 ok_kind。"""
        doc = resumelib.load_resumes()
        for r in doc.get("resumes", []):
            if r.get("id") == version_id:
                r.pop("sha256", None)
        resumelib.save_resumes(doc)

    def test_a_non_utf8_mother_without_a_fingerprint_is_archived_byte_faithfully(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        raw = "我的简历：十个项目经验".encode("gbk")
        with open(store.p_path("resumes", "母版.md"), "wb") as f:
            f.write(raw)
        self._drop_sha(v1["id"])

        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        snap = self._snap_path(v1["id"])
        self.assertTrue(os.path.exists(snap), "母版原文必须先被存档再覆盖")
        with open(snap, "rb") as f:
            self.assertEqual(f.read(), raw, "快照必须逐字节等于原文（老实现写出 0 字节假存档）")
        self.assertNotEqual(os.path.getsize(snap), 0, "绝不许留一个 0 字节的假存档")

        # 读侧的另一半（Task 1.9 / I-2）：快照**有字节却读不出来**（非 UTF-8）时，
        # 绝不能说成"存过档、内容是空的" —— 那份空串会被界面当成一份空简历显示出来。
        # 这正是"文件读不出来被当成文件是空的"，只是搬到了快照的读侧。
        _text, _p, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch",
                         "快照读不出来时不许说'存过档、内容是空的'（那会让界面显示空简历）")

    def test_a_crlf_mother_is_archived_byte_faithfully(self):
        v1 = resumelib.create_version("母版", "第一行\n第二行")
        resumelib.choose(v1["id"])
        raw = "第一行\r\n第二行".encode("utf-8")
        with open(store.p_path("resumes", "母版.md"), "wb") as f:
            f.write(raw)

        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        with open(self._snap_path(v1["id"]), "rb") as f:
            self.assertEqual(f.read(), raw, "CRLF 的原文也必须逐字节进快照（老实现被归一成 LF）")

    def test_a_mismatched_mother_is_still_archived_but_not_claimed_as_the_mother(self):
        """核对不通过时：**字节照样留着**（不丢用户写的东西），但**不假装**它是母版原文。

        `read_version` 会如实报 `mismatch` —— **存档 ≠ 冒充**。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        raw = "被别的工具改过的内容".encode("gbk")
        with open(store.p_path("resumes", "母版.md"), "wb") as f:
            f.write(raw)                       # sha256 保持不动 → 必然对不上

        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        snap = self._snap_path(v1["id"])
        self.assertTrue(os.path.exists(snap), "覆盖前必须先把盘上那份字节存下来")
        with open(snap, "rb") as f:
            self.assertEqual(f.read(), raw)
        _text, _path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch", "不许把对不上的内容冒充成母版原文")
        self.assertIsNone(resumelib.by_id(v1["id"]).get("mother_file"),
                          "核对不通过时不许记 mother_file")

    def test_an_emptied_mother_file_never_becomes_a_zero_byte_fake_archive(self):
        """**"有字节才存档"那条闸的钉子**（`if raw:` 被改成恒真时必须变红）。

        盘上那份母版文件被别的东西清成 0 字节时：**没有字节可存** —— 那就一个快照文件都不该有。
        否则 `resumes/快照/<id>.md` 会是一个 0 字节文件，而 `read_version` 里"有快照就走快照"
        的顺序会让这一版从此被当成"存过档"，可档里什么都没有 —— 又一个"以为留了档其实没留"。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        with open(store.p_path("resumes", "母版.md"), "wb") as f:
            f.write(b"")                        # 程序外被清空（sha256 保持不动）

        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        self.assertFalse(os.path.exists(self._snap_path(v1["id"])),
                         "0 字节不是「存档」：不许留下一个空的假快照")
        self.assertIsNone(resumelib.by_id(v1["id"]).get("mother_file"))
        # 没有快照时，v1 已经不是 `<名字>.md` 的主人了（v2 才是）→ 如实报 missing。
        # 若留下一份 0 字节假快照，"有快照先读快照"那条顺序会让它变成 mismatch ——
        # 拿一个空档冒充「存过」，正是要钉死的事。
        self.assertEqual(resumelib.read_version(v1["id"])[2], "missing")

    def test_a_provably_stale_snapshot_is_repaired_when_the_true_mother_returns(self):
        """坏字节占位后，若真正的母版内容回到了盘上，快照必须能被修正。

        不加这条例外 → 盘上与记录一致了、`read_version` 还一直喊 `mismatch`（假红），
        真原文也再也补不进去（复查实测）。
        """
        v1 = resumelib.create_version("母版", "原文内容")
        resumelib.choose(v1["id"])
        with open(store.p_path("resumes", "母版.md"), "w", encoding="utf-8", newline="") as f:
            f.write("被改坏的内容")
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        self.assertIsNone(resumelib.by_id(v1["id"])["mother_file"])       # 没冒充（口径不变）
        resumelib.purge(v2["id"])
        with open(store.p_path("resumes", "母版.md"), "w", encoding="utf-8", newline="") as f:
            f.write("原文内容")
        self.assertEqual(resumelib.read_version(v1["id"])[2], "latest")   # 前提：盘上就是它
        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])
        with open(self._snap_path(v1["id"]), "rb") as f:
            self.assertEqual(f.read(), "原文内容".encode("utf-8"), "真原文必须能补存进快照")
        # 修正后的快照**就是**母版原文 → 这条记录可以（也应当）重新记上存档位置。
        self.assertEqual(resumelib.by_id(v1["id"])["mother_file"],
                         "resumes/%s/%s.md" % (resumelib.SNAPSHOT_DIR, v1["id"]))
        self.assertEqual(resumelib.read_version(v1["id"])[2], "mother")

    def test_a_stale_snapshot_is_never_rewritten_without_a_fingerprint(self):
        """记录**没有** `sha256` 时，"快照过时"这件事**没有依据** → 一个字节都不许动。

        变异的钉子：把 `stale` 判定里的 `bool(expected)` 去掉，本测试必须变红
        （那时只要"快照文本的 sha != 空串"就成立 = 恒真，于是空有形状就重写快照）。
        宁可不动，也绝不凭猜测改写一份自称"不可变的参照原点"。
        """
        v1 = resumelib.create_version("母版", "原文内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        self.assertTrue(os.path.exists(snap))
        old = 1000000000                      # 2001-09-09：把 mtime 钉在过去，重写必然改变它
        os.utime(snap, (old, old))
        before = os.stat(snap).st_mtime_ns

        self._drop_sha(v1["id"])              # 人工改坏 / 老记录：没有内容指纹
        # 造出"看起来过时"的形状：盘上那份与快照不同（但记录里没有 sha 可以证明谁对）
        store.write_text(store.p_path("resumes", "母版.md"), "程序外改过的内容")

        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertEqual(os.stat(snap).st_mtime_ns, before,
                         "没有 sha256 就没有依据 → 快照一个字节都不许重写")
        with open(snap, "rb") as f:
            self.assertEqual(f.read(), "原文内容".encode("utf-8"))

    def test_an_unreadable_mother_file_aborts_the_save_instead_of_overwriting_it(self):
        """母版原文**读不出来**（被别的程序锁住 / 权限）→ 中止本次保存，绝不覆盖它。

        方向更安全（宁可这次不保存，也绝不把用户那份东西冲掉），但必须**说清楚**：
        不兜住 `OSError` 的话它一路穿到 `app.py` 变成裸 500（用户只看到「服务器内部错误」）。
        """
        v1 = resumelib.create_version("母版", "原文内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        real_read_bytes = store.read_bytes

        def fake_read_bytes(p):
            if os.path.abspath(str(p)) == os.path.abspath(path):
                raise PermissionError(13, "拒绝访问")
            return real_read_bytes(p)

        with mock.patch.object(store, "read_bytes", side_effect=fake_read_bytes):
            with self.assertRaises(RuntimeError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        self.assertIn("留档", str(ctx.exception))                       # 说人话，不是裸异常文本
        self.assertEqual(store.read_text(path), "原文内容", "读不出来就不覆盖：绝不能把那份冲掉")
        self.assertFalse(os.path.exists(self._snap_path(v1["id"])), "没留成档就别留半个快照")
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "这次保存整体中止：不许留下一条没有内容的版本记录")

    def test_a_repaired_stale_snapshot_keeps_the_bytes_it_overwrites_aside(self):
        """修正一份"按记录判断过时"的快照之前，旧快照的字节必须先留档（Task 1.10 第 2 条）。

        复查实测的**不可逆**路径：把母版记录里的 ``sha256`` 改成**盘上那份**（记录被人工改坏），
        `read_version` 于是报 `latest`（在记录看来盘上那份就是这一版）→ 续一次就把**真快照**
        覆盖成盘上那份、还记上 `mother_file`。真快照的字节从此**永久消失**。
        "按记录判断"的证明力全在记录里：记录坏了，判断就错 —— 所以覆盖前必须留档。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        with open(snap, "rb") as f:
            true_mother_bytes = f.read()
        self.assertEqual(true_mother_bytes, "第一版内容".encode("utf-8"))
        resumelib.purge(v2["id"])            # 记录没了 → 文件主人退回 v1（盘上仍是第二版内容）
        disk = store.p_path("resumes", "母版.md")
        doc = resumelib.load_resumes()       # 人工把记录的 sha256 改成盘上那份（复查实测那一步）
        for r in doc["resumes"]:
            if r.get("id") == v1["id"]:
                r["sha256"] = resumelib.sha256_of(store.read_text(disk))
        resumelib.save_resumes(doc)
        self.assertEqual(resumelib.read_version(v1["id"])[2], "latest")   # 前提（在记录看来）

        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        side = [f for f in os.listdir(os.path.dirname(snap))
                if f.startswith(v1["id"] + ".md.stale-")]
        self.assertEqual(len(side), 1,
                         "旧快照被覆盖前必须先留档（**直接写一份副本**到 <id>.md.stale-<8位>）：%r" % (side,))
        with open(os.path.join(os.path.dirname(snap), side[0]), "rb") as f:
            self.assertEqual(f.read(), true_mother_bytes, "真快照的字节不许静默丢掉")
        # 修正本身照旧生效（Task 1.9 的功能不能因为留档就没了）
        with open(snap, "rb") as f:
            self.assertEqual(f.read(), "第二版内容".encode("utf-8"))

    def test_neither_the_snapshot_nor_the_live_file_is_this_version_keeps_the_snapshot_untouched(self):
        """`m_kind in ("latest", "mother")` 这道闸（Task 1.10 第 7 条）。

        **两边都不是这一版时，绝不拿盘上那份去改写参照原点**。
        变异取证：把这半个条件去掉，`stale` 会在"快照被改坏 **且** 盘上那份也不是这一版"时
        也为真 → **子版/别人的内容被写进快照**（复查实测过）。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        with open(snap, "w", encoding="utf-8", newline="") as f:
            f.write("快照被改坏")                      # 快照不是这一版
        with open(store.p_path("resumes", "母版.md"), "w", encoding="utf-8", newline="") as f:
            f.write("盘上那份也不是这一版")              # 盘上那份也不是
        self.assertEqual(resumelib.read_version(v1["id"])[2], "mismatch")     # 前提
        old = 1000000000                     # 把 mtime 钉在过去：重写必然改变它
        os.utime(snap, (old, old))
        before_mtime, before_bytes = os.stat(snap).st_mtime_ns, store.read_bytes(snap)

        v3 = resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertEqual(v3["rev"], 3)                     # 续版照常发生
        self.assertEqual(os.stat(snap).st_mtime_ns, before_mtime, "快照一个字节都不许动")
        self.assertEqual(store.read_bytes(snap), before_bytes)
        self.assertEqual(store.read_bytes(snap), "快照被改坏".encode("utf-8"))

    def test_a_snapshot_write_failure_is_a_human_sentence_and_leaves_no_half_written_file(self):
        """E-1（Task 1.10c）：写母版快照必须**原子**（临时文件 + `os.replace`），失败翻成人话。

        旧写法是裸的 `store.write_bytes(snap_path, raw)`（= `open(path, "wb")`）：它**先把旧快照
        截断成 0 字节**再一段段往里写 —— 中途失败（磁盘满 / 被 kill）就留下一个**半截快照**，
        而记录仍指着它（"以为留了档，取回来却是残缺的"）；而且裸 `OSError` 穿到
        `app.py` 的 `/api/resumes` 就是 **500**（用户只看到「服务器内部错误」）。

        这里把 `os.replace` 打坏（模拟换名那一步失败）：
        - 原子写 → 放弃这次保存、抛**专用异常**（一句中文人话，路由接成 409），盘上**没有半截快照**；
        - 变异取证：把原子写改回"直接写"（不碰 `os.replace`）→ 本用例的 `assertRaises` 立刻红。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        snap = self._snap_path(v1["id"])

        with mock.patch.object(resumelib.os, "replace", side_effect=OSError(28, "磁盘空间不足")):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        sentence = str(ctx.exception)
        self.assertIn("快照", sentence)          # 说清楚是哪一步失败（不是裸 Errno 文本）
        self.assertIn("写", sentence)
        self.assertFalse(os.path.exists(snap), "写不成的快照不许留半截（更不许留个 0 字节的假档）")
        room = os.path.dirname(snap)
        leftovers = [f for f in (os.listdir(room) if os.path.isdir(room) else [])
                     if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "写失败后不许在快照目录里留临时文件垃圾：%r" % (leftovers,))
        self.assertEqual(store.read_text(store.p_path("resumes", "母版.md")), "第一版内容",
                         "这次保存整体中止：母版原文一个字节都不许被写坏")
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "这次保存整体中止：不许留下一条没有正文的版本记录")


class AtomicContentWriteTest(IsolatedCase):
    """P0-13：**正文写入必须原子**，失败要翻成人话，且不许留下孤儿快照。

    旧写法 `_write_content` = `open(path, "w")`（**截断后写**）：中途失败（磁盘满 / 进程被 kill /
    文件名超长 `WinError 123`）就留下**半截简历**；而**族谱第一家**时没有快照兜底 ——
    那份半截东西就是用户唯一的一份。而且裸 `OSError` 穿到路由 = **500**（用户只看到
    「服务器内部错误」），对"没写成、这次保存已中止"什么都没说清。

    这里按重要性钉四件事：①正文一个字节都不许被写坏；②不许登记新版本（"记录了新版而正文是旧的"
    比半截更坏：那是另一句用户可见的假话）；③失败时**撤掉这次新建的快照**（一次保存要么整体
    成立、要么整体不成立）；④撤不掉时要**照实说**（日志 + 那句人话里明写"没有登记"）。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _inject_content_write_failure(self, path):
        """只让**正文**那一跳失败（磁盘满），快照那一跳照常成功 —— 这正是"孤儿快照"的成因。

        原子写的最后一步是 `os.replace(tmp, 目标)`：把**目标路径**上的那一次打成 `OSError`。
        """
        real_replace = os.replace

        def fake_replace(src, dst):
            if os.path.abspath(str(dst)) == os.path.abspath(path):
                raise OSError(28, "磁盘空间不足")
            return real_replace(src, dst)

        return mock.patch.object(resumelib.os, "replace", side_effect=fake_replace)

    def test_a_content_write_failure_keeps_the_old_bytes_and_withdraws_the_fresh_snapshot(self):
        """需求一：正文写失败 → 旧正文逐字节不变 + 没有新记录 + **撤掉刚写的快照**。"""
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        before = store.read_bytes(path)
        snap = self._snap_path(v1["id"])

        with self._inject_content_write_failure(path):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        sentence = str(ctx.exception)
        self.assertIn("简历", sentence)          # 说清是哪一步失败
        self.assertIn("保存", sentence)          # 一句人话，不是裸 Errno 文本
        self.assertEqual(store.read_bytes(path), before,
                         "①`resumes/<名字>.md` 逐字节仍是旧内容（绝不留半截简历）")
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "②这次保存整体中止：不许登记新版本（记录了新版而正文是旧的 = 更坏的一句假话）")
        self.assertFalse(os.path.exists(snap),
                         "③失败时把这次新建的快照撤掉：盘上不许留一个没人引用的孤儿快照")
        room = os.path.dirname(snap)
        leftovers = [f for f in (os.listdir(room) if os.path.isdir(room) else [])
                     if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "失败后不许留临时文件垃圾：%r" % (leftovers,))

    def test_an_overlong_name_is_a_human_sentence_and_writes_nothing(self):
        """需求二：300 字符的名字（写盘抛 `OSError`）必须变成**一句人话**，不是裸 500。

        诚实边界：文件名的组件上限是 255（Windows 上原子写的最后一步 `os.replace` 报
        `WinError 123`，旧写法 `open(path, "w")` 报 `[Errno 22]`，Linux 上报 `ENAMETOOLONG`）
        ⇒ **超长名字的目标文件根本建不出来**，所以这一条里
        **没有**（也不可能有）"旧正文"可被截断；"真有旧正文时一个字节都不动"由
        `test_a_content_write_failure_keeps_the_old_bytes_and_withdraws_the_fresh_snapshot`
        与既有的两条钉子（`test_an_unreadable_mother_file_...` / `test_a_snapshot_write_failure_...`）覆盖。
        这里钉的是：**不许把裸 `OSError` 抛给路由**（那是 500），并且不许留下
        半截文件 / 临时文件垃圾 / 版本记录，也**不许波及别的简历**。
        """
        other = resumelib.create_version("别人", "别人那份内容\r\n第二行")
        other_path = store.p_path("resumes", "别人.md")
        other_bytes = store.read_bytes(other_path)
        long_name = "长" * 300

        with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
            resumelib.create_version(long_name, "超长名字的正文")

        sentence = str(ctx.exception)
        self.assertIn("简历", sentence)
        self.assertIn("保存", sentence)
        self.assertNotIsInstance(ctx.exception, OSError, "裸 OSError 穿到路由就是 500")
        self.assertFalse(os.path.exists(store.p_path("resumes", long_name + ".md")),
                         "写不成的正文连文件都不该建出来（更不许留半截）")
        room = store.p_path("resumes")
        leftovers = [f for f in os.listdir(room) if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "失败后不许在简历目录里留临时文件垃圾：%r" % (leftovers,))
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [other["id"]],
                         "这次保存整体中止：不许留下一条没有正文的版本记录")
        self.assertEqual(store.read_bytes(other_path), other_bytes,
                         "这次失败不许波及别的简历：别人那份正文逐字节未变")

    def test_a_successful_write_lands_the_exact_bytes(self):
        """正面钉：写成功时目标内容与传入的 `raw` **逐字节相同**（含 CRLF / 非 ASCII）。"""
        content = "第一行\r\n第二行\n第三行\t结尾带表情🙂"
        rec = resumelib.create_version("保真", content)

        self.assertEqual(store.read_bytes(store.p_path("resumes", "保真.md")),
                         content.encode("utf-8"),
                         "原子写不许改变字节（CRLF 不许被归一成 LF、非 ASCII 不许被换编码）")
        self.assertEqual(rec["size"], len(content.encode("utf-8")), "size 就是磁盘字节数")

    def test_the_old_bytes_are_still_there_at_the_moment_of_the_swap(self):
        """原子性的**直接**钉子：换名那一刻，目标文件里**仍是**旧正文；目标**绝不许被就地写**。

        非原子的"截断后写"（`open(path, "w")`）在那一刻目标**已经被截断**了；"往目标就地拷贝"
        的写法（`shutil.copy` / `copyfile`）则在那一刻目标正被**一段段覆盖** ——
        两种都是"中途失败 = 半截简历"的窗口。所以这里钉**两条**：
        ① 全程**没有任何"往目标文件本身拷贝"**（`shutil.copyfile` 的目的地是目标 → 红）；
        ② 目标路径上的换名发生时，目标里仍是旧正文（截断后写的实现根本不会有这个时刻）。

        变异取证（都实测过）：
          - 改回 `open(path, "w")` → **②红**（换名那一刻根本不存在）；
          - 改成 `shutil.move(tmp, 目标)` → **②红**，而 **①不红** —— 因为临时文件与目标
            **同目录**（这是原子写的前提），同盘 move 内部走的就是 `os.rename`（仍然是一次改名），
            "跨盘退化成 copy"那条分支在这里永远走不到。**这条要如实说**：`shutil.move` 这个变异
            对"非原子"本身**没有**直接反证力，红的只是"原子换名这一步还在不在"；
          - 改成 `shutil.copyfile(tmp, 目标)`（**另做的一次变异**，只为验①有没有牙）→ **①红**
            （报出"往目标就地拷贝"的目的地路径）。也就是说：① 只在"直接往目标拷贝"的写法上才有牙。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        before = store.read_bytes(path)
        real_replace = os.replace
        real_copyfile = shutil.copyfile
        seen, in_place_copies = [], []

        def spy_replace(src, dst):
            if os.path.abspath(str(dst)) == os.path.abspath(path):
                seen.append(store.read_bytes(path))     # 换名前一刻，目标里是什么？
            return real_replace(src, dst)

        def spy_copyfile(src, dst, *a, **k):
            # 目的地就是**目标文件本身** = 就地覆盖（copy 写一半失败 → 半截正文）
            in_place_copies.append(str(dst))
            return real_copyfile(src, dst, *a, **k)

        with mock.patch.object(resumelib.os, "replace", side_effect=spy_replace), \
                mock.patch.object(shutil, "copyfile", side_effect=spy_copyfile):
            resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        self.assertEqual([c for c in in_place_copies
                          if os.path.abspath(str(c)) == os.path.abspath(path)], [],
                         "①目标只能靠**同目录换名**上场，绝不许被就地拷贝/截断写：%r" % (in_place_copies,))
        self.assertEqual(seen, [before],
                         "②换名前一刻目标里必须**仍是旧正文**（不是 0 字节、也不是半截）")
        self.assertEqual(store.read_bytes(path), "第二版内容".encode("utf-8"))

    def test_an_unremovable_fresh_snapshot_is_reported_honestly(self):
        """③ 的诚实兜底：撤不掉刚写的快照时**照实说**，绝不假装撤掉了。

        撤掉失败不是"静默留着"：① 一句日志（`resume.snapshot_orphan_left`）
        ② 那句给用户看的话里明写"没有登记"（盘上那份是什么、为什么还在）。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        snap = self._snap_path(v1["id"])
        real_remove = os.remove

        def fake_remove(p):
            if os.path.abspath(str(p)) == os.path.abspath(snap):
                raise PermissionError(13, "拒绝访问")
            return real_remove(p)

        with self._inject_content_write_failure(path), \
                mock.patch.object(resumelib.os, "remove", side_effect=fake_remove):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        sentence = str(ctx.exception)
        self.assertIn("快照", sentence)
        self.assertIn("没有登记", sentence, "撤不掉就必须如实说盘上留了一份没登记的快照")
        self.assertTrue(os.path.exists(snap), "撤不掉就是撤不掉（这里如实承认它还在盘上）")
        self.assertIn("resume.snapshot_orphan_left", _events_text(),
                      "撤不掉这件事必须留下日志（不许静默）")
        self.assertEqual(store.read_bytes(path), "第一版内容".encode("utf-8"),
                         "就算快照撤不掉，正文也绝不许被写坏")


class LedgerWriteFailureTest(IsolatedCase):
    """台账（``resumes.json``）写失败 = **半成功**：新正文已落在盘上、版本记录里却没有它。

    收口前实测的形态：``create_version`` 走到最后一步 ``save_resumes`` 抛**裸 ``OSError``**
    → 路由（``app.py`` 的 ``POST /api/resumes``）接不住它 → **500**，而盘上多了一份
    "记录里没有"的正文。比 500 更坏的是它给"第二句假话"留了余地：用户以为这次保存没发生，
    盘上却已经有新内容；**延续**时连 ``/api/resumes`` 的 ``unregistered`` 面板都看不见它
    （文件名就是已登记的那个），而界面上"最新版"取回来的还是旧的（旧正文在快照里）。

    收口口径（**选 A：回滚正文**，理由写在 ``create_version`` 的 docstring 里）：
    ① 这次保存**整体不成立** —— 正文收回到写之前（族谱第一家就把刚写下的那份撤掉）；
    ② 失败翻成**既有**专用异常（一句人话，``app.py:2258`` 接成 409，不是 500）；
    ③ 万一收不回来就**照实说**（日志 + 那句话里明写"没有登记"），并且**这次新建的**快照
    一定要留下（那些旧字节只剩这一份了）。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    @staticmethod
    def _inject_ledger_failure():
        """只让**台账**那一跳失败（``store.write_json`` 是 ``save_resumes`` 的落盘点）。"""
        return mock.patch.object(store, "write_json", side_effect=OSError(28, "磁盘空间不足"))

    def test_a_failed_ledger_write_rolls_the_content_back_and_says_so(self):
        """延续时台账写失败 → 正文逐字节回到写之前 + 没有新记录 + 撤掉这次新建的快照 + 人话。"""
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        before = store.read_bytes(path)
        snap = self._snap_path(v1["id"])
        self.assertFalse(os.path.exists(snap), "前置条件：这次延续才会新建母版快照")

        with self._inject_ledger_failure():
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        sentence = str(ctx.exception)
        self.assertNotIsInstance(ctx.exception, OSError, "裸 OSError 穿到路由就是 500")
        self.assertIn("版本记录", sentence)            # 说清是哪一步失败
        self.assertIn("保存", sentence)
        self.assertIn("收回", sentence)                # 并且说清盘面现在是什么样
        self.assertEqual(store.read_bytes(path), before,
                         "正文必须回到写之前：绝不留「正文在盘上、版本记录里没有」的半成功状态")
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "这次保存整体不成立：不许留下一条没有正文的版本记录")
        self.assertFalse(os.path.exists(snap),
                         "这次新建的快照也要撤掉：记录里没有它，盘上就是一份没人引用的孤儿档")
        self.assertIn("resume.ledger_write_failed", _events_text(),
                      "必须留下一句日志说清台账没写成（不许静默）")

    def test_the_first_version_of_a_lineage_is_removed_again_when_the_ledger_write_fails(self):
        """族谱第一家：此前**没有**这个文件 → 收口就是撤掉刚写下的那一份（不留孤儿正文）。"""
        with self._inject_ledger_failure():
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("独苗", "正文内容")

        sentence = str(ctx.exception)
        self.assertNotIsInstance(ctx.exception, OSError, "裸 OSError 穿到路由就是 500")
        self.assertIn("版本记录", sentence)
        self.assertIn("收回", sentence)
        self.assertFalse(os.path.exists(store.p_path("resumes", "独苗.md")),
                         "刚写下的正文要撤掉（连「未登记」列表里都不该出现它）")
        self.assertEqual(resumelib.list_versions(), [], "没有新记录")
        room = store.p_path("resumes")
        self.assertEqual([f for f in os.listdir(room) if f.endswith(".md")], [],
                         "盘上不许留下这次写下的任何 .md（%r）" % (os.listdir(room),))
        self.assertIn("resume.ledger_write_failed", _events_text())

    def test_an_unrestorable_content_is_reported_honestly_and_the_snapshot_is_kept(self):
        """收不回来（连"把旧正文写回去"也炸）→ **照实说**，且那份新建的快照**绝不能撤**。

        为什么不能撤：它里面是**覆盖前盘上那份旧字节** —— 正文收不回来的这一刻，那些字节
        只剩这一份了。撤掉才是真丢数据（这是"字节至少要有一处可达"那条底线）。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        path = store.p_path("resumes", "母版.md")
        before = store.read_bytes(path)
        snap = self._snap_path(v1["id"])
        real_replace = os.replace
        state = {"ledger_failed": False}

        def flaky_write_json(_path, _obj):
            state["ledger_failed"] = True
            raise OSError(28, "磁盘空间不足")

        def fake_replace(src, dst):
            if state["ledger_failed"] and os.path.abspath(str(dst)) == os.path.abspath(path):
                raise OSError(28, "磁盘空间不足")     # 台账炸了之后，把旧正文写回去这一步也炸
            return real_replace(src, dst)

        with mock.patch.object(store, "write_json", side_effect=flaky_write_json), \
                mock.patch.object(resumelib.os, "replace", side_effect=fake_replace):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])

        sentence = str(ctx.exception)
        self.assertIn("版本记录", sentence)
        self.assertIn("没有登记", sentence, "收不回来就必须如实说：盘上多了一份没登记的新正文")
        self.assertEqual(store.read_bytes(path), "第二版内容".encode("utf-8"), "收不回来就是收不回来")
        self.assertTrue(os.path.exists(snap),
                        "正文没收回时那份新建的快照绝不能撤 —— 覆盖前的旧字节只剩它这一份")
        self.assertEqual(store.read_bytes(snap), before, "那份旧字节逐字节还在")
        self.assertIn("resume.ledger_rollback_failed", _events_text(),
                      "收不回来这件事必须留下日志（不许静默）")


class SnapshotArchiveFailureTest(IsolatedCase):
    """Task 1.10b 修 I-1：**留档失败 → 放弃本次修正**（绝不静默把旧快照盖掉，那是不可逆的）。

    下面三条各自钉住一个写法，但**它们的身份不一样**（上一轮这里写的是"都在旧实现上实测为红"，
    其中第③条在旧实现上**本来就是绿的** —— 那句话是假的，这里如实说清）：
    ① 出任何岔子都"继续覆盖" → 旧快照的字节**在整棵树里再也搜不到**（**旧实现的回归钉子**）；
    ② 旁挂同名就跳过留档（只判存在、不判内容）→ 真快照一样被盖掉（**旧实现的回归钉子**）；
    ③ 同名且**内容相同**时不去重复写一份 —— 这条锁的是**新契约**（"已留过档"要看**内容**，
       不是看名字在不在）；旧实现恰好也满足它，所以它在旧实现上**不是红的**，
       价值在于将来有人把这条改坏时会立刻变红。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _broken_fingerprint(self):
        """造出"按记录判断快照已过时、而盘上那份恰好就是这一版"的形状。

        与 ``test_a_repaired_stale_snapshot_keeps_the_bytes_it_overwrites_aside`` 同一套动作：
        人工把母版记录的 ``sha256`` 改成**盘上那份**（记录被改坏）→ ``read_version`` 报 ``latest``
        → 下一次延续就会走进"修正旧快照"那条路（也正是要先留档的那条路）。
        """
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])   # → 留下 v1 的快照
        resumelib.purge(resumelib.list_versions()[1]["id"])                  # 记录没了 → 主人退回 v1
        disk = store.p_path("resumes", "母版.md")
        doc = resumelib.load_resumes()
        for r in doc["resumes"]:
            if r.get("id") == v1["id"]:
                r["sha256"] = resumelib.sha256_of(store.read_text(disk))
        resumelib.save_resumes(doc)
        self.assertEqual(resumelib.read_version(v1["id"])[2], "latest", "前置条件：在记录看来盘上就是 v1")
        return resumelib.by_id(v1["id"]), self._snap_path(v1["id"])

    def test_a_failed_archive_aborts_the_repair_and_leaves_the_old_snapshot_byte_identical(self):
        """① 留档失败（写旁挂那一步抛 `OSError`）→ **放弃本次修正**：旧快照一个字节都没变。

        ⚠️ 注入点跟着**实现**走（需求二把旁挂改成原子写之后：留档那一跳是
        "同目录临时文件 + `os.replace`"）。**只换注入点，断言一个字都没动**；
        而且它反过来就是需求二的钉子：有人把旁挂改回**直写**（`store.write_bytes(side, old)`）时，
        这里根本没有对旁挂的 `os.replace` 可打 → 修正一路做完 → 下面的 `assertRaises` 立刻红。
        """
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        self.assertTrue(before, "前置条件：旧快照里有字节（不然没什么可丢的）")
        before_state = resumelib.read_version(v1["id"])     # (text, path, kind)，用来核对"与修正前一致"
        side = _stale_side_name(snap, before)
        real_replace = os.replace

        def fake_replace(src, dst):
            if os.path.abspath(str(dst)) == os.path.abspath(side):
                raise OSError(28, "磁盘空间不足")
            return real_replace(src, dst)

        with mock.patch.object(resumelib.os, "replace", side_effect=fake_replace):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertEqual(store.read_bytes(snap), before,
                         "留档失败却继续写 → 旧快照被永久盖掉（复查实测：整棵树里再也搜不到）")
        self.assertIn("留档", str(ctx.exception))            # 说人话（路由接成 409 给用户）
        self.assertEqual(resumelib.read_version(v1["id"]), before_state,
                         "本次修正已放弃 → 这一版的取回结果必须与修正前**逐字一致**")
        # 连版本记录都没多出来（整体中止，不是"半保存"）
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [v1["id"]],
                         "这次保存整体中止：不许留下一条没有正文的新版本")
        self.assertIn("不可逆", _events_text(),
                      "必须留下一句日志说清为什么宁可这次不修（见 resumelib 的 SnapshotArchiveError）")

    def test_a_name_collision_with_different_bytes_is_archived_under_a_new_name(self):
        """② 旁挂同名但**内容不同** → 换名留档（`-2`）：旧字节一定出现在某一支旁挂里。"""
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        side = _stale_side_name(snap, before)
        store.write_bytes(side, b"someone else's bytes")      # 重名文件（内容与旧快照不同）

        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertEqual(store.read_bytes(side), b"someone else's bytes", "别人的同名文件一个字节都不许动")
        room = os.path.dirname(snap)
        found = [f for f in os.listdir(room)
                 if f.startswith(v1["id"] + ".md.stale-") and store.read_bytes(os.path.join(room, f)) == before]
        self.assertTrue(found,
                        "重名就跳过留档 → 旧快照被盖掉且**整棵树里再也搜不到**（不可逆）。"
                        "旁挂目录里现有：%r" % (sorted(os.listdir(room)),))
        self.assertNotEqual(os.path.basename(found[0]), os.path.basename(side))
        self.assertEqual(store.read_bytes(snap), "第二版内容".encode("utf-8"), "修正本身照旧生效")

    def test_an_identical_side_file_counts_as_already_archived(self):
        """③ 旁挂同名且**内容相同** → 不重复留（只 1 个旁挂）。

        "已留过档"的判据是**内容**：同名就跳过（只判存在不判内容）是错的（见上一条），
        但内容相同还硬写第二份同样是错的（旁挂会无意义地翻倍）。
        """
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        side = _stale_side_name(snap, before)
        store.write_bytes(side, before)                      # 旧字节**就是**这一支旁挂里那份

        resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        room = os.path.dirname(snap)
        sides = sorted(f for f in os.listdir(room) if f.startswith(v1["id"] + ".md.stale-"))
        self.assertEqual(sides, [os.path.basename(side)],
                         "内容相同 = 已经留过档：不许再写第二份（旁挂只该有 %r）：%r"
                         % (os.path.basename(side), sides))
        self.assertEqual(store.read_bytes(side), before, "旧字节仍在（已经留过的那一份）")
        self.assertEqual(store.read_bytes(snap), "第二版内容".encode("utf-8"), "修正本身照旧生效")

    def test_an_unreadable_side_candidate_name_is_never_reused_and_the_repair_is_abandoned(self):
        """E-2（Task 1.10c）：旁挂名**存在却读不出来**时，绝不当它"空着"、更不去写它。

        `_archive_old_snapshot` 探测候选旁挂名用的是 `store.read_bytes(side)`：
        文件不存在 → `None`（名字空着，可以写）；**存在但读不出来** → 抛 `OSError`。
        旧实现在这条分支上的写法是 `existing = _UNREADABLE`（哨兵，既不是 None 也不等于旧字节）
        → 换下一个候选名；九个候选全读不出来就**放弃本次修正**。
        写成 `existing = None`（"读不出来"被当成"名字空着"）的后果：直接往一个**读不出来的文件上写**
        （权限/被锁时写失败 → 裸 `OSError`；写得进去就是把那份东西盖掉，而它本来可能是别人的留档）。

        这条分支以前没有测试（复查 MUT8 全绿）—— 这里补上：
        变异取证：把 `_UNREADABLE` 那一支改成"名字可用"（`existing = None`），
        下面的 `assertRaises` 立刻红（它会去写旁挂并一路把修正做完）。
        """
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        room = os.path.dirname(snap)
        real_read_bytes = store.read_bytes

        def fake_read_bytes(p):
            # 只打坏"探测旁挂名"的读取：那个名字存在、却读不出来
            if ".stale-" in str(p):
                raise PermissionError(13, "拒绝访问")
            return real_read_bytes(p)

        with mock.patch.object(store, "read_bytes", side_effect=fake_read_bytes):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertIn("留档", str(ctx.exception))          # 说人话（路由接成 409 给用户）
        self.assertEqual(real_read_bytes(snap), before,
                         "放弃修正：旧快照一个字节都没变")
        sides = [f for f in os.listdir(room) if ".stale-" in f]
        self.assertEqual(sides, [],
                         "读不出来的名字被当成「空着」了：往一个读不出来的文件上写了东西（%r）" % (sides,))

    def test_an_unreadable_old_snapshot_is_a_human_sentence_not_a_bare_oserror(self):
        """M-3：旧快照**读不出来**时也不许裸 `OSError` 穿出 `create_version`（否则路由只接 409 的那条路走不到）。"""
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        real_read_bytes = store.read_bytes

        def fake_read_bytes(p):
            if os.path.abspath(str(p)) == os.path.abspath(snap):
                raise PermissionError(13, "拒绝访问")
            return real_read_bytes(p)

        with mock.patch.object(store, "read_bytes", side_effect=fake_read_bytes):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertIn("留档", str(ctx.exception))
        self.assertEqual(store.read_bytes(snap), before, "读不出来就不修：旧快照一个字节都没变")

    def test_the_side_archive_goes_through_the_single_atomic_write_path(self):
        """需求二：留档旁挂也走**同一条原子写**（`_atomic_write_bytes`：同目录临时文件 + `os.replace`）。

        为什么不让它"直写"（``store.write_bytes(side, old)``）：
        ① 那样一来 ``_atomic_write_bytes`` 的 docstring 里那句"只有这一条落盘实现"就是**说的和
           做的不一样**（验收报告也照着写了一遍）；
        ② 直写半途失败会留下一个**半截的** ``.stale-<8位>`` 文件 —— 那个名字是**内容寻址**的
           （声称自己就是那份 sha 的字节），内容却只有一半，名与实不符。

        失败语义这里**正合适**（这是选 (A) 前要判断的那一条）：原子写抛的本来就是
        ``SnapshotArchiveError``（调用方据此放弃本次修正、路由接成 409），临时文件也在 ``finally``
        里清掉 —— 不会在快照目录里积垃圾、更不会在旁挂名上留下半截。

        两条断言在旧实现上**都红**：① 旁挂名**是**由 `os.replace` 换上去的（原子那一步真的发生了）；
        ② **没有**任何一次"直接往旁挂名上写"。
        """
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        side = _stale_side_name(snap, before)
        replaced, direct = [], []
        real_replace, real_write_bytes = os.replace, store.write_bytes

        def spy_replace(src, dst):
            if os.path.abspath(str(dst)) == os.path.abspath(side):
                replaced.append(str(src))
            return real_replace(src, dst)

        def spy_write_bytes(p, raw):
            if os.path.abspath(str(p)) == os.path.abspath(side):
                direct.append(str(p))
            return real_write_bytes(p, raw)

        with mock.patch.object(resumelib.os, "replace", side_effect=spy_replace), \
                mock.patch.object(store, "write_bytes", side_effect=spy_write_bytes):
            resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertEqual(direct, [],
                         "别往旁挂名上直写（半截失败就留下一个名字谎称内容 sha 的文件）：%r" % (direct,))
        self.assertEqual(len(replaced), 1,
                         "留档旁挂必须走原子写（同目录临时文件 + os.replace）换上去：%r" % (replaced,))
        self.assertEqual(store.read_bytes(side), before, "留档内容照旧是覆盖前那份旧字节")

    def test_a_torn_side_write_leaves_no_half_written_archive_behind(self):
        """原子写的直接好处：留档**写坏**时不会在快照目录里留下半截的 ``.stale-*``。

        注入的是"该落的那份字节写了一半就报 `OSError`"（磁盘满 / 被 kill 的形态）：
        - 原子写：那半截只可能落在**临时文件**里，`finally` 把它删掉 → 目录里没有旁挂、没有 `.tmp-`；
        - 直写（旧实现）：半截**就是**那个 ``.stale-<8位>`` 文件（名字还谎称它就是那份 sha 的字节）
          → 本用例的 `leftovers` 断言红。
        """
        v1, snap = self._broken_fingerprint()
        before = store.read_bytes(snap)
        room = os.path.dirname(snap)
        real_write_bytes = store.write_bytes

        def torn_write_bytes(p, raw):
            # 只打坏"这份旧快照字节的落盘"：新实现写的是同目录临时文件，旧实现写的就是旁挂名本身。
            if raw == before and os.path.abspath(os.path.dirname(str(p))) == os.path.abspath(room):
                with open(p, "wb") as f:
                    f.write(raw[: max(1, len(raw) // 2)])       # 半截
                raise OSError(28, "磁盘空间不足")
            return real_write_bytes(p, raw)

        with mock.patch.object(store, "write_bytes", side_effect=torn_write_bytes):
            with self.assertRaises(resumelib.SnapshotArchiveError) as ctx:
                resumelib.create_version("母版", "第三版内容", parent_id=v1["id"])

        self.assertIn("留档", str(ctx.exception))       # 说人话（路由接成 409 给用户）
        self.assertEqual(store.read_bytes(snap), before, "留档失败 → 放弃本次修正：旧快照一个字节都没变")
        leftovers = sorted(f for f in os.listdir(room) if ".stale-" in f or f.startswith(".tmp-"))
        self.assertEqual(leftovers, [],
                         "写坏的那半截不许留在盘上（它的名字会谎称自己就是那份 sha 的字节）：%r"
                         % (leftovers,))
        self.assertEqual(resumelib.read_version(v1["id"])[2], "latest",
                         "修正已放弃 → 取回结果与修正前一致")


class ReadPathFingerprintGateTest(IsolatedCase):
    """Task 1.10 第 1/9 条：**读路径的每一条出口都要过"没有 sha256 就不认盘上那份"这道闸**。

    旧实现（Task 1.9）只在"快照有字节却读不出来"那一支过闸：抹掉记录的 `sha256` 之后，
    `read_version` 会把 `<名字>.md` 里**别人的内容**当成"最新版原文"报出去（界面标题就写着
    「最新版原文」）；"文件主人"那一支更直接：文件**有字节却读不出来**时 `store.read_text`
    给空串，没有 sha 可核对时就被当成"这一版就是最新版、内容是空的"。
    两条都是同一个思维习惯：**"读不出来"被当成"文件是空的"**。

    反方向的边界（第 9 条）同样要钉住：0 字节 / 纯空白的快照是**合法**的历史残留
    （Task 1.8 之前的实现真能写出 0 字节快照），那时唯一诚实的答案是 `mother` + 空文本。
    """

    def _snap_path(self, version_id):
        return store.p_path("resumes", resumelib.SNAPSHOT_DIR, version_id + ".md")

    def _drop_sha(self, version_id):
        doc = resumelib.load_resumes()
        for r in doc.get("resumes", []):
            if r.get("id") == version_id:
                r.pop("sha256", None)
        resumelib.save_resumes(doc)

    def test_an_unreadable_snapshot_without_a_fingerprint_never_reports_the_live_file_as_this_version(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        v2 = resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        with open(snap, "wb") as f:
            f.write("母版原文（GBK）".encode("gbk"))     # 快照**有字节却读不出来**
        self._drop_sha(v1["id"])                        # 记录也没有指纹 → 没有任何依据
        resumelib.purge(v2["id"])                       # 文件主人退回 v1（盘上仍是第二版内容）

        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch",
                         "没有 sha256 时绝不能改口说「盘上那份是这一版的最新版原文」")
        self.assertEqual(text, "")
        self.assertNotEqual(text, "第二版内容", "把 <名字>.md 里别人的内容当成了这一版的正文")
        self.assertEqual(path, snap, "既然读不出来，就该指着快照（那份读不出来的东西）说话")

    def test_an_unreadable_live_file_without_a_fingerprint_is_a_mismatch_not_an_empty_latest(self):
        v1 = resumelib.create_version("独苗", "原始内容")
        path = store.p_path("resumes", "独苗.md")
        with open(path, "wb") as f:
            f.write("读不出来的字节".encode("gbk"))
        self._drop_sha(v1["id"])

        text, got_path, kind = resumelib.read_version(v1["id"])
        self.assertEqual(kind, "mismatch",
                         "文件**有字节却读不出来**，绝不能说成「最新版、内容是空的」")
        self.assertEqual(text, "")
        self.assertEqual(got_path, path, "内容读不出来也要指出是哪个文件出事")

    def test_a_deleted_live_file_without_a_fingerprint_is_a_mismatch_not_an_empty_latest(self):
        """**无 `sha256` + 盘上文件被删** → 必须 `mismatch`（Task 1.10b 修 I-2）。

        旧实现在"文件主人"这一支只问 `not text`：文件**被删**与文件**是空的**都被
        `store.read_text` 兜成同一个空串，于是记录里没有 `sha256` 时（`_kind_on_disk`
        无从核对）就报成 `latest` + 空文本，而 `path` 还指着**一个不存在的文件** ——
        界面（`web/index.html:2349-2353`）标题写「**最新版原文**」、正文空白。
        这正是铁律那条"读不出来被当成文件是空的"的最难看的一版：**东西都没了**却报成功。

        钉住三种情况的区别：① 文件**不存在** → `mismatch`（"磁盘上的东西不是它了"包括
        "东西都没了"）；② 文件存在且**有字节**读不出来 → `mismatch`（同上一条测试）；
        ③ 文件存在且**真的没有可丢的字节** → 照旧（记录有 sha 时由 `_kind_on_disk` 说话）。
        """
        v1 = resumelib.create_version("独苗", "原始内容")
        path = store.p_path("resumes", "独苗.md")
        self._drop_sha(v1["id"])                 # 记录里没有指纹 → 没有任何可比对的依据
        os.remove(path)                          # 程序外把文件删了
        self.assertFalse(os.path.exists(path))   # 前提：真的没了

        text, got_path, kind = resumelib.read_version(v1["id"])

        self.assertEqual(kind, "mismatch",
                         "文件**不存在**时绝不能说成「最新版原文、内容是空的」——"
                         "东西都没了，报成 latest 就是一句用户可见的假话")
        self.assertEqual(text, "")
        self.assertNotEqual(kind, "missing", "「存过、但盘上那份没了」是 mismatch，不是「从来没存过」")

    def test_a_zero_byte_snapshot_without_a_fingerprint_is_still_a_legal_empty_mother(self):
        v1 = resumelib.create_version("母版", "第一版内容")
        resumelib.choose(v1["id"])
        resumelib.create_version("母版", "第二版内容", parent_id=v1["id"])
        snap = self._snap_path(v1["id"])
        with open(snap, "wb") as f:
            f.write(b"")                        # Task 1.8 之前真能造出 0 字节快照
        self._drop_sha(v1["id"])

        text, path, kind = resumelib.read_version(v1["id"])
        self.assertEqual((text, kind), ("", "mother"),
                         "0 字节快照是**合法空档**，不是「读不出来」（写成 if not text: mismatch 就错了）")
        self.assertEqual(path, snap)

        # 纯空白同理：`content_bytes_present` 按 `strip()` 判"有没有可丢的字节" → 也算空档
        with open(snap, "wb") as f:
            f.write("\n".encode("utf-8"))
        self.assertEqual(resumelib.read_version(v1["id"])[2], "mother",
                         "纯空白快照同样是合法空档，不是「读不出来」")


if __name__ == "__main__":
    import unittest
    unittest.main()
