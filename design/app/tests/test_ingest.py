import io
import json
import zipfile
from pathlib import Path

import pytest

from server import ingest
from server.ingest import (
    clean_source,
    detect_archive,
    empty_counts,
    extract_zip,
    safe_destination,
    summarize,
    usable_name,
    _environment_name,
    _executor_name,
)


class TestSafeDestination:
    def test_normal_path(self, tmp_path):
        target = safe_destination(tmp_path, "folder/file.json")
        assert target == (tmp_path / "folder" / "file.json").resolve()

    def test_traversal_blocked(self, tmp_path):
        with pytest.raises(ValueError):
            safe_destination(tmp_path, "../../etc/passwd")

    def test_absolute_path_blocked(self, tmp_path):
        # /etc/passwd превращается в 'etc/passwd' после lstrip("/")
        # и будет сохранён внутрь корня — это не уязвимость, но тест должен
        # проверять, что результат остаётся внутри корня
        target = safe_destination(tmp_path, "/etc/passwd")
        assert target.is_relative_to(tmp_path.resolve())
        assert target == (tmp_path / "etc" / "passwd").resolve()

    def test_backslash_blocked(self, tmp_path):
        with pytest.raises(ValueError):
            safe_destination(tmp_path, "..\\..\\etc\\passwd")

    def test_dot_dot_blocked(self, tmp_path):
        with pytest.raises(ValueError):
            safe_destination(tmp_path, "a/../../b")

    def test_macosx_meta_blocked(self, tmp_path):
        with pytest.raises(ValueError):
            safe_destination(tmp_path, "__MACOSX/foo")

    def test_empty_name(self, tmp_path):
        with pytest.raises(ValueError):
            safe_destination(tmp_path, "")


class TestExtractZip:
    def test_basic_zip(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("results/1-result.json", '{"status": "passed"}')
        ingest.extract_zip(buf.getvalue(), tmp_path)
        assert (tmp_path / "results" / "1-result.json").is_file()

    def test_too_many_files(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(10_001):
                zf.writestr(f"f{i}.json", "x")
        with pytest.raises(ValueError, match="слишком много файлов"):
            ingest.extract_zip(buf.getvalue(), tmp_path)

    def test_not_zip(self, tmp_path):
        with pytest.raises(ValueError, match="не zip"):
            ingest.extract_zip(b"not a zip", tmp_path)

    def test_skips_macosx(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("__MACOSX/results/1-result.json", "garbage")
            zf.writestr("results/1-result.json", '{"status": "passed"}')
        ingest.extract_zip(buf.getvalue(), tmp_path)
        assert (tmp_path / "results" / "1-result.json").is_file()


class TestCleanSource:
    def test_collapses_whitespace(self):
        assert clean_source("  a   b  ") == "a b"

    def test_truncates(self):
        assert len(clean_source("x" * 200)) == 80

    def test_empty(self):
        assert clean_source("") == ""


class TestUsableName:
    def test_generic_filtered(self):
        assert usable_name("allure<3") == ""
        assert usable_name("ci") == ""
        assert usable_name("local") == ""

    def test_custom_kept(self):
        assert usable_name("frontend") == "frontend"
        assert usable_name("backend-api") == "backend-api"

    def test_case_insensitive(self):
        assert usable_name("Local") == ""
        assert usable_name("GITLAB") == ""


class TestExecutorName:
    def test_valid_payload(self):
        payload = {"name": "frontend"}
        assert _executor_name(payload) == "frontend"

    def test_prefers_report_name(self):
        payload = {"reportName": "backend", "name": "ci"}
        assert _executor_name(payload) == "backend"

    def test_generic_field_skipped(self):
        payload = {"name": "gitlab"}
        assert _executor_name(payload) == ""

    def test_not_dict(self):
        assert _executor_name("string") == ""
        assert _executor_name(None) == ""


class TestEnvironmentName:
    def test_properties_format(self):
        raw = b"project=my-project\ncustom=x\n"
        assert _environment_name(raw) == "my-project"

    def test_json_dict(self):
        raw = json.dumps({"project": "api-service"}).encode()
        assert _environment_name(raw) == "api-service"

    def test_json_list(self):
        # Массив не начинается с { — должен возвращать "" (считается properties)
        raw = json.dumps([{"name": "repo", "value": "backend"}]).encode()
        assert _environment_name(raw) == ""

    def test_empty(self):
        assert _environment_name(b"") == ""
        assert _environment_name(b"no equals sign") == ""


class TestDetectArchive:
    def test_invalid_zip(self):
        assert detect_archive(b"not zip") == {"name": "", "sources": []}

    def test_with_executor(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("executor.json", json.dumps({"name": "frontend"}))
            zf.writestr("results/1-result.json", '{"uuid": "1", "status": "passed"}')
        result = ingest.detect_archive(buf.getvalue())
        assert result["name"] == "frontend"

    def test_with_folder_source(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("unit/1-result.json", '{"uuid": "1", "status": "passed"}')
        result = ingest.detect_archive(buf.getvalue())
        assert "unit" in result["sources"]

    def test_empty_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            pass
        result = ingest.detect_archive(buf.getvalue())
        assert result == {"name": "", "sources": []}


class TestSummarize:
    def _make_results(self, tmp_path) -> Path:
        results = tmp_path / "results"
        results.mkdir()
        (results / "1-result.json").write_text(
            json.dumps({"uuid": "1", "status": "passed", "stop": 100})
        )
        (results / "2-result.json").write_text(
            json.dumps({"uuid": "2", "status": "failed", "stop": 101})
        )
        (results / "3-result.json").write_text(
            json.dumps({"uuid": "3", "status": "broken", "stop": 102})
        )
        (results / "4-result.json").write_text(
            json.dumps({"uuid": "4", "status": "skipped", "stop": 103})
        )
        (results / "not-result.json").write_text("not json")
        return results

    def test_counts(self, tmp_path):
        results = self._make_results(tmp_path)
        counts, parts = ingest.summarize(results)
        assert counts["passed"] == 1
        assert counts["failed"] == 1
        assert counts["broken"] == 1
        assert counts["skipped"] == 1
        assert counts["total"] == 4

    def test_deduplicate_same_uuid(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "a-result.json").write_text(
            json.dumps({"uuid": "same", "status": "failed", "stop": 100})
        )
        (results / "b-result.json").write_text(
            json.dumps({"uuid": "same", "status": "passed", "stop": 200})
        )
        counts, _ = ingest.summarize(results)
        # проходит последний с тем же uuid
        assert counts["total"] == 1
        assert counts["passed"] == 1

    def test_invalid_json_skipped(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "bad.json").write_text("{{{{")
        counts, parts = ingest.summarize(results)
        assert counts["total"] == 0

    def test_grouped_by_source(self, tmp_path):
        results = tmp_path / "results"
        results.mkdir()
        (results / "src-a").mkdir()
        (results / "src-b").mkdir()
        (results / "src-a" / "1-result.json").write_text(json.dumps({"uuid": "1", "status": "passed"}))
        (results / "src-b" / "2-result.json").write_text(json.dumps({"uuid": "2", "status": "failed"}))
        counts, parts = ingest.summarize(results)
        assert len(parts) == 2
        src_counts = {p["source"]: p for p in parts}
        assert src_counts["src-a"]["total"] == 1
        assert src_counts["src-b"]["total"] == 1

    def test_empty_counts(self):
        counts = empty_counts()
        assert counts["passed"] == 0
        assert counts["failed"] == 0
        assert counts["broken"] == 0
        assert counts["skipped"] == 0
        assert counts["unknown"] == 0
        assert counts["total"] == 0
