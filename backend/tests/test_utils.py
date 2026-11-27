from server.main import (
    badge_svg,
    format_when,
    safe_next,
    slugify,
    unique_slug,
    text,
)


class TestSlugify:
    def test_latin(self):
        assert slugify("Hello World") == "hello-world"

    def test_cyrillic(self):
        assert slugify("Привет мир") == "privet-mir"

    def test_special_chars(self):
        assert slugify("  Foo!!! Bar  ") == "foo-bar"

    def test_empty_returns_project(self):
        assert slugify("   ") == "project"

    def test_emoji(self):
        assert slugify("Foo😀Bar") == "foo-bar"

    def test_trailing_dashes(self):
        assert slugify("Foo---") == "foo"

    def test_max_length(self):
        long_name = "a" * 100
        assert len(slugify(long_name)) <= 40


class TestUniqueSlug:
    def test_not_taken(self, data_dir, db_init):
        from server import db
        assert unique_slug("myproject") == "myproject"

    def test_taken_generates_suffix(self, data_dir, db_init):
        from server import db
        db.create_project("myproject", "myproject", 5)
        assert unique_slug("myproject") == "myproject-2"

    def test_multiple_taken(self, data_dir, db_init):
        from server import db
        db.create_project("p", "p", 5)
        db.create_project("p-2", "p-2", 5)
        assert unique_slug("p") == "p-3"


class TestSafeNext:
    def test_valid_internal(self):
        assert safe_next("/project/test") == "/project/test"

    def test_absolute_url_blocked(self):
        assert safe_next("https://evil.com") == "/"

    def test_protocol_relative_blocked(self):
        assert safe_next("//evil.com") == "/"

    def test_backslash_blocked(self):
        assert safe_next("/\\evil.com") == "/"

    def test_empty(self):
        assert safe_next("") == "/"


class TestFormatWhen:
    def test_iso_format(self):
        assert format_when("2025-01-15T14:30:00Z") == "15.01.2025 14:30"

    def test_invalid_input(self):
        assert format_when("not-a-date") == "not-a-date"

    def test_none(self):
        assert format_when(None) == ""


class TestBadgeSvg:
    def test_all_passed(self):
        svg = badge_svg(10, 0, 0, 10)
        assert "10 ок" in svg
        assert "#1d6b45" in svg

    def test_has_failures(self):
        svg = badge_svg(8, 2, 0, 10)
        assert "2 упало" in svg
        assert "#9d3419" in svg

    def test_has_broken(self):
        svg = badge_svg(8, 0, 1, 9)
        assert "1 упало" in svg

    def test_empty_total(self):
        svg = badge_svg(0, 0, 0, 0)
        assert "пусто" in svg

    def test_svg_structure(self):
        svg = badge_svg(5, 1, 0, 6)
        assert svg.startswith('<svg xmlns=')
        assert 'width="132"' in svg
        assert 'aria-label' in svg


class TestText:
    def test_utf8_passthrough(self):
        assert text("Привет") == "Привет"

    def test_latin1_encoded(self):
        # Браузер иногда шлёт UTF-8-данные, прочитанные как latin-1
        assert text("Привет".encode("utf-8").decode("latin-1")) == "Привет"
