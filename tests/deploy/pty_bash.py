# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Fresh-child PTY setup for the native smoke process fixture."""

from __future__ import annotations

import fcntl
import os
import termios


if __name__ == "__main__":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())
    os.execvp("bash", ["bash", "--noprofile", "--norc", "-i"])
