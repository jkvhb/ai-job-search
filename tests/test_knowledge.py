# -*- coding: utf-8 -*-
import unittest

import knowledge
import log
import store

from tests.base import IsolatedCase as _Isolated   # noqa: E402


def fake_chat_factory(responses):
    calls = []

    def fake_chat(base_url, api_key, model, messages, **kw):
        calls.append(messages)
        return responses.pop(0)

    fake_chat.calls = calls
    return fake_chat


class ExtractTest(_Isolated):
    def test_extract_returns_nodes(self):
        chat = fake_chat_factory(['{"nodes":[{"id":"rlhf","term":"RLHF","definition":"d",'
                                  '"plain_explanation":"p","category":"核心概念"}]}'])
        nodes = knowledge.extract_nodes("JD文本", {"text_model": {"base_url": "u", "api_key": "k",
                                                                  "model": "m"}}, chat=chat)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["id"], "rlhf")
        self.assertEqual(nodes[0]["layer"], 0)

    def test_extract_assigns_layer_zero(self):
        chat = fake_chat_factory(['{"nodes":[{"id":"a","term":"A","definition":"d","plain_explanation":"p"}]}'])
        nodes = knowledge.extract_nodes("JD", {"text_model": {"base_url": "u", "api_key": "k", "model": "m"}},
                                        chat=chat)
        self.assertEqual(nodes[0]["layer"], 0)

    def test_extract_handles_empty(self):
        chat = fake_chat_factory(['{"nodes":[]}'])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u", "api_key": "k",
                                                                       "model": "m"}}, chat=chat), [])


class SourceMergeTest(_Isolated):
    def test_merge_sources_dedupes_by_url(self):
        a = [{"url": "https://x", "title": "T"}]
        b = [{"url": "https://x", "title": "T2"}, {"url": "https://y", "title": "T3"}]
        out = knowledge.merge_sources(a, b)
        self.assertEqual(len(out), 2)
        self.assertEqual({s["url"] for s in out}, {"https://x", "https://y"})


class BrainstormTest(_Isolated):
    def test_brainstorm_adds_layer1_nodes(self):
        chat = fake_chat_factory(['{"related":[{"id":"sft","term":"SFT","definition":"d",'
                                  '"plain_explanation":"p","relation":"前置"}]}'])
        core = [knowledge.normalize_node({"id": "rlhf", "term": "RLHF", "definition": "d",
                                          "plain_explanation": "p"}, 0)]
        nodes = knowledge.brainstorm(core, {"text_model": {"base_url": "u", "api_key": "k",
                                                           "model": "m"}}, chat=chat)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["layer"], 1)
        self.assertEqual(core[0]["related"][0]["id"], "sft")

    def test_brainstorm_skips_when_empty(self):
        self.assertEqual(knowledge.brainstorm([], {"text_model": {"base_url": "u", "api_key": "k",
                                                                 "model": "m"}}), [])


class SourceAndTimelineTest(_Isolated):
    def test_attach_sources_marks_verified(self):
        def fake_search(query, providers=None, **kw):
            # 来源必须是本领域内容，否则会被 filter_relevant 丢弃（见 SearchQualityTest）
            return [{"title": "大模型数据标注规范", "url": "https://x", "snippet": "语料标注流程", "date": "2022"}]

        nodes = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                           "plain_explanation": "p"})]
        out = knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                       search=fake_search)
        self.assertEqual(out[0]["confidence"], "verified")
        self.assertEqual(out[0]["sources"][0]["url"], "https://x")

    def test_attach_sources_marks_ai_when_no_result(self):
        def empty_search(query, providers=None, **kw):
            return []

        nodes = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                           "plain_explanation": "p"})]
        out = knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                       search=empty_search)
        self.assertEqual(out[0]["confidence"], "ai-generated")
        self.assertEqual(out[0]["sources"], [])

    def test_attach_timeline_when_verified(self):
        chat = fake_chat_factory(['{"timeline":[{"year":"2022","text":"ChatGPT 发布"}]}'])
        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x", "snippet": "s", "date": ""}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=chat)
        self.assertEqual(out[0]["timeline"][0]["year"], "2022")


class ResilienceTest(_Isolated):
    def test_brainstorm_survives_non_list_related(self):
        # 合法 JSON 但 related 不是数组 → 不能抛，其他核心点要照常处理
        chat = fake_chat_factory(['{"related":"hello"}',
                                  '{"related":[{"id":"b1","term":"B1","definition":"d",'
                                  '"plain_explanation":"p","relation":"前置"}]}'])
        core = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                          "plain_explanation": "p"}, 0),
                knowledge.normalize_node({"id": "b", "term": "B", "definition": "d",
                                          "plain_explanation": "p"}, 0)]
        out = knowledge.brainstorm(core, {"text_model": {"base_url": "u", "api_key": "k",
                                                         "model": "m"}}, chat=chat)
        self.assertEqual([n["id"] for n in out], ["b1"])

    def test_extract_returns_empty_on_unparseable(self):
        chat = fake_chat_factory(["完全不是 JSON"])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u",
                                                                       "api_key": "k", "model": "m"}},
                                                 chat=chat), [])

    def test_extract_returns_empty_on_non_list_nodes(self):
        chat = fake_chat_factory(['{"nodes":"oops"}'])
        self.assertEqual(knowledge.extract_nodes("JD", {"text_model": {"base_url": "u",
                                                                       "api_key": "k", "model": "m"}},
                                                 chat=chat), [])

    def test_attach_timeline_survives_bad_snippet_type(self):
        chat = fake_chat_factory(['{"timeline":[{"year":"2022","text":"X"}]}'])
        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x", "snippet": {"unexpected": "shape"}}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=chat)
        self.assertEqual(out[0]["timeline"][0]["year"], "2022")

    def test_attach_timeline_skips_sourceless_without_calling_chat(self):
        def must_not_call(*a, **kw):
            raise AssertionError("对没有来源的节点绝不该调用模型")

        nodes = [{"id": "a", "term": "A", "definition": "d", "plain_explanation": "p",
                  "sources": [], "confidence": "ai-generated", "timeline": [], "related": [], "layer": 0}]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}}, chat=must_not_call)
        self.assertEqual(out[0]["timeline"], [])

    def test_attach_timeline_parallel_matches_sequential(self):
        import threading as _threading
        lock = _threading.Lock()

        def fake_chat(base_url, api_key, model, messages, **kw):
            with lock:
                return '{"timeline":[{"year":"2022","text":"X"}]}'

        nodes = [{"id": "n%d" % i, "term": "N%d" % i, "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x%d" % i, "snippet": "s"}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 0}
                 for i in range(8)]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}},
                                        chat=fake_chat, max_workers=4)
        self.assertEqual(len(out), 8)
        self.assertTrue(all(n["timeline"] for n in out))

    def test_attach_timeline_cap(self):
        def fake_chat(base_url, api_key, model, messages, **kw):
            return '{"timeline":[{"year":"2022","text":"X"}]}'

        nodes = [{"id": "n%d" % i, "term": "N%d" % i, "definition": "d", "plain_explanation": "p",
                  "sources": [{"title": "T", "url": "https://x%d" % i, "snippet": "s"}],
                  "confidence": "verified", "timeline": [], "related": [], "layer": 1 if i >= 3 else 0}
                 for i in range(6)]
        out = knowledge.attach_timeline(nodes, {"text_model": {"base_url": "u", "api_key": "k",
                                                               "model": "m"}},
                                        chat=fake_chat, max_timelines=3)
        with_tl = [n for n in out if n["timeline"]]
        self.assertEqual(len(with_tl), 3)
        # 核心节点优先（layer 0 先于 layer 1）
        self.assertTrue(all(n["layer"] == 0 for n in with_tl))


class SearchQualityTest(_Isolated):
    def test_build_query_is_bare_term_without_domain_anchor(self):
        """查询**刻意不加领域词**——实测数据（2026-10-08，5 术语 × 2 写法，真实 Tavily）：

        指标 =「既切题（标题/摘要字面含术语）又属本领域」的结果数
          A `{t} 是什么`       合计 8/15
          B `{t} 大模型 AI`    合计 5/15（省略恢复 3→0、抽样检验 1→0）
        机理：查询里出现「大模型 AI」时，Tavily 返回的是泛 AI 页面
        （「15款大模型透明度测评」这类），与术语无关；而 filter_relevant 只验领域、
        **不验切题**，这些页面会通过过滤并被标成「有来源」——
        比原来那条 Microsoft 的错更危险（伪装成可信来源）。
        引号变体 C≡A、D≡B 逐条相同（引号无效）；定义消歧策略 E 只有 2/9，比 A 更差。
        → 不存在好的领域锚定查询；过滤是主护栏，查询保持裸术语。
        """
        q = knowledge.build_query({"term": "省略恢复"})
        self.assertEqual(q, "省略恢复 是什么")
        self.assertNotIn("大模型", q)
        self.assertNotIn("AI", q)
        # 锚定常量必须已被删除，防止后人加回去
        self.assertFalse(hasattr(knowledge, "DOMAIN_TAG"))

    def test_filter_drops_off_domain_results(self):
        # 夹具是真实抓到的反例：用户报告 data/profiles/default/reports/
        # 20261008_语音文本评测_72分.html 里知识点「省略恢复」的第 3 条来源（原文照抄，
        # snippet 为原文前 ~180 字节选，全文 1435 字）。旧代码把 Office 的「撤销/恢复操作」
        # 当成「省略恢复」的资料来源，正是必须修掉的缺陷。
        raw = [{"title": "撤销、恢复或重复操作 | Microsoft Support",
                "url": "https://support.microsoft.com/zh-cn/office/foundations-experiences/"
                       "undo-redo-or-repeat-an-action",
                "snippet": "## Global\n\n# 撤销、恢复或重复操作\n\n可在 Microsoft Word、PowerPoint 和 Excel "
                           "中撤消、恢复或重复许多操作。 只要不超过撤消限制，可在保存后撤消更改，然后再次保存"
                           "（默认情况下，Office 保存最近 100 个可撤消操作）。\n\n### 撤消操作\n\n若要撤消操作，"
                           "请按键盘上的 Ctrl+Z ，或在快速访问工具栏上选择“ 撤消 ”。"},
               {"title": "大模型数据标注中的省略恢复", "url": "https://ai", "snippet": "在语音对话数据里补齐省略成分"}]
        kept = knowledge.filter_relevant(raw)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], "https://ai")

    def test_filter_drops_electrical_transformer_keeps_deep_learning_one(self):
        """同名跨领域漏洞的验收靶心：电力「变压器」必须丢，深度学习 Transformer 必须留。

        两条都是真实来源原文（data/profiles/default/reports/
        20261008_语音文本评测_72分.html 里知识点「Transformer 架构」的第 2、3 条来源）。
        旧规则只有一层关键词，"transformer" 在 DOMAIN_KEYWORDS 里 → 电力件也被判成
        「本领域」，这正是必须修掉的同名漏洞。
        新规则：命中弱关键词 transformer，同时命中否决词（voltage / winding）→ 丢弃；
        Transformer (deep learning) 命中强关键词 deep learning → 保留。
        """
        electrical = {"title": "What is a Transformer? Types, Working Principle, and Applications",
                      "url": "https://plcblog.in/electrical/transformer/"
                             "transformer-working-types-applications.php",
                      "snippet": "In a shell type transformer, the magnetic core completely surrounds "
                                 "the windings. Both the low and high-voltage windings are placed on the "
                                 "central limb, and the magnetic flux splits into two parallel paths in "
                                 "the outer limbs. This structure is compact and stronger mechanically. "
                                 "[...] The core of a transformer is made of thin silicon steel sheets "
                                 "(laminations) to reduce energy losses. Since steel is a conductor, a "
                                 "changing magnetic field can induce unwanted currents called eddy "
                                 "currents inside the core, which cause heating and energy loss. [...] "
                                 "In a core type transformer, the windings (coils) are placed around two "
                                 "vertical limbs of a rectangular magnetic core."}
        deep_learning = {"title": "Transformer (deep learning)",
                         "url": "https://en.wikipedia.org/wiki/Transformer_(deep_learning)",
                         "snippet": "A transformer is a deep learning architecture"}
        kept = knowledge.filter_relevant([electrical, deep_learning])
        self.assertEqual([s["url"] for s in kept],
                         ["https://en.wikipedia.org/wiki/Transformer_(deep_learning)"])
        # 靶心的那一半：电力变压器单独喂进去也必须为 0
        self.assertEqual(knowledge.filter_relevant([electrical]), [])

    def test_filter_keeps_weak_keyword_when_no_veto_signal(self):
        # 弱关键词单独命中不算数，但**没有否决词时必须保留**——否则会误杀真 AI 来源
        # （原文：Attention Is All You Need，摘要里只有 transformer，无强关键词）
        raw = [{"title": "Attention Is All You Need", "url": "https://arxiv.org/abs/1706.03762",
                "snippet": "We propose a new simple network architecture, the Transformer, "
                           "based solely on attention mechanisms"}]
        self.assertEqual(len(knowledge.filter_relevant(raw)), 1)

    def test_filter_keeps_english_ai_sources(self):
        # 反向回归：英文 AI 来源不能被误杀
        raw = [{"title": "Transformer (deep learning)",
                "url": "https://en.wikipedia.org/wiki/Transformer_(deep_learning)",
                "snippet": "A transformer is a deep learning architecture"},
               {"title": "Attention Is All You Need", "url": "https://arxiv.org/abs/1706.03762",
                "snippet": "We propose a new simple network architecture, the Transformer, "
                           "based solely on attention mechanisms"}]
        self.assertEqual(len(knowledge.filter_relevant(raw)), 2)

    def test_filter_keeps_chinese_ai_terms_without_english_hits(self):
        # 反向回归：漏词会误杀正确来源——自然语言／强化学习／OpenAI 都不能丢
        raw = [{"title": "自然语言处理入门", "url": "https://x1", "snippet": "自然语言处理是人工智能的分支"},
               {"title": "强化学习基础", "url": "https://x2", "snippet": "强化学习通过奖励信号学习策略"},
               {"title": "OpenAI 发布新模型", "url": "https://x3", "snippet": "OpenAI announced a new model"}]
        self.assertEqual(len(knowledge.filter_relevant(raw)), 3)

    def test_filter_keeps_empty_as_empty(self):
        self.assertEqual(knowledge.filter_relevant([]), [])

    def test_filter_ascii_short_keywords_need_word_boundary(self):
        # 短 ASCII 关键词的子串误命中：available 里的 ai、storage/paragraph 里的 rag
        # （注意 "agent" 是 5 字母，按规则仍走子串匹配——见汇报里的已知残留风险）
        raw = [{"title": "This feature is available in Microsoft Teams", "url": "https://n1",
                "snippet": "email detail"},
               {"title": "Cloud storage and paragraph formatting", "url": "https://n2",
                "snippet": "management report"},
               {"title": "RAG 检索增强生成实践", "url": "https://y1", "snippet": "用 LLMs 做检索增强"},
               {"title": "AI 数据标注", "url": "https://y2", "snippet": ""}]
        kept = knowledge.filter_relevant(raw)
        self.assertEqual([s["url"] for s in kept], ["https://y1", "https://y2"])

    def test_attach_sources_marks_ai_generated_when_all_filtered_out(self):
        def only_off_domain(query, providers=None, **kw):
            return [{"title": "Microsoft 撤销回复", "url": "https://ms", "snippet": "撤销操作指南"}]

        nodes = [knowledge.normalize_node({"id": "a", "term": "省略恢复", "definition": "d",
                                           "plain_explanation": "p"})]
        out = knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                       search=only_off_domain)
        self.assertEqual(out[0]["confidence"], "ai-generated")
        self.assertEqual(out[0]["sources"], [])

    def test_attach_sources_respects_max_searches(self):
        calls = []

        def counting(query, providers=None, **kw):
            calls.append(query)
            return [{"title": "大模型 A", "url": "https://a", "snippet": "模型"}]

        nodes = [{"id": "n%d" % i, "term": "T%d" % i, "definition": "d", "plain_explanation": "p",
                  "sources": [], "confidence": "ai-generated", "timeline": [], "related": [],
                  "layer": 1 if i >= 2 else 0} for i in range(5)]
        knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                 search=counting, max_searches=2)
        self.assertEqual(len(calls), 2)
        # 核心优先：被搜的应该是 layer 0 的那两个
        self.assertTrue(all("T0" in c or "T1" in c for c in calls))

    def test_attach_sources_asks_for_five_candidates(self):
        # Tavily 按**请求**计费、不按条数 → 多拿候选是纯赚（过滤后能留下更多）
        seen = {}

        def counting(query, providers=None, **kw):
            seen.update(kw)
            return [{"title": "大模型 A", "url": "https://a", "snippet": "模型"}]

        nodes = [knowledge.normalize_node({"id": "a", "term": "A", "definition": "d",
                                           "plain_explanation": "p"})]
        knowledge.attach_sources(nodes, [{"name": "t", "base_url": "u", "api_key": "k"}],
                                 search=counting)
        self.assertEqual(seen.get("max_results"), 5)


if __name__ == "__main__":
    unittest.main()
