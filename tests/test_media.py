"""Audio-stream selection, subtitle discovery and sidecar protection checks."""
import unittest
from media_to_subs.media import AudioStreamInfo, normalize_stream_language, select_audio_stream


def stream(index, language, default=False):
    return AudioStreamInfo(index, 'pcm_s16le', normalize_stream_language(language), language, None, default, False)


class StreamSelectionTests(unittest.TestCase):
    def test_supported_original_wins_over_default_dub(self):
        streams = [stream(0, 'fra', True), stream(2, 'jpn')]
        self.assertEqual(select_audio_stream(streams).index, 2)
        self.assertEqual(select_audio_stream(streams, preferred_language='ja').index, 2)

    def test_wrong_language_guard_with_single_and_multiple_streams(self):
        for preferred, other in [('ja', 'fr'), ('ja', 'ko'), ('ko', 'ja')]:
            for count in (1, 2):
                with self.subTest(preferred=preferred, count=count), self.assertRaises(ValueError):
                    select_audio_stream([stream(i, other) for i in range(count)], preferred_language=preferred)

    def test_explicit_stream_index_is_absolute_and_keeps_language_guard(self):
        streams = [stream(2, 'jpn'), stream(5, 'kor')]
        self.assertEqual(select_audio_stream(streams, stream_index=5).language, 'ko')
        for index in (0, 1, 9):
            with self.assertRaises(ValueError):
                select_audio_stream(streams, stream_index=index)
        with self.assertRaises(ValueError):
            select_audio_stream(streams, preferred_language='ja', stream_index=5)

    def test_unknown_allows_override_but_known_unsupported_auto_fails(self):
        self.assertEqual(select_audio_stream([stream(0, 'und')], preferred_language='ja').index, 0)
        with self.assertRaises(ValueError):
            select_audio_stream([stream(0, 'eng')])

    def test_order_is_deterministic_and_default_wins(self):
        self.assertEqual(select_audio_stream([stream(4, 'ja'), stream(2, 'ja')]).index, 2)
        self.assertEqual(select_audio_stream([stream(4, 'ja', True), stream(2, 'ja')]).index, 4)

    def test_known_unsupported_tag_never_relabelled_by_title(self):
        self.assertEqual(normalize_stream_language('fre', 'Japanese'), 'fr')
        self.assertEqual(normalize_stream_language('und', 'Japanese Audio'), 'ja')
        self.assertEqual(normalize_stream_language('japanese'), 'ja')
        self.assertEqual(normalize_stream_language('korean'), 'ko')
        self.assertEqual(normalize_stream_language('zh_Hant'), 'zh-tw')


class SubtitleSuitabilityTests(unittest.TestCase):
    def test_target_language_never_accepts_original_or_mixed_bilingual(self):
        from media_to_subs.media import _subtitle_language
        from media_to_subs.subtitles import parse_srt
        for text in ('これは日本語の文章です', '中文翻译\nこれは日本語の文章です', '한국어 문장입니다'):
            transcript = parse_srt('1\n00:00:00,000 --> 00:00:01,000\n' + text)
            self.assertIsNone(_subtitle_language(transcript, 'zh-cn'))
        transcript = parse_srt('1\n00:00:00,000 --> 00:00:01,000\n這是完整的合成繁體中文測試')
        self.assertEqual(_subtitle_language(transcript, 'zh-tw'), 'zh-tw')

    def test_obvious_partial_tracks_are_not_full_dialogue(self):
        from media_to_subs.media import _full_dialogue
        from media_to_subs.subtitles import parse_srt
        transcript = parse_srt('1\n00:00:00,000 --> 00:00:01,000\nsynthetic dialogue')
        for title, forced, duration in [('Signs & Songs',False,3), ('Full dialogue',True,3), ('',False,600)]:
            self.assertFalse(_full_dialogue(transcript, duration, title, forced))
        self.assertTrue(_full_dialogue(transcript, 3, 'English SDH'))

    def test_unsupported_metadata_is_not_relabelled_english(self):
        from media_to_subs.media import _subtitle_language, language_from_name
        from media_to_subs.subtitles import parse_srt
        transcript = parse_srt('1\n00:00:00,000 --> 00:00:01,000\nBonjour ceci est une phrase synthetique')
        self.assertEqual(language_from_name('.fre.srt'), 'fr')
        self.assertIsNone(_subtitle_language(transcript, 'fr'))


class SidecarEnumerationTests(unittest.TestCase):
    def test_all_candidates_are_protected_even_when_not_selectable(self):
        import os
        import tempfile
        from pathlib import Path
        from media_to_subs.media import sidecar_paths
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root/'video.mkv'
            media.write_bytes(b'fixture')
            target = root/'video.ja.srt'
            target.write_text('malformed')
            for name in ('video.zh.forced.srt', 'video.fr.ass', 'video.ko.VTT', 'video.ssa'):
                (root/name).write_text('not selectable')
            os.link(target, root/'video.link.srt')
            (root/'video.symlink.srt').symlink_to(target)
            (root/'video.dangling.srt').symlink_to(root/'absent.srt')
            (root/'video2.en.srt').write_text('unrelated')
            (root/'video.txt').write_text('unrelated')
            found = sidecar_paths(media)
            self.assertEqual(len(found), 8)
            self.assertTrue((root/'video.symlink.srt') in found)
            self.assertTrue((root/'video.dangling.srt') in found)
            self.assertEqual(found, tuple(sorted(found, key=lambda p: (p.name.casefold(), p.name))))


    def test_bitmap_sidecars_are_protected_from_overwrite(self):
        import tempfile
        from pathlib import Path
        from media_to_subs.media import sidecar_paths
        from media_to_subs.files import validate_output_path
        from media_to_subs.errors import FileSafetyError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            media = root/'input.mkv'
            media.write_bytes(b'media fixture')
            for extension in ('.sub', '.idx', '.sup', '.PGS', '.mks'):
                sidecar = root/('input.en' + extension)
                sidecar.write_bytes(b'authored bitmap preserve')
                inventory = sidecar_paths(media)
                self.assertIn(sidecar, inventory)
                with self.assertRaises(FileSafetyError):
                    validate_output_path(sidecar, input_path=media, sidecars=inventory, overwrite=True)
                self.assertEqual(sidecar.read_bytes(), b'authored bitmap preserve')



class FFmpegMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise unittest.SkipTest('FFmpeg and FFprobe required for actual media fixtures')

    def setUp(self):
        import tempfile
        from pathlib import Path
        from media_to_subs.config import STTConfig
        from media_to_subs.runtime import Cancellation
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scratch = self.root/'scratch'
        self.scratch.mkdir()
        self.config = STTConfig()
        self.cancel = Cancellation()
        self.media = self.root/'fixture.mkv'

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, *, chinese=False, title='Full dialogue'):
        import subprocess
        english = self.root/'track-en.srt'
        english.write_text('1\n00:00:00,100 --> 00:00:02,000\nFirst synthetic sentence.\n\n2\n00:00:02,100 --> 00:00:03,800\nSecond synthetic sentence.\n')
        command = ['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','sine=frequency=440:duration=4',
                   '-f','lavfi','-i','sine=frequency=660:duration=4','-i',str(english)]
        if chinese:
            target = self.root/'track-zh.srt'
            target.write_text(english.read_text().replace('First synthetic sentence.', '這是第一句合成測試字幕。').replace('Second synthetic sentence.', '這是第二句合成測試字幕。'))
            command += ['-i',str(target)]
        command += ['-map','0:a','-map','1:a','-map','2:s']
        if chinese:
            command += ['-map','3:s']
        command += ['-c:a','pcm_s16le','-c:s','srt','-metadata:s:a:0','language=fre','-disposition:a:0','default',
                    '-metadata:s:a:1','language=jpn','-disposition:a:1','0','-metadata:s:s:0','language=eng',
                    '-metadata:s:s:0',f'title={title}']
        if chinese:
            command += ['-metadata:s:s:1','language=chi']
        subprocess.run(command+[str(self.media)],check=True,capture_output=True,timeout=20)

    def test_probe_selection_and_extraction_use_owned_pcm(self):
        import hashlib
        import wave
        from media_to_subs.media import inspect_media, extract_audio
        self.fixture()
        before = hashlib.sha256(self.media.read_bytes()).hexdigest()
        info = inspect_media(self.media, self.config, self.cancel)
        self.assertAlmostEqual(info.duration, 4, places=2)
        selected = select_audio_stream(list(info.audio_streams))
        self.assertEqual(selected.index, 1)
        audio = extract_audio(info, selected, self.scratch, self.config, self.cancel)
        with wave.open(str(audio)) as reader:
            self.assertEqual((reader.getnchannels(),reader.getsampwidth(),reader.getframerate()),(1,2,16000))
            self.assertAlmostEqual(reader.getnframes()/16000,4,places=2)
        self.assertEqual(before, hashlib.sha256(self.media.read_bytes()).hexdigest())
        self.assertEqual(audio.parent, self.scratch)
        with self.assertRaises(Exception):
            extract_audio(info, selected, self.scratch, self.config, self.cancel)

    def test_sidecar_target_beats_embedded_original_without_mutation(self):
        from media_to_subs.media import discover
        self.fixture()
        sidecar = self.root/'fixture.zh-tw.srt'
        original = b'1\r\n00:00:00,100 --> 00:00:03,800\r\n' + '這是完整的合成繁體中文測試。'.encode() + b'\r\n'
        sidecar.write_bytes(original)
        result = discover(self.media, self.config, self.scratch, self.cancel, lambda _: None)
        self.assertEqual(result.language, 'zh-tw')
        self.assertEqual(result.provenance['source_kind'], 'sidecar')
        self.assertEqual(result.provenance['source_path'], str(sidecar))
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_embedded_target_beats_local_original_and_reports_index(self):
        from media_to_subs.media import discover
        self.fixture(chinese=True)
        (self.root/'fixture.ja.srt').write_text('1\n00:00:00,100 --> 00:00:03,800\nこれは合成字幕のテストです。\n')
        result = discover(self.media, self.config, self.scratch, self.cancel, lambda _: None)
        self.assertEqual(result.language, 'zh-cn')
        self.assertEqual(result.provenance['source_kind'], 'embedded')
        self.assertEqual(result.provenance['subtitle_stream'], 3)

    def test_forced_or_signs_track_does_not_block_whisper(self):
        from media_to_subs.media import discover
        self.fixture(title='English Signs & Songs')
        (self.root/'fixture.zh.forced.srt').write_text('1\n00:00:00,100 --> 00:00:03,800\n這是合成中文招牌測試。\n')
        self.assertIsNone(discover(self.media,self.config,self.scratch,self.cancel,lambda _:None))

    def test_force_transcribe_bypasses_discovery_and_invalid_media_is_safe(self):
        from dataclasses import replace
        from media_to_subs.media import discover, inspect_media
        from media_to_subs.errors import ProcessingError
        from pathlib import Path
        forced = replace(self.config, force_transcribe=True, ffprobe_binary='missing-not-called')
        self.assertIsNone(discover(self.media,forced,self.scratch,self.cancel,lambda _:None))
        for path in (Path('https://user:private-key@invalid.example/media'), self.root/'missing.wav', self.root):
            with self.assertRaises(ProcessingError) as caught:
                inspect_media(path, self.config, self.cancel)
            self.assertNotIn('private-key', str(caught.exception))

    def test_vtt_ass_and_ssa_sidecars_convert_in_scratch(self):
        from media_to_subs.media import discover
        self.fixture(title='Signs only')
        vtt = 'WEBVTT\n\n00:00.100 --> 00:03.800\nこれは合成字幕のテストです。\n'
        ass = '[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.10,0:00:03.80,Default,,0,0,0,,これは合成字幕のテストです。\n'
        for extension, payload in (('.vtt',vtt),('.ass',ass),('.ssa',ass)):
            with self.subTest(extension=extension):
                sidecar = self.root/('fixture.ja'+extension)
                sidecar.write_text(payload)
                original = sidecar.read_bytes()
                result = discover(self.media,self.config,self.scratch,self.cancel,lambda _:None)
                self.assertIsNotNone(result)
                self.assertEqual(result.provenance['source_kind'], 'sidecar')
                self.assertEqual(result.language, 'ja')
                self.assertIn('合成字幕',result.segments[0].text)
                self.assertEqual(sidecar.read_bytes(),original)
                sidecar.unlink()  # test-owned fixture only

    def test_embedded_ass_blank_line_regression_preserved(self):
        import subprocess
        from media_to_subs.media import discover
        ass = self.root/'track.ass'
        ass.write_text('[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.10,0:00:02.00,Default,,0,0,0,,First synthetic line\\N\\NSecond synthetic line\nDialogue: 0,0:00:02.10,0:00:03.80,Default,,0,0,0,,A separate synthetic cue\n')
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=4','-i',str(ass),'-map','0:a','-map','1:s','-c:a','pcm_s16le','-c:s','ass','-metadata:s:s:0','language=eng',str(self.media)],check=True,capture_output=True,timeout=20)
        result = discover(self.media,self.config,self.scratch,self.cancel,lambda _:None)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.segments),2)
        self.assertEqual(result.segments[0].text, 'First synthetic line\n\nSecond synthetic line')
        self.assertEqual(result.provenance['source_kind'],'embedded')

    def test_delayed_audio_keeps_original_media_timeline(self):
        import subprocess
        import wave
        from media_to_subs.media import inspect_media, extract_audio
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:size=16x16:duration=4',
                        '-itsoffset','1','-f','lavfi','-i','sine=frequency=440:duration=2',
                        '-map','0:v','-map','1:a','-c:v','ffv1','-c:a','pcm_s16le',str(self.media)],
                       check=True,capture_output=True,timeout=20)
        info = inspect_media(self.media,self.config,self.cancel)
        selected = select_audio_stream(list(info.audio_streams))
        audio = extract_audio(info,selected,self.scratch,self.config,self.cancel)
        with wave.open(str(audio)) as reader:
            self.assertAlmostEqual(reader.getnframes()/16000,3,places=2)
            self.assertEqual(reader.readframes(15000),b'\0\0'*15000)
            self.assertNotEqual(reader.readframes(16000),b'\0\0'*16000)

    def test_unsupported_bitmap_sidecars_are_never_sent_to_text_conversion(self):
        from unittest.mock import patch
        from media_to_subs import media
        self.fixture()
        sidecars = []
        for extension in ('.sub', '.idx', '.sup', '.pgs', '.mks'):
            path = self.root/('fixture.zh' + extension)
            path.write_bytes(b'authored binary subtitle fixture')
            sidecars.append(path)
        with patch.object(media, '_converted_subtitle', wraps=media._converted_subtitle) as convert:
            result = media.discover(self.media,self.config,self.scratch,self.cancel,lambda _:None)
        self.assertEqual(result.language, 'en')
        self.assertEqual(result.provenance['source_kind'], 'embedded')
        self.assertTrue(all(call.args[0] not in sidecars for call in convert.call_args_list))
        self.assertTrue(all(path.read_bytes() == b'authored binary subtitle fixture' for path in sidecars))
