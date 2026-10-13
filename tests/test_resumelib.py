# -*- coding: utf-8 -*-
"""resumelib（简历版本库：族谱 + 母版快照 + 取回规则）的测试。

继承 IsolatedCase：store.DATA_ROOT 与 log.LOG_FILE 同时被重定向到临时目录，
绝不在真实 data/ 上跑。
"""
import os
import threading
from datetime import datetime

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
        self.assertEqual(kind, "mismatch")                       # ← 旧代码这里是 "latest"（静默给别人的内容）
        self.assertEqual(text, "乙的全新简历")                    # 内容照常给，便于对账
        self.assertEqual(path, store.p_path("resumes", "同名.md"))
        self.assertEqual(resumelib.sha256_of(text), stranger["sha256"])        # 逐字就是乙那一份
        self.assertNotEqual(resumelib.sha256_of(text), a2["sha256"])           # 与 a2 记录不符（证据）

    def test_a_live_version_whose_file_was_orphaned_by_purge_is_mismatch(self):
        """**全是未移出记录**的孤儿路径（界面真会读到的那种）：

        purge 掉最新版 a3 后，``<名字>.md`` 里留下的是 a3 的内容；此时 a2 既是母版又是
        "文件主人"，旧代码会以 ``kind="latest"`` 把 **a3 的内容**当成 a2 的原文返回 ——
        记录里的 sha 明明对不上。修好后必须报 ``mismatch``（a2 真正的原文仍在它的快照里）。
        """
        a1 = resumelib.create_version("同名", "甲的第一版")
        resumelib.choose(a1["id"])
        a2 = resumelib.create_version("同名", "甲的第二版", parent_id=a1["id"])
        resumelib.choose(a2["id"])
        a3 = resumelib.create_version("同名", "甲的第三版", parent_id=a2["id"])
        resumelib.purge(a3["id"])
        self.assertEqual([r["id"] for r in resumelib.list_versions()], [a1["id"], a2["id"]])   # 全是活的
        text, path, kind = resumelib.read_version(a2["id"])
        self.assertEqual(kind, "mismatch")                       # ← 旧代码这里是 "latest"
        self.assertEqual(text, "甲的第三版")                      # 拿到的是已被彻底删除的 a3 的内容
        self.assertEqual(path, store.p_path("resumes", "同名.md"))
        self.assertNotEqual(resumelib.sha256_of(text), resumelib.by_id(a2["id"])["sha256"])
        # a2 真正的原文没丢：还在它自己的快照里（记录也如实写了 mother_file）
        self.assertEqual(store.read_text(self._snap_path(a2["id"])), "甲的第二版")
        self.assertEqual(resumelib.by_id(a2["id"])["mother_file"],
                         "resumes/%s/%s.md" % (resumelib.SNAPSHOT_DIR, a2["id"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
