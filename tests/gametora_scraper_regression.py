import unittest

from scrapers.gametora_scraper import _parse_banner_text


class GameToraBannerParsingTests(unittest.TestCase):
    def test_parses_banner_without_generated_css_classes(self):
        banner = _parse_banner_text(
            "30125",
            "Support Card Gacha\n"
            "8\xa0Sept\xa02026,\xa01:00 – 20\xa0Sept\xa02026,\xa00:59\n"
            "Oguri Cap (SSR Wit) New, 0.75%\n"
            "Yaeno Muteki (SSR Guts) New, 0.75%\n",
        )

        self.assertIsNotNone(banner)
        self.assertEqual(banner.banner_id, "30125")
        self.assertEqual(banner.banner_type, "Support Card Gacha")
        self.assertEqual(banner.start_date, "8\xa0Sept\xa02026,\xa01:00")
        self.assertEqual(banner.end_date, "20\xa0Sept\xa02026,\xa00:59")
        self.assertEqual([item.name for item in banner.items], ["Oguri Cap", "Yaeno Muteki"])
        self.assertEqual([item.card_type for item in banner.items], ["SSR", "SSR"])
        self.assertTrue(all(item.is_new for item in banner.items))

    def test_ignores_non_banner_text(self):
        self.assertIsNone(_parse_banner_text("unknown", "Gacha in Uma Musume\nBody"))


if __name__ == "__main__":
    unittest.main()
