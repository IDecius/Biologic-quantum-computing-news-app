from __future__ import annotations

import json
import io
import queue
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

from app import (
    NewsWidget,
    _already_opened_today,
    _mark_opened_today,
    main,
    scheduled_delay_ms,
)
from news import (
    Article,
    NewsError,
    _decode_google_batch_response,
    _feed_url,
    _fetch_article_text,
    extract_article_text,
    fetch_articles,
    fetch_article_bodies,
    filter_recent_articles,
    parse_feed,
    _provider_http_error,
    summarize_from_feed,
    summarize_with_ai,
)


class ParseFeedTests(unittest.TestCase):
    def test_parses_relevant_story_and_removes_markup(self) -> None:
        feed = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Quantum computing advances bioinformatics</title>
          <link>https://example.org/story</link>
          <description>&lt;p&gt;A new quantum method for genomic analysis.&lt;/p&gt;</description>
          <source>Research News</source>
          <pubDate>Thu, 15 Oct 2026 17:14:33 GMT</pubDate>
        </item></channel></rss>"""

        articles = parse_feed(feed)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Quantum computing advances bioinformatics")
        self.assertEqual(articles[0].description, "A new quantum method for genomic analysis.")
        self.assertEqual(articles[0].source, "Research News")
        self.assertGreater(articles[0].relevance, 0)
        self.assertIsNotNone(articles[0].published_at)

    def test_excludes_stories_missing_either_research_area(self) -> None:
        feed = b"""<rss><channel>
        <item><title>Quantum computing hardware</title>
          <link>https://example.org/quantum</link></item>
        <item><title>New genomics database</title>
          <link>https://example.org/genomics</link></item>
        </channel></rss>"""

        self.assertEqual(parse_feed(feed), [])

    def test_prioritizes_quantum_computing_applied_to_biology(self) -> None:
        feed = b"""<rss><channel><item>
          <title>Quantum computing for bioinformatics</title>
          <link>https://example.org/computing</link>
        </item><item>
          <title>Quantum biology and molecular biology</title>
          <link>https://example.org/biology</link>
        </item></channel></rss>"""

        articles = parse_feed(feed)

        self.assertGreater(articles[0].relevance, articles[1].relevance)

    def test_international_feed_url_uses_requested_language_and_region(self) -> None:
        url = _feed_url('"quantum biology"', ("en-GB", "GB", "GB:en"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        self.assertEqual(query["hl"], ["en-GB"])
        self.assertEqual(query["gl"], ["GB"])
        self.assertEqual(query["ceid"], ["GB:en"])

    def test_recent_filter_keeps_only_known_dates_within_one_calendar_month(self) -> None:
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        articles = [
            Article(
                title=f"Article {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description="",
                published_at=published_at,
                relevance=1,
            )
            for index, published_at in enumerate(
                (
                    now - timedelta(days=30),
                    now.replace(month=9, day=15),
                    None,
                    now + timedelta(minutes=1),
                )
            )
        ]

        recent = filter_recent_articles(articles, now=now)

        self.assertEqual([article.title for article in recent], ["Article 0"])

    def test_ranks_publication_date_before_relevance(self) -> None:
        now = datetime.now().astimezone()
        articles = [
            Article(
                title=title,
                url=f"https://example.org/{index}",
                source="Research News",
                description="Quantum biology research news",
                published_at=published_at,
                relevance=relevance,
            )
            for index, (title, published_at, relevance) in enumerate(
                (
                    ("Older and more relevant", now - timedelta(days=2), 100),
                    ("Newest and less relevant", now, 10),
                    ("Older, lower relevance", now - timedelta(days=2), 20),
                )
            )
        ]

        with (
            patch("news.FEED_SEARCHES", ()),
            patch("news.FEED_LOCALES", ()),
            patch(
                "news.filter_recent_articles",
                return_value=articles,
            ),
        ):
            ranked, _ = fetch_articles()

        self.assertEqual(
            [article.title for article in ranked],
            [
                "Newest and less relevant",
                "Older and more relevant",
                "Older, lower relevance",
            ],
        )

    def test_invalid_xml_raises_a_clear_error(self) -> None:
        with self.assertRaisesRegex(NewsError, "XML valide"):
            parse_feed(b"<rss>")

    def test_extracts_article_paragraphs_without_navigation_or_scripts(self) -> None:
        page = """<!doctype html><html><body>
        <nav><p>Navigation unrelated to the article with plenty of text here.</p></nav>
        <main><article>
          <h1>Research results</h1>
          <p>The researchers report a detailed biological result from their study.</p>
          <p>A second paragraph describes how the quantum method was evaluated.</p>
          <script><p>This hidden text must never appear in the summary.</p></script>
        </article></main></body></html>"""

        text = extract_article_text(page)

        self.assertIn("detailed biological result", text)
        self.assertIn("quantum method was evaluated", text)
        self.assertNotIn("Navigation unrelated", text)
        self.assertNotIn("hidden text", text)

    def test_extracts_article_description_when_no_paragraph_is_available(self) -> None:
        page = (
            '<html><head><meta name="description" '
            'content="An article description with useful research details."/></head></html>'
        )

        self.assertEqual(
            extract_article_text(page),
            "An article description with useful research details.",
        )

    def test_does_not_treat_google_news_landing_page_as_article_text(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            "https://news.google.com/rss/articles/example"
        )

        with patch("news.urllib.request.urlopen", return_value=response):
            text = _fetch_article_text(
                "https://news.google.com/rss/articles/example"
            )

        self.assertEqual(text, "")

    def test_decodes_editor_urls_from_google_news_batch_response(self) -> None:
        result = [
            [
                "wrb.fr",
                "Fbv4je",
                json.dumps(["garturlres", "https://publisher.example/article"]),
                None,
                "3",
            ]
        ]

        decoded = _decode_google_batch_response(
            ")]}'\n\n" + json.dumps(result)
        )

        self.assertEqual(decoded, {3: "https://publisher.example/article"})

    def test_resolves_google_link_before_fetching_article_body(self) -> None:
        article = Article(
            title="Quantum biology story",
            url="https://news.google.com/rss/articles/example",
            source="Research News",
            description="RSS excerpt",
            published_at=None,
            relevance=15,
        )

        with (
            patch(
                "news._resolve_google_news_urls",
                return_value=({0: "https://publisher.example/article"}, {}),
            ),
            patch(
                "news._fetch_article_text",
                return_value="The publisher's detailed article paragraph.",
            ) as fetch_text,
        ):
            articles, warnings = fetch_article_bodies([article])

        self.assertEqual(articles[0].url, "https://publisher.example/article")
        self.assertEqual(
            articles[0].article_text,
            "The publisher's detailed article paragraph.",
        )
        self.assertEqual(warnings, [])
        fetch_text.assert_called_once_with("https://publisher.example/article")

    def test_report_has_one_point_per_article_without_an_api_key(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]

        with patch.dict(
            "os.environ",
            {
                "NEWS_AI_API_KEY": "",
                "GEMINI_API_KEY": "",
                "NEWS_AI_PROVIDER": "",
            },
            clear=True,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(len(points), 5)
        self.assertEqual([point["article_index"] for point in points], [1, 2, 3, 4, 5])
        self.assertEqual(points[0]["summary"], "RSS excerpt 1")

    def test_local_summary_uses_detailed_article_text_when_available(self) -> None:
        article = Article(
            title="Quantum biology story",
            url="https://example.org/story",
            source="Research News",
            description="Short RSS excerpt.",
            published_at=None,
            relevance=15,
            article_text=(
                "Article body sentence one. "
                "Article body sentence two. "
                "Article body sentence three. "
                "Article body sentence four. "
                "Article body sentence five."
            ),
        )

        points = summarize_from_feed([article])

        self.assertEqual(len(points), 1)
        self.assertIn("Article body sentence four.", points[0]["summary"])
        self.assertNotIn("Article body sentence five.", points[0]["summary"])

    def test_uses_gemini_default_model_and_validates_its_report(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
                article_text=(
                    "Full article text with enough detail for a longer summary."
                    if index == 1
                    else ""
                ),
            )
            for index in range(1, 6)
        ]
        response_body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "points": [
                                            {
                                                "article_index": index,
                                                "summary": f"Résumé {index}",
                                            }
                                            for index in range(1, 6)
                                        ]
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            response_body
        ).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "NEWS_AI_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "test-gemini-key",
                    "NEWS_AI_API_KEY": "",
                    "NEWS_AI_MODEL": "",
                },
                clear=True,
            ),
            patch("news.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(len(points), 5)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3.8-flash:generateContent",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), "test-gemini-key")
        self.assertNotIn("test-gemini-key", request.full_url)
        request_body = json.loads(request.data)
        prompt_input = request_body["contents"][0]["parts"][0]["text"]
        self.assertIn("Full article text with enough detail", prompt_input)
        self.assertIn("rédige en français", request_body["system_instruction"]["parts"][0]["text"])

    def test_uses_mistral_first_even_if_gemini_was_previously_selected(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]
        response_body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "points": [
                                    {
                                        "article_index": index,
                                        "summary": f"Résumé Mistral {index}",
                                    }
                                    for index in range(1, 6)
                                ]
                            }
                        )
                    }
                }
            ]
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            response_body
        ).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "MISTRAL_API_KEY": "test-mistral-key",
                    "GEMINI_API_KEY": "test-gemini-key",
                    "NEWS_AI_PROVIDER": "gemini",
                    "MISTRAL_MODEL": "",
                    "NEWS_AI_MODEL": "gemini-3.8-flash",
                },
                clear=True,
            ),
            patch("news.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(points[0]["summary"], "Résumé Mistral 1")
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.mistral.ai/v1/chat/completions",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer test-mistral-key",
        )
        request_body = json.loads(request.data)
        self.assertEqual(request_body["model"], "mistral-small-latest")
        self.assertIn(
            "rédige en français",
            request_body["messages"][0]["content"],
        )
        self.assertNotIn("test-mistral-key", request.full_url)

    def test_falls_back_to_gemini_when_mistral_fails(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]
        gemini_points = [
            {"article_index": index, "summary": f"Résumé Gemini {index}"}
            for index in range(1, 6)
        ]

        with (
            patch.dict(
                "os.environ",
                {
                    "MISTRAL_API_KEY": "test-mistral-key",
                    "GEMINI_API_KEY": "test-gemini-key",
                },
                clear=True,
            ),
            patch(
                "news._summarize_with_chat_completions",
                side_effect=NewsError("Mistral a répondu HTTP 503."),
            ) as mistral,
            patch(
                "news._summarize_with_gemini",
                return_value=gemini_points,
            ) as gemini,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(points, gemini_points)
        mistral.assert_called_once()
        gemini.assert_called_once_with(articles, "test-gemini-key")

    def test_falls_back_to_groq_when_mistral_and_gemini_fail(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]
        groq_points = [
            {"article_index": index, "summary": f"Résumé Groq {index}"}
            for index in range(1, 6)
        ]

        with (
            patch.dict(
                "os.environ",
                {
                    "MISTRAL_API_KEY": "test-mistral-key",
                    "GEMINI_API_KEY": "test-gemini-key",
                    "GROQ_API_KEY": "test-groq-key",
                    "MISTRAL_MODEL": "",
                    "MISTRAL_API_BASE_URL": "",
                    "GEMINI_API_BASE_URL": "",
                    "GROQ_MODEL": "",
                    "GROQ_API_BASE_URL": "",
                },
                clear=True,
            ),
            patch(
                "news._summarize_with_chat_completions",
                side_effect=[
                    NewsError("Mistral a répondu HTTP 429."),
                    groq_points,
                ],
            ) as chat_completion,
            patch(
                "news._summarize_with_gemini",
                side_effect=NewsError("Gemini a répondu HTTP 503."),
            ) as gemini,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(points, groq_points)
        self.assertEqual(chat_completion.call_count, 2)
        self.assertEqual(
            chat_completion.call_args_list[0].kwargs,
            {
                "provider": "Mistral",
                "base_url": "https://api.mistral.ai/v1",
                "model": "mistral-small-latest",
            },
        )
        self.assertEqual(
            chat_completion.call_args_list[1].args[1],
            "test-groq-key",
        )
        self.assertEqual(
            chat_completion.call_args_list[1].kwargs,
            {
                "provider": "Groq",
                "base_url": "https://api.groq.com/openai/v1",
                "model": "llama-3.3-70b-versatile",
            },
        )
        gemini.assert_called_once_with(articles, "test-gemini-key")

    def test_uses_groq_when_only_the_groq_key_is_configured(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]
        response_body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "points": [
                                    {
                                        "article_index": index,
                                        "summary": f"Résumé Groq {index}",
                                    }
                                    for index in range(1, 6)
                                ]
                            }
                        )
                    }
                }
            ]
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            response_body
        ).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "MISTRAL_API_KEY": "",
                    "GEMINI_API_KEY": "",
                    "GROQ_API_KEY": "test-groq-key",
                    "GROQ_API_BASE_URL": "",
                    "GROQ_MODEL": "",
                    "NEWS_AI_PROVIDER": "",
                },
                clear=True,
            ),
            patch("news.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            points = summarize_with_ai(articles)

        self.assertEqual(points[0]["summary"], "Résumé Groq 1")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.groq.com/openai/v1/chat/completions",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer test-groq-key",
        )
        self.assertNotIn("test-groq-key", request.full_url)
        self.assertEqual(
            json.loads(request.data)["model"],
            "llama-3.3-70b-versatile",
        )

    def test_reports_all_provider_failures_when_fallbacks_fail(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]

        with (
            patch.dict(
                "os.environ",
                {
                    "MISTRAL_API_KEY": "test-mistral-key",
                    "GEMINI_API_KEY": "test-gemini-key",
                    "GROQ_API_KEY": "test-groq-key",
                },
                clear=True,
            ),
            patch(
                "news._summarize_with_chat_completions",
                side_effect=[
                    NewsError("Mistral a répondu HTTP 429."),
                    NewsError("Groq a répondu HTTP 503."),
                ],
            ),
            patch(
                "news._summarize_with_gemini",
                side_effect=NewsError("Gemini a répondu HTTP 503."),
            ),
        ):
            with self.assertRaisesRegex(
                NewsError,
                "Mistral indisponible.*Gemini indisponible.*Groq indisponible",
            ):
                summarize_with_ai(articles)

    def test_includes_gemini_error_details_without_exposing_api_key(self) -> None:
        articles = [
            Article(
                title=f"Quantum biology story {index}",
                url=f"https://example.org/{index}",
                source="Research News",
                description=f"RSS excerpt {index}",
                published_at=None,
                relevance=15,
            )
            for index in range(1, 6)
        ]
        error_body = json.dumps(
            {"error": {"message": "The requested model is unavailable."}}
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://example.org/gemini",
            404,
            "Not Found",
            {},
            io.BytesIO(error_body),
        )

        with (
            patch.dict(
                "os.environ",
                {
                    "NEWS_AI_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "test-gemini-key",
                    "NEWS_AI_API_KEY": "",
                },
                clear=True,
            ),
            patch("news.urllib.request.urlopen", side_effect=http_error),
        ):
            with self.assertRaisesRegex(
                NewsError, "HTTP 404 : The requested model is unavailable"
            ) as raised:
                summarize_with_ai(articles)

        self.assertNotIn("test-gemini-key", str(raised.exception))

    def test_groq_403_error_explains_access_checks_and_keeps_provider_detail(self) -> None:
        error_body = json.dumps(
            {"error": {"message": "The API key is not authorized."}}
        ).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://example.org/groq",
            403,
            "Forbidden",
            {},
            io.BytesIO(error_body),
        )

        error = _provider_http_error("Groq", http_error, "test-groq-key")

        self.assertIn("HTTP 403", str(error))
        self.assertIn("The API key is not authorized.", str(error))
        self.assertIn("Vérifiez que la clé API est active", str(error))
        self.assertNotIn("test-groq-key", str(error))

    def test_groq_cloudflare_1010_does_not_blame_api_key(self) -> None:
        error_body = b"<html><body>Sorry, you have been blocked. Error code: 1010</body></html>"
        http_error = urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions",
            403,
            "Forbidden",
            {"CF-Ray": "test-ray-123"},
            io.BytesIO(error_body),
        )

        error = _provider_http_error("Groq", http_error, "test-groq-key")

        self.assertIn("Cloudflare 1010", str(error))
        self.assertIn("ne prouve pas que la clé ou le projet est incorrect", str(error))
        self.assertIn("test-ray-123", str(error))
        self.assertIn("Ray ID", str(error))
        self.assertNotIn("Vérifiez que la clé API est active", str(error))
        self.assertNotIn("test-groq-key", str(error))


class WindowVisibilityTests(unittest.TestCase):
    def _widget_with_results(self, result: tuple[str, object]) -> NewsWidget:
        widget = NewsWidget.__new__(NewsWidget)
        widget.root = MagicMock()
        widget.window_visible = False
        widget.results = queue.Queue()
        widget.results.put(result)
        widget.refresh_button = MagicMock()
        widget.status = MagicMock()
        widget.loading_indicator = MagicMock()
        widget._display_report = MagicMock()
        return widget

    def test_first_window_reveal_waits_until_report_is_rendered(self) -> None:
        events: list[str] = []
        widget = self._widget_with_results(
            ("success", ([], [], []))
        )
        widget._display_report.side_effect = lambda *_args: events.append("render")
        widget.root.deiconify.side_effect = lambda: events.append("show")

        widget._poll_results()

        self.assertEqual(events, ["render", "show"])
        self.assertTrue(widget.window_visible)
        widget.loading_indicator.stop.assert_called_once()
        widget.loading_indicator.pack_forget.assert_called_once()

    def test_first_window_stays_hidden_while_results_are_pending(self) -> None:
        widget = self._widget_with_results(("success", ([], [], [])))
        widget.results = queue.Queue()

        widget._poll_results()

        widget.root.deiconify.assert_not_called()
        self.assertFalse(widget.window_visible)

    def test_refresh_starts_loading_animation(self) -> None:
        widget = self._widget_with_results(("success", ([], [], [])))
        with patch("app.threading.Thread") as thread:
            widget.refresh()

        widget.loading_indicator.start.assert_called_once_with(12)
        widget.status.configure.assert_called_once_with(
            text="Recherche des nouvelles…"
        )
        thread.return_value.start.assert_called_once()


class StartupTests(unittest.TestCase):
    def test_manual_launch_shows_the_window_with_loading_indicator(self) -> None:
        with (
            patch("app.argparse.ArgumentParser") as argument_parser,
            patch("app.tk.Tk") as tk_root,
            patch("app.NewsWidget") as widget_class,
        ):
            argument_parser.return_value.parse_args.return_value = SimpleNamespace(
                scheduled=False
            )
            root = tk_root.return_value

            main()

        root.withdraw.assert_called_once()
        widget_class.assert_called_once_with(root, show_loading_on_start=True)
        root.mainloop.assert_called_once()


class ScheduledDelayTests(unittest.TestCase):
    def test_waits_until_eleven_before_opening(self) -> None:
        now = datetime(2026, 10, 16, 10, 30, tzinfo=timezone.utc)

        self.assertEqual(scheduled_delay_ms(now), 30 * 60 * 1000)

    def test_opens_immediately_at_or_after_eleven(self) -> None:
        now = datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc)

        self.assertEqual(scheduled_delay_ms(now), 0)

    def test_records_automatic_opening_for_the_current_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict("os.environ", {"LOCALAPPDATA": temporary_directory}):
                self.assertFalse(_already_opened_today())
                _mark_opened_today()
                self.assertTrue(_already_opened_today())
                marker = Path(temporary_directory) / (
                    "ActualitesBiologieQuantique/last-opened.txt"
                )
                self.assertEqual(
                    marker.read_text(encoding="utf-8"),
                    datetime.now().astimezone().date().isoformat(),
                )


if __name__ == "__main__":
    unittest.main()
