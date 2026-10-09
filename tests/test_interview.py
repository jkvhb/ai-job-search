# -*- coding: utf-8 -*-
"""interview.py —— 面试流水线：转写 → AI 分析 → 反哺知识库 → 渲染复盘报告。

铁律：**绝不真跑 vidkit、绝不真调模型**。转写走外部 venv 子进程（本项目零依赖），
在本测试里 runner 一律注入假的；模型调用同理走假 chat。
"""
import io
import json
import os
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
        interview.run_pipeline("j1", "iv1", {}, deps=self._deps())
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
        interview.run_pipeline("j1", "iv1", {}, deps=self._deps())
        self.assertEqual(seen, ["transcribing", "analyzing", "done"])

    def test_failure_sets_error_state_and_keeps_the_audio(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", {}, deps=self._deps(boom="transcribe"))
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["state"], "error")
        self.assertIn("vidkit", st.get("error", ""))
        self.assertTrue(os.path.exists(store.interview_file("iv1", "audio.wav")))   # 绝不删录音

    def test_failure_does_not_leave_a_transcript(self):
        """转写失败时绝不能写出半截 transcript.json —— 否则重试会「跳过转写」拿空文本去分析。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", {}, deps=self._deps(boom="transcribe"))
        self.assertFalse(os.path.exists(store.interview_file("iv1", "transcript.json")))

    def test_retry_skips_transcription_when_transcript_exists(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        store.save_interview_json("iv1", "transcript.json", {"text": "已有的转写", "segments": []})
        called = []
        deps = self._deps()
        deps["runner"] = lambda cmd, **kw: called.append(cmd) or ""
        interview.run_pipeline("j1", "iv1", {}, deps=deps)
        self.assertEqual(called, [])                              # 没有再跑转写
        self.assertEqual(store.load_interview_json("iv1", "transcript.json")["text"], "已有的转写")

    def test_text_upload_skips_transcription(self):
        store.save_interview_json("iv1", "source.txt", {"x": 1})
        store.save_stream("iv1", "source.txt", io.BytesIO("面试官：你好".encode("utf-8")), max_bytes=10_000)
        called = []
        deps = self._deps()
        deps["runner"] = lambda cmd, **kw: called.append(cmd) or ""
        interview.run_pipeline("j1", "iv1", {}, kind="text", deps=deps)
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
        interview.run_pipeline("j1", "iv1", {}, kind="text", deps=self._deps())
        self.assertNotIn("transcribing", seen)

    def test_missing_audio_raises_and_keeps_error_state(self):
        with self.assertRaises(RuntimeError):
            interview.run_pipeline("j1", "iv1", {}, deps=self._deps())
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "error")

    def test_analysis_failure_keeps_audio_and_transcript(self):
        """模型环节挂了：录音与已转写内容都要留着，重试只需重跑分析。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        deps = self._deps()
        deps["chat"] = fake_chat_factory(["完全不是 JSON"])
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", {}, deps=deps)
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
        interview.run_pipeline("j1", "iv1", {}, deps=deps)
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
        interview.run_pipeline("j1", "iv1", {}, deps=deps)
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
        interview.run_pipeline("j1", "iv1", {}, kind="text", deps=deps)
        path = os.path.join(store.p_path("reports"), "iv_iv1.html")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(store.load_interview_json("iv1", "status.json")["state"], "done")
        self.assertIn("interview", store.read_text(path))

    def test_start_runs_in_a_thread_and_reaches_a_terminal_state(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        t = interview.start("j1", "iv1", {}, deps=self._deps())
        self.assertTrue(t.daemon)                       # 主程序退出不该被转写线程拖住
        deadline = time.time() + 10
        while time.time() < deadline:
            if interview.status("iv1").get("state") in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(interview.status("iv1")["state"], "done")
        self.assertFalse(interview.is_running("iv1"))

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
        t = interview.start("j1", "iv1", {}, deps=deps)
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
        interview.run_pipeline("j1", "iv1", {}, deps=self._deps())
        events = [json.loads(l)["event"] for l in store.read_text(log.LOG_FILE).splitlines() if l.strip()]
        self.assertIn("interview.pipeline_start", events)
        self.assertIn("interview.pipeline_done", events)

    def test_pipeline_logs_error_on_failure(self):
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        with self.assertRaises(Exception):
            interview.run_pipeline("j1", "iv1", {}, deps=self._deps(boom="transcribe"))
        events = [json.loads(l)["event"] for l in store.read_text(log.LOG_FILE).splitlines() if l.strip()]
        self.assertIn("interview.pipeline_error", events)

    def test_status_records_audio_metadata(self):
        """上传时的元数据（原始文件名/字节数）必须被后续状态写入继承下来 —— 前端要显示它。"""
        store.save_stream("iv1", "audio.wav", io.BytesIO(b"x" * 100), max_bytes=10_000)
        store.save_interview_json("iv1", "status.json",
                                  {"state": "queued", "original_name": "面试录音.m4a", "audio_bytes": 100})
        interview.run_pipeline("j1", "iv1", {}, deps=self._deps())
        st = store.load_interview_json("iv1", "status.json")
        self.assertEqual(st["original_name"], "面试录音.m4a")
        self.assertEqual(st["audio_bytes"], 100)
        self.assertEqual(st["state"], "done")
        self.assertGreaterEqual(st["elapsed"], 0)


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


class VidkitProbeTest(unittest.TestCase):
    def test_vidkit_python_uses_the_scripts_layout(self):
        self.assertEqual(interview._vidkit_python("D:/x"), os.path.join("D:/x", ".venv", "Scripts", "python.exe"))

    def test_find_vidkit_returns_none_for_missing_dir(self):
        self.assertIsNone(interview.find_vidkit({"vidkit_dir": "D:/definitely/not/here"}))


if __name__ == "__main__":
    unittest.main()
