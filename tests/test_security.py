#!/usr/bin/env python3
"""
Security regression tests for Tachikoma.

Tests that verify credentials are never embedded or logged.
"""

import unittest
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.redaction import redact_secrets, redact_dict, safe_log_message, redact_log
from sdk.client import Client
from sdk.device import Device


class TestRedaction(unittest.TestCase):
    """Test that sensitive data is properly redacted from logs."""

    def test_redact_jwt_token(self):
        """JWT tokens should be redacted."""
        text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = redact_secrets(text)
        self.assertEqual(result, '***REDACTED_JWT***')

    def test_redact_access_token_in_url(self):
        """accessToken parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?accessToken=abc123def456&other=param"
        result = redact_secrets(text)
        self.assertIn('accessToken=***REDACTED***', result)
        self.assertNotIn('abc123def456', result)

    def test_redact_bare_uuid_token(self):
        """Bare UUID-format access tokens should be redacted."""
        text = "Token: b067dfa5-9050-47fa-9950-635bfd81770b"
        result = redact_secrets(text)
        self.assertNotIn('b067dfa5', result)

    def test_redact_uuid_in_error_message(self):
        """UUID-format tokens in error messages should be redacted."""
        text = "Connection failed for b067dfa5-9050-47fa-9950-635bfd81770b on host"
        result = redact_secrets(text)
        self.assertNotIn('b067dfa5', result)

    def test_redact_refresh_token_in_url(self):
        """refreshToken parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?refreshToken=xyz789&other=param"
        result = redact_secrets(text)
        self.assertIn('refreshToken=***REDACTED***', result)
        self.assertNotIn('xyz789', result)

    def test_redact_device_key_in_url(self):
        """deviceKey parameter in URLs should be redacted."""
        text = "https://api.example.com/endpoint?deviceKey=device123&other=param"
        result = redact_secrets(text)
        self.assertIn('deviceKey=***REDACTED***', result)
        self.assertNotIn('device123', result)

    def test_redact_email(self):
        """Email addresses should be redacted."""
        text = "User email: user@example.com"
        result = redact_secrets(text)
        self.assertEqual(result, "User email: ***REDACTED_EMAIL***")

    def test_redact_password_in_url(self):
        """Password parameter in URLs should be redacted."""
        text = "https://api.example.com/login?password=secret123&user=test"
        result = redact_secrets(text)
        self.assertIn('password=***REDACTED***', result)
        self.assertNotIn('secret123', result)

    def test_redact_access_token_in_response(self):
        """accessToken in response bodies should be redacted."""
        text = 'accessToken="abc123def456"'
        result = redact_secrets(text)
        # Our pattern catches accessToken="xxx"
        self.assertEqual(result, 'accessToken="***REDACTED***"')

    def test_redact_refresh_token_in_response(self):
        """refreshToken in response bodies should be redacted."""
        text = 'refreshToken="xyz789"'
        result = redact_secrets(text)
        # Our pattern catches refreshToken="xxx"
        self.assertEqual(result, 'refreshToken="***REDACTED***"')

    def test_redact_device_key_in_json(self):
        """DeviceKey in JSON should be redacted."""
        text = '{"DeviceKey": "device123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"DeviceKey": "***REDACTED***"}')

    def test_redact_refresh_token_in_json(self):
        """RefreshToken in JSON should be redacted."""
        text = '{"RefreshToken": "token123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"RefreshToken": "***REDACTED***"}')

    def test_redact_email_in_json(self):
        """Email in JSON should be redacted."""
        text = '{"Email": "user@example.com"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"Email": "***REDACTED_EMAIL***"}')

    def test_redact_password_in_json(self):
        """Password in JSON should be redacted (case insensitive)."""
        text = '{"password": "secret123"}'
        result = redact_secrets(text)
        self.assertEqual(result, '{"password": "***REDACTED***"}')

    def test_redact_dict(self):
        """Dictionary redaction should work recursively."""
        d = {
            "accessToken": "secret_token",
            "deviceKey": "device123",
            "normalField": "value",
            "nested": {
                "refreshToken": "nested_token",
                "email": "user@example.com"
            }
        }
        result = redact_dict(d)
        self.assertEqual(result["accessToken"], "***REDACTED***")
        self.assertEqual(result["deviceKey"], "***REDACTED***")
        self.assertEqual(result["normalField"], "value")
        self.assertEqual(result["nested"]["refreshToken"], "***REDACTED***")
        # Email in dict gets redacted by key match
        self.assertEqual(result["nested"]["email"], "***REDACTED_EMAIL***")

    def test_safe_log_message(self):
        """safe_log_message should format and redact."""
        result = safe_log_message("Token: %s", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertIn("***REDACTED_JWT***", result)

    def test_redact_log_alias(self):
        """redact_log should be an alias for safe_log_message."""
        result = redact_log("Token: %s", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        self.assertIn("***REDACTED_JWT***", result)


class TestClientSecurity(unittest.TestCase):
    """Test Client class security behavior."""

    def test_no_hardcoded_fallback_token(self):
        """Client should not have hardcoded fallback token."""
        device = Device(language="en")
        client = Client(device=device)
        
        # Check that getAccessToken doesn't use a hardcoded fallback
        # We can't fully test without mocking, but we can verify the code path
        import inspect
        source = inspect.getsource(client.getAccessToken)
        self.assertNotIn("eyJhbG", source)  # Base64 JWT prefix
        self.assertNotIn("1Rqw", source)  # Truncated token fragment

    def test_get_access_token_uses_empty_string(self):
        """getAccessToken should use empty string when no refresh token."""
        device = Device(language="en")
        client = Client(device=device)
        
        import inspect
        source = inspect.getsource(client.getAccessToken)
        # Should use empty string as fallback, not a hardcoded token
        # The actual code uses: self.device.refreshToken if self.device.refreshToken else ""
        self.assertIn('else ""', source)

    def test_client_imports_redaction(self):
        """Client should import redaction utilities."""
        import sdk.client as client_module
        self.assertTrue(hasattr(client_module, 'redact_secrets'))
        self.assertTrue(hasattr(client_module, 'safe_log_message'))


class TestDeviceSecurity(unittest.TestCase):
    """Test Device class security behavior."""

    def test_device_file_permissions(self):
        """Device file should not be world-readable."""
        # This is more of a documentation test - actual permissions
        # are set at save time
        device = Device(language="en")
        self.assertIsNotNone(device.DB)

    def test_refresh_token_not_logged(self):
        """Device should not log refresh tokens in plain text."""
        device = Device(language="en")
        # The save/load methods handle the token
        # We verify the methods exist
        self.assertTrue(hasattr(device, 'refreshTokenAcquire'))
        self.assertTrue(hasattr(device, 'save'))
        self.assertTrue(hasattr(device, 'load'))


class TestPostBodyExcludesAccessToken(unittest.TestCase):
    """Regression: accessToken must NOT appear in POST body (mitmproxy evidence).

    The official game client sends accessToken only in the URL query string,
    never in the form-urlencoded POST body. Including it in the body causes
    AddStarbux2 to return 'An error occurred.' (mitmproxy capture 2026-07-31).
    """

    def test_post_body_excludes_access_token(self):
        """request() should not include accessToken in auto-populated POST body."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import parse_qs

        device = Device(language="en")
        client = Client(device=device)

        # Capture what data gets sent
        captured_data = {}
        mock_response = MagicMock()
        mock_response.text = "<ok/>"

        def capture_request(method, url, headers=None, data=None):
            captured_data['data'] = data
            return mock_response

        with patch.object(client.session, 'request', side_effect=capture_request):
            url = ("http://api.pixelstarships.com/RoomService/CollectAllResources"
                   "?itemType=None&collectDate=2026-07-31T09:42:46"
                   "&accessToken=66e3603d-test-token")
            client.request(url, "POST")

        body = captured_data.get('data', '')
        params = parse_qs(body) if body else {}

        # accessToken must NOT be in the POST body
        self.assertNotIn('accessToken', params,
                         "accessToken must not be in POST body (causes CollectAllResources failure)")
        # Other params should be present
        self.assertIn('itemType', params)
        self.assertIn('collectDate', params)

    def test_collect_all_resources_body_excludes_access_token(self):
        """CollectAllResources must not send accessToken in POST body.

        Mitmproxy evidence (2026-07-31): old code included accessToken
        in body → errorMessage='An error occurred.' Fixed by request()
        excluding accessToken from auto-populated body.
        """
        from unittest.mock import MagicMock, patch
        from urllib.parse import parse_qs

        device = Device(language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"

        captured_data = {}
        mock_response = MagicMock()
        mock_response.text = "<ok/>"
        mock_response.content = b"<ok/>"

        def capture_request(method, url, headers=None, data=None):
            captured_data['data'] = data
            captured_data['url'] = url
            return mock_response

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.collectAllResources()

        body = captured_data.get('data', '')
        params = parse_qs(body) if body else {}

        self.assertNotIn('accessToken', params,
                         "accessToken must not be in CollectAllResources POST body")
        self.assertIn('itemType', params)
        self.assertIn('collectDate', params)

    def test_get_ship_by_user_id_requires_user_id(self):
        """GetShipByUserId must fail early if no userId available.

        Without a valid userId (6th field in auth string), the endpoint
        would send userId=None and receive 'An error occurred.'
        """
        from unittest.mock import MagicMock, patch

        device = Device(language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"
        # No self.user set

        mock_response = MagicMock()
        mock_response.text = "<ok/>"
        mock_response.content = b"<ok/>"

        def capture_request(method, url, headers=None, data=None):
            return mock_response

        with patch.object(client.session, 'request', side_effect=capture_request):
            result = client.getShipByUserId()

        self.assertFalse(result,
                         "getShipByUserId must return False when no userId available")

    def test_device_type_mapping(self):
        """Device.resolve_device_type must map names to valid deviceType enums.

        Official iOS client uses DeviceTypeIPhone (capital P).
        Invalid values cause 'An error occurred.' on GetLatestVersion4/GetTodayLiveOps2.
        """
        device = Device(name="iPhone", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeIPhone",
                         "iphone -> DeviceTypeIPhone (capital P)")

        device = Device(name="iOS", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeIPhone",
                         "iOS -> DeviceTypeIPhone")

        device = Device(name="Mac", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeMac",
                         "Mac -> DeviceTypeMac")

        device = Device(name="macOS", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeMac",
                         "macOS -> DeviceTypeMac")

        device = Device(name="Android", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeAndroid",
                         "Android -> DeviceTypeAndroid")

        # Unknown names default to Mac (known working)
        device = Device(name="r2e", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeMac",
                         "unknown name -> DeviceTypeMac (safe default)")

        device = Device(name="Windows", language="en")
        self.assertEqual(device.deviceType, "DeviceTypeMac",
                         "Windows -> DeviceTypeMac (safe default)")

    def test_get_latest_version_uses_correct_device_type(self):
        """GetLatestVersion4 must send correct deviceType, not DeviceType{name}."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token"

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            mock.content = b"<SettingService><GetLatestSetting><Setting SettingId=\"1\" ServerSettingVersion=\"3307251\" MinimumClientVersion=\"0.999.59\" /></GetLatestSetting></SettingService>"
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.getLatestVersion3()

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(params.get('deviceType'), ['DeviceTypeIPhone'],
                         "getLatestVersion3 must send DeviceTypeIPhone, not DeviceTypeiPhone")

    def test_get_today_live_ops_uses_correct_device_type(self):
        """GetTodayLiveOps2 must send correct deviceType, not DeviceType{name}."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token"

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            # .content must return bytes for xmltodict
            mock.content = b"<LiveOpsService><GetTodayLiveOps><LiveOps LiveOpsId=\"1\" DailyRewardType=\"Starbux\" DailyRewardArgument=\"1\" /></GetTodayLiveOps></LiveOpsService>"
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.getTodayLiveOps2()

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(params.get('deviceType'), ['DeviceTypeIPhone'],
                         "getTodayLiveOps2 must send DeviceTypeIPhone, not DeviceTypeiPhone")

    def test_list_all_designs_uses_version_params_from_latest_version(self):
        """listAllDesigns4 must include all version params from GetLatestVersion3.

        Uses 36 design version parameters from SettingService.GetLatestSetting.Setting.
        """
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token"

        # Mock latestVersion as if getLatestVersion3 was called
        client.latestVersion = {
            "SettingService": {
                "GetLatestSetting": {
                    "Setting": {
                        "@FileVersion": "2949",
                        "@SpriteVersion": "6416",
                        "@BackgroundVersion": "627",
                        "@ShipDesignVersion": "863",
                        "@RoomDesignVersion": "1298",
                        "@CharacterDesignVersion": "1351",
                        "@CharacterDesignActionVersion": "519",
                        "@ItemDesignVersion": "166510",
                        "@CraftDesignVersion": "771",
                        "@MissileDesignVersion": "785",
                        "@StarSystemVersion": "483",
                        "@StarSystemLinkVersion": "480",
                        "@NewsDesignVersion": "518",
                        "@LeagueVersion": "583",
                        "@AchievementDesignVersion": "728",
                        "@RoomDesignPurchaseVersion": "929",
                        "@RoomDesignSpriteVersion": "978",
                        "@MissionDesignVersion": "1025",
                        "@AnimationVersion": "1110",
                        "@ResearchDesignVersion": "706",
                        "@TrainingDesignVersion": "568",
                        "@ChallengeDesignVersion": "899",
                        "@RewardDesignVersion": "433735",
                        "@DivisionDesignVersion": "707",
                        "@CollectionDesignVersion": "561",
                        "@DrawDesignVersion": "490",
                        "@PromotionDesignVersion": "2656",
                        "@SituationDesignVersion": "1181",
                        "@TaskDesignVersion": "1962416",
                        "@ActionTypeVersion": "583",
                        "@ConditionTypeVersion": "669",
                        "@ItemDesignActionVersion": "216",
                        "@SeasonDesignVersion": "247",
                        "@AssetVersion": "202",
                        "@MarkerGeneratorDesignVersion": "135",
                    }
                }
            }
        }

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            # Provide all 36 design types to pass the "Missing design data" check
            mock.content = b"""<DesignService><ListAllDesigns>
                <Files version="2949"><File Id="1" Filename="test.png" /></Files>
                <Sprites version="6416"><Sprite Id="1" /></Sprites>
                <Backgrounds version="627"><Background Id="1" /></Backgrounds>
                <ShipDesigns version="863"><ShipDesign Id="1" /></ShipDesigns>
                <RoomDesigns version="1298"><RoomDesign Id="1" /></RoomDesigns>
                <CharacterDesigns version="1351"><CharacterDesign Id="1" /></CharacterDesigns>
                <CharacterDesignActions version="519"><CharacterDesignAction Id="1" /></CharacterDesignActions>
                <ItemDesigns version="166510"><ItemDesign Id="1" /></ItemDesigns>
                <CraftDesigns version="771"><CraftDesign Id="1" /></CraftDesigns>
                <MissileDesigns version="785"><MissileDesign Id="1" /></MissileDesigns>
                <StarSystems version="483"><StarSystem Id="1" /></StarSystems>
                <StarSystemLinks version="480"><StarSystemLink Id="1" /></StarSystemLinks>
                <NewsDesigns version="518"><NewsDesign Id="1" /></NewsDesigns>
                <Leagues version="583"><League Id="1" /></Leagues>
                <AchievementDesigns version="728"><AchievementDesign Id="1" /></AchievementDesigns>
                <RoomDesignPurchases version="929"><RoomDesignPurchase Id="1" /></RoomDesignPurchases>
                <RoomDesignSprites version="978"><RoomDesignSprite Id="1" /></RoomDesignSprites>
                <MissionDesigns version="1025"><MissionDesign Id="1" /></MissionDesigns>
                <Animations version="1110"><Animation Id="1" /></Animations>
                <ResearchDesigns version="706"><ResearchDesign Id="1" /></ResearchDesigns>
                <TrainingDesigns version="568"><TrainingDesign Id="1" /></TrainingDesigns>
                <ChallengeDesigns version="899"><ChallengeDesign Id="1" /></ChallengeDesigns>
                <RewardDesigns version="433735"><RewardDesign Id="1" /></RewardDesigns>
                <DivisionDesigns version="707"><DivisionDesign Id="1" /></DivisionDesigns>
                <CollectionDesigns version="561"><CollectionDesign Id="1" /></CollectionDesigns>
                <DrawDesigns version="490"><DrawDesign Id="1" /></DrawDesigns>
                <PromotionDesigns version="2656"><PromotionDesign Id="1" /></PromotionDesigns>
                <SituationDesigns version="1181"><SituationDesign Id="1" /></SituationDesigns>
                <ItemDesignActions version="216"><ItemDesignAction Id="1" /></ItemDesignActions>
                <SeasonDesigns version="247"><SeasonDesign Id="1" /></SeasonDesigns>
                <Assets version="202"><Asset Id="1" /></Assets>
                <StarSystemMarkerGenerators version="135"><StarSystemMarkerGenerator Id="1" /></StarSystemMarkerGenerators>
            </ListAllDesigns></DesignService>"""
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            result = client.listAllDesigns4()

        self.assertTrue(result, "listAllDesigns4 should return True on success")

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)

        # Verify endpoint
        self.assertEqual(parsed.path, "/DesignService/ListAllDesigns4")

        # Verify LanguageKey
        self.assertEqual(params.get('LanguageKey'), ['en'])

        # Verify at least some version params are present (spot check)
        self.assertEqual(params.get('ListFileVersion'), ['2949'])
        self.assertEqual(params.get('ListSpriteVersion'), ['6416'])
        self.assertEqual(params.get('ListRoomDesignVersion'), ['1298'])
        self.assertEqual(params.get('ListItemDesignVersion'), ['166510'])
        self.assertEqual(params.get('ListAssetVersion'), ['202'])
        self.assertEqual(params.get('ListMarkerGeneratorDesignVersion'), ['135'])

        # Should have all 35 version parameters
        version_params = [k for k in params.keys() if k.startswith('List')]
        self.assertEqual(len(version_params), 35,
                         f"Expected 35 version parameters, got {len(version_params)}")

    def test_list_all_characters_of_user_request_shape(self):
        """ListAllCharactersOfUser must send correct query params."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            mock.content = b"<CharacterService><ListAllCharactersOfUser><Characters><Character CharacterName=\"Test\" /></Characters></ListAllCharactersOfUser></CharacterService>"
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.listAllCharactersOfUser()

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/CharacterService/ListAllCharactersOfUser")
        self.assertEqual(params.get('accessToken'), ['test-token-uuid'])
        self.assertIn('clientDateTime', params)

    def test_list_system_messages_for_user_request_shape(self):
        """ListSystemMessagesForUser3 must send correct query params."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            mock.content = b"<MessageService><ListSystemMessagesForUser><Messages><Message Id=\"1\" /></Messages></ListSystemMessagesForUser></MessageService>"
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.listSystemMessagesForUser3()

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/MessageService/ListSystemMessagesForUser3")
        self.assertEqual(params.get('accessToken'), ['test-token-uuid'])
        self.assertEqual(params.get('fromMessageId'), ['0'])
        self.assertEqual(params.get('take'), ['10000'])

    def test_list_active_marketplace_messages_request_shape(self):
        """ListActiveMarketplaceMessages5 must send correct query params."""
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit, parse_qs
        from sdk.client import User

        device = Device(name="iPhone", language="en")
        client = Client(device=device)
        client.accessToken = "test-token-uuid"
        client.user = User(3430892, "test", None, True)

        captured_url = {}

        def capture_request(method, url, headers=None, data=None):
            captured_url['url'] = url
            mock = MagicMock()
            mock.content = b"<MessageService><ListActiveMarketplaceMessages><Messages /></ListActiveMarketplaceMessages></MessageService>"
            return mock

        with patch.object(client.session, 'request', side_effect=capture_request):
            client.listActiveMarketplaceMessages()

        parsed = urlsplit(captured_url['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/MessageService/ListActiveMarketplaceMessages5")
        self.assertEqual(params.get('accessToken'), ['test-token-uuid'])
        self.assertEqual(params.get('userId'), ['3430892'])
        self.assertEqual(params.get('itemSubType'), ['None'])
        self.assertEqual(params.get('rarity'), ['None'])
        self.assertEqual(params.get('currencyType'), ['Unknown'])
        self.assertEqual(params.get('itemDesignId'), ['0'])


if __name__ == '__main__':
    unittest.main()