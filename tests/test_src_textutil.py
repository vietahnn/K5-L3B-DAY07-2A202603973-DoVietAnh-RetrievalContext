"""Unit tests for the re-implemented ingestion and retrieval code.

These are offline checks of behaviour, not of benchmark quality. They run
without a model, without a vector store and without network access, so they
can be run before the expensive setup.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent import OpenAICompatibleController
from src.benchmark import (
    BenchmarkError,
    is_hit,
    match_entry,
    score_context,
    score_ranking,
    validate_questions,
)
from src.chunking import build_chunks, split_sentences, structure_chunks
from src.llm import parse_json_object
from src.models import DocumentPage
from src.retrievers import BM25Index, reciprocal_rank_fusion
from src.textutil import evidence_present, normalize_text, tokenize


class TextUtilTests(unittest.TestCase):
    def test_normalize_keeps_paragraphs_and_folds_typography(self):
        text = "One ﬁ line\nwrapped here\n\nSecond para here"
        result = normalize_text(text)
        self.assertIn("One fi line wrapped here", result)
        self.assertIn("Second para here", result)
        self.assertIn("\n\n", result)

    def test_canon_absorbs_pdf_line_wrap_artifacts(self):
        quote = "upper-middle-income economies"
        wrapped = "upper-middle- income economies"
        self.assertTrue(evidence_present(wrapped, quote))

    def test_evidence_present_tolerates_trailing_period(self):
        self.assertTrue(evidence_present("the rate fell to 11 percent", "fell to 11 percent."))
        self.assertFalse(evidence_present("the rate fell to 12 percent", "fell to 11 percent"))

    def test_tokenize_drops_stopwords_and_keeps_numbers(self):
        tokens = tokenize("How many people live on less than $8.30 a day?")
        self.assertIn("8.30", tokens)
        self.assertIn("people", tokens)
        self.assertNotIn("the", tokens)
        self.assertNotIn("and", tokens)


class SentenceSplitTests(unittest.TestCase):
    def test_decimal_point_is_not_a_boundary(self):
        sentences = split_sentences("Emissions rose 2.4 percentage points in 2024. Next fact.")
        self.assertEqual(len(sentences), 2)

    def test_quoted_title_period_is_not_a_boundary(self):
        text = 'See the Poverty, Prosperity, and Planet Report 2026: Jobs at the Center.'
        self.assertEqual(len(split_sentences(text)), 1)

    def test_ordinary_sentences_split(self):
        self.assertEqual(len(split_sentences("First one. Second one. Third one.")), 3)


class ChunkingTests(unittest.TestCase):
    def _page(self):
        return DocumentPage(
            document_id="doc",
            source="doc.pdf",
            page=4,
            text="",
            metadata={"section": "Overview", "topic": "Jobs"},
            blocks=[
                {"kind": "running_head", "text": "Overview"},
                {"kind": "subheading", "text": "Better-paying work"},
                {"kind": "body", "text": "First paragraph of the section. " * 12},
                {"kind": "body", "text": "Second paragraph of the section. " * 12},
            ],
        )

    def test_chunks_carry_heading_and_respect_the_cap(self):
        chunks = structure_chunks(self._page(), max_chars=700)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertTrue(chunk.text.startswith("Better-paying work"))
            self.assertLessEqual(len(chunk.text), 700 + len("Better-paying work") + 2)

    def test_chunk_ids_are_unique_and_traceable(self):
        chunks = build_chunks([self._page()], "structure", max_chars=700)
        ids = [c.chunk_id for c in chunks]
        self.assertEqual(len(ids), len(set(ids)))
        for chunk in chunks:
            self.assertEqual(chunk.page, 4)
            self.assertEqual(chunk.document_id, "doc")

    def test_fixed_strategy_is_a_different_boundary(self):
        page = self._page()
        page.text = "x" * 1200
        structure = build_chunks([page], "structure", max_chars=700)
        fixed = build_chunks([page], "fixed", size=500, overlap=50)
        self.assertNotEqual([c.text for c in structure], [c.text for c in fixed])


class BM25Tests(unittest.TestCase):
    def _chunks(self):
        return [
            {
                "id": "c1",
                "text": "In 2026, an estimated 811 million people live below $3.00 a day.",
                "metadata": {"section": "Overview"},
                "source_id": "doc",
                "source": "doc.pdf",
                "page": 25,
            },
            {
                "id": "c2",
                "text": "The global median income is $9.20 a day.",
                "metadata": {"section": "Overview"},
                "source_id": "doc",
                "source": "doc.pdf",
                "page": 25,
            },
        ]

    def test_exact_term_match_ranks_first(self):
        index = BM25Index(self._chunks())
        results = index.search("811 million people extreme poverty", top_k=2)
        self.assertEqual(results[0]["id"], "c1")
        self.assertGreater(results[0]["score"], 0)

    def test_no_match_returns_nothing(self):
        index = BM25Index(self._chunks())
        self.assertEqual(index.search("zzzznonexistentterm", top_k=2), [])

    def test_filters_are_respected(self):
        index = BM25Index(self._chunks())
        results = index.search("811 million", filters={"section": "Foreword"}, top_k=2)
        self.assertEqual(results, [])

    def test_scores_are_descending(self):
        index = BM25Index(self._chunks())
        results = index.search("million people day income", top_k=2)
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


class FusionTests(unittest.TestCase):
    def test_rrf_rewards_documents_found_by_both_arms(self):
        dense = [{"id": "both"}, {"id": "dense-only"}]
        lexical = [{"id": "both"}]
        fused = reciprocal_rank_fusion([dense, lexical], k=60)
        self.assertEqual(fused[0]["id"], "both")
        # A document found by both arms outranks one found by a single arm.
        self.assertGreater(fused[0]["score"], fused[1]["score"])

    def test_absent_document_contributes_nothing(self):
        fused = reciprocal_rank_fusion([[{"id": "a"}], [{"id": "b"}]], k=60)
        self.assertEqual(fused[0]["score"], fused[1]["score"])


class BenchmarkTests(unittest.TestCase):
    def _question(self):
        return {
            "id": "q1",
            "split": "dev",
            "question": "How many people live in extreme poverty?",
            "expected_answer": "811 million",
            "gold": [
                {"source_id": "doc", "page": 9, "evidence": "an estimated 811 million people"},
                {"source_id": "doc", "page": 25, "evidence": "811 million people"},
            ],
        }

    def _chunk(self, chunk_id, page, text):
        return {
            "id": chunk_id,
            "page": page,
            "source_id": "doc",
            "metadata": {"document_id": "doc"},
            "text": text,
        }

    def test_hit_needs_the_right_page_and_the_right_quote(self):
        question = self._question()
        right = self._chunk("c1", 9, "In 2026, an estimated 811 million people live below the line.")
        wrong_page = self._chunk("c2", 3, "In 2026, an estimated 811 million people live below the line.")
        wrong_quote = self._chunk("c3", 9, "The number is 700 million people.")
        self.assertTrue(is_hit(right, question))
        self.assertFalse(is_hit(wrong_page, question))
        self.assertFalse(is_hit(wrong_quote, question))

    def test_alternative_gold_sources_both_count(self):
        question = self._question()
        alternative = self._chunk("c1", 25, "Currently 811 million people are poor.")
        self.assertTrue(is_hit(alternative, question))

    def test_score_ranking_reports_ranks(self):
        question = self._question()
        results = [
            self._chunk("c1", 3, "Unrelated text about something else."),
            self._chunk("c2", 9, "An estimated 811 million people live below the line."),
        ]
        scores = score_ranking(results, question)
        self.assertFalse(scores["hit_at_1"])
        self.assertTrue(scores["evidence_hit_at_3"])
        self.assertEqual(scores["first_hit_rank"], 2)

    def test_context_scoring_needs_the_evidence_in_the_context(self):
        question = self._question()
        results = [
            self._chunk("c1", 9, "An estimated 811 million people live below the line."),
            self._chunk("c2", 4, "Some other passage entirely."),
        ]
        self.assertTrue(score_context(["c1"], results, question))
        self.assertFalse(score_context(["c2"], results, question))
        self.assertFalse(score_context([], results, question))

    def test_no_evidence_questions_are_valid(self):
        questions = [
            {
                "id": "n1",
                "split": "dev",
                "question": "What is the poverty rate in Vietnam?",
                "answerable": False,
                "absence_reason": "no mention in the corpus",
            }
        ]
        validate_questions(questions)

    def test_invalid_questions_are_rejected(self):
        with self.assertRaises(BenchmarkError):
            validate_questions([{"id": "", "split": "dev", "question": "x"}])
        with self.assertRaises(BenchmarkError):
            validate_questions(
                [{"id": "a", "split": "dev", "question": "x", "gold": [], "expected_answer": "y"}]
            )


class LLMParsingTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_json_object('{"action": "search"}'), {"action": "search"})

    def test_fenced_json(self):
        text = 'Here you go:\n```json\n{"action": "answer", "citations": []}\n```'
        self.assertEqual(parse_json_object(text)["action"], "answer")

    def test_json_embedded_in_prose(self):
        text = 'Sure! {"action": "search", "query": "poverty"} hope that helps'
        self.assertEqual(parse_json_object(text)["query"], "poverty")

    def test_no_json_raises(self):
        with self.assertRaises(Exception):
            parse_json_object("there is no json here")

    def test_prose_with_citation_becomes_an_answer(self):
        """A model that answers in prose still produces a usable answer."""
        text = "Nearly half of humanity - about 3.7 billion people [S:doc:p025:c0002]."
        payload = parse_json_object(text)
        self.assertEqual(payload["action"], "answer")
        self.assertIn("[S:doc:p025:c0002]", payload["answer"])

    def test_malformed_json_is_repaired(self):
        """Unescaped quotes in a query must not cost the whole question."""
        text = '{"action":" "search", "query": "\\"new data\\" \\"economies\\""}'
        payload = parse_json_object(text)
        self.assertEqual(payload["action"], "search")
        self.assertIn("new data", payload["query"])


class ControllerCitationTests(unittest.TestCase):
    """The controller must not drop a citation the model already gave."""

    def test_missing_inline_citation_is_appended(self):
        answer = OpenAICompatibleController._ensure_inline_citations(
            "1.5 billion people.", ["S:doc:p020:c0001"]
        )
        self.assertIn("[S:doc:p020:c0001]", answer)

    def test_present_inline_citation_is_left_alone(self):
        original = "1.5 billion people [S:doc:p020:c0001]."
        answer = OpenAICompatibleController._ensure_inline_citations(
            original, ["S:doc:p020:c0001"]
        )
        self.assertEqual(answer, original)

    def test_no_citations_leaves_answer_unchanged(self):
        original = "Không đủ bằng chứng."
        self.assertEqual(
            OpenAICompatibleController._ensure_inline_citations(original, []), original
        )


class FixtureTests(unittest.TestCase):
    """The starter's fixture contract must still hold."""

    def test_fixture_gold_resolves(self):
        chunks = json.loads((ROOT / "data" / "example_chunks.json").read_text(encoding="utf-8"))
        questions = json.loads(
            (ROOT / "data" / "example_questions.json").read_text(encoding="utf-8")
        )
        for question in questions:
            for gold in question["gold"]:
                self.assertTrue(
                    any(
                        c["source_id"] == gold["source_id"]
                        and c["page"] == gold["page"]
                        and gold["evidence"] in c["text"]
                        for c in chunks
                    )
                )


if __name__ == "__main__":
    unittest.main()
