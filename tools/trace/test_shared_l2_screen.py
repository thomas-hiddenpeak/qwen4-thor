import unittest
from types import SimpleNamespace
from screen_shared_l2 import LocalityScreen


def event(layer, expert):
    return SimpleNamespace(kind='nvme_read', layer=layer, expert=expert,
                           request_id='one', runtime_phase='prefill')


class ScreenTest(unittest.TestCase):
    def test_cross_layer_expert_identity(self):
        screen = LocalityScreen()
        for layer in (0, 1, 0):
            screen(event(layer, 7))
        row = screen.finish()['rows'][0]
        self.assertEqual((row['shared_hits'], row['local_hits']), (1, 1))

    def test_active_layer_reuse_at_equal_total_budget(self):
        screen = LocalityScreen()
        for _ in range(2):
            for expert in range(512):
                screen(event(0, expert))
        row = screen.finish()['rows'][0]
        self.assertEqual(row['load_requests'], 1024)
        self.assertEqual((row['shared_hits'], row['local_hits']), (512, 0))

    def test_shared_lru_matches_list_reference_and_budget(self):
        screen = LocalityScreen()
        cache = []
        hits = 0
        sequence = [(i // 512, i % 512) for i in range(900)]
        sequence += sequence[400:] + sequence[:100]
        for key in sequence:
            if key in cache:
                hits += 1
                cache.remove(key)
            cache.append(key)
            cache = cache[-768:]
            screen(event(*key))
        self.assertEqual(screen.finish()['rows'][0]['shared_hits'], hits)
        self.assertEqual(list(screen.shared), cache)


if __name__ == '__main__':
    unittest.main()
