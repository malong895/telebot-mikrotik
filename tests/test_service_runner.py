import unittest

import service_runner


class SupervisorLoopTests(unittest.TestCase):
    def test_fast_crash_is_backed_off_and_restarted(self):
        runs = []
        sleeps = []

        def run_once():
            runs.append("run")
            return 1, 0.2

        service_runner.supervise(
            run_once,
            sleep_fn=sleeps.append,
            should_stop=lambda: False,
            max_cycles=2,
        )

        self.assertEqual(2, len(runs))
        self.assertEqual([10.0], sleeps)

    def test_stable_process_uses_short_restart_delay(self):
        self.assertEqual(2.0, service_runner.restart_delay(360.0))


if __name__ == "__main__":
    unittest.main()
