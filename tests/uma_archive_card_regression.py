import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from services.uma_archive_card import card_html, grade_icon, uma_portrait
from services import uma_archive_card as cards


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
        for name in ['T.M. Opera O', 'T M Opera O', 'TM Opera O', ' Ｔ．Ｍ． Opera O ']:
            with self.subTest(name=name):
                self.assertTrue(uma_portrait(name).endswith('chara_stand_101501.webp'))

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

    def test_outfit_portraits_and_normalized_names(self):
        for name, variant, card_id in [
            ('Mihono Bourbon', 'CODE: ICING', 102602),
            ('El Condor Pasa', 'Kukulkan Warrior', 101402),
            ('Haru Urara', 'New Year ♪ New Urara!', 105202),
            (' haru urara ', '[new year new urara]', 105202),
            ('Mihono Bourbon', 'ＣＯＤＥ： ＩＣＩＮＧ', 102602),
            ('Mihono Bourbon', 'Unknown outfit', 102601),
            ('Mihono Bourbon', None, 102601),
        ]:
            with self.subTest(name=name, variant=variant):
                self.assertTrue(uma_portrait(name, variant).endswith(f'chara_stand_{card_id}.webp'))
        self.assertIsNone(uma_portrait('Unknown Uma', 'CODE: ICING'))
        self.assertEqual(uma_portrait('Haru Urara', 'CODE: ICING'), uma_portrait('Haru Urara'))

    def test_both_card_layouts_use_outfit_portraits(self):
        row = dict(uma_name='Mihono Bourbon', variant='CODE: ICING', trainer_name='Trainer',
                   club_name='Club', score=20000, rank='UG', position=1, participants=30)
        images = {uma_portrait(row['uma_name']): 'data:image/webp;base64,base',
                  uma_portrait(row['uma_name'], row['variant']): 'data:image/webp;base64,outfit'}
        for personal in (False, True):
            with self.subTest(personal=personal):
                html = card_html([row], 'Title', 'Scope', 'Page 1', images, personal=personal)
                self.assertIn('data:image/webp;base64,outfit', html)
                self.assertNotIn('data:image/webp;base64,base', html)


class ArchiveImageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cards._render_cache.clear()
        self.addCleanup(cards._render_cache.clear)
        lock = patch.object(cards, '_render_cache_lock', asyncio.Lock())
        lock.start()
        self.addCleanup(lock.stop)
        self.rows = [dict(uma_name='Special Week', variant='Original', trainer_name='Trainer',
                          club_name='Club', score=20000, rank='UG', position=1, participants=30)]

    async def test_identical_pages_reuse_image_and_concurrent_requests_render_once(self):
        async def render(*args, **kwargs):
            await asyncio.sleep(0)
            return b'png'

        with patch.object(cards, '_render_uncached', new=AsyncMock(side_effect=render)) as renderer:
            images = await asyncio.gather(*(cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
                                            for _ in range(3)))
            self.assertEqual(images, [b'png'] * 3)
            renderer.assert_awaited_once()

    async def test_changed_visible_scores_and_scope_get_new_images(self):
        with patch.object(cards, '_render_uncached', new=AsyncMock(return_value=b'png')) as renderer:
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            self.rows[0]['score'] += 1
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            await cards.render_card(self.rows, 'Title', 'Other club', 'Page 1')
            self.assertEqual(renderer.await_count, 3)

    async def test_cache_expires_and_byte_limit_evicts_old_images(self):
        with patch.object(cards, '_render_uncached', new=AsyncMock(return_value=b'png')) as renderer, \
             patch.object(cards.time, 'monotonic', return_value=0) as clock, \
             patch.object(cards, 'RENDER_CACHE_MAX_BYTES', 4):
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            clock.return_value = 301
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            self.assertEqual(renderer.await_count, 2)
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 2')
            self.assertEqual(len(cards._render_cache), 1)
            await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            self.assertEqual(renderer.await_count, 4)

    async def test_failed_images_are_not_cached(self):
        with patch.object(cards, '_render_uncached', new=AsyncMock(side_effect=[RuntimeError(), b'png'])) as renderer:
            with self.assertRaises(RuntimeError):
                await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1')
            self.assertEqual(await cards.render_card(self.rows, 'Title', 'Scope', 'Page 1'), b'png')
            self.assertEqual(renderer.await_count, 2)

    async def test_image_fetch_includes_outfit_portrait(self):
        self.rows[0].update(uma_name='Mihono Bourbon', variant='CODE: ICING')
        # Stop after fetching, before starting the browser; verify the real fetch path.
        with patch.object(cards, 'image_data', new=AsyncMock(return_value={})) as fetch, \
             patch.object(cards, 'card_html', side_effect=RuntimeError('stop before browser')):
            with self.assertRaisesRegex(RuntimeError, 'stop before browser'):
                await cards._render_uncached(self.rows, 'Title', 'Scope', 'Page 1')
        urls = fetch.await_args.args[0]
        self.assertIn(uma_portrait('Mihono Bourbon', 'CODE: ICING'), urls)
        self.assertNotIn(uma_portrait('Mihono Bourbon'), urls)


if __name__ == '__main__':
    unittest.main()
