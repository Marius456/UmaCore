import unittest

from services.uma_archive_card import card_html, grade_icon, uma_portrait


class ArchiveCardTests(unittest.TestCase):
    def test_grade_icons_match_game_tiers_and_plus_and_ultra_levels(self):
        for grade, index in [('G', 0), ('A+', 13), ('SS+', 17), ('UG', 18),
                             ('UG9', 27), ('UF', 28), ('UF4', 32), ('UA9', 87)]:
            with self.subTest(grade=grade):
                self.assertTrue(grade_icon(grade).endswith(f'utx_txt_rank_{index:02d}.webp'))
        self.assertIsNone(grade_icon('../unknown'))

    def test_portrait_mapping_and_missing_character_fallback(self):
        self.assertTrue(uma_portrait('Special Week').endswith('chara_stand_100101.webp'))
        self.assertIsNone(uma_portrait('Unknown Uma'))

    def test_card_escapes_user_text_and_includes_local_images_without_dates(self):
        row = dict(uma_name='Special Week', variant='Original', trainer_name='<script>alert(1)</script>',
                   club_name='A & B', score=20000, rank='UG', position=1, participants=30)
        images = {uma_portrait(row['uma_name']): 'data:image/webp;base64,portrait',
                  grade_icon('UG'): 'data:image/webp;base64,grade'}
        html = card_html([row], 'Overall', 'All clubs', 'Page 1/3', images)
        self.assertIn('data:image/webp;base64,portrait', html)
        self.assertIn('data:image/webp;base64,grade', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('20,000', html)
        self.assertNotIn('Scanned', html)
        personal = card_html([row], 'Your Uma Archive', 'Rank #1', 'Page 1/1', {}, personal=True)
        self.assertIn('#1 / 30', personal)
        self.assertIn('Special Week', personal)


if __name__ == '__main__':
    unittest.main()
