import unittest
from unittest.mock import patch

import mikrotik


class RouterResultContextTests(unittest.TestCase):
    def test_offline_result_preserves_wifi_and_check_target(self):
        router = {
            "name": "MOTEL-OUR-PLACE",
            "wifi": "MOTEL-OUR-PLACE",
            "host": "202.57.211.212",
            "check_host": "1.1.1.1",
        }
        with patch.object(mikrotik, "_connect", side_effect=OSError("offline")):
            result = mikrotik.check_router(router)
        self.assertEqual("MOTEL-OUR-PLACE", result["wifi"])
        self.assertEqual("1.1.1.1", result["check_host"])
        self.assertFalse(result["online"])


if __name__ == "__main__":
    unittest.main()
