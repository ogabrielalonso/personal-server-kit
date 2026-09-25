import unittest

from pskit import budget as B


class BudgetTest(unittest.TestCase):
    def test_32_gib_values(self):
        b = B.compute(32 * B.GIB, with_ci=True)
        g = b.as_gib()
        self.assertEqual((g["brain_low"], g["brain_high"], g["brain_max"]), (4.0, 8.0, 10.0))
        self.assertEqual((g["user_high"], g["user_max"]), (12.25, 14.25))
        self.assertEqual((g["ci_high"], g["ci_max"]), (3.0, 4.0))
        self.assertEqual(g["system_low"], 2.0)
        self.assertAlmostEqual(g["margin_bytes"], 3.75, places=2)

    def test_ceilings_stay_below_ram(self):
        for gib in (4, 6, 8, 12, 16, 24, 32, 64, 128):
            for ci in (False, True):
                b = B.compute(gib * B.GIB, with_ci=ci)
                self.assertLess(b.ceilings_sum(), b.ram_bytes, (gib, ci))
                self.assertGreaterEqual(b.margin_bytes, 1 * B.GIB, (gib, ci))
                self.assertLessEqual(b.brain_low, b.brain_high)
                self.assertLessEqual(b.brain_high, b.brain_max)

    def test_brain_peak_drives_reservation(self):
        b = B.compute(32 * B.GIB, brain_peak_bytes=4 * B.GIB)
        self.assertEqual(b.brain_max, 5 * B.GIB)
        huge = B.compute(32 * B.GIB, brain_peak_bytes=40 * B.GIB)
        self.assertLessEqual(huge.brain_max, 32 * B.GIB / 3)

    def test_too_small(self):
        with self.assertRaises(ValueError):
            B.compute(1 * B.GIB)

    def test_systemd_sizes(self):
        self.assertEqual(B.to_systemd(10 * B.GIB), "10G")
        self.assertEqual(B.to_systemd(1536 * B.MIB), "1536M")
        self.assertEqual(B.to_systemd(0), "0")

    def test_swap_size(self):
        self.assertEqual(B.swap_size_bytes(32 * B.GIB, 500 * B.GIB), 16 * B.GIB)
        self.assertEqual(B.swap_size_bytes(8 * B.GIB, 500 * B.GIB), 4 * B.GIB)
        self.assertEqual(B.swap_size_bytes(8 * B.GIB, 20 * B.GIB), 2 * B.GIB)
        self.assertEqual(B.swap_size_bytes(8 * B.GIB, 5 * B.GIB), 0)


if __name__ == "__main__":
    unittest.main()
