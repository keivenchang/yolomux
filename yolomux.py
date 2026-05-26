#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""CLI entrypoint for YOLOMux."""

from __future__ import annotations

from yolomux_lib.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
