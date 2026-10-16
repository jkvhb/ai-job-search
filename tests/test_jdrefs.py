# -*- coding: utf-8 -*-
"""`jdrefs`（「指向岗位的引用点」单一真源）的测试 + 【守卫测试】。

**这一块的价值不在重构本身，而在「以后再也不会漏」**：清单原先散在 4 个模块里各列一遍，
实测已经漏了两处（`report_state/<报告id>.json`、`interviews/*/feedback.json`）——
删完 JD 之后它们变成盘上孤儿，预演里也不列；下次再加一个「指向岗位」的产物还会漏，
而漏的那一刻**没有任何机制报警**。

**本文件的四条硬纪律**（每一条都对应本项目栽过的那类事故）：

1. **「读不出来」绝不等于「没有引用」**（本文件里最值钱的一条）：每一份会被扫描的文件都被
   换成真实的 GBK 坏字节（不是 mock、不是"假装抛"），断言 `refs_of_jd` 给出**带 `err` 的
   Ref**（`count` 用 `-1` 而不是 `0`），并且 `refs_of_jd_or_raise` **响亮抛错**。
   "读不出来 ⇒ 当成没引用" 正是今天两次事故的形态。
2. **坏文件 ⇒ 整次拒绝、不留半改状态**：逐个 kind 地把它的文件写成坏字节，断言
   `rewrite_jd_refs` 抛 `JdRefsRefused`，并且**六个文件的字节逐一未变**（不是"看着没事"）。
3. **落盘失败要回滚**：注入一次写失败，断言六个文件逐字节回到动手之前。
4. **守卫测试必须有牙**：它自己先在「故意摘掉一个 kind」的变异下红（见 `GuardTest` 的两条：
   一条在测试里用 `mock.patch.dict` 真摘掉一个 kind，一条是交付时人工验证过的变异记录）。

⚠️ 本文件**不** import `jdid` / `jobdup` / `jddelete`：它们是本批第 2~5 步要接线的调用方，
此刻正被别人改着。夹具一律用 `store` 的公开写入口直接造盘上形态（那才是被测的真输入）。
"""
import json
import os
import shutil
import unittest
from unittest import mock

import jdrefs
import store

from tests.base import IsolatedCase

# 两个合法的七段 JD id（`jdid.parse_id` 的口径；本文件不 import jdid，只当字符串用）
JD_A = "本科_甲公司_标注_文本_北京_13-26K_0001"
JD_B = "本科_甲公司_标注_文本_北京_13-26K_0002"

REPORT_ID = "r1"                 # 报告 id = 报告文件名去掉 .html（与 app/store 同一约定）
INTERVIEW_ID = "i1"

# 齐备的六种引用（验收 1 要求「6 条」）。顺序即 `jdrefs.KINDS` 的登记顺序。
SIX_KINDS = ["jds", "jobs", "kb", "iv_status", "report_state", "iv_feedback"]


def _gbk_bytes(text="中文乱码"):
    """一份**真实的**非 UTF-8 字节（记事本按 ANSI/GBK 存过中文就是这个形态）。

    为什么不用 mock：本项目丢数据的现场就是"有字节、却读不出来"，用真实字节才能证明
    `read_*_strict` 这一族的判据真的被问到了（mock 出来的异常是假的输入）。
    """
    return text.encode("gbk")


def _interviews_file(filename):
    return os.path.join(store.interviews_dir(), INTERVIEW_ID, filename)


def _breakers():
    """「会被扫描、也可能读不出来」的五份文件的路径（逐 kind 造坏文件用）。"""
    return {
        "jds": lambda: store.p_path("jds.json"),
        "jobs": lambda: store.jobs_path(),
        "kb": lambda: store.knowledge_path(),
        "iv_status": lambda: _interviews_file("status.json"),
        "iv_feedback": lambda: _interviews_file("feedback.json"),
    }


class _Case(IsolatedCase):
    """公共夹具 + 隔离硬保险。"""

    def setUp(self):
        super(_Case, self).setUp()
        # ⚠️ 本文件所有探针都必须落在临时目录里（真实 data/ 里是用户 145 张卡）。
        self.assertTrue(os.path.abspath(store.DATA_ROOT).startswith(os.path.abspath(self.tmp)),
                        "隔离没生效：DATA_ROOT=%s 不在临时目录 %s 下" % (store.DATA_ROOT, self.tmp))

    def _reset(self):
        """清空这个临时 profile（同一用例里换一个场景时用）。"""
        d = store.profile_dir()
        self.assertTrue(os.path.abspath(d).startswith(os.path.abspath(self.tmp)),
                        "拒绝在临时目录之外清目录：%s" % d)
        shutil.rmtree(d, ignore_errors=True)
        store.ensure_profile()

    # ---------------------------------------------------------------- 夹具
    def _fixture(self, jd_id=JD_A):
        """造一份**齐备六种引用**的盘上形态（一个 kind 都不少）。

        第 5 条（`report_state/<报告id>.json`）与第 6 条（`feedback.json`）就是今天漏掉的两处：
        夹具里少造一条，"6 条"那条验收就永远查不出漏。
        """
        store.write_json(store.p_path("jds.json"),
                         {"version": 1, "seq": 1, "jds": [{"id": jd_id, "id_history": [],
                                                           "created_at": "2026-10-03 00:00"}]})
        store.upsert_job({"id": REPORT_ID, "jd_id": jd_id, "report": REPORT_ID + ".html",
                          "resume": "母版", "score": 70.0, "created_at": "2026-10-03 00:35"})
        store.save_knowledge({"version": 1, "nodes": [
            {"id": "n1", "term": "语音识别", "from_jds": [{"id": jd_id, "job_title": "甲"}]}]})
        self.assertTrue(store.save_interview_json(INTERVIEW_ID, "status.json",
                                                 {"state": "done", "job_id": jd_id}))
        self.assertTrue(store.save_report_state(REPORT_ID, {"要会 ASR": True}))
        self.assertTrue(store.save_interview_json(
            INTERVIEW_ID, "feedback.json",
            {jd_id: {"applied": True, "jd_id": jd_id, "at": "2026-10-04 10:00"}}))

    def _kinds(self, refs):
        return [r.kind for r in refs]

    def _refs_of(self, jd_id):
        return jdrefs.refs_of_jd(jd_id)

    def _bytes_of_everything(self):
        """六份文件的**原始字节**（坏文件也读得出来 —— 用 read_bytes，绝不用 read_text）。"""
        paths = [store.p_path("jds.json"), store.jobs_path(), store.knowledge_path(),
                 _interviews_file("status.json"), store.report_state_path(REPORT_ID),
                 _interviews_file("feedback.json")]
        return dict((p, store.read_bytes(p)) for p in paths)

    def _dangling(self, jd_id):
        """盘上还有哪些地方指着 `jd_id`（改名之后必须一条都不剩）。返回给用户看的说明行。"""
        out = []
        for rec in (store.read_json(store.p_path("jds.json"), {}) or {}).get("jds") or []:
            if isinstance(rec, dict) and rec.get("id") == jd_id:
                out.append("jds.json 里还有一条 id=%s" % jd_id)
        for rec in store.load_jobs():
            if isinstance(rec, dict) and (str(rec.get("jd_id") or "") == jd_id
                                          or str(rec.get("id") or "") == jd_id):
                out.append("jobs.json 里还有一条挂在 %s 下：%s" % (jd_id, rec.get("id")))
        for node in (store.load_knowledge().get("nodes") or []):
            for x in (node.get("from_jds") or []):
                if isinstance(x, dict) and str(x.get("id") or "").strip() == jd_id:
                    out.append("知识库 %s 的 from_jds 还指着 %s" % (node.get("id"), jd_id))
        st = store.load_interview_json(INTERVIEW_ID, "status.json", {})
        if isinstance(st, dict) and st.get("job_id") == jd_id:
            out.append("面试记录 status.json 的 job_id 还指着 %s" % jd_id)
        fb = store.load_interview_json(INTERVIEW_ID, "feedback.json", {})
        if isinstance(fb, dict) and jd_id in fb:
            out.append("面试记录 feedback.json 还有以 %s 为键的条目" % jd_id)
        return out


# ---------------------------------------------------------------- ① 只读扫描
class RefsOfJdTest(_Case):
    def test_a_fixture_with_all_six_kinds_reports_exactly_six_refs(self):
        """**验收 1**：齐备六种引用的夹具上，`refs_of_jd` 返回 6 条（含今天漏的那两条）。"""
        self._fixture()
        refs = self._refs_of(JD_A)
        self.assertEqual(sorted(self._kinds(refs)), sorted(SIX_KINDS),
                         "六种引用没有齐：%r" % (refs,))
        self.assertEqual(len(refs), 6)
        self.assertEqual([r for r in refs if r.err], [], "正常夹具上不该有「读不出来」：%r" % (refs,))
        self.assertTrue(all(r.count >= 1 for r in refs), "命中条数不可能是 0：%r" % (refs,))
        # 逐文件的 kind：一条 Ref 一个文件，且 path 就是那个文件
        by_kind = dict((r.kind, r) for r in refs)
        self.assertEqual(by_kind["report_state"].path, store.report_state_path(REPORT_ID))
        self.assertEqual(by_kind["iv_feedback"].path, _interviews_file("feedback.json"))
        self.assertEqual(by_kind["iv_status"].path, _interviews_file("status.json"))
        self.assertEqual(by_kind["jobs"].path, store.jobs_path())
        self.assertEqual(by_kind["kb"].path, store.knowledge_path())
        self.assertEqual(by_kind["jds"].path, store.p_path("jds.json"))
        self.assertTrue(all(r.how for r in refs), "每条 Ref 都要说清「这是哪一处引用」")

    def test_the_two_kinds_that_were_missed_today_are_really_scanned(self):
        """把今天漏的两处**单独**证明一遍：拿掉那份产物 ⇒ 那个 kind 必须从结果里消失。

        与上一条的分工：上一条查"齐备时是 6 条"，这一条查"它真的是被那份产物驱动扫出来的"
        （不是恒真的、也不是被别的产物顺带凑出来的）。
        """
        self._fixture()
        self.assertIn("report_state", self._kinds(self._refs_of(JD_A)))
        os.remove(store.report_state_path(REPORT_ID))
        self.assertNotIn("report_state", self._kinds(self._refs_of(JD_A)),
                         "报告勾选文件都没了，`report_state` 还在列表里（这一处是假的）")

        self._reset()
        self._fixture()
        self.assertIn("iv_feedback", self._kinds(self._refs_of(JD_A)))
        store.save_interview_json(INTERVIEW_ID, "feedback.json", {})       # 键没了
        self.assertNotIn("iv_feedback", self._kinds(self._refs_of(JD_A)),
                         "feedback.json 里已经没有这个 jd 的键了，`iv_feedback` 还在列表里")

    def test_a_jd_nobody_references_is_an_empty_list_not_an_error(self):
        """反面对照：**真的**没有任何引用 ⇒ 空列表（与"读不出来"是两件相反的事）。"""
        self._fixture(JD_A)
        self.assertEqual(self._refs_of("本科_乙公司_评测_语音_上海_20-30K_0009"), [])
        self.assertTrue(self._refs_of(JD_A), "同一个夹具里 JD_A 必须有引用（否则上一条恒真）")

    def test_merged_into_is_registered_but_marked_as_batch_7_2(self):
        """`merged_into` 登记了、也扫得到，但**标注**处理属批 7.2（本步不碰）。"""
        self._fixture(JD_A)
        jobs = store.load_jobs()
        jobs.append({"id": "r_old", "jd_id": JD_A, "merged_into": JD_A,
                     "created_at": "2026-10-01 00:00"})
        store.save_jobs(jobs)
        refs = [r for r in self._refs_of(JD_A) if r.kind == "merged_into"]
        self.assertEqual(len(refs), 1, "merged_into 没被登记/没被扫到：%r" % (refs,))
        self.assertIn("7.2", jdrefs.KINDS["merged_into"].note or "",
                      "悬空引用必须**在登记表里**写明它归批 7.2，不能只写在注释里")

    def test_a_blank_id_is_refused_instead_of_returning_no_references(self):
        """空 id 不猜：返回空列表会被读成「没有引用」，所以响亮拒绝。"""
        self._fixture()
        for bad in ("", "   ", None):
            with self.subTest(jd_id=bad):
                with self.assertRaises(ValueError):
                    self._refs_of(bad)

    def test_the_same_source_is_read_once_even_though_three_kinds_need_it(self):
        """三条 kind（jobs / report_state / merged_into）共用台账那份严格读 ⇒ 只读一次。

        这不是省一次 IO 的洁癖：三处各读一遍的话，它们量的可能是**两个时刻**的台账
        （中间挤进一次 upsert），预演出的清单就自相矛盾。
        """
        self._fixture()
        calls = []
        real = store.read_jobs_strict

        def watch(path=None):
            calls.append(path)
            return real(path)

        with mock.patch.object(store, "read_jobs_strict", watch):
            self._refs_of(JD_A)
        self.assertEqual(len(calls), 1, "台账被读了 %d 次（应当只读一次）：%r" % (len(calls), calls))


# ---------------------------------------------------------------- ② 硬约束 1：读不出来 ≠ 没有引用
class UnreadableSourceTest(_Case):
    """**本文件最值钱的一组**：每一份会被扫描的文件都有"读不出来"的用例。"""

    def test_every_kind_reports_an_error_instead_of_pretending_there_is_no_reference(self):
        for kind, path_of in sorted(_breakers().items()):
            with self.subTest(kind=kind):
                self._reset()
                self._fixture()
                store.write_bytes(path_of(), _gbk_bytes())
                refs = self._refs_of(JD_A)
                mine = [r for r in refs if r.kind == kind]
                self.assertEqual(len(mine), 1,
                                 "`%s` 读不出来时必须有它那条 Ref（现在是 %r）" % (kind, refs))
                self.assertTrue(mine[0].err, "「读不出来」必须如实上报：%r" % (mine[0],))
                self.assertEqual(mine[0].count, -1,
                                 "读不出来时 count 必须是 -1（未知 0 = 「没有引用」）")
                self.assertIn(os.path.basename(path_of()), mine[0].err,
                              "错误说明要点名是哪一份文件读不出来：%r" % (mine[0].err,))
                kinds = set(r.kind for r in jdrefs.unreadable_refs(refs))
                self.assertIn(kind, kinds, "读不出来的那一处没进 `unreadable_refs`：%r" % (kinds,))
                if kind == "jobs":
                    # 台账读不出来 ⇒ 另外两处（报告 id / merged_into）**同样是未知**：
                    # 把它们报成"没有引用"就是本项目要根除的那个形态。
                    self.assertEqual(kinds, {"jobs", "report_state", "merged_into"},
                                     "台账读不出来时，依赖它的那两处被当成「没有引用」了")

    def test_the_strict_entry_point_raises_instead_of_returning_a_short_list(self):
        """给调用方的「抛错」那条路：`refs_of_jd_or_raise` 在任一处读不出来时响亮抛错。"""
        self._fixture()
        self.assertTrue(jdrefs.refs_of_jd_or_raise(JD_A))          # 正常时照常返回
        store.write_bytes(store.knowledge_path(), _gbk_bytes())
        with self.assertRaises(jdrefs.JdRefsUnreadable) as cm:
            jdrefs.refs_of_jd_or_raise(JD_A)
        self.assertIn("knowledge.json", str(cm.exception))

    def test_the_unreadable_kind_does_not_swallow_the_readable_ones(self):
        """一处读不出来**不影响**别处的如实上报（宁多不少：坏一处只多说一句，绝不少列）。"""
        self._fixture()
        store.write_bytes(_interviews_file("status.json"), _gbk_bytes())
        kinds = set(self._kinds(self._refs_of(JD_A)))
        self.assertEqual(kinds, set(SIX_KINDS), "一处坏了，别的引用点就消失了：%r" % (kinds,))

    def test_a_missing_or_zero_byte_file_is_not_unreadable(self):
        """不存在 / 0 字节**不算**读不出来（合法的「第一次」，里面没有可指的东西）。"""
        self._fixture()
        for path in (store.p_path("jds.json"), store.jobs_path(), store.knowledge_path(),
                     _interviews_file("status.json"), _interviews_file("feedback.json")):
            store.write_bytes(path, b"")
        refs = self._refs_of(JD_A)
        self.assertEqual(refs, [], "0 字节的文件被当成「读不出来」了：%r" % (refs,))
        self.assertEqual(jdrefs.refs_of_jd_or_raise(JD_A), [])


# ---------------------------------------------------------------- ③ 重写（改名）
class RewriteJdRefsTest(_Case):
    def test_rename_moves_all_six_and_leaves_nothing_dangling(self):
        """**验收 2**：改名后 `refs_of_jd(旧)` 为空、`refs_of_jd(新)` 覆盖全部、悬空 0 条。"""
        self._fixture()
        result = jdrefs.rewrite_jd_refs(JD_A, JD_B)
        self.assertEqual([k for k, _ok, _err in result if _ok], SIX_KINDS[:1] + SIX_KINDS[1:],
                         "六项都要报 ok：%r" % (result,))
        self.assertEqual(self._refs_of(JD_A), [], "改完还有东西指着旧 id")
        self.assertEqual(sorted(self._kinds(self._refs_of(JD_B))), sorted(SIX_KINDS),
                         "新 id 没有覆盖全部引用点：%r" % (self._refs_of(JD_B),))
        self.assertEqual(self._dangling(JD_A), [], "改名后仍有悬空引用")
        # 逐项的真实内容：不是「扫出来对」而是「盘上那份对」
        self.assertEqual(json.loads(store.read_text(store.p_path("jds.json")))["jds"][0]["id"], JD_B)
        self.assertEqual(store.load_jobs()[0]["jd_id"], JD_B)
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], JD_B)
        self.assertEqual(store.load_interview_json(INTERVIEW_ID, "status.json")["job_id"], JD_B)
        fb = store.load_interview_json(INTERVIEW_ID, "feedback.json")
        self.assertEqual(list(fb), [JD_B])
        self.assertEqual(fb[JD_B]["jd_id"], JD_B, "条目里那份 `jd_id` 副本没跟着改")
        # 报告 id 不变 ⇒ 报告勾选文件**不需要**改名（它的键是报告 id，不是 jd id）
        self.assertTrue(os.path.isfile(store.report_state_path(REPORT_ID)),
                        "报告勾选文件被顺手删/改名了（它按报告 id 命名，不该动）")

    def test_the_old_id_lands_in_id_history(self):
        """本体改名要留痕（与 `jdid.rename_jd` 同一形状）：旧 id 进 `id_history`。"""
        self._fixture()
        jdrefs.rewrite_jd_refs(JD_A, JD_B)
        rec = json.loads(store.read_text(store.p_path("jds.json")))["jds"][0]
        self.assertEqual(rec["id"], JD_B)
        self.assertEqual(rec["id_history"], [JD_A])

    def test_a_bad_file_refuses_the_whole_rewrite_and_changes_nothing(self):
        """**验收 4 + 硬约束 2**：逐 kind 造坏文件 ⇒ 整次拒绝，且**六份文件字节逐一未变**。

        "跳过坏的那一项、改其它" 是本条针对的形态：那会留下「jds.json 指向新 id、知识库里
        还指着旧 id」的两种身份（本项目最贵的错），所以只许整次拒绝。
        """
        for kind, path_of in sorted(_breakers().items()):
            with self.subTest(kind=kind):
                self._reset()
                self._fixture()
                before = self._bytes_of_everything()
                store.write_bytes(path_of(), _gbk_bytes())
                before_broken = self._bytes_of_everything()
                with self.assertRaises(jdrefs.JdRefsRefused) as cm:
                    jdrefs.rewrite_jd_refs(JD_A, JD_B)
                self.assertIn(os.path.basename(path_of()), str(cm.exception),
                              "拒绝的理由要点名那份读不出来的文件：%s" % cm.exception)
                self.assertEqual(self._bytes_of_everything(), before_broken,
                                 "整次拒绝了，盘上却有文件被改（半改状态）")
                self.assertNotEqual(before, before_broken)      # 证明坏文件确实写进去了
                self.assertTrue(self._refs_of(JD_A), "被拒绝的那次不该动任何引用")

    def test_a_write_failure_rolls_back_to_the_original_bytes(self):
        """落盘中途失败 ⇒ 用原像**逐字节**回滚（绝不留半改状态）。"""
        self._fixture()
        before = self._bytes_of_everything()
        real = jdrefs._write_interview_json
        state = {"n": 0}

        def boom(path, obj):
            state["n"] += 1
            if state["n"] >= 2:
                raise OSError("注入的写盘失败（测试用）")
            return real(path, obj)

        with mock.patch.object(jdrefs, "_write_interview_json", boom):
            with self.assertRaises(jdrefs.JdRefsWriteFailed):
                jdrefs.rewrite_jd_refs(JD_A, JD_B)
        self.assertEqual(state["n"], 2, "注入点没被打到，这条测试没验证到东西")
        self.assertEqual(self._bytes_of_everything(), before, "回滚没把文件逐字节还原")

    def test_old_equal_new_is_a_noop(self):
        self._fixture()
        before = self._bytes_of_everything()
        self.assertEqual(jdrefs.rewrite_jd_refs(JD_A, JD_A), [])
        self.assertEqual(self._bytes_of_everything(), before)

    def test_a_blank_id_is_refused(self):
        self._fixture()
        for old, new in (("", JD_B), (JD_A, ""), (None, JD_B), (JD_A, "  ")):
            with self.subTest(old=old, new=new):
                with self.assertRaises(ValueError):
                    jdrefs.rewrite_jd_refs(old, new)

    def test_explicit_refs_limit_which_kinds_are_rewritten(self):
        """传 `refs` = **显式指定改哪几种**（合并/删除各只要一部分）：没指定的**一个字都不动**。"""
        self._fixture()
        refs = [r for r in self._refs_of(JD_A) if r.kind in ("kb", "iv_status")]
        result = jdrefs.rewrite_jd_refs(JD_A, JD_B, refs=refs)
        self.assertEqual([k for k, _ok, _err in result], ["kb", "iv_status"])
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], JD_B)
        self.assertEqual(store.load_interview_json(INTERVIEW_ID, "status.json")["job_id"], JD_B)
        self.assertEqual(store.load_jobs()[0]["jd_id"], JD_A, "没指定的 kind 被顺手改了")
        self.assertEqual(json.loads(store.read_text(store.p_path("jds.json")))["jds"][0]["id"], JD_A)

    def test_merged_into_is_reported_as_not_handled_in_this_step(self):
        """悬空引用本步**明文不处理**：如实报 `ok=False` + 理由，绝不假装改好了。"""
        self._fixture()
        jobs = store.load_jobs()
        jobs.append({"id": "r_old", "jd_id": JD_A, "merged_into": JD_A})
        store.save_jobs(jobs)
        refs = self._refs_of(JD_A)
        result = jdrefs.rewrite_jd_refs(JD_A, JD_B, refs=refs)
        got = [x for x in result if x[0] == "merged_into"]
        self.assertEqual(len(got), 1, "登记了却没在结果里出现：%r" % (result,))
        self.assertFalse(got[0][1], "本步没处理它，不许报 ok：%r" % (got[0],))
        self.assertIn("7.2", got[0][2])

    def test_rewriting_an_unknown_jd_writes_nothing(self):
        """没有引用命中 ⇒ 一个字节都不写（别为了"改个名"白重写整份文件）。"""
        self._fixture()
        before = self._bytes_of_everything()
        self.assertEqual(jdrefs.rewrite_jd_refs(JD_A, JD_B, refs=[]), [])
        self.assertEqual(self._bytes_of_everything(), before)

    def test_a_whitespace_padded_kb_reference_is_seen_and_moved(self):
        """手改出来的 `" jd1 "`（前后带空白）：预演扫到它 ⇒ 重写也必须真的改它。

        「预演列出 3 条、真改 2 条」正是本模块要根除的形态，所以 detect 与 rewrite 必须是
        **同一把尺子**（去空白比）。逐字比的那份实现（`kb._repoint_refs_in`）会漏掉它。
        """
        self._fixture()
        store.save_knowledge({"version": 1, "nodes": [
            {"id": "n1", "term": "语音识别", "from_jds": [{"id": " " + JD_A + " "}]}]})
        self.assertEqual([r.kind for r in self._refs_of(JD_A) if r.kind == "kb"], ["kb"])
        jdrefs.rewrite_jd_refs(JD_A, JD_B, refs=[r for r in self._refs_of(JD_A) if r.kind == "kb"])
        self.assertEqual(store.load_knowledge()["nodes"][0]["from_jds"][0]["id"], JD_B)


# ---------------------------------------------------------------- ④ 守卫测试（比代码本身值钱）
class GuardTest(_Case):
    def test_every_path_product_is_registered_or_whitelisted(self):
        """**核心断言**：每个「构造 profile 内路径」的产物都要有下落（登记 或 白名单+理由）。

        没下落 ⇒ 红，并在报错里直接给出「要么登记、要么加白名单并写理由」的指引。
        """
        missing = jdrefs.unregistered_writers()
        self.assertEqual(missing, [], jdrefs.guard_advice(missing))

    def test_the_scanner_really_found_things(self):
        """守卫自己的假绿防线：扫不到东西时必须红（换个解析方式就能"全绿"是可耻的）。"""
        found = jdrefs.scanned_products()
        self.assertTrue(found, "一个「构造 profile 内路径」的调用都没扫到 —— 解析方式不对")
        self.assertGreaterEqual(len(found), 8, "扫到的产物太少，八成是漏扫了：%r" % (found,))
        for product in ("jds.json", "jobs.json", "knowledge.json", "report_state",
                        "interviews/status.json", "interviews/feedback.json"):
            self.assertIn(product, found, "登记表里的 %r 居然没被扫到" % (product,))

    def test_the_whitelist_never_covers_a_registered_product(self):
        """白名单**不许**覆盖已登记的产物：否则"摘掉一个 kind"会被白名单接住，守卫就没牙了。"""
        covered = set()
        for kind in jdrefs.KINDS.values():
            covered |= set(kind.products)
        self.assertEqual(covered & set(jdrefs.WHITELIST), set(),
                         "白名单里出现了已登记的产物（守卫会被静默解除武装）")
        self.assertNotIn("report_state", jdrefs.WHITELIST)

    def test_every_whitelist_entry_carries_a_reason(self):
        """白名单不是"免死金牌"：每一条都必须写清理由（一句话，能说服下一个改代码的人）。"""
        self.assertTrue(jdrefs.WHITELIST, "白名单是空的？那 reports/jd 这两条策略没法表达")
        for product, why in jdrefs.WHITELIST.items():
            with self.subTest(product=product):
                self.assertIsInstance(why, str)
                self.assertGreaterEqual(len(why.strip()), 10,
                                        "白名单 %r 的理由太短，等于没写：%r" % (product, why))

    def test_the_whitelist_says_why_reports_and_jd_text_are_not_rewritten(self):
        """设计 §1 第 8 条：`reports/*.html` 与 `jd/<id>.txt` 是**策略上不重写** ⇒ 白名单+理由。"""
        for product in ("reports", "jd"):
            why = jdrefs.WHITELIST.get(product, "")
            self.assertIn("不重写", why, "%r 的白名单理由没说清「策略上不重写」：%r" % (product, why))

    def test_the_guard_is_red_when_a_kind_is_removed(self):
        """**变异证明**：故意从 `KINDS` 里摘掉一个 kind ⇒ 守卫必须红，并给出指引。

        （交付时还人工跑过一次真变异：把 `report_state` 整个从 `KINDS` 里删掉，见提交说明。）
        """
        with mock.patch.dict(jdrefs.KINDS):
            del jdrefs.KINDS["report_state"]
            missing = jdrefs.unregistered_writers()
            advice = jdrefs.guard_advice(missing)
        self.assertIn("report_state", missing,
                      "摘掉 report_state 之后守卫居然全绿 —— 它没牙：%r" % (missing,))
        self.assertIn("KINDS", advice)
        self.assertIn("白名单", advice)
        self.assertIn("理由", advice)

    def test_the_two_kinds_that_live_in_jobs_json_are_both_registered(self):
        """`jobs.json` 里有**两种字段级引用点**：`jd_id`（#2）与 `merged_into`（#7）。

        按"产物"扫的守卫只能看见**文件名**，所以"摘掉 jobs 这一种"它看不出来（同一个文件
        还被 merged_into 认领）。这两条的存在性在这里单独钉住：摘掉任一个 ⇒ 这条红。
        """
        for kind in ("jobs", "merged_into"):
            with self.subTest(kind=kind):
                self.assertIn(kind, jdrefs.KINDS)
                self.assertIn("jobs.json", jdrefs.KINDS[kind].products)

    def test_removing_each_registered_kind_is_caught(self):
        """逐个 kind 变异：**每一个**都摘掉试一遍（只试一个等于只证明了几分之一）。

        ⚠️ 只对"产物没有被别的 kind 认领"的 kind 有意义：`jobs` / `merged_into` 共用
        `jobs.json`（摘掉其中一个，产物仍被另一个认领 —— 见上面那条单独钉存在性的测试）。
        """
        tested = 0
        for kind in sorted(jdrefs.KINDS):
            shared = [p for p in jdrefs.KINDS[kind].products
                      if sum(1 for other in jdrefs.KINDS.values() if p in other.products) > 1]
            if shared:
                continue
            tested += 1
            with self.subTest(kind=kind):
                with mock.patch.dict(jdrefs.KINDS):
                    del jdrefs.KINDS[kind]
                    missing = jdrefs.unregistered_writers()
                self.assertTrue(missing,
                                "摘掉 %s 之后守卫全绿：它的产物没被扫到（守卫有洞）" % kind)
        self.assertGreaterEqual(tested, 5, "这条测试实际只变异了 %d 个 kind，太少了" % tested)


# ---------------------------------------------------------------- ⑤ 模块卫生（铁律）
class ModuleHygieneTest(_Case):
    def _src(self):
        src = store.read_text(os.path.join(store.ROOT, "jdrefs.py"))
        self.assertTrue(src, "读不到 jdrefs.py")
        return src

    def test_it_never_takes_the_business_locks_itself(self):
        """锁序铁律：`_JD_LOCK` / `_MERGE_LOCK` / `_KB_LOCK` **一个都不许**在本模块里被取。

        前三把是调用方持有的外层锁；`_KB_LOCK` 由 `kb._with_kb_write` 内部持 ——
        本模块自己去拿任何一把，都会把「上层锁 → store/kb 的锁」这条顺序搞成环。
        判据用 AST（不是正则）：说明文字里提到它们是对的（铁律就写在文档里）。
        """
        import ast
        tree = ast.parse(self._src())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
        for lock in ("_JD_LOCK", "_MERGE_LOCK", "_KB_LOCK"):
            self.assertNotIn(lock, used, "jdrefs 自己取了 %s（锁序会成环）" % lock)

    def test_the_knowledge_write_goes_through_kbs_only_write_entry(self):
        """写 `knowledge.json` **只许**走 `kb._with_kb_write`（坏库哨兵与锁的唯一入口）。"""
        src = self._src()
        self.assertIn("kb._with_kb_write(", src, "没有走 kb 的唯一写入口")
        self.assertEqual(src.count("store.save_knowledge("), 1,
                         "`store.save_knowledge` 只许出现一次（在 `_with_kb_write` 的临界区里）")

    def test_the_rewrite_really_runs_inside_the_kb_lock(self):
        """运行时证据（比 AST 硬）：写 knowledge.json 的那一刻，`kb._KB_LOCK` 必须归本线程。"""
        import kb
        self._fixture()
        owned = []
        real = store.save_knowledge

        def watch(data, key=None):
            owned.append(kb._KB_LOCK._is_owned())
            return real(data, key)

        with mock.patch.object(store, "save_knowledge", watch):
            jdrefs.rewrite_jd_refs(JD_A, JD_B)
        self.assertTrue(owned, "知识库改写一次都没落盘，这条测试没验证到东西")
        self.assertEqual(owned, [True], "写 knowledge.json 时没有持 kb._KB_LOCK：%r" % (owned,))

    def test_it_does_not_import_the_modules_that_will_import_it(self):
        """第 2~5 步里 `jdid` / `jddelete` / `jobdup` 会 import 本模块 ⇒ 本模块**绝不**反向 import。

        成环的代价不是"好看不好看"：这三个模块 import 本模块、本模块 import 它们，
        谁先被 import 谁就拿到半成品模块（AttributeError 现场取决于启动顺序）。
        """
        import ast
        tree = ast.parse(self._src())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= set(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertTrue(names, "一个 import 都没找到？解析方式不对")
        self.assertEqual(names - {"ast", "glob", "os", "collections", "store", "log", "kb"},
                         set(), "出现了白名单之外的 import（第三方依赖 / 会成环的模块）")
        for forbidden in ("jdid", "jddelete", "jobdup", "app", "interview", "jdfacts"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
