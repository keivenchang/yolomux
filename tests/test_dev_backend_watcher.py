# SPDX-FileCopyrightText: Copyright (c) 2026 Keiven Chang. All rights reserved.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from __future__ import annotations

from pathlib import Path

from yolomux_lib import cli


def write_source(path: Path, text: str = "pass\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dev_backend_snapshot_covers_nested_modules_and_detects_tree_changes(tmp_path: Path) -> None:
    write_source(tmp_path / "yolomux.py")
    write_source(tmp_path / "tools" / "tmux_wall.py")
    write_source(tmp_path / "yolomux_lib" / "server.py")
    nested = tmp_path / "yolomux_lib" / "tmux" / "tmux_utils.py"
    write_source(nested)

    initial = cli.dev_backend_source_snapshot(tmp_path)

    assert str(nested) in initial
    added = tmp_path / "yolomux_lib" / "workspace" / "new_backend.py"
    write_source(added)
    after_add = cli.dev_backend_source_snapshot(tmp_path)
    assert after_add != initial
    assert str(added) in after_add

    nested.unlink()
    after_remove = cli.dev_backend_source_snapshot(tmp_path)
    assert after_remove != after_add
    assert str(nested) not in after_remove


def test_dev_backend_snapshot_ignores_non_python_and_bytecode_files(tmp_path: Path) -> None:
    write_source(tmp_path / "yolomux.py")
    write_source(tmp_path / "yolomux_lib" / "server.py")
    write_source(tmp_path / "yolomux_lib" / "README.txt", "not backend source\n")
    bytecode = tmp_path / "yolomux_lib" / "__pycache__" / "server.pyc"
    write_source(bytecode, "not bytecode\n")

    snapshot = cli.dev_backend_source_snapshot(tmp_path)

    assert str(tmp_path / "yolomux_lib" / "server.py") in snapshot
    assert str(tmp_path / "yolomux_lib" / "README.txt") not in snapshot
    assert str(bytecode) not in snapshot
