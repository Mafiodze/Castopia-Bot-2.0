from __future__ import annotations

import unittest

from cogs.constants import FOOTER_TEXT
from cogs.dsc import _article_embed
from cogs.page_parsing import Article
from cogs.tg import _article_message
from cogs.txt_processing import article_card_text, article_meta_lines, strip_rating_lead


def sample() -> Article:
    return Article(
        title="Ритуал 6 — «Пожиратели»",
        url="https://castopia.site/ritual-6",
        text="рейтинг: 5.0 1 / 100 % Первое предложение статьи. Второе не нужно.",
        tags=frozenset({"тип:ритуал", "статус:проверка"}),
        author="Ihavebeenloool",
        last_edit="23 августа 2026",
        rating=5.0,
        votes_count=1,
        popularity=100,
    )


class CardTests(unittest.TestCase):
    def test_strips_rating_widget(self) -> None:
        self.assertEqual(
            strip_rating_lead("рейтинг: 5.0 1 / 100 % Первое предложение."),
            "Первое предложение.",
        )

    def test_card_order(self) -> None:
        text = article_card_text(sample())
        self.assertTrue(text.startswith("Первое предложение статьи."))
        self.assertNotIn("рейтинг: 5.0 1 / 100 % Первое", text)
        pos = [text.index(part) for part in (
            "Первое предложение",
            "Автор: Ihavebeenloool",
            "рейтинг: 5.0 1 / 100 %",
            "Теги:",
            "последнее изменение: 23 августа 2026",
        )]
        self.assertEqual(pos, sorted(pos))
        self.assertNotIn("Правка", text)

    def test_discord_license_only_in_footer(self) -> None:
        embed = _article_embed(sample())
        self.assertEqual(embed.title, sample().title)
        self.assertEqual(embed.footer.text, FOOTER_TEXT)
        self.assertNotIn(FOOTER_TEXT, embed.description or "")
        self.assertIn("Автор:", embed.description or "")
        self.assertIn("последнее изменение:", embed.description or "")

    def test_telegram_title_then_prose_license_once(self) -> None:
        html = _article_message(sample())
        self.assertTrue(html.startswith("<b>Ритуал 6"))
        self.assertIn("Первое предложение статьи.", html)
        self.assertEqual(html.count(FOOTER_TEXT), 1)
        author_at = html.index("Автор:")
        rating_at = html.index("рейтинг:")
        self.assertLess(author_at, rating_at)
        self.assertLess(html.index("Первое предложение"), author_at)

    def test_meta_license_opt_in(self) -> None:
        without = "\n".join(article_meta_lines(sample()))
        with_lic = "\n".join(article_meta_lines(sample(), include_license=True))
        self.assertNotIn(FOOTER_TEXT, without)
        self.assertTrue(with_lic.endswith(FOOTER_TEXT))
