"""Chat/topic link parsing tests.

User request (2026-09-12): commands must run in a specific forum topic,
given as a link like https://t.me/c/3537976236/7. config.parse_chat_link
extracts (chat, topic); load_settings() applies it; get_history filters
messages by topic; send_command uses reply_to=<topic>.
"""

import unittest

from heroku_mcp.config import parse_chat_link


class ParseChatLinkTest(unittest.TestCase):
    def test_private_topic_link(self):
        # The exact link from the user's request.
        self.assertEqual(
            parse_chat_link("https://t.me/c/3537976236/7"),
            (3537976236, 7),
        )

    def test_private_link_trailing_slash_and_no_scheme(self):
        self.assertEqual(parse_chat_link("https://t.me/c/1/2/"), (1, 2))
        self.assertEqual(parse_chat_link("t.me/c/123456/42"), (123456, 42))

    def test_public_topic_link(self):
        self.assertEqual(
            parse_chat_link("https://t.me/somegroup/7"),
            ("somegroup", 7),
        )

    def test_plain_chat_without_topic(self):
        self.assertEqual(parse_chat_link("https://t.me/mybot"), ("mybot", 0))
        self.assertEqual(parse_chat_link("@username"), ("username", 0))
        self.assertEqual(parse_chat_link("3537976236"), (3537976236, 0))
        self.assertEqual(parse_chat_link("-1001234567890"), (-1001234567890, 0))

    def test_me_and_empty(self):
        self.assertEqual(parse_chat_link("me"), ("me", 0))
        self.assertEqual(parse_chat_link(""), ("me", 0))

    def test_malformed_link_raises(self):
        with self.assertRaises(ValueError):
            parse_chat_link("https://t.me/c/notanumber/7")


class TopicFilterTest(unittest.TestCase):
    """_topic_filter must keep only messages of the configured topic."""

    def _msg(self, reply_to_msg_id=None, reply_to_top_id=None, forum=False):
        m = unittest.mock.MagicMock()
        r = unittest.mock.MagicMock()
        r.forum_topic = forum
        r.reply_to_msg_id = reply_to_msg_id
        r.reply_to_top_id = reply_to_top_id
        m.reply_to = r if (reply_to_msg_id or reply_to_top_id or forum) else None
        return m

    def setUp(self):
        from unittest import mock
        from heroku_mcp import server
        self._patcher = mock.patch.object(server.settings, "her_topic_id", 7)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_no_topic_configured_passes_everything(self):
        from unittest import mock
        from heroku_mcp import server
        with mock.patch.object(server.settings, "her_topic_id", 0):
            self.assertTrue(server._topic_filter(self._msg()))
            self.assertTrue(server._topic_filter(self._msg(reply_to_msg_id=999)))

    def test_topic_root_reply_matches(self):
        from heroku_mcp import server
        self.assertTrue(server._topic_filter(self._msg(reply_to_msg_id=7)))
        self.assertTrue(server._topic_filter(self._msg(reply_to_top_id=7)))

    def test_other_topics_excluded(self):
        from heroku_mcp import server
        self.assertFalse(server._topic_filter(self._msg(reply_to_msg_id=999)))
        self.assertFalse(server._topic_filter(self._msg()))


if __name__ == "__main__":
    unittest.main()
