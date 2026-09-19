import unittest

import app


class AppTest(unittest.TestCase):
    def test_combined_app_serves_api_and_twilio_routes(self):
        paths = {route.path for route in app.app.routes}
        self.assertTrue({"/health", "/twilio/voice", "/twilio/gather"} <= paths)


if __name__ == "__main__":
    unittest.main()
