import io
import json
import logging
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

from server import db, settings

log = logging.getLogger("allure3")

STATUSES = ("passed", "failed", "broken", "skipped", "unknown")
SKIP_NAMES = {"__MACOSX", ".DS_Store"}
SKIP_DIRS = {"allure-results", "allure-result", "results", "target", "build"}
SOURCE_LABELS = {"project", "repository", "repo"}


def project_dir(slug: str) -> Path:
    return settings.DATA_DIR / "projects" / slug


def run_dir(slug: str, run_id: str) -> Path:
    return project_dir(slug) / "runs" / run_id


def results_dir(slug: str, run_id: str) -> Path:
    return run_dir(slug, run_id) / "results"


def report_dir(slug: str, run_id: str) -> Path:
    return run_dir(slug, run_id) / "report"


def report3_dir(slug: str, run_id: str) -> Path:
    return run_dir(slug, run_id) / "report3"


def history3_path(slug: str) -> Path:
    return project_dir(slug) / "allure3-history.jsonl"


def report3_ready(slug: str, run_id: str) -> bool:
    return (report3_dir(slug, run_id) / "index.html").is_file()


def remove_run_files(slug: str, run_id: str) -> None:
    shutil.rmtree(run_dir(slug, run_id), ignore_errors=True)


def safe_destination(root: Path, name: str) -> Path:
    raw = name.replace("\\", "/").lstrip("/")
    parts = raw.split("/")
    if not raw or any(part in {"", ".", ".."} or part in SKIP_NAMES for part in parts):
        raise ValueError("небезопасный путь в загрузке")
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("небезопасный путь в загрузке")
    return target


def extract_zip(data: bytes, dest: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("это не zip-архив") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > 10_000:
            raise ValueError("в архиве слишком много файлов")
        unpacked = sum(info.file_size for info in infos)
        if unpacked > settings.MAX_UNPACKED:
            raise ValueError("распакованный архив слишком большой")
        for info in infos:
            if info.filename.startswith("__MACOSX/") or info.filename.endswith(".DS_Store"):
                continue
            target = safe_destination(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def place_file(dest: Path, name: str, data: bytes) -> None:
    target = safe_destination(dest, Path(name).name if "/" not in name and "\\" not in name else name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def store_blobs(dest: Path, blobs: list[tuple[str, bytes]]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in blobs:
        lower = name.lower()
        if lower.endswith(".zip"):
            extract_zip(data, dest)
        else:
            place_file(dest, name, data)


GENERIC_NAMES = {
    "",
    "allure<3",
    "gitlab",
    "jenkins",
    "github",
    "local",
    "ci",
    "allure",
    "teamcity",
}


def clean_source(value: str) -> str:
    return " ".join(value.split())[:80]


def usable_name(value: str) -> str:
    cleaned = clean_source(value)
    if cleaned.lower() in GENERIC_NAMES:
        return ""
    return cleaned


def _folder_source(path: str) -> str:
    parts = [part for part in path.replace("\\", "/").split("/")[:-1] if part not in SKIP_DIRS and part not in {".", ".."}]
    if not parts:
        return ""
    return clean_source(parts[0])


def _executor_name(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("reportName", "buildName", "name"):
        found = usable_name(str(payload.get(key) or ""))
        if found:
            return found
    return ""


def _environment_name(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="ignore")
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).lower() in {"project", "project_name", "repository", "repo"}:
                    found = usable_name(str(value))
                    if found:
                        return found
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and str(item.get("name") or "").lower() in {"project", "repository", "repo"}:
                    found = usable_name(str(item.get("value") or item.get("values") or ""))
                    if found:
                        return found
        return ""
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() in {"project", "project_name", "repository", "repo"}:
            found = usable_name(value)
            if found:
                return found
    return ""


def detect_archive(data: bytes) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return {"name": "", "sources": []}
    named = ""
    sources: list[str] = []
    labels: list[str] = []
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.file_size > 2_000_000:
                continue
            path = info.filename
            low = path.lower()
            if low.endswith("executor.json"):
                try:
                    named = named or _executor_name(json.loads(archive.read(info)))
                except (json.JSONDecodeError, UnicodeError):
                    continue
            elif low.endswith("environment.properties") or low.endswith("environment.json"):
                named = named or _environment_name(archive.read(info))
            elif low.endswith("-result.json"):
                folder = _folder_source(path)
                if folder:
                    sources.append(folder)
                if info.file_size < 400_000:
                    try:
                        payload = json.loads(archive.read(info))
                    except (json.JSONDecodeError, UnicodeError):
                        payload = None
                    if isinstance(payload, dict):
                        for label in payload.get("labels") or []:
                            if not isinstance(label, dict):
                                continue
                            if str(label.get("name") or "").lower() not in SOURCE_LABELS:
                                continue
                            found = usable_name(str(label.get("value") or ""))
                            if found:
                                labels.append(found)
    unique = sorted(set(sources), key=str.lower)
    unique_labels = sorted(set(labels), key=str.lower)
    if not named and len(unique_labels) == 1:
        named = unique_labels[0]
    if not named and len(unique) == 1:
        named = unique[0]
    return {"name": usable_name(named), "sources": unique}


def source_name(path: Path, results: Path, data: dict, fallback: str) -> str:
    for label in data.get("labels") or []:
        if not isinstance(label, dict):
            continue
        if str(label.get("name") or "").lower() not in SOURCE_LABELS:
            continue
        value = clean_source(str(label.get("value") or ""))
        if value:
            return value
    relative = path.relative_to(results)
    folders = [part for part in relative.parts[:-1] if part not in SKIP_DIRS]
    if folders:
        return clean_source(folders[0]) or fallback
    return fallback


def empty_counts() -> dict:
    counts = {status: 0 for status in STATUSES}
    counts["total"] = 0
    return counts


def add_status(counts: dict, status: str) -> None:
    if status not in STATUSES:
        status = "unknown"
    counts[status] += 1
    counts["total"] += 1


def summarize(results: Path, fallback: str = "общая") -> tuple[dict, list[dict]]:
    fallback = clean_source(fallback) or "общая"
    grouped: dict[str, tuple[dict, Path]] = {}
    for path in results.rglob("*-result.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        key = str(data.get("historyId") or data.get("uuid") or path)
        previous = grouped.get(key)
        if previous is None or (data.get("stop") or 0) >= (previous[0].get("stop") or 0):
            grouped[key] = (data, path)
    overall = empty_counts()
    by_source: dict[str, dict] = {}
    for data, path in grouped.values():
        status = data.get("status") or "unknown"
        add_status(overall, status)
        source = source_name(path, results, data, fallback)
        bucket = by_source.setdefault(source, empty_counts())
        add_status(bucket, status)
    parts = [{"source": name, **counts} for name, counts in sorted(by_source.items(), key=lambda item: item[0].lower())]
    return overall, parts


def backfill_parts(slug: str, runs) -> None:
    pending = [run for run in runs if run["status"] == "ready"]
    if not pending:
        return
    existing = db.parts_for_project(pending[0]["project_id"])
    for run in pending:
        if existing.get(run["id"]):
            continue
        results = results_dir(slug, run["id"])
        if not results.is_dir():
            continue
        _counts, parts = summarize(results, run["executor_name"] or "общая")
        if parts:
            db.save_parts(run["id"], parts)


def copy_history(previous_report: Path | None, results: Path) -> None:
    target = results / "history"
    if target.exists() or previous_report is None:
        return
    source = previous_report / "history"
    if source.is_dir():
        shutil.copytree(source, target)


def ensure_executor(results: Path, meta: dict) -> None:
    path = results / "executor.json"
    if path.exists():
        return
    payload = {

        "name": meta["name"] or "Allure<3",
        "type": meta["type"] or "local",
        "url": meta["url"],
        "buildOrder": meta["build_order"],
        "buildName": meta["name"] or f"#{meta['build_order']}",
        "reportName": meta["project_name"],
        "reportUrl": meta["report_url"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def ensure_environment(results: Path, text: str) -> None:
    path = results / "environment.properties"
    if path.exists() or not text.strip():
        return
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _cli_tail(proc: subprocess.CompletedProcess) -> str:
    return "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-20:])


def generate(results: Path, output: Path) -> None:
    binary = settings.ALLURE_BIN
    if shutil.which(binary) is None and not Path(binary).is_file():
        raise RuntimeError("не найден Allure CLI. Поставьте allure или соберите Docker-образ.")
    output.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [binary, "generate", str(results), "-o", str(output), "--clean"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("allure generate работал дольше 5 минут") from exc
    if proc.returncode != 0:
        raise RuntimeError(_cli_tail(proc) or "allure generate завершился с ошибкой")


def generate3(slug: str, results: Path, output: Path, name: str, keep: int) -> None:
    binary = settings.ALLURE3_BIN
    if shutil.which(binary) is None and not Path(binary).is_file():
        raise RuntimeError("не найден Allure 3 CLI")
    output.mkdir(parents=True, exist_ok=True)
    history = history3_path(slug)
    history.parent.mkdir(parents=True, exist_ok=True)
    config = output.parent / "allurerc.json"
    config.write_text(
        json.dumps(
            {
                "name": name or "Allure<3",
                "output": str(output),
                "historyPath": str(history),
                "appendHistory": True,
                "historyLimit": max(1, int(keep)),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [binary, "generate", str(results), "--config", str(config), "--output", str(output)],
            cwd=output.parent,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("allure3 generate работал дольше 5 минут") from exc
    if proc.returncode != 0:
        raise RuntimeError(_cli_tail(proc) or "allure3 generate завершился с ошибкой")
    if not (output / "index.html").is_file():
        raise RuntimeError("Allure 3 не записал index.html")


def build_run(project, blobs: list[tuple[str, bytes]], meta: dict) -> dict:
    if not blobs:
        raise ValueError("приложите zip или JSON-результаты")
    run_id = str(uuid.uuid4())
    slug = project["slug"]
    results = results_dir(slug, run_id)
    report = report_dir(slug, run_id)
    report3 = report3_dir(slug, run_id)
    order = db.begin_run(
        project["id"],
        run_id,
        meta.get("name") or "",
        meta.get("url") or "",
        meta.get("type") or "",
        meta.get("branch") or "",
    )
    try:
        store_blobs(results, blobs)
        fallback = meta.get("source") or meta.get("name") or "общая"
        counts, parts = summarize(results, fallback)
        if counts["total"] == 0:
            raise ValueError("в загрузке нет ни одного читаемого *-result.json")
        previous = db.latest_ready(project["id"])
        report_url = f"/reports/{slug}/{run_id}/index.html"
        ensure_executor(
            results,
            {
                "name": meta.get("name") or "",
                "type": meta.get("type") or "",
                "url": meta.get("url") or "",
                "build_order": order,
                "project_name": project["name"],
                "report_url": report_url,
            },
        )
        ensure_environment(results, meta.get("environment") or "")
        previous_report = report_dir(slug, previous["id"]) if previous else None
        copy_history(previous_report, results)
        generate(results, report)
        parked = results.parent / "history-v2"
        history_dir = results / "history"
        if history_dir.is_dir():
            if parked.exists():
                shutil.rmtree(parked)
            history_dir.rename(parked)
        try:
            generate3(slug, results, report3, project["name"], project["keep_reports"])
        except Exception as exc:
            log.warning("Allure 3 не собран: %s", exc)
            shutil.rmtree(report3, ignore_errors=True)
        finally:
            if parked.is_dir() and not history_dir.exists():
                parked.rename(history_dir)
        db.finish_run(run_id, counts)
        db.save_parts(run_id, parts)
        for old_slug, old_id in db.prune(project["id"], project["keep_reports"]):
            remove_run_files(old_slug, old_id)
    except Exception as exc:
        db.fail_run(run_id, str(exc))
        remove_run_files(slug, run_id)
        raise
    return {
        "id": run_id,
        "build_order": order,
        "url": f"/reports/{slug}/{run_id}/index.html",
        "latest": f"/reports/{slug}/latest",
        "sources": [part["source"] for part in parts],
        **counts,
    }
