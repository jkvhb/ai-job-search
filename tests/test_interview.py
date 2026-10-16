# -*- coding: utf-8 -*-
"""interview.py —— 面试流水线：转写 → AI 分析 → 反哺知识库 → 渲染复盘报告。

铁律：**绝不真跑 vidkit、绝不真调模型**。转写走外部 venv 子进程（本项目零依赖），
在本测试里 runner 一律注入假的；模型调用同理走假 chat。
"""
import io
import json
import os
import queue
import threading
import time
import unittest

import interview
import log
import store

from tests.base import IsolatedCase


def fake_chat_factory(responses):
    """假模型：按顺序吐预设回复，并记录每次的 prompt。

    注意签名与 tests/test_knowledge.py 的那个**不同**：interview.py 的注入约定是
    `chat(prompt, cfg)`（模型细节由 llm 适配层负责），本测试只关心「问了什么、答了什么」。
    """
    calls = []

    def fake_chat(prompt, cfg=None, **kw):
        calls.append(prompt)
        if not responses:
            raise AssertionError("假模型被调用的次数比预设回复还多（analyze 不该多问）")
        return responses.pop(0)

    fake_chat.calls = calls
    return fake_chat


class TranscribeCallTest(IsolatedCase):
    """转写必须走 vidkit 的 venv + 模块，绝不把 faster-whisper 引进本项目。"""

    def _fake_vidkit(self, name="video-link-extractor"):
        """造一个「venv 里真有 python.exe」的假 vidkit 目录。

        不造的话 find_vidkit 会返回 None，测试就永远走「找不到 vidkit」那条分支 ——
        那样 _vidkit_python 里的路径拼接即使写错，断言也照样绿（凭据式假绿）。
        """
        root = os.path.join(self.tmp, "fake-vidkit", name)
        os.makedirs(os.path.join(root, ".venv", "Scripts"))
        py = os.path.join(root, ".venv", "Scripts", "python.exe")
        with open(py, "wb") as f:
            f.write(b"")
        return root, py

    def test_uses_the_configured_venv_and_module(self):
        root, py = self._fake_vidkit()
        calls = []

        def fake_runner(cmd, **kw):
            calls.append(cmd)
            return json.dumps({"text": "转写内容", "segments": [{"start": 0, "end": 1, "text": "转写内容"}],
                               "language": "zh", "duration": 10.0}, ensure_ascii=False)

        out = interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": root}, runner=fake_runner)
        self.assertEqual(out["text"], "转写内容")
        self.assertEqual(len(out["segments"]), 1)
        # 断言必须钉在**具体的解释器路径**上：只在命令里找 "vidkit" 的话，
        # 连「压根没走 venv」都发现不了（目录名里本来就含 vidkit）。
        self.assertEqual(calls[0][0], py)
        self.assertIn("vidkit", " ".join(calls[0]))          # 走的是 vidkit 的模块
        self.assertIn("transcribe", " ".join(calls[0]))

    def test_missing_vidkit_raises_a_clear_error(self):
        with self.assertRaises(RuntimeError) as cm:
            interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": os.path.join(self.tmp, "definitely-not-here")},
                                     runner=lambda *a, **k: "")
        self.assertIn("vidkit", str(cm.exception))

    def test_vidkit_dir_exists_but_venv_is_missing_raises(self):
        """目录在、.venv 没建 → 必须报「虚拟环境不存在」，且绝不悄悄返回空转写。

        这条专门守住 implement 点 3：静默返回空会让用户拿到一份「0 字转写」的报告，
        比起报错更难发现。
        """
        bare = os.path.join(self.tmp, "bare-vidkit")
        os.makedirs(bare)
        with self.assertRaises(RuntimeError) as cm:
            interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": bare}, runner=lambda *a, **k: "")
        self.assertIn("vidkit", str(cm.exception))
        self.assertIn("虚拟环境", str(cm.exception))

    def test_bad_json_from_runner_raises(self):
        root, _ = self._fake_vidkit()
        with self.assertRaises(RuntimeError):
            interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": root},
                                     runner=lambda *a, **k: "not json at all")

    def test_json_without_text_field_raises(self):
        root, _ = self._fake_vidkit()
        with self.assertRaises(RuntimeError):
            interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": root},
                                     runner=lambda *a, **k: '{"segments":[]}')

    def test_progress_lines_before_the_json_are_tolerated(self):
        """vidkit 会先打进度日志：不能因为前面几行不是 JSON 就判「转写失败」。"""
        root, _ = self._fake_vidkit()
        noisy = ("加载模型 medium...\n"
                 "转写中 10%\n转写中 90%\n"
                 + json.dumps({"text": "转写内容", "segments": []}, ensure_ascii=False) + "\n")
        out = interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": root},
                                       runner=lambda *a, **k: noisy)
        self.assertEqual(out["text"], "转写内容")

    def test_default_probe_finds_the_sibling_project(self):
        """没配 vidkit_dir 时探测本项目同级目录（用假 ROOT 验证拼接方向，不碰真盘）。"""
        parent = os.path.join(self.tmp, "workspace")
        root = os.path.join(parent, "video-link-extractor")
        os.makedirs(os.path.join(root, ".venv", "Scripts"))
        open(os.path.join(root, ".venv", "Scripts", "python.exe"), "wb").close()
        old_root = interview.ROOT
        interview.ROOT = os.path.join(parent, "ai-job-search")
        self.addCleanup(setattr, interview, "ROOT", old_root)
        self.assertEqual(interview.find_vidkit({}), root)


class AnalyzeTest(IsolatedCase):
    def test_returns_normalised_sections(self):
        chat = fake_chat_factory(['{"summary":"总评","questions":[{"q":"问","category":"AI基础技术",'
                                  '"answer_quality":"好","comment":"c"}],"focus_areas":[{"area":"AI基础技术",'
                                  '"share":60}],"new_terms":[{"term":"新词","definition":"d"}],'
                                  '"interviewer_quality":{"score":4,"comment":"c"},'
                                  '"answer_review":[{"topic":"t","verdict":"答砸了"}],'
                                  '"weak_concepts":[{"term":"弱词"}],'
                                  '"next_round_prep":{"likely_followups":["f"],"to_study":["s"]}}'])
        out = interview.analyze({"text": "转写"}, {"job_title": "岗"}, {"text_model": {}}, chat=chat)
        for k in ("summary", "questions", "focus_areas", "new_terms",
                  "interviewer_quality", "answer_review", "weak_concepts", "next_round_prep"):
            self.assertIn(k, out)
        # 归一化绝不能把模型给的真值丢掉/改坏（只补缺、只清洗）
        self.assertEqual(out["summary"], "总评")
        self.assertEqual(out["questions"][0]["q"], "问")
        self.assertEqual(out["questions"][0]["answer_quality"], "好")
        self.assertEqual(out["focus_areas"][0]["share"], 60)
        self.assertEqual(out["interviewer_quality"]["score"], 4)
        self.assertEqual(out["next_round_prep"]["to_study"], ["s"])

    def test_missing_sections_are_filled_with_empty_defaults(self):
        chat = fake_chat_factory(['{"summary":"只有总评"}'])
        out = interview.analyze({"text": "x"}, {}, {"text_model": {}}, chat=chat)
        self.assertEqual(out["questions"], [])
        self.assertEqual(out["weak_concepts"], [])
        self.assertEqual(out["next_round_prep"]["to_study"], [])
        self.assertEqual(out["next_round_prep"]["likely_followups"], [])
        self.assertEqual(out["interviewer_quality"]["score"], 0)

    def test_analysis_reaches_the_transcript_text(self):
        """prompt 里必须真有转写正文 —— 否则模型是在凭空编一份复盘。"""
        chat = fake_chat_factory(['{"summary":"s"}'])
        interview.analyze({"text": "面试官：请解释 RLHF 的奖励模型"}, {"job_title": "AI训练师"},
                          {"text_model": {}}, chat=chat)
        self.assertIn("RLHF 的奖励模型", chat.calls[0])
        self.assertIn("AI训练师", chat.calls[0])

    def test_bad_shape_sections_are_coerced_not_crashed(self):
        """模型偶尔吐错类型（字符串当列表、负数占比、非 dict 项）→ 归一化兜住，绝不崩。"""
        chat = fake_chat_factory([json.dumps({"summary": 123, "questions": "oops",
                                              "focus_areas": [{"area": "A", "share": -5},
                                                              {"area": "B", "share": "40"},
                                                              "垃圾", {"area": ""}],
                                              "weak_concepts": ["裸字符串", {"term": "  "}, 5],
                                              "next_round_prep": "oops"}, ensure_ascii=False)])
        out = interview.analyze({"text": "x"}, {}, {"text_model": {}}, chat=chat)
        self.assertEqual(out["summary"], "123")
        self.assertEqual(out["questions"], [])
        self.assertEqual([f["area"] for f in out["focus_areas"]], ["B"])   # 负数/空/非 dict 全丢
        self.assertEqual([w["term"] for w in out["weak_concepts"]], ["裸字符串"])
        self.assertEqual(out["next_round_prep"]["to_study"], [])

    def test_long_transcript_is_summarised_then_analysed(self):
        """超长转写先 map 摘要再 reduce 分析：reduce 只收到摘要，不再收到几万字原文。"""
        long_text = "甲" * (interview.MAX_ANALYZE_CHARS + 5000)
        chat = fake_chat_factory(['{"summary":"块摘要一"}', '{"summary":"块摘要二"}', '{"summary":"总评"}'])
        out = interview.analyze({"text": long_text}, {}, {"text_model": {}}, chat=chat)
        self.assertEqual(out["summary"], "总评")
        self.assertEqual(len(chat.calls), 3)                       # 两块 map + 一次 reduce
        self.assertIn("块摘要一", chat.calls[-1])
        self.assertIn("块摘要二", chat.calls[-1])
        self.assertNotIn(long_text, chat.calls[-1])                # 没有把 4.5 万字原样甩给 reduce

    def test_unparsable_reply_raises(self):
        chat = fake_chat_factory(["完全不是 JSON"])
        with self.assertRaises(RuntimeError):
            interview.analyze({"text": "x"}, {}, {"text_model": {}}, chat=chat)

    def test_dict_reply_is_accepted(self):
        """注入的 chat 直接返回 dict 时不能再去 json.loads（会把合法结果砸掉）。"""
        out = interview.analyze({"text": "x"}, {}, {"text_model": {}},
                                chat=lambda prompt, cfg=None: {"summary": "直接给字典"})
        self.assertEqual(out["summary"], "直接给字典")

    def test_runner_gets_vidkit_dir_as_cwd(self):
        """子进程必须在 vidkit 目录里跑：它要 import vidkit、读 models/。"""
        root = os.path.join(self.tmp, "vk-cwd")
        os.makedirs(os.path.join(root, ".venv", "Scripts"))
        open(os.path.join(root, ".venv", "Scripts", "python.exe"), "wb").close()
        seen = {}

        def runner(cmd, **kw):
            seen.update(kw)
            return json.dumps({"text": "t", "segments": []})

        interview.run_transcribe("/tmp/a.wav", {"vidkit_dir": root}, runner=runner)
        self.assertEqual(seen.get("cwd"), root)
        self.assertEqual(seen.get("cfg"), {"vidkit_dir": root})


class PipelineTest(IsolatedCase):
    def setUp(self):
        """把流水线的 cfg 钉在**本用例临时目录里的假 vidkit** 上。

        ⚠️ 这里**必须**显式给 `vidkit_dir`：`find_vidkit` 在没有配置时会去探测
        **本项目的同级目录** `../video-link-extractor`（interview.py:217-220）。本机恰好装着它，
        所以 `cfg={}` 也绿；但把仓库复制到别处（或在没装 vidkit 的机器上）跑，
        `find_vidkit` 返回 None → `run_transcribe` 先抛「找不到 vidkit」，整批 PipelineTest
        11 条全红（实测：复制到 %TEMP% 后 errors=7 / failures=4）。

        注意 `run_transcribe` **即使注入了 runner 也先要求 vidkit 目录与它的 venv 存在**
        （interview.py:347-353，runner 只替掉最后那一步子进程），所以光注入 runner 不够 ——
        假 venv 里的 `python.exe` 让 `_vidkit_python` 的存在性检查通过，而 runner 仍然是假的，
        真子进程一次都不会起。
        """
        super().setUp()
        self.cfg = {"vidkit_dir": self._fake_vidkit()}

    def _fake_vidkit(self, name="video-link-extractor"):
        """造一个「venv 里真有 python.exe」的假 vidkit 目录（与 TranscribeCallTest 同形）。"""
        root = os.path.join(self.tmp, "fake-vidkit", name)
        os.makedirs(os.path.join(root, ".venv", "Scripts"))
        py = os.path.join(root, ".venv", "Scripts", "python.exe")
        with open(py, "wb") as f:
            f.write(b"")
        return root

    def _deps(self, text="转写内容", reply=None, boom=None):
        """假转写 + 假模型：整条链同步跑完，绝不真跑 vidkit / 真调模型。"""
        def runner(cmd, **kw):
            if boom == "transcribe":
                raise RuntimeError("vidkit 挂了")
            return json.dumps({"text": text, "segments": [{"start": 0, "end": 1, "text": text}],
                               "language": "zh", "duration": 10.0}, ensure_ascii=False)
        reply = reply or json.dumps({"summary": "总评", "questions": [], "focus_areas": [],
                                     "new_terms": [], "interviewer_quality": {"score": 4, "comment": "c"},
                                     "answer_review": [], "weak_concepts": [],
                                     "next_round_prep": {"likely_followups": [], "to_study": []}},
                                    ensure_ascii=False)
        chat = fake_chat_factory([reply])
        return {"runner": runner, "chat": chat, "render": lambda *a, **k: "报告文件名.html"}

    def test_pipeline_writes_transcript_analysis_and_done_status(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps())
        self.assertTrue(os.path.exists(store.interview_file("iv1", "transcript.json")))
        self.assertTrue(os.path.exists(store.interview_file("iv1", "analysis.json")))
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "done")

    def test_every_state_is_written_to_status_json(self):
        """状态机的每个状态都必须真的落盘（前端轮询只看 status.json）。"""
        seen = []
        real = store.save_interview_json

        def spy(interview_id, filename, obj, **kw):
            if filename == "status.json":
                seen.append(obj["state"])
            return real(interview_id, filename, obj, **kw)

        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        old = store.save_interview_json
        store.save_interview_json = spy
        self.addCleanup(setattr, store, "save_interview_json", old)
        interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps())
        self.assertEqual(seen, ["transcribing", "analyzing", "done"])

    def test_failure_sets_error_state_and_keeps_the_audio(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps(boom="transcribe"))
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["state"], "error")
        self.assertIn("vidkit", st.get("error", ""))
        self.assertTrue(os.path.exists(store.interview_file("iv1", "audio.wav")))   # 绝不删录音

    def test_failure_does_not_leave_a_transcript(self):
        """转写失败时绝不能写出半截 transcript.json —— 否则重试会「跳过转写」拿空文本去分析。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps(boom="transcribe"))
        self.assertFalse(os.path.exists(store.interview_file("iv1", "transcript.json")))

    def test_retry_skips_transcription_when_transcript_exists(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        store.save_interview_json("iv1", "transcript.json", {"text": "已有的转写", "segments": []})
        called = []
        deps = self._deps()
        deps["runner"] = lambda cmd, **kw: called.append(cmd) or ""
        interview.run_pipeline("j1", "iv1", self.cfg, deps=deps)
        self.assertEqual(called, [])                              # 没有再跑转写
        self.assertEqual(store.load_interview_json("iv1", "transcript.json")["text"], "已有的转写")

    def test_text_upload_skips_transcription(self):
        store.save_interview_json("iv1", "source.txt", {"x": 1})
        store.save_stream("iv1", "source.txt", io.BytesIO("面试官：你好".encode("utf-8")), max_bytes=10_000)
        called = []
        deps = self._deps()
        deps["runner"] = lambda cmd, **kw: called.append(cmd) or ""
        interview.run_pipeline("j1", "iv1", self.cfg, kind="text", deps=deps)
        self.assertEqual(called, [])
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "done")
        tr = store.load_interview_json("iv1", "transcript.json")
        self.assertEqual(tr["text"], "面试官：你好")
        self.assertEqual(tr["segments"], [])

    def test_kind_text_never_reports_transcribing(self):
        """文字稿没有转写阶段：状态直接从 analyzing 起，前端不会白等 27 分钟。"""
        seen = []
        real = store.save_interview_json

        def spy(interview_id, filename, obj, **kw):
            if filename == "status.json":
                seen.append(obj["state"])
            return real(interview_id, filename, obj, **kw)

        store.save_stream("iv1", "source.txt", io.BytesIO("你好".encode("utf-8")), max_bytes=10_000)
        old = store.save_interview_json
        store.save_interview_json = spy
        self.addCleanup(setattr, store, "save_interview_json", old)
        interview.run_pipeline("j1", "iv1", self.cfg, kind="text", deps=self._deps())
        self.assertNotIn("transcribing", seen)

    def test_missing_audio_raises_and_keeps_error_state(self):
        with self.assertRaises(RuntimeError):
            interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps())
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "error")

    def test_analysis_failure_keeps_audio_and_transcript(self):
        """模型环节挂了：录音与已转写内容都要留着，重试只需重跑分析。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        deps = self._deps()
        deps["chat"] = fake_chat_factory(["完全不是 JSON"])
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", self.cfg, deps=deps)
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "error")
        self.assertTrue(os.path.exists(store.interview_file("iv1", "audio.wav")))
        self.assertTrue(os.path.exists(store.interview_file("iv1", "transcript.json")))

    def test_pipeline_feeds_analysis_back_into_the_knowledge_base(self):
        """反哺：转写里命中的概念写回知识库（被问过 +1），答砸的降级。

        断言用 **asked_count 本身**，不用「复习队列排第一」之类的顺序 —— 顺序断言在
        平局兜底（术语升序）下可能靠巧合通过。
        """
        kb = __import__("kb")
        kb.absorb([{"term": "RLHF"}, {"term": "语音识别"}], {"id": "j1"})
        kb.set_state("RLHF", "已掌握")
        reply = json.dumps({"summary": "s", "weak_concepts": [{"term": "RLHF"}],
                            "questions": [{"q": "RLHF 怎么训的"}], "focus_areas": [], "new_terms": [],
                            "interviewer_quality": {}, "answer_review": [],
                            "next_round_prep": {}}, ensure_ascii=False)
        deps = self._deps(text="我们聊了 RLHF 和语音识别")
        deps["chat"] = fake_chat_factory([reply])
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        interview.run_pipeline("j1", "iv1", self.cfg, deps=deps)
        cards = {n["term"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["RLHF"]["asked_count"], 1)
        self.assertEqual(cards["RLHF"]["last_outcome"], "没答上")
        self.assertEqual(cards["RLHF"]["state"], "学习中")          # 已掌握 → 答砸 → 降级
        self.assertEqual(cards["语音识别"]["asked_count"], 1)

    def test_hits_are_saved_on_the_report_data(self):
        reply = json.dumps({"summary": "s"}, ensure_ascii=False)
        deps = self._deps(text="我们聊了 RLHF")
        deps["chat"] = fake_chat_factory([reply])
        rendered = []
        deps["render"] = lambda out, jd_id, iv_id: rendered.append((out, jd_id, iv_id)) or "f.html"
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        interview.run_pipeline("j1", "iv1", self.cfg, deps=deps)
        data, jd_id, iv_id = rendered[0]
        self.assertEqual(data["kind"], "interview")
        self.assertEqual(jd_id, "j1")
        self.assertEqual(iv_id, "iv1")
        self.assertEqual(data["meta"]["duration"], 10.0)
        self.assertEqual(data["meta"]["language"], "zh")
        self.assertEqual(data["summary"], "s")            # 分析结果被原样带进报告数据
        self.assertEqual(data["meta"]["chars"], len("我们聊了 RLHF"))

    def test_default_render_writes_a_report_file(self):
        """不注入 render 时走真实现：必须真的落一份 HTML（而不是静默什么都不做）。"""
        store.save_stream("iv1", "source.txt", io.BytesIO("面试官：你好".encode("utf-8")), max_bytes=10_000)
        deps = self._deps()
        del deps["render"]
        interview.run_pipeline("j1", "iv1", self.cfg, kind="text", deps=deps)
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "done")
        self.assertIn("interview", store.read_text(path))

    def test_second_run_does_not_double_count_the_interview(self):
        """重试（转写成功但分析挂了）绝不能把「被问过」再加一次 —— 那会直接污染复习优先级。

        真实现里第二次 run_pipeline 会跳过转写、重跑分析，然后走到反哺那一步。
        """
        import kb
        kb.absorb([{"term": "RLHF"}], {"id": "j1"})
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        reply = json.dumps({"summary": "s"}, ensure_ascii=False)
        first = self._deps(text="我们聊了 RLHF")
        first["chat"] = fake_chat_factory([reply])
        interview.run_pipeline("j1", "iv1", self.cfg, deps=first)
        self.assertEqual({n["term"]: n for n in store.load_knowledge()["nodes"]}["RLHF"]["asked_count"], 1)

        again = self._deps(text="我们聊了 RLHF")
        again["chat"] = fake_chat_factory([reply])
        interview.run_pipeline("j1", "iv1", self.cfg, deps=again)      # 重试
        self.assertEqual({n["term"]: n for n in store.load_knowledge()["nodes"]}["RLHF"]["asked_count"], 1)
        self.assertEqual(store.load_interview_json("iv1", "feedback.json")["j1"]["applied"], True)

    def test_feedback_idempotency_is_per_job_not_global(self):
        """同一场面试被算到另一个岗位上时，必须照样反哺（标记按 (面试, 岗位) 记，不是一个全局布尔）。"""
        import kb
        kb.absorb([{"term": "RLHF"}], {"id": "j1"})
        self.assertIsNotNone(interview._apply_feedback("j1", {"text": "聊了 RLHF"}, {}, "iv1"))
        self.assertIsNone(interview._apply_feedback("j1", {"text": "聊了 RLHF"}, {}, "iv1"))
        self.assertIsNotNone(interview._apply_feedback("j2", {"text": "聊了 RLHF"}, {}, "iv1"))
        cards = {n["term"]: n for n in store.load_knowledge()["nodes"]}
        self.assertEqual(cards["RLHF"]["asked_count"], 2)        # j1 一次 + j2 一次

    def test_failed_kb_write_does_not_leave_a_feedback_marker(self):
        """知识库写盘失败时绝不能先落标记：否则那次反哺永远补不回来。"""
        import kb
        kb.absorb([{"term": "RLHF"}], {"id": "j1"})
        old = kb.record_interview

        def boom(*a, **k):
            raise RuntimeError("写盘失败")

        kb.record_interview = boom
        self.addCleanup(setattr, kb, "record_interview", old)
        with self.assertRaises(RuntimeError):
            interview._apply_feedback("j1", {"text": "聊了 RLHF"}, {}, "iv1")
        self.assertFalse(interview._feedback_done("iv1", "j1"))
        kb.record_interview = old
        self.assertIsNotNone(interview._apply_feedback("j1", {"text": "聊了 RLHF"}, {}, "iv1"))

    def test_start_runs_in_a_thread_and_reaches_a_terminal_state(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        t = interview.start("j1", "iv1", self.cfg, deps=self._deps())
        self.assertTrue(t.daemon)                       # 主程序退出不该被转写线程拖住
        # 确定性等它真的结束（join，不是 sleep 轮询赌它跑完 —— 那正是这条用例曾经 1/5 偶发红的原因：
        # 状态先写成 done、线程登记还没摘，中间那道缝里 is_running 仍是 True）
        t.join(10)
        self.assertFalse(t.is_alive(), "流水线线程 10 秒内没跑完")
        self.assertEqual(interview.status("iv1")["state"], "done")
        self.assertFalse(interview.is_running("iv1"))

    def test_terminal_state_is_published_atomically_with_thread_deregistration(self):
        """不变式：**所有写盘/写日志都在线程登记期内**，而**终态发布必须是最后一步**。

        这是「先写 done、后摘登记」那条竞态的根因用例。用固定 sleep / 反复轮询去测只能是碰运气
        （偶发复现不了、修好了也证明不了），所以这里用检查点把线程**钉在关键位置上**，
        两个方向各钉一次（按它们真实发生的顺序，不假设谁先谁后）：

          A. 「终态发布之前的最后一步」（终态写盘之前的最后一次日志）：
             落盘状态必须**还不是**终态，且 is_running() 是 True。
             反过来就是那条偶发红：前端/测试看到「已完成却还在跑」。
          B. 「终态已写盘、登记尚未摘除」：`_THREADS_LOCK` 必须被线程握着 ——
             只有两者在同一次临界区里做完，读者才不可能在中间钻进来。

        另外钉住**顺序**（`seen == [before_publish, terminal_written]`）：终态发布必须是线程的
        最后一步 —— 摘登记之后线程若还能写日志，隔离守卫（tests/base.py）就看不见它了，
        它就能在真实路径被还原之后写进用户文件（那是已经发生过的真事故）。
        """
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        checkpoints = queue.Queue()        # 线程把 (检查点名, 放行闸) 放进来
        seen = []
        real_event, real_save = log.log_event, store.save_interview_json

        def checkpoint(kind):
            """把自己钉在这个位置上，等测试放行（Event，不是 sleep）。"""
            rel = threading.Event()
            checkpoints.put((kind, rel))
            rel.wait(5)

        def hooked_log_event(event, *a, **kw):
            if event == "interview.pipeline_done":
                checkpoint("before_publish")
            return real_event(event, *a, **kw)

        def hooked_save(interview_id, filename, obj, **kw):
            out = real_save(interview_id, filename, obj, **kw)
            if filename == "status.json" and isinstance(obj, dict) and obj.get("state") == "done":
                checkpoint("terminal_written")
            return out

        log.log_event = hooked_log_event
        self.addCleanup(setattr, log, "log_event", real_event)
        store.save_interview_json = hooked_save
        self.addCleanup(setattr, store, "save_interview_json", real_save)

        t = interview.start("j1", "iv1", self.cfg, deps=self._deps())
        try:
            while len(seen) < 2:
                try:
                    kind, rel = checkpoints.get(timeout=5)
                except queue.Empty:
                    self.fail("流水线没走到第 %d 个关键点（已到：%s）" % (len(seen) + 1, seen))
                seen.append(kind)
                try:
                    if kind == "before_publish":
                        # A：终态还没发布 —— 落盘状态绝不能是终态，线程也还在跑
                        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "analyzing")
                        self.assertNotIn(interview.status("iv1")["state"], ("done", "error"))
                        self.assertTrue(interview.is_running("iv1"))
                    else:
                        # B：终态已落盘但登记没摘 —— 必须在同一次临界区里
                        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "done")
                        held = interview._THREADS_LOCK.acquire(blocking=False)
                        if held:
                            interview._THREADS_LOCK.release()
                        self.assertFalse(held, "终态写盘时没持 _THREADS_LOCK：状态与线程登记不是原子发布")
                finally:
                    rel.set()
        finally:
            while True:                     # 失败退出时也别把线程挂在闸门上
                try:
                    _, rel = checkpoints.get_nowait()
                except queue.Empty:
                    break
                rel.set()
            t.join(10)
        self.assertEqual(seen, ["before_publish", "terminal_written"])   # 顺序：终态发布是最后一步
        # 不变式的终局：终态 + 不在跑
        self.assertFalse(t.is_alive())
        self.assertFalse(interview.is_running("iv1"))
        self.assertEqual(interview.status("iv1")["state"], "done")

    def test_stale_running_state_is_reported_as_interrupted(self):
        store.save_interview_json("iv1", "status.json",
                                  {"state": "transcribing", "started_at": "2026-01-01 00:00:00"})
        self.assertEqual(interview.status("iv1")["state"], "interrupted")   # 没有活动线程

    def test_stale_analyzing_state_is_also_interrupted(self):
        store.save_interview_json("iv1", "status.json",
                                  {"state": "analyzing", "started_at": "2026-01-01 00:00:00"})
        self.assertEqual(interview.status("iv1")["state"], "interrupted")

    def test_live_thread_state_is_not_reported_as_interrupted(self):
        """有活动线程时绝不能说「已中断」——那会让用户以为可以重试，两条流水线同时跑。"""
        gate = __import__("threading").Event()
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)

        def runner(cmd, **kw):
            gate.wait(5)
            return json.dumps({"text": "t", "segments": []})

        deps = self._deps()
        deps["runner"] = runner
        t = interview.start("j1", "iv1", self.cfg, deps=deps)
        try:
            deadline = time.time() + 5
            # 等线程真的进了转写（status.json 是线程自己写的，先等它出现再断言）
            while time.time() < deadline and not interview.status("iv1").get("state"):
                time.sleep(0.01)
            self.assertTrue(interview.is_running("iv1"))
            self.assertEqual(interview.status("iv1")["state"], "transcribing")
        finally:
            gate.set()
            t.join(10)

    def test_finished_state_is_not_interrupted(self):
        store.save_interview_json("iv1", "status.json", {"state": "done", "started_at": "2026-01-01 00:00:00"})
        self.assertEqual(interview.status("iv1")["state"], "done")
        self.assertEqual(interview.status("no-such-id"), {})

    def test_list_interviews_by_job(self):
        store.save_interview_json("iv1", "status.json", {"state": "done", "job_id": "j1"})
        store.save_interview_json("iv2", "status.json", {"state": "error", "job_id": "j1", "error": "x"})
        store.save_interview_json("iv3", "status.json", {"state": "done", "job_id": "j2"})
        self.assertEqual(interview.list_interviews("j2"), ["iv3"])
        self.assertEqual(sorted(interview.list_interviews("j1")), ["iv1", "iv2"])
        self.assertEqual(interview.list_interviews(""), [])

    def test_pipeline_logs_start_and_done(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps())
        events = [json.loads(l)["event"] for l in store.read_text(log.LOG_FILE).splitlines() if l.strip()]
        self.assertIn("interview.pipeline_start", events)
        self.assertIn("interview.pipeline_done", events)

    def test_pipeline_logs_error_on_failure(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps(boom="transcribe"))
        events = [json.loads(l)["event"] for l in store.read_text(log.LOG_FILE).splitlines() if l.strip()]
        self.assertIn("interview.pipeline_error", events)

    def test_status_records_audio_metadata(self):
        """上传时的元数据（原始文件名/字节数）必须被后续状态写入继承下来 —— 前端要显示它。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        store.save_interview_json("iv1", "status.json",
                                  {"state": "queued", "original_name": "面试录音.m4a", "audio_bytes": 100})
        interview.run_pipeline("j1", "iv1", self.cfg, deps=self._deps())
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["original_name"], "面试录音.m4a")
        self.assertEqual(st["audio_bytes"], 100)
        self.assertEqual(st["state"], "done")
        self.assertGreaterEqual(st["elapsed"], 0)


class StartGuardTest(IsolatedCase):
    """同一次面试被连点两次上传 / 转写中又点重试：绝不允许跑两条流水线。

    真跑的话是两条 27 分钟的转写互相覆盖文件，测试等不起，所以用「假 pipeline + Event 阻塞」
    把「第一次还在跑」这个瞬间**确定性地**造出来。
    """

    def _blocking_pipeline(self):
        import threading
        gate = threading.Event()
        blocked = threading.Event()
        calls = []

        def fake_run_pipeline(jd_id, interview_id, cfg, kind="audio", deps=None):
            calls.append((jd_id, interview_id))
            store.save_interview_json(interview_id, "status.json", {"state": "transcribing"})
            blocked.set()
            gate.wait(10)                       # 卡在「转写中」，直到测试放行
            store.save_interview_json(interview_id, "status.json", {"state": "done"})

        return fake_run_pipeline, gate, blocked, calls

    def _patch_pipeline(self, fake):
        old = interview.run_pipeline
        interview.run_pipeline = fake
        self.addCleanup(setattr, interview, "run_pipeline", old)

    def test_second_start_reuses_the_live_thread_and_does_not_run_again(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        fake, gate, blocked, calls = self._blocking_pipeline()
        self._patch_pipeline(fake)
        first = None
        try:
            first = interview.start("j1", "iv1", {}, deps={})
            self.assertTrue(blocked.wait(5), "假流水线没跑起来")
            self.assertTrue(interview.is_running("iv1"))
            second = interview.start("j1", "iv1", {}, deps={})   # 转写中又点了一次
            self.assertIs(second, first)                        # 复用已有线程
            self.assertEqual(len(calls), 1)                     # 没有第二次执行
            self.assertEqual(interview.status("iv1")["state"], "transcribing")
        finally:
            gate.set()
            if first is not None:
                first.join(10)
        self.assertEqual(len(calls), 1)
        self.assertFalse(interview.is_running("iv1"))
        self.assertEqual(interview.status("iv1")["state"], "done")

    def test_start_again_after_finishing_runs_a_new_round(self):
        """跑完了必须能再起一轮（否则「重试」在终态上永远无效）。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        fake, gate, blocked, calls = self._blocking_pipeline()
        self._patch_pipeline(fake)
        first = interview.start("j1", "iv1", {}, deps={})
        blocked.wait(5)
        gate.set()
        first.join(10)
        self.assertFalse(interview.is_running("iv1"))
        second = interview.start("j1", "iv1", {}, deps={})       # 终态后重试
        second.join(10)
        self.assertIsNot(second, first)
        self.assertEqual(len(calls), 2)                          # 真的又跑了一轮
        self.assertFalse(second.is_alive())


class DefaultRunnerTest(IsolatedCase):
    def test_default_runner_runs_a_real_process(self):
        """_default_runner 真的起子进程并拿回 stdout（用本机 python，不碰 vidkit）。"""
        import sys
        out = interview._default_runner([sys.executable, "-c", "print('hi')"], cfg={}, cwd=self.tmp)
        self.assertIn("hi", out)

    def test_default_runner_raises_on_nonzero_exit(self):
        import sys
        with self.assertRaises(RuntimeError) as cm:
            interview._default_runner([sys.executable, "-c", "import sys; sys.exit(3)"], cfg={}, cwd=self.tmp)
        self.assertIn("3", str(cm.exception))


class FfprobeDurationTest(IsolatedCase):
    """上传时的时长探测（规格 §5「预计 ≈ N 分钟」的数据来源）。

    **绝不真跑 ffprobe**：runner 一律注入。这里只钉两件事 —— 解析正确，以及
    「拿不到就 0.0、绝不抛」（上传不能因为一个提示失败）。
    """

    def _fake(self, out=None, boom=None):
        calls = []

        def runner(cmd, cfg=None, cwd=None, **kw):
            calls.append(cmd)
            if boom:
                raise boom
            return out
        return runner, calls

    def test_parses_seconds_from_ffprobe_output(self):
        runner, calls = self._fake(out="3725.5\n")
        self.assertEqual(interview.ffprobe_duration("a.m4a", runner=runner), 3725.5)
        self.assertIn("ffprobe", calls[0][0])                      # 调的真是 ffprobe
        self.assertIn("format=duration", calls[0])                 # 只问时长
        self.assertEqual(calls[0][-1], "a.m4a")

    def test_tolerates_noise_and_blank_lines(self):
        for out, want in (("\n  3.5  \n", 3.5), ("N/A\n12.0\n", 12.0), ("0\n", 0.0)):
            runner, _ = self._fake(out=out)
            self.assertEqual(interview.ffprobe_duration("a.wav", runner=runner), want, repr(out))

    def test_returns_zero_instead_of_raising(self):
        for out in ("", "   ", "N/A", "not-a-number", None, "-5"):
            runner, _ = self._fake(out=out)
            self.assertEqual(interview.ffprobe_duration("a.wav", runner=runner), 0.0, repr(out))
        for boom in (OSError("ffprobe 不存在"), RuntimeError("超时"), ValueError("坏输出")):
            runner, _ = self._fake(boom=boom)
            self.assertEqual(interview.ffprobe_duration("a.wav", runner=runner), 0.0, repr(boom))

    def test_missing_ffprobe_is_a_silent_zero(self):
        """没装 ffprobe：静默返回 0（不抛、不阻断上传）—— 用注入的 runner 证明真的没起子进程。"""
        runner, calls = self._fake(out="1\n")
        import unittest.mock as mock
        with mock.patch.object(interview.shutil, "which", return_value=None):
            self.assertEqual(interview.ffprobe_duration("a.wav"), 0.0)
        self.assertEqual(calls, [])                                # 注入的 runner 一次都没被调


class EstimateHintTest(IsolatedCase):
    """status() 里的 estimate_hint：**只有真知道音频时长时才有这个键**，绝不编。"""

    def _status(self, **extra):
        store.save_interview_json("iv1", "status.json", dict({"state": "done"}, **extra))
        return interview.status("iv1")

    def test_hint_present_when_duration_is_known(self):
        st = self._status(audio_duration=3725.0)
        hint = st["estimate_hint"]
        self.assertEqual(hint["duration_sec"], 3725.0)
        self.assertEqual(hint["estimate_sec"], round(3725.0 * 0.9, 1))
        self.assertIn("预计 ≈", hint["text"])
        self.assertIn("音频时长 × 0.9", hint["text"])

    def test_short_audio_is_reported_in_seconds(self):
        """3 秒的片段说「预计 ≈ 1 分钟」就是错的 —— 不到一分钟按秒报。"""
        self.assertEqual(self._status(audio_duration=3.0)["estimate_hint"]["text"],
                         "预计 ≈ 3 秒（音频时长 × 0.9）")
        self.assertEqual(self._status(audio_duration=70.0)["estimate_hint"]["text"],
                         "预计 ≈ 1 分钟（音频时长 × 0.9）")

    def test_hint_absent_when_duration_is_unknown(self):
        for extra in ({}, {"audio_duration": 0}, {"audio_duration": 0.0},
                      {"audio_duration": None}, {"audio_duration": "abc"}, {"audio_duration": -3}):
            st = self._status(**extra)
            self.assertNotIn("estimate_hint", st, repr(extra))
            self.assertNotIn("预计", str(st), repr(extra))          # 整个状态里都不该冒出预计

    def test_hint_survives_state_transitions(self):
        """上传时写下的 audio_duration 必须被后续 _set_status 继承 —— 否则预计会在第一步就消失。"""
        store.save_interview_json("iv1", "status.json",
                                  {"state": "queued", "audio_duration": 600.0, "original_name": "a.m4a"})
        interview._set_status("iv1", "analyzing")
        st = interview.status("iv1")
        self.assertEqual(st["audio_duration"], 600.0)
        self.assertEqual(st["estimate_hint"]["estimate_sec"], 540.0)
        self.assertEqual(st["original_name"], "a.m4a")


class VidkitProbeTest(unittest.TestCase):
    def test_vidkit_python_uses_the_scripts_layout(self):
        self.assertEqual(interview._vidkit_python("D:/x"), os.path.join("D:/x", ".venv", "Scripts", "python.exe"))

    def test_find_vidkit_returns_none_for_missing_dir(self):
        self.assertIsNone(interview.find_vidkit({"vidkit_dir": "D:/definitely/not/here"}))


# ⚠️ 阶段 4A 的真回归：岗位背景（job_title / company / job_category）搬进了 `jds.json`，
# 而 `interview._job_of()` 原来只在 `jobs.json` 里按 id 找 —— 于是面试分析的提示词
# **静默少掉「岗位：…」「公司：…」两行**（不报错、不抛异常，只是模型少看了背景）。
# 所以这里每一条都断言到**提示词字符串**，不是只断言函数返回值。
class JobBackgroundTest(IsolatedCase):
    """岗位背景必须穿过 `jobs.json → jd_id → jds.json` 到达面试提示词。"""

    JID = "本科_金沙人力_数据标注质检_视频_成都_6-8K_0001"

    def _jd(self, **kw):
        import jdid
        jd = {"id": self.JID, "job_title": "视频标注质检", "company": "四川金沙人力资源开发管理有限公司",
              "job_category": "数据标注/质检", "status": "面试中"}
        jd.update(kw)
        jdid.upsert_jd(jd)

    def _analysis(self, analysis_id="20261012_视频标注质检_82分", **kw):
        """分析记录：**阶段 4A 之后它没有 job_title / company / job_category**。"""
        import app
        rec = {"id": analysis_id, "jd_id": self.JID, "report": analysis_id + ".html",
               "resume": "我的简历", "score": 82.0}
        rec.update(kw)
        app._save_analysis(rec)
        return analysis_id

    def _reply(self):
        return json.dumps({"summary": "s"}, ensure_ascii=False)

    def test_job_of_returns_the_jd_background_when_the_analysis_has_none(self):
        """① 分析记录里已无岗位背景 → `_job_of` 仍返回正确的 title/company/category。"""
        self._jd()
        aid = self._analysis()
        # 先钉住前提：分析记录里**真的没有**这些字段（否则这条用例什么都没验证）
        self.assertNotIn("job_title", store.find_job(aid))
        self.assertNotIn("company", store.find_job(aid))
        job = interview._job_of(aid)
        self.assertEqual(job["job_title"], "视频标注质检")
        self.assertEqual(job["company"], "四川金沙人力资源开发管理有限公司")
        self.assertEqual(job["job_category"], "数据标注/质检")
        # 分析级字段也要在（返回的是「一条岗位记录」这个既有形状，不是 JD 记录本身）
        self.assertEqual(job["id"], aid)
        self.assertEqual(job["score"], 82.0)

    def test_the_prompt_really_carries_the_job_and_company_lines(self):
        """② 面试提示词里**真的出现**「岗位：…」「公司：…」（断言字符串，不是只断言返回值）。"""
        self._jd()
        aid = self._analysis()
        chat = fake_chat_factory([self._reply()])
        interview.run_pipeline(aid, "iv1", {}, kind="text", deps={
            "runner": None,
            "chat": chat,
            "render": lambda *a, **k: "f.html",
        })
        self.assertEqual(len(chat.calls), 1)
        prompt = chat.calls[0]
        self.assertIn("岗位：视频标注质检", prompt)
        self.assertIn("公司：四川金沙人力资源开发管理有限公司", prompt)

    def test_missing_jd_record_falls_back_to_the_flat_fields_and_never_crashes(self):
        """③ 没有 JD 记录（还没迁移 / 老记录）→ 退回扁平字段；两条都缺 → 空串，不崩。"""
        store.upsert_job({"id": "老岗位_66分", "job_title": "老岗位", "company": "老公司"})
        job = interview._job_of("老岗位_66分")
        self.assertEqual((job["job_title"], job["company"]), ("老岗位", "老公司"))
        self.assertEqual(interview._job_of("根本不存在"), {})
        # 两条路都读不到时：提示词里不出现岗位/公司两行，但流水线照跑
        chat = fake_chat_factory([self._reply()])
        store.save_interview_json("iv2", "source.txt", {"x": 1})
        store.save_stream("iv2", "source.txt", io.BytesIO("你好".encode("utf-8")), max_bytes=10_000)
        interview.run_pipeline("根本不存在", "iv2", {}, kind="text",
                               deps={"chat": chat, "render": lambda *a, **k: "f.html"})
        self.assertNotIn("岗位：", chat.calls[0])
        self.assertEqual(store.load_interview_json("iv2", "status.json")["state"], "done")

    def test_interview_feedback_lands_on_the_jd_id_not_the_report_name(self):
        """反哺知识库必须记在 **JD id** 上（喂报告文件名 = 一次面试又被算成一个新岗位）。"""
        import kb
        self._jd()
        aid = self._analysis()
        kb.absorb([{"term": "RLHF"}], {"id": self.JID})
        interview.run_pipeline(aid, "iv1", {}, kind="text", deps={
            "chat": fake_chat_factory([json.dumps({"summary": "s"}, ensure_ascii=False)]),
            "render": lambda *a, **k: "f.html",
        })
        card = {n["term"]: n for n in store.load_knowledge()["nodes"]}["RLHF"]
        self.assertEqual([x.get("id") for x in card["from_jds"]], [self.JID])
        self.assertNotIn(aid, [x.get("id") for x in card["from_jds"]])
        # 幂等标记也记在 JD id 上（否则重试会再算一次）
        self.assertEqual(store.load_interview_json("iv1", "feedback.json")[self.JID]["applied"], True)


class StatusUnreadableTest(IsolatedCase):
    """`status.json` **读不出来** ≠「这条面试记录不存在」（体检 P1-23）。

    旧行为：`_read_status` 走 `store.load_interview_json(..., {})`（一切失败都兜成 `{}`）⇒
    `status()` 回 `{}` ⇒ 路由 404「找不到该面试记录」；`list_interviews` 里 `continue` ⇒
    那场面试**从列表里消失**。两处都是把"读不出来"说成别的东西。

    判据与仓库里别处**同一把尺子**（`jdid._interview_status_paths` / `jobdup._interview_status_files`）：
    `store.file_has_bytes_to_lose(path)` —— **文件不存在 / 真的是 0 字节** = 合法的"还没有状态"，
    照旧回空（对照组）；其余（有字节却读不出来）必须响亮。
    """

    def _unreadable(self, interview_id, text="{\"state\": \"done\", \"job_id\": \"中文岗位\"}"):
        path = store.interview_file(interview_id, "status.json")
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        raw = text.encode("gbk")
        with open(path, "wb") as f:
            f.write(raw)
        return path, raw

    def test_status_raises_instead_of_pretending_the_record_is_missing(self):
        path, raw = self._unreadable("iv_bad")
        self.assertGreater(os.path.getsize(path), 0, "反证：盘上确实有内容")
        self.assertRaises(UnicodeDecodeError, raw.decode, "utf-8")
        # 旧行为的形状（这一句才是"把读不出来说成别的东西"本身）：
        # 读不出来被兜成 `{}` —— 与"没有这条记录"**完全同一个信号**
        self.assertEqual(store.load_interview_json("iv_bad", "status.json", {}), {},
                         "旧行为：读不出来被兜成 {}（路由据此回 404「找不到该面试记录」）")

        with self.assertRaises(interview.RecordUnreadable) as ctx:
            interview.status("iv_bad")
        self.assertIn("status.json", str(ctx.exception), "没说是哪个文件读不出来")
        self.assertIn(os.path.basename(path), str(ctx.exception))

    def test_list_interviews_raises_instead_of_dropping_the_record(self):
        store.save_interview_json("iv_ok", "status.json", {"state": "done", "job_id": "j1"})
        self._unreadable("iv_bad")
        with self.assertRaises(interview.RecordUnreadable) as ctx:
            interview.list_interviews("j1")
        self.assertIn("status.json", str(ctx.exception))

    def test_a_missing_status_file_is_still_empty_and_never_raises(self):
        """对照组：**确实没有**照旧 `{}` / 空列表（既有语义一个字不改）。"""
        self.assertFalse(os.path.exists(store.interview_file("nope", "status.json")))
        self.assertEqual(interview.status("nope"), {})
        self.assertEqual(interview.list_interviews("nope"), [])

    def test_a_zero_byte_status_file_is_still_empty(self):
        """0 字节 = 里面一个字节都没有（与 store 的写盘判据同一把尺子）⇒ 照旧 `{}`。"""
        store.save_interview_json("iv0", "status.json", {"state": "done"})
        open(store.interview_file("iv0", "status.json"), "wb").close()
        self.assertEqual(interview.status("iv0"), {})

    def test_a_non_object_status_file_is_unreadable_not_missing(self):
        """结构不对（JSON 能解析、但不是对象）同样是"读不出来" —— 与 `read_knowledge_strict` 同一口径。"""
        store.save_interview_json("iv_arr", "status.json", ["不是对象"])
        with self.assertRaises(interview.RecordUnreadable):
            interview.status("iv_arr")

    def test_a_readable_record_still_returns_the_same_shape(self):
        """对照组：读得出来时形状不变（state / interrupted / estimate_hint 三条分支照旧）。"""
        store.save_interview_json("iv1", "status.json",
                                  {"state": "transcribing", "started_at": "2026-01-01 00:00:00"})
        self.assertEqual(interview.status("iv1")["state"], "interrupted")   # 没有活动线程
        store.save_interview_json("iv2", "status.json", {"state": "done", "job_id": "j2"})
        self.assertEqual(interview.list_interviews("j2"), ["iv2"])


if __name__ == "__main__":
    unittest.main()
