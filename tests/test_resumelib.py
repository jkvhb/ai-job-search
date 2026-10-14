# -*- coding: utf-8 -*-
"""resumelib（简历版本库：族谱 + 母版快照 + 取回规则）的测试。

继承 IsolatedCase：store.DATA_ROOT 与 log.LOG_FILE 同时被重定向到临时目录，
绝不在真实 data/ 上跑。
"""
import json
import os
import threading
from datetime import datetime
from unittest import mock

import llm
import resumelib
import store

from tests.base import IsolatedCase


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


if __name__ == "__main__":
    import unittest
    unittest.main()
