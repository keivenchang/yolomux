# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Public compatibility alias for the single host-identity implementation.

Both import paths intentionally resolve to one module object so cached identity
and test/embedding overrides cannot diverge.
"""

import sys

from .infra import host_identity as _implementation

sys.modules[__name__] = _implementation
