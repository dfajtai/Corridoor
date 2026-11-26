"""Saving and loading of the settings and the profile copy."""

import unittest
from dataclasses import replace

from corridoor import store
from corridoor.errors import StepError
from corridoor.settings import Settings
from tests.support import SessionTestCase


class StoreTest(SessionTestCase):
    """The encrypted store, with the fake XOR in place of DPAPI."""

    def test_settings_round_trip(self) -> None:
        self.assertEqual(store.load_settings(), self.settings)

    def test_nothing_saved_gives_none(self) -> None:
        store.SETTINGS_FILE.unlink()
        self.assertIsNone(store.load_settings())

    def test_unknown_saved_options_are_ignored(self) -> None:
        self.stored_settings_json({"host": "h", "use_pageant": True})
        self.assertEqual(store.load_settings(), Settings(host="h"))

    def test_profile_copy_is_loaded_with_embedded_text(self) -> None:
        profile = store.load_profile(self.settings)
        self.assertIsNone(profile.path)
        self.assertIn("remote 127.0.0.1", profile.text)

    def test_uncopied_profile_stays_the_users_file(self) -> None:
        settings = replace(self.settings, copy_profile=False)
        store.save_settings(settings)
        self.assertFalse(store.PROFILE_FILE.exists())
        self.assertEqual(store.load_profile(settings).path, self.profile.resolve())

    def test_missing_profile_is_reported(self) -> None:
        store.PROFILE_FILE.unlink()
        self.profile.unlink()
        with self.assertRaises(StepError):
            store.save_settings(self.settings)

    def test_copy_survives_when_the_original_is_gone(self) -> None:
        self.profile.unlink()
        store.save_settings(self.settings)  # keeps the stored copy
        self.assertTrue(store.PROFILE_FILE.is_file())

    def test_forbidden_profile_cannot_be_saved(self) -> None:
        self.write_profile("client\nplugin evil.dll\n")
        with self.assertRaises(StepError):
            store.save_settings(self.settings)

    def test_remove_temp_files(self) -> None:
        leftover = self.secret_dir / "tmp_old.txt"
        leftover.write_text("secret")
        store.remove_temp_files()
        self.assertFalse(leftover.exists())


if __name__ == "__main__":
    unittest.main()
