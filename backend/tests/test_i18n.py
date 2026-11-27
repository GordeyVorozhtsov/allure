from server.i18n import messages


class TestI18n:
    def test_ru_default(self):
        m = messages("ru")
        assert m["brand"] == "Allure<3"
        assert m["projects"] == "Проекты"
        assert m["admin"] == "Админ"

    def test_en_override(self):
        m = messages("en")
        assert m["brand"] == "Allure<3"  # остаётся из RU
        assert m["projects"] == "Projects"
        assert m["admin"] == "administrator"

    def test_en_prefix_ru(self):
        """EN словарь наследует RU и переопределяет только ключи."""
        m = messages("en")
        # ключ 'logout' есть в обоих -> берётся из EN
        assert m["logout"] == "Log out"
        # ключ 'brand' есть только в RU -> берётся из RU
        assert m["brand"] == "Allure<3"

    def test_unknown_lang_falls_back_to_ru(self):
        m = messages("fr")
        assert m["projects"] == "Проекты"

    def test_all_keys_present(self):
        ru = messages("ru")
        en = messages("en")
        for key in ru:
            assert key in en, f"ключ {key} отсутствует в EN"
