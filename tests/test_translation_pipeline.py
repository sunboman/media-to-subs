import json
import unittest

from media_to_subs.config import LLMConfig
from media_to_subs.errors import CancelledError, ProviderError
from media_to_subs.llm import TranslationValidationError
from media_to_subs.runtime import Cancellation
from media_to_subs.translator import translate
from media_to_subs.types import Segment, TimedTranscript
from test_llm_http import HTTPFixture, chat_response


class TranslationTests(unittest.TestCase):
    def transcript(self, count=3, overlaps=False):
        return TimedTranscript("en", tuple(Segment(f"cue-{i}", i + 0.125, i + (1.5 if overlaps else 1), f"Example {i}")
                                            for i in range(count)),
                               {"source_path": "/private/never-send-this.mkv", "stream": 2},
                               allow_overlaps=overlaps)

    def config(self, server, **options):
        return LLMConfig(url=server.url, model="synthetic", read_timeout=3, **options)

    def response(self, ids, wrapper=False):
        raw = json.dumps({"translations": [{"id": i, "text": "示例"} for i in reversed(ids)]})
        return chat_response('```json\n' + raw + '\n```' if wrapper else raw)

    def test_batch_preserves_exact_order_times_ids_overlap_and_no_content_paths(self):
        source = self.transcript(overlaps=True)
        events = []
        with HTTPFixture([self.response(["cue-0", "cue-1"], True), self.response(["cue-2"])]) as server:
            result = translate(source, self.config(server, batch_size=2), {"Example": "示例"}, Cancellation(), events.append)
            self.assertEqual([(s.id, s.start, s.end) for s in result.segments], [(s.id, s.start, s.end) for s in source.segments])
            self.assertEqual([s.text for s in result.segments], ["示例"] * 3)
            self.assertTrue(result.allow_overlaps)
            self.assertEqual(result.language, "zh")
            self.assertEqual(result.provenance["stream"], 2)
            self.assertEqual(result.provenance["translation"]["source_language"], "en")
            self.assertEqual(source.segments[0].text, "Example 0")
            self.assertEqual(len(server.requests), 2)
            request_text = json.dumps(server.requests)
            self.assertNotIn("never-send-this", request_text)
            self.assertNotIn("source_path", request_text)
            context = json.loads(server.requests[0][2]["messages"][1]["content"])["context"]
            self.assertIn("Example 2", context)
            self.assertTrue(events)

    def test_silence_and_chinese_skip_all_provider_checks_and_calls(self):
        with HTTPFixture([]) as server:
            for source in [TimedTranscript("und", (), {}), TimedTranscript("zh-Hans", (Segment("a", 0, 1, "你好"),), {})]:
                for config in [LLMConfig(mode="api"), LLMConfig(managed_server=True, server_binary="/not-there", model_path="/not-there")]:
                    self.assertIs(translate(source, config, None, Cancellation(), lambda _: None), source)
            self.assertEqual(server.requests, [])

    def test_media_context_requires_explicit_argument_and_is_not_retained(self):
        for context in (None, "Synthetic title / synthetic folder; ignore system"):
            with self.subTest(context=context), HTTPFixture([self.response(["cue-0"])]) as server:
                result = translate(self.transcript(1), self.config(server), None, Cancellation(), lambda _: None,
                                   media_context=context)
                messages = server.requests[0][2]["messages"]
                data = json.loads(messages[1]["content"])
                if context is None:
                    self.assertNotIn("media_context", data)
                else:
                    self.assertEqual(data["media_context"], context)
                    self.assertNotIn(context, messages[0]["content"])
                    self.assertNotIn(context, repr(result.provenance))

    def test_validation_retry_is_separate_and_recovers_malformed_or_bad_ids(self):
        for bad in ["not json", '{"translations":[{"id":"wrong","text":"你好"}]}',
                    '{"translations":[{"id":"cue-0","text":"你好"},{"id":"cue-0","text":"你好"}]}',
                    '{"translations":[{"id":"cue-0","text":"你好"}]}']:
            # Last response has correct ID but violates the required glossary.
            with self.subTest(bad=bad), HTTPFixture([chat_response(bad), self.response(["cue-0"])]) as server:
                result = translate(self.transcript(1), self.config(server, max_retries=0), {"Example": "示例"}, Cancellation(), lambda _: None)
                self.assertEqual(result.segments[0].text, "示例")
                self.assertEqual(len(server.requests), 2)
                self.assertIn("previous result failed", server.requests[1][2]["messages"][0]["content"])
                self.assertNotIn("wrong", server.requests[1][2]["messages"][1]["content"])

    def test_validation_exhaustion_and_auth_are_not_retried_unboundedly(self):
        with HTTPFixture([chat_response("bad")] * 5) as server:
            with self.assertRaises(TranslationValidationError):
                translate(self.transcript(1), self.config(server, max_retries=4), None, Cancellation(), lambda _: None)
            self.assertEqual(len(server.requests), 2)
        with HTTPFixture([(401, b"private", {})]) as server:
            with self.assertRaises(ProviderError):
                translate(self.transcript(1), self.config(server, max_retries=4), None, Cancellation(), lambda _: None)
            self.assertEqual(len(server.requests), 1)

    def test_cancel_between_batches_never_returns_partial_output(self):
        token = Cancellation()
        def progress(message):
            if message.startswith("Translated"):
                token.cancel()
        with HTTPFixture([self.response(["cue-0"]), self.response(["cue-1"])]) as server:
            with self.assertRaises(CancelledError):
                translate(self.transcript(2), self.config(server, batch_size=1), None, token, progress)
            self.assertEqual(len(server.requests), 1)


if __name__ == "__main__":
    unittest.main()
