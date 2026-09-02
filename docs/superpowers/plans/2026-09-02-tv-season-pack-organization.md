# TV Season-Pack Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Organize every recognizable episode in a completed TV season pack, safely retain its exact library-file manifest, and remove all of those files if the job is deleted.

**Architecture:** Add organizer helpers that parse one supported season/episode marker per filename, preflight all destination paths before writing, and roll back files created in a failed attempt. The worker uses this path only for multi-video TV downloads; it records every completed pack path in a new child table so deletion can remove only paths created for that job.

**Tech Stack:** Python 3.12, FastAPI, SQLModel, SQLite, pytest.

---

## File structure

- `src/skald/models.py` — declares the `OrganizedFile` manifest table linked to a media job.
- `src/skald/db.py` — creates the manifest table for existing SQLite databases.
- `src/skald/organizer.py` — parses episode markers, preflights pack targets, and creates a pack atomically.
- `src/skald/worker.py` — branches multi-file TV jobs to the pack organizer and persists the resulting manifest.
- `src/skald/routes/jobs.py` — deletes every recorded pack library file before removing its job.
- `tests/test_organizer.py` — proves parsing, preflight, and rollback behavior.
- `tests/test_worker.py` — proves pack organization and existing single-file compatibility.
- `tests/test_routes.py` — proves deletion removes only a job's manifest files.

### Task 1: Add the organized-file manifest

**Files:**
- Modify: `src/skald/models.py:27-42`
- Modify: `src/skald/db.py:12-17`
- Modify: `tests/test_routes.py:353-385`

- [ ] **Step 1: Write the failing deletion test**

  Add this test after `test_delete_job_removes_organized_library_file`:

  ```python
  def test_delete_job_removes_all_recorded_pack_files(tmp_path, monkeypatch):
      monkeypatch.setenv("DB_PATH", str(tmp_path / "pack-delete.db"))
      app = create_app()
      season = tmp_path / "library" / "Show" / "Season 01"
      created = [season / "Show - S01E01.mkv", season / "Show - S01E02.mkv"]
      preserved = season / "Show - S01E03.mkv"
      season.mkdir(parents=True)
      for path in [*created, preserved]:
          path.write_text("data")

      with TestClient(app) as client:
          app.state.qbit = FakeQbit()
          with Session(app.state.engine) as session:
              job = MediaJob(
                  type=MediaType.TV, title="Show", release_title="Show.S01",
                  qbit_hash="hash", category="skald-tv", status=JobStatus.ORGANIZED,
              )
              session.add(job)
              session.commit()
              session.refresh(job)
              session.add_all([OrganizedFile(job_id=job.id, path=str(path)) for path in created])
              session.commit()

          response = client.post(f"/jobs/{job.id}/delete", follow_redirects=False)

      assert response.status_code == 303
      assert not any(path.exists() for path in created)
      assert preserved.exists()
  ```

- [ ] **Step 2: Run the test and confirm RED**

  Run: `uv run pytest tests/test_routes.py::test_delete_job_removes_all_recorded_pack_files -v`

  Expected: collection/import fails because `OrganizedFile` does not yet exist.

- [ ] **Step 3: Define and migrate the child table**

  Add to `src/skald/models.py` after `MediaJob`:

  ```python
  class OrganizedFile(SQLModel, table=True):
      id: Optional[int] = Field(default=None, primary_key=True)
      job_id: int = Field(foreign_key="mediajob.id", index=True)
      path: str
  ```

  Import `OrganizedFile` where the route test needs it. In `migrate_schema`, add an idempotent table creation after the existing `library_path` migration:

  ```python
  connection.exec_driver_sql(
      "CREATE TABLE IF NOT EXISTS organizedfile "
      "(id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path VARCHAR NOT NULL)"
  )
  connection.exec_driver_sql(
      "CREATE INDEX IF NOT EXISTS ix_organizedfile_job_id ON organizedfile (job_id)"
  )
  ```

- [ ] **Step 4: Run the focused test and confirm its behavior failure**

  Run: `uv run pytest tests/test_routes.py::test_delete_job_removes_all_recorded_pack_files -v`

  Expected: FAIL because deletion still only uses the scalar `library_path`.

### Task 2: Parse and preflight a TV pack

**Files:**
- Modify: `tests/test_organizer.py:5-21`
- Modify: `src/skald/organizer.py:1-17`

- [ ] **Step 1: Write failing parser and preflight tests**

  Add imports for `build_tv_pack_targets` and `TvPackError`, then add:

  ```python
  @pytest.mark.parametrize("name, expected", [
      ("Show.S01E02.mkv", (1, 2)),
      ("Show.S01.E02.mkv", (1, 2)),
      ("Show_S01_E02.mkv", (1, 2)),
      ("Show-S01-E02.mkv", (1, 2)),
  ])
  def test_build_tv_pack_targets_accepts_supported_episode_markers(tmp_path, name, expected):
      source = tmp_path / name
      source.write_text("data")
      assert build_tv_pack_targets(str(tmp_path / "tv"), "Show", [source]) == [
          (source, tv_target_path(str(tmp_path / "tv"), "Show", *expected, ".mkv"))
      ]


  def test_build_tv_pack_targets_rejects_ambiguous_and_missing_markers(tmp_path):
      ambiguous = tmp_path / "Show.S01E01.S01E02.mkv"
      unmarked = tmp_path / "Show.episode.mkv"
      ambiguous.write_text("data")
      unmarked.write_text("data")

      with pytest.raises(TvPackError, match="Ambiguous episode markers"):
          build_tv_pack_targets(str(tmp_path / "tv"), "Show", [ambiguous])
      with pytest.raises(TvPackError, match="No video files with SxxEyy"):
          build_tv_pack_targets(str(tmp_path / "tv"), "Show", [unmarked])
  ```

- [ ] **Step 2: Run the tests and confirm RED**

  Run: `uv run pytest tests/test_organizer.py -k 'pack_targets' -v`

  Expected: import failure because the pack API is absent.

- [ ] **Step 3: Implement deterministic mapping and preflight**

  In `src/skald/organizer.py`, add `import re`, then define:

  ```python
  EPISODE_MARKER = re.compile(r"s(\d{2})[._-]?e(\d{2})", re.IGNORECASE)


  class TvPackError(Exception):
      pass


  def build_tv_pack_targets(tv_root: str, series: str, video_files: list[Path]) -> list[tuple[Path, Path]]:
      mappings = []
      targets = set()
      for source in video_files:
          markers = list(EPISODE_MARKER.finditer(source.name))
          if len(markers) > 1:
              raise TvPackError(f"Ambiguous episode markers in {source.name}")
          if not markers:
              continue
          season, episode = (int(value) for value in markers[0].groups())
          target = tv_target_path(tv_root, series, season, episode, source.suffix)
          if target in targets:
              raise TvPackError(f"Duplicate target path: {target}")
          if target.exists():
              raise TvPackError(f"Target already exists: {target}")
          targets.add(target)
          mappings.append((source, target))
      if not mappings:
          raise TvPackError("No video files with SxxEyy episode markers were found")
      return mappings
  ```

- [ ] **Step 4: Run the focused tests and confirm GREEN**

  Run: `uv run pytest tests/test_organizer.py -k 'pack_targets' -v`

  Expected: PASS.

### Task 3: Link a pack atomically and organize it in the worker

**Files:**
- Modify: `tests/test_organizer.py`
- Modify: `tests/test_worker.py:83-114`
- Modify: `src/skald/organizer.py`
- Modify: `src/skald/worker.py:7-92`

- [ ] **Step 1: Write failing rollback and integration tests**

  Add a unit test that monkeypatches `link_file` to succeed once and then raise `OSError("disk full")`; it must assert the first target is removed and `TvPackError` contains `disk full`.

  Add this worker test:

  ```python
  async def test_poll_once_organizes_all_episodes_in_tv_pack(tmp_path):
      engine = make_engine()
      content_dir = tmp_path / "downloads" / "Show.S01"
      nested = content_dir / "season"
      nested.mkdir(parents=True)
      for episode in range(1, 3):
          (nested / f"Show.S01.E{episode:02d}.mkv").write_text(str(episode))
      (nested / "Show.srt").write_text("subtitle")

      with Session(engine) as session:
          session.add(MediaJob(type=MediaType.TV, title="Show", release_title="Show.S01",
                               qbit_hash="hash", category="skald-tv", status=JobStatus.DOWNLOADING))
          session.commit()
      qbit = FakeQbit({"hash": TorrentStatus(hash="hash", progress=1.0, state="uploading",
                      content_path=str(content_dir), save_path=str(tmp_path / "downloads"))})

      with Session(engine) as session:
          await poll_once(session, qbit, str(tmp_path / "movies"), str(tmp_path / "tv"))

      targets = [tmp_path / "tv" / "Show" / "Season 01" / f"Show - S01E{episode:02d}.mkv"
                 for episode in range(1, 3)]
      assert all(path.exists() for path in targets)
      with Session(engine) as session:
          job = session.exec(select(MediaJob)).one()
          assert job.status == JobStatus.ORGANIZED
          assert {file.path for file in session.exec(select(OrganizedFile)).all()} == {str(path) for path in targets}
  ```

- [ ] **Step 2: Run the focused tests and confirm RED**

  Run: `uv run pytest tests/test_organizer.py tests/test_worker.py -k 'rollback or tv_pack' -v`

  Expected: FAIL because pack linking and the worker branch do not exist.

- [ ] **Step 3: Implement atomic linking and the worker branch**

  Add this helper to `organizer.py`:

  ```python
  def link_tv_pack(mappings: list[tuple[Path, Path]]) -> list[Path]:
      created = []
      try:
          for source, target in mappings:
              link_file(source, target)
              created.append(target)
      except OSError as exc:
          cleanup_errors = []
          for target in reversed(created):
              try:
                  remove_organized_file(target)
              except OSError as cleanup_exc:
                  cleanup_errors.append(str(cleanup_exc))
          detail = f"Failed to organize TV pack: {exc}"
          if cleanup_errors:
              detail += f"; cleanup failed: {'; '.join(cleanup_errors)}"
          raise TvPackError(detail) from exc
      return created
  ```

  In `organize_job`, before choosing `source = video_files[0]`, branch when the job is TV and has more than one video. Build mappings and link them. On `TvPackError`, set `NEEDS_ATTENTION`, save its message, commit, and return. On success, create one `OrganizedFile(job_id=job.id, path=str(target))` per result, set `library_path` to the first result for backwards-compatible display, set `ORGANIZED`, and commit. Leave movies and one-video TV jobs on their existing path.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

  Run: `uv run pytest tests/test_organizer.py tests/test_worker.py -k 'rollback or tv_pack or completed_tv_episode' -v`

  Expected: PASS; single-episode TV remains metadata-driven.

### Task 4: Delete each recorded pack file safely

**Files:**
- Modify: `src/skald/routes/jobs.py:8-13,108-139`
- Modify: `tests/test_routes.py:353-385`

- [ ] **Step 1: Implement manifest-aware deletion**

  Import `OrganizedFile`. Before the existing scalar `library_path` fallback, query the job manifest and remove every recorded path:

  ```python
  organized_files = session.exec(
      select(OrganizedFile).where(OrganizedFile.job_id == job.id)
  ).all()
  if organized_files:
      for organized_file in organized_files:
          remove_organized_file(Path(organized_file.path))
          session.delete(organized_file)
  elif job.library_path:
      remove_organized_file(Path(job.library_path))
  ```

  Keep qBittorrent deletion before library deletion and keep the existing error response unchanged.

- [ ] **Step 2: Run the manifest deletion test and confirm GREEN**

  Run: `uv run pytest tests/test_routes.py::test_delete_job_removes_all_recorded_pack_files tests/test_routes.py::test_delete_job_removes_organized_library_file -v`

  Expected: PASS; recorded paths disappear and the unrecorded episode remains.

### Task 5: Full verification

**Files:**
- Verify only

- [ ] **Step 1: Run regression tests**

  Run: `uv run pytest -q`

  Expected: PASS with no failures.

- [ ] **Step 2: Check the final patch**

  Run: `git diff --check && git diff -- src/skald/models.py src/skald/db.py src/skald/organizer.py src/skald/worker.py src/skald/routes/jobs.py tests/test_organizer.py tests/test_worker.py tests/test_routes.py`

  Expected: no whitespace errors; changes are limited to TV-pack organization, manifest tracking, deletion, and tests.
