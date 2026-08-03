import pathlib
import tempfile
import unittest
from unittest import mock

from podcast_radar.transcriber import (
    MAX_PROMPT_DESCRIPTION_CHARS,
    _episode_prompt_context,
    download_youtube_audio,
)


class TranscriberTests(unittest.TestCase):
    def test_episode_prompt_context_includes_metadata(self) -> None:
        context = _episode_prompt_context(
            {
                "feed_name": "Example Podcast",
                "title": "An AI Episode",
                "hosts_json": '["Host One", "Host Two"]',
                "description": "<p>Guest from OpenAI talks about model behavior.</p>",
            }
        )

        self.assertEqual(context["feed_name"], "Example Podcast")
        self.assertEqual(context["episode_title"], "An AI Episode")
        self.assertEqual(context["episode_hosts"], "Host One, Host Two")
        self.assertIn("Guest from OpenAI", context["episode_description"])

    def test_episode_prompt_context_caps_description_for_whisper(self) -> None:
        context = _episode_prompt_context(
            {
                "feed_name": "Example Podcast",
                "title": "An AI Episode",
                "hosts_json": "[]",
                "description": "AI infrastructure " * 100,
            }
        )

        description = context["episode_description"]
        self.assertLessEqual(len(description), MAX_PROMPT_DESCRIPTION_CHARS)
        self.assertFalse(description.endswith(" "))

    def test_youtube_audio_is_normalized_for_whisper(self) -> None:
        output = pathlib.Path(tempfile.gettempdir()) / "ai-radar-youtube-audio-test.wav"
        output.touch()
        try:
            with mock.patch("podcast_radar.transcriber.shutil.which", return_value="/opt/homebrew/bin/yt-dlp"), mock.patch(
                "podcast_radar.transcriber._run_transcription_command"
            ) as run:
                download_youtube_audio("https://youtube.com/watch?v=example", output, command_output=False)

            command = run.call_args.args[0]
            self.assertIn("wav", command)
            self.assertIn("ffmpeg:-ar 16000 -ac 1", command)
            self.assertEqual(command[-2:], [str(output), "https://youtube.com/watch?v=example"])
        finally:
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
