from __future__ import annotations

import unittest

from seek_barrier import PlaybackSeekBarrier


class PlaybackSeekBarrierTest(unittest.TestCase):
    def test_old_timestamp_is_rejected_until_paused_target_arrives(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=False, now=10.0)

        self.assertFalse(
            barrier.observe(67_444, playing=False, rate=10.0, now=10.1)
        )
        self.assertFalse(
            barrier.observe(
                400_173,
                playing=False,
                rate=10.0,
                now=10.2,
            )
        )
        self.assertTrue(
            barrier.observe(400_173, playing=False, rate=10.0, now=10.41)
        )
        self.assertTrue(barrier.target_is_ready(400_173))

    def test_confirmed_paused_target_is_not_reapplied_on_resume(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=False, now=10.0)
        barrier.observe(400_173, playing=False, rate=10.0, now=10.1)
        barrier.observe(400_173, playing=False, rate=10.0, now=10.31)

        target = barrier.playback_started(now=11.0)

        self.assertIsNone(target)
        self.assertFalse(barrier.playback_confirmation_pending())

    def test_unconfirmed_paused_seek_reapplies_and_rejects_old_time(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=False, now=10.0)
        self.assertFalse(
            barrier.observe(67_444, playing=False, rate=10.0, now=10.1)
        )

        target = barrier.playback_started(now=11.0)

        self.assertEqual(target, 400_173)
        self.assertTrue(barrier.playback_confirmation_pending())
        self.assertFalse(
            barrier.observe(67_444, playing=True, rate=10.0, now=11.1)
        )

    def test_exact_post_seek_time_confirms_playback(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=True, now=11.0)

        self.assertFalse(
            barrier.observe(
                400_173,
                playing=True,
                rate=10.0,
                now=11.1,
            )
        )
        self.assertTrue(barrier.playback_confirmation_pending())
        self.assertTrue(
            barrier.observe(400_173, playing=True, rate=10.0, now=11.31)
        )
        self.assertFalse(barrier.playback_confirmation_pending())
        self.assertTrue(barrier.target_is_ready(400_173))
        self.assertEqual(barrier.confirmation_serial, 1)
        self.assertEqual(barrier.confirmed_video_ms, 400_173)

    def test_far_future_timestamp_is_not_mistaken_for_target(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(100_000, playing=True, now=10.0)

        self.assertFalse(
            barrier.observe(500_000, playing=True, rate=1.0, now=10.1)
        )
        self.assertTrue(barrier.playback_confirmation_pending())

    def test_new_seek_discards_the_previous_target_settle_period(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(100_000, playing=False, now=10.0)
        self.assertFalse(
            barrier.observe(100_000, playing=False, rate=1.0, now=10.1)
        )

        barrier.request(200_000, playing=False, now=10.15)

        self.assertFalse(
            barrier.observe(100_000, playing=False, rate=1.0, now=10.2)
        )
        self.assertFalse(
            barrier.observe(200_000, playing=False, rate=1.0, now=10.25)
        )
        self.assertTrue(
            barrier.observe(200_000, playing=False, rate=1.0, now=10.46)
        )
        self.assertEqual(barrier.confirmed_target_ms, 200_000)
        self.assertEqual(barrier.confirmation_serial, 1)

    def test_timeout_releases_barrier_without_confirmation(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=True, now=10.0)

        self.assertTrue(
            barrier.observe(67_444, playing=True, rate=10.0, now=18.1)
        )
        self.assertFalse(barrier.playback_confirmation_pending())
        self.assertEqual(barrier.confirmation_serial, 0)

    def test_timeout_expires_even_without_a_new_timestamp(self) -> None:
        barrier = PlaybackSeekBarrier()
        barrier.request(400_173, playing=True, now=10.0)

        self.assertTrue(barrier.expire(now=18.1))
        self.assertFalse(barrier.playback_confirmation_pending())


if __name__ == "__main__":
    unittest.main()
