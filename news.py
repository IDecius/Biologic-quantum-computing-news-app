"""News retrieval and optional AI summaries for the daily research digest."""

from __future__ import annotations

import calendar
import email.utils
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import html
from html.parser import HTMLParser
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

FEED_SEARCHES = (
    '"quantum biology"',
    '"quantum computing" bioinformatics OR genomics OR proteins',
    '"quantum computing" biology "drug discovery"',
    '"quantum sensing" biology OR biological',
    '"biologie quantique"',
    '"informatique quantique" bioinformatique OR biologie',
)
FEED_LOCALES = (
    ("en-US", "US", "US:en"),
    ("en-GB", "GB", "GB:en"),
    ("en-CA", "CA", "CA:en"),
    ("fr-CA", "CA", "CA:fr"),
    ("en-AU", "AU", "AU:en"),
)
FEED_URL = "https://news.google.com/rss/search"
MAX_ARTICLES = 10
FEED_MAX_WORKERS = 10
MAX_ARTICLE_BYTES = 1_000_000
MAX_ARTICLE_TEXT_CHARS = 10_000
GOOGLE_BATCHEXECUTE_URL = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute"
)
GOOGLE_ARTICLE_CONTEXT = [
    ["X", "X", ["X", "X"], None, None, 1, 1, "CA:fr", None, 1,
     None, None, None, None, None, 0, 1],
    "X",
    "X",
    1,
    [1, 1, 1],
    1,
    1,
    None,
    0,
    0,
    None,
    0,
]

QUANTUM_TERMS = ("quantum", "quantique", "quantiques")
BIOLOGY_TERMS = (
    "bioinformatics",
    "bioinformatique",
    "biology",
    "biological",
    "biologie",
    "biomolecular",
    "genomics",
    "genomic",
    "genomique",
    "genomiques",
    "protein",
    "proteins",
    "proteine",
    "proteines",
    "molecular",
    "molecule",
    "molecules",
    "moleculaire",
    "biomedical",
    "biomedicale",
    "biomedicales",
    "photosynthesis",
    "photosynthese",
    "enzyme",
    "enzymes",
    "drug discovery",
    "life sciences",
)
COMPUTING_TERMS = (
    "quantum computing",
    "quantum computer",
    "computing",
    "computational",
    "computation",
    "bioinformatics",
    "bioinformatique",
    "informatique quantique",
    "calcul quantique",
    "ordinateur quantique",
    "algorithm",
    "algorithme",
    "qubit",
)


class NewsError(Exception):
    """An expected error while retrieving news or contacting the AI service."""


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    description: str
    published_at: datetime | None
    relevance: int
    article_text: str = ""

    @property
    def display_date(self) -> str:
        if self.published_at is None:
            return "Date inconnue"
        return self.published_at.astimezone().strftime("%d %b %Y")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html.unescape(value))
    return re.sub(r"\s+", " ", " ".join(extractor.parts)).strip()


class _ArticleBodyExtractor(HTMLParser):
    _SKIPPED_TAGS = {
        "aside",
        "footer",
        "form",
        "header",
        "iframe",
        "nav",
        "noscript",
        "script",
        "style",
        "svg",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.article_depth = 0
        self.paragraph_depth = 0
        self.current_paragraph: list[str] = []
        self.paragraphs: list[tuple[bool, str]] = []
        self.metadata_description = ""
        self.google_signature = ""
        self.google_timestamp = ""

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self.skip_depth:
            self.skip_depth += 1
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "div":
            self.google_signature = values.get("data-n-a-sg", self.google_signature)
            self.google_timestamp = values.get("data-n-a-ts", self.google_timestamp)
        if tag in self._SKIPPED_TAGS:
            self.skip_depth = 1
            return
        if tag in {"article", "main"}:
            self.article_depth += 1
        if tag == "p":
            if self.paragraph_depth == 0:
                self.current_paragraph = []
            self.paragraph_depth += 1
        if tag == "meta":
            meta_name = values.get("name", "").casefold()
            if meta_name == "description" or values.get("property", "").casefold() in {
                "og:description",
                "twitter:description",
            }:
                description = values.get("content", "").strip()
                if len(description) > len(self.metadata_description):
                    self.metadata_description = description

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "p" and self.paragraph_depth:
            self.paragraph_depth -= 1
            if self.paragraph_depth == 0:
                text = re.sub(
                    r"\s+", " ", "".join(self.current_paragraph)
                ).strip()
                if len(text) >= 40:
                    self.paragraphs.append((self.article_depth > 0, text))
                self.current_paragraph = []
        if tag in {"article", "main"} and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and self.paragraph_depth:
            self.current_paragraph.append(data)

    def get_text(self) -> str:
        article_paragraphs = [
            text for is_article_text, text in self.paragraphs if is_article_text
        ]
        paragraphs = article_paragraphs or [
            text for _, text in self.paragraphs
        ]
        if paragraphs:
            return "\n\n".join(dict.fromkeys(paragraphs))[:MAX_ARTICLE_TEXT_CHARS]
        return self.metadata_description[:MAX_ARTICLE_TEXT_CHARS]


def extract_article_text(html_content: str) -> str:
    """Extract readable paragraphs from an HTML article page."""
    extractor = _ArticleBodyExtractor()
    extractor.feed(html_content)
    extractor.close()
    return extractor.get_text()


def _fetch_article_text(url: str) -> str:
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise NewsError("Le lien de l’article n’est pas une adresse Web valide.")

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ActualitesBiologieQuantique/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        final_host = urllib.parse.urlparse(response.geturl()).hostname or ""
        if final_host.casefold() == "news.google.com":
            return ""
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return ""
        page_bytes = response.read(MAX_ARTICLE_BYTES + 1)
        if len(page_bytes) > MAX_ARTICLE_BYTES:
            page_bytes = page_bytes[:MAX_ARTICLE_BYTES]
        encoding = response.headers.get_content_charset() or "utf-8"

    try:
        page = page_bytes.decode(encoding, errors="replace")
    except LookupError:
        page = page_bytes.decode("utf-8", errors="replace")
    return extract_article_text(page)


def _google_article_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/").split("/")
    if (
        parsed.hostname == "news.google.com"
        and len(path) >= 2
        and path[-2] in {"articles", "read"}
    ):
        return path[-1] or None
    return None


def _fetch_google_signature(
    article_url: str,
) -> tuple[str, str, str] | None:
    article_id = _google_article_id(article_url)
    if article_id is None:
        return None

    source_query = urllib.parse.parse_qs(urllib.parse.urlparse(article_url).query)
    language = source_query.get("hl", ["en-US"])[0]
    region = source_query.get("gl", ["US"])[0]
    edition = source_query.get("ceid", [f"{region}:{language.split('-')[0]}"])[0]
    params = urllib.parse.urlencode(
        {"hl": language, "gl": region, "ceid": edition}
    )
    params_url = (
        f"https://news.google.com/rss/articles/"
        f"{urllib.parse.quote(article_id, safe='')}?{params}"
    )
    request = urllib.request.Request(
        params_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ActualitesBiologieQuantique/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        page = response.read(MAX_ARTICLE_BYTES + 1).decode("utf-8", errors="replace")

    extractor = _ArticleBodyExtractor()
    extractor.feed(page)
    extractor.close()
    if not extractor.google_signature or not extractor.google_timestamp:
        return None
    return article_id, extractor.google_timestamp, extractor.google_signature


def _decode_google_batch_response(response_text: str) -> dict[int, str]:
    body = response_text
    if "\n\n" in body:
        body = body.split("\n\n", 1)[1]
    body = body.lstrip()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1] if "\n" in body else body[4:]

    try:
        rows = json.loads(body.lstrip())
    except json.JSONDecodeError as exc:
        raise NewsError("La réponse Google Actualités n’est pas un JSON valide.") from exc

    decoded: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row[0] != "wrb.fr" and row[1] != "Fbv4je":
            continue
        result = row[2]
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                continue
        if not isinstance(result, list) or len(result) < 2:
            continue
        if result[0] != "garturlres" or not isinstance(result[1], str):
            continue
        request_id = next(
            (value for value in reversed(row[3:]) if value is not None),
            None,
        )
        if isinstance(request_id, str) and request_id.isdigit():
            decoded[int(request_id)] = result[1]
    return decoded


def _resolve_google_news_urls(
    articles: list[Article],
) -> tuple[dict[int, str], dict[int, str]]:
    google_links = [
        (index, article.url)
        for index, article in enumerate(articles)
        if (urllib.parse.urlparse(article.url).hostname or "").casefold()
        == "news.google.com"
    ]
    if not google_links:
        return {}, {}

    resolved: dict[int, str] = {}
    errors: dict[int, str] = {}
    pending: list[tuple[int, str, str, str]] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_google_signature, url): index
            for index, url in google_links
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                signature = future.result()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors[index] = str(exc)
                continue
            if signature is None:
                errors[index] = "Google Actualités n’a pas fourni les paramètres du lien."
                continue
            article_id, timestamp, signed_value = signature
            pending.append((index, article_id, timestamp, signed_value))

    if not pending:
        return resolved, errors

    envelopes = []
    for index, article_id, timestamp, signature in pending:
        request_data = [
            "garturlreq",
            GOOGLE_ARTICLE_CONTEXT,
            article_id,
            int(timestamp) if timestamp.isdigit() else timestamp,
            signature,
        ]
        envelopes.append(
            [
                "Fbv4je",
                json.dumps(request_data, separators=(",", ":")),
                None,
                str(index),
            ]
        )
    form = urllib.parse.urlencode(
        {"f.req": json.dumps([envelopes], separators=(",", ":"))}
    ).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_BATCHEXECUTE_URL,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (compatible; ActualitesBiologieQuantique/1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        batch_response = response.read(MAX_ARTICLE_BYTES + 1).decode(
            "utf-8", errors="replace"
        )
    decoded = _decode_google_batch_response(batch_response)

    for index, _, _, _ in pending:
        url = decoded.get(index)
        if not url:
            errors[index] = "Google Actualités n’a pas renvoyé l’adresse de l’éditeur."
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors[index] = "Google Actualités a renvoyé une adresse non valide."
            continue
        resolved[index] = url
    return resolved, errors


def fetch_article_bodies(
    articles: list[Article],
) -> tuple[list[Article], list[str]]:
    """Fetch available article pages without blocking other feed results."""
    enriched = list(articles)
    warnings: list[str] = []
    try:
        resolved, resolution_errors = _resolve_google_news_urls(articles)
    except (NewsError, urllib.error.URLError, TimeoutError, OSError) as exc:
        warnings.append(f"Impossible de résoudre les liens Google Actualités : {exc}")
    else:
        for index, url in resolved.items():
            enriched[index] = replace(enriched[index], url=url)
        for index, error in resolution_errors.items():
            warnings.append(
                f"Impossible de trouver la page éditeur pour "
                f"{articles[index].source} : {error}"
            )

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_article_text, article.url): index
            for index, article in enumerate(enriched)
        }
        for future in as_completed(futures):
            index = futures[future]
            article = enriched[index]
            try:
                article_text = future.result()
            except (NewsError, urllib.error.URLError, TimeoutError, OSError) as exc:
                warnings.append(
                    f"Texte intégral indisponible pour {article.source} : {exc}"
                )
                continue
            if not article_text:
                warnings.append(
                    f"Texte intégral indisponible pour {article.source} "
                    "(page inaccessible ou sans texte extractible)."
                )
                continue
            enriched[index] = replace(article, article_text=article_text)
    return enriched, warnings


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _score(title: str, description: str, source: str) -> int:
    text = f"{title} {description} {source}".casefold()
    quantum_matches = sum(text.count(term) for term in QUANTUM_TERMS)
    biology_matches = sum(text.count(term) for term in BIOLOGY_TERMS)
    computing_matches = sum(text.count(term) for term in COMPUTING_TERMS)
    if not quantum_matches or not biology_matches:
        return 0
    return quantum_matches * 10 + biology_matches * 5 + computing_matches * 15


def parse_feed(xml_data: bytes) -> list[Article]:
    """Parse one Google News RSS response and keep stories matching both topics."""
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        raise NewsError(f"Le flux d’actualités n’est pas un XML valide : {exc}") from exc

    articles: list[Article] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title or not url:
            continue

        source = (item.findtext("source") or "").strip()
        description = _plain_text(item.findtext("description") or "")
        relevance = _score(title, description, source)
        if relevance == 0:
            continue

        articles.append(
            Article(
                title=title,
                url=url,
                source=source or "Source inconnue",
                description=description,
                published_at=_parse_date(item.findtext("pubDate")),
                relevance=relevance,
            )
        )
    return articles


def _feed_url(
    query: str, locale: tuple[str, str, str] = FEED_LOCALES[0]
) -> str:
    language, region, edition = locale
    params = urllib.parse.urlencode(
        {
            "q": query,
            "hl": language,
            "gl": region,
            "ceid": edition,
        }
    )
    return f"{FEED_URL}?{params}"


def _one_month_before(now: datetime) -> datetime:
    month_index = now.year * 12 + now.month - 2
    year, month_offset = divmod(month_index, 12)
    month = month_offset + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def filter_recent_articles(
    articles: list[Article], now: datetime | None = None
) -> list[Article]:
    """Keep only articles with a known publication date in the last calendar month."""
    current_time = now or datetime.now().astimezone()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = _one_month_before(current_time)
    cutoff_utc = cutoff.astimezone(timezone.utc)
    current_utc = current_time.astimezone(timezone.utc)

    return [
        article
        for article in articles
        if article.published_at is not None
        and article.published_at.astimezone(timezone.utc) >= cutoff_utc
        and article.published_at.astimezone(timezone.utc) <= current_utc
    ]


def fetch_articles() -> tuple[list[Article], list[str]]:
    """Fetch international RSS searches and rank stories no older than one month."""
    by_url: dict[str, Article] = {}
    warnings: list[str] = []

    feed_searches = [
        (query, locale)
        for query in FEED_SEARCHES
        for locale in FEED_LOCALES
    ]

    def fetch_feed(query: str, locale: tuple[str, str, str]) -> list[Article]:
        request = urllib.request.Request(
            _feed_url(query, locale),
            headers={"User-Agent": "ActualitesQuantiqueBiologique/1.0"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return parse_feed(response.read())

    with ThreadPoolExecutor(max_workers=FEED_MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_feed, query, locale): (query, locale)
            for query, locale in feed_searches
        }
        for future in as_completed(futures):
            query, locale = futures[future]
            try:
                feed_articles = future.result()
            except (urllib.error.URLError, TimeoutError, OSError, NewsError) as exc:
                warnings.append(
                    f"Recherche « {query} » ({locale[2]}) : {exc}"
                )
                continue

            for article in feed_articles:
                existing = by_url.get(article.url)
                if existing is None or article.relevance > existing.relevance:
                    by_url[article.url] = article

    recent_articles = filter_recent_articles(list(by_url.values()))

    if not recent_articles:
        details = "\n".join(warnings)
        message = (
            "Aucune nouvelle pertinente publiée au cours du dernier mois "
            "n’a été reçue des flux internationaux."
        )
        if details:
            message = f"{message}\n{details}"
        raise NewsError(message)

    now = datetime.now().astimezone()

    def rank(article: Article) -> tuple[float, int]:
        published = article.published_at
        publication_timestamp = (
            published.astimezone(timezone.utc).timestamp()
            if published is not None
            else float("-inf")
        )
        return publication_timestamp, article.relevance

    ranked = sorted(recent_articles, key=rank, reverse=True)
    return ranked[:MAX_ARTICLES], warnings


def summarize_from_feed(articles: list[Article]) -> list[dict[str, Any]]:
    points = []
    for index, article in enumerate(articles, start=1):
        text = article.article_text or article.description
        if article.article_text:
            sentences = re.split(r"(?<=[.!?])\s+", text.strip())
            summary = " ".join(sentences[:4]).strip()[:900]
        else:
            summary = article.description or (
                "Aucun extrait ni texte d’article n’est disponible."
            )
        points.append({"article_index": index, "summary": summary})
    return points


def _article_summary_input(article: Article) -> dict[str, str]:
    return {
        "title": article.title,
        "source": article.source,
        "rss_excerpt": article.description,
        "article_text": article.article_text[:MAX_ARTICLE_TEXT_CHARS],
    }


def _summarize_with_gemini(
    articles: list[Article], api_key: str
) -> list[dict[str, Any]]:
    base_url = os.environ.get(
        "GEMINI_API_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    ).rstrip("/")
    model = os.environ.get("NEWS_AI_MODEL", "").strip() or "gemini-3.8-flash"
    prompt = (
        "Tu es un rédacteur scientifique prudent. Pour chaque article de la "
        "liste, peu importe la langue du texte source, rédige en français "
        "un résumé détaillé de 3 à 5 phrases. Utilise "
        "en priorité le texte de l’article; si celui-ci est vide, utilise son "
        "extrait RSS. N’ajoute aucun fait qui n’est pas présent dans ces textes. "
        "Retourne exactement un point pour chacun des articles fournis, sans en "
        "omettre ni en fusionner. Réponds uniquement en JSON avec la forme "
        '{"points":[{"article_index":1,"summary":"..."}]}. '
        "Les indices sont ceux de la liste fournie et commencent à 1."
    )
    content = json.dumps(
        [
            {"article_index": index, **_article_summary_input(article)}
            for index, article in enumerate(articles, start=1)
        ],
        ensure_ascii=False,
    )
    payload = {
        "system_instruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": content}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    endpoint = (
        f"{base_url}/models/{urllib.parse.quote(model, safe='')}:generateContent"
    )
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read())
        content = result["candidates"][0]["content"]["parts"][0]["text"]
        points = json.loads(content)["points"]
    except urllib.error.HTTPError as exc:
        raise _provider_http_error("Gemini", exc, api_key) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NewsError(f"Le service Gemini est inaccessible : {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NewsError("La réponse de Gemini est invalide.") from exc

    return _validate_points(points, articles)


def _summarize_with_chat_completions(
    articles: list[Article],
    api_key: str,
    *,
    provider: str,
    base_url: str,
    model: str,
) -> list[dict[str, Any]]:
    prompt = (
        "Tu es un rédacteur scientifique prudent. Pour chaque article, "
        "peu importe la langue du texte source, rédige en français "
        "un résumé détaillé de 3 à 5 phrases. Utilise en priorité le texte "
        "de l’article; si celui-ci est vide, utilise son extrait RSS. "
        "N’ajoute aucun fait absent de ces textes. Retourne exactement un "
        "point pour chaque article fourni, sans en omettre ni en fusionner. "
        "Réponds en JSON avec la forme "
        '{"points":[{"article_index":1,"summary":"..."}]}. '
        "Les indices sont ceux de la liste fournie et commencent à 1."
    )
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    [
                        {
                            "article_index": index,
                            **_article_summary_input(article),
                        }
                        for index, article in enumerate(articles, start=1)
                    ],
                    ensure_ascii=False,
                ),
            },
        ],
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read())
        content = result["choices"][0]["message"]["content"]
        points = json.loads(content)["points"]
    except urllib.error.HTTPError as exc:
        raise _provider_http_error(provider, exc, api_key) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NewsError(f"Le service {provider} est inaccessible : {exc}") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NewsError(f"La réponse de {provider} est invalide.") from exc
    return _validate_points(points, articles)


def _provider_http_error(
    provider: str, error: urllib.error.HTTPError, api_key: str
) -> NewsError:
    detail = ""
    try:
        response_body = error.read()
    except OSError:
        response_body = b""
    finally:
        error.close()

    if response_body:
        try:
            error_data = json.loads(response_body)
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = response_body.decode("utf-8", errors="replace").strip()
        else:
            if isinstance(error_data, dict):
                error_value = error_data.get("error", error_data)
                if isinstance(error_value, dict):
                    detail = str(
                        error_value.get("message")
                        or error_value.get("detail")
                        or error_value.get("error")
                        or ""
                    )
                elif isinstance(error_value, str):
                    detail = error_value

    if api_key:
        detail = detail.replace(api_key, "[clé masquée]")
    detail = re.sub(r"\s+", " ", detail).strip()
    cloudflare_1010 = bool(
        re.search(
            r"(?:error\s*)?code\s*[:#]?\s*1010\b|\berror\s+1010\b",
            detail,
            re.IGNORECASE,
        )
    )

    message = f"{provider} a répondu HTTP {error.code}"
    if detail:
        message += f" : {detail[:400]}"
    if error.code == 403:
        if cloudflare_1010:
            message += (
                ". Le code Cloudflare 1010 indique un blocage de la requête "
                "selon la signature du client; il ne prouve pas que la clé "
                "ou le projet est incorrect."
            )
            ray_id = error.headers.get("CF-Ray") if error.headers else None
            if ray_id:
                message += f" Identifiant Cloudflare (Ray ID) : {ray_id}."
            message += (
                " Essayez un autre réseau, sans VPN ni proxy si possible. "
                "Si le blocage persiste, contactez le support Groq avec "
                "le Ray ID, jamais avec la clé API."
            )
        else:
            message += (
                ". Vérifiez que la clé API est active, qu’elle appartient au bon "
                "compte/projet et que ce compte a accès à l’API et au modèle."
            )
    elif error.code == 429:
        message += (
            ". La limite de requêtes ou de quota est atteinte; l’application "
            "essaie le fournisseur suivant."
        )
    elif error.code == 503:
        message += (
            ". Le service est temporairement indisponible; l’application "
            "essaie le fournisseur suivant."
        )
    return NewsError(message)


def summarize_with_ai(articles: list[Article]) -> list[dict[str, Any]]:
    """Try Mistral, Gemini, then Groq before legacy provider configuration."""
    mistral_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    providers: list[tuple[str, str, str, str]] = []
    if mistral_key:
        providers.append(
            (
                "Mistral",
                mistral_key,
                os.environ.get("MISTRAL_API_BASE_URL", "").strip()
                or "https://api.mistral.ai/v1",
                os.environ.get("MISTRAL_MODEL", "").strip()
                or "mistral-small-latest",
            )
        )
    if gemini_key:
        providers.append(("Gemini", gemini_key, "", ""))
    if groq_key:
        providers.append(
            (
                "Groq",
                groq_key,
                os.environ.get("GROQ_API_BASE_URL", "").strip()
                or "https://api.groq.com/openai/v1",
                os.environ.get("GROQ_MODEL", "").strip()
                or "llama-3.3-70b-versatile",
            )
        )

    provider_errors: list[str] = []
    for provider, api_key, base_url, model in providers:
        try:
            if provider == "Gemini":
                return _summarize_with_gemini(articles, api_key)
            return _summarize_with_chat_completions(
                articles,
                api_key,
                provider=provider,
                base_url=base_url,
                model=model,
            )
        except NewsError as exc:
            provider_errors.append(f"{provider} indisponible : {exc}")

    if provider_errors:
        raise NewsError(" ".join(provider_errors))

    provider = os.environ.get("NEWS_AI_PROVIDER", "").strip().casefold()
    api_key = os.environ.get("NEWS_AI_API_KEY", "").strip()
    if not provider:
        if gemini_key:
            provider = "gemini"
        elif api_key:
            provider = "openai"
        else:
            return summarize_from_feed(articles)

    if provider == "gemini":
        if not gemini_key:
            raise NewsError("La clé Gemini est introuvable (GEMINI_API_KEY).")
        return _summarize_with_gemini(articles, gemini_key)
    if provider != "openai":
        raise NewsError(
            "Fournisseur IA inconnu. Choisissez 'gemini' ou 'openai' "
            "dans NEWS_AI_PROVIDER."
        )
    if not api_key:
        raise NewsError("La clé OpenAI est introuvable (NEWS_AI_API_KEY).")

    return _summarize_with_chat_completions(
        articles,
        api_key,
        provider="OpenAI",
        base_url=os.environ.get(
            "NEWS_AI_BASE_URL", "https://api.openai.com/v1"
        ),
        model=os.environ.get("NEWS_AI_MODEL", "gpt-4o-mini"),
    )


def _validate_points(
    points: Any, articles: list[Article]
) -> list[dict[str, Any]]:
    if not isinstance(points, list):
        raise NewsError("Le rapport du service IA ne contient pas une liste de points.")

    validated: list[dict[str, Any]] = []
    seen: set[int] = set()
    for point in points:
        if not isinstance(point, dict):
            continue
        article_index = point.get("article_index")
        summary = point.get("summary")
        if (
            isinstance(article_index, int)
            and not isinstance(article_index, bool)
            and 1 <= article_index <= len(articles)
            and article_index not in seen
            and isinstance(summary, str)
            and summary.strip()
        ):
            validated.append(
                {"article_index": article_index, "summary": summary.strip()}
            )
            seen.add(article_index)

    expected_indexes = set(range(1, len(articles) + 1))
    if seen != expected_indexes:
        raise NewsError("Le rapport IA ne contient pas un résumé pour chaque article.")
    return validated
