# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
title: Video Production
author: Open Computer Use Contributors
version: 1.0.0
required_open_webui_version: 0.5.17
description: Switch in the chat integrations menu that offers the remotion video skill for the next message.

Open WebUI shows an active, global filter with toggle = True as a switch, off
by default, and sends the ids of the switches the user turned on as
metadata["filter_ids"]. This filter does nothing with the request. The Computer
Use filter (computer_link_filter.py) owns the system prompt: when this id is
selected and listed among the model's active toggle filters, it asks the
orchestrator for the prompt with the remotion skill.

The init scripts install this file with the id video_production_filter, the
VIDEO_FILTER_ID constant of computer_link_filter.py.
"""

from typing import Optional


class Filter:
    # Read by Open WebUI with getattr on the loaded instance.
    toggle = True
    # Video camera outline, shown next to the switch.
    icon = (
        "data:image/svg+xml;base64,"
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAy"
        "NCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0i"
        "MS41IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxy"
        "ZWN0IHg9IjIuNSIgeT0iNSIgd2lkdGg9IjE0IiBoZWlnaHQ9IjE0IiByeD0iMiIvPjxwYXRo"
        "IGQ9Ik0xNi41IDEwbDUtM3YxMGwtNS0zeiIvPjwvc3ZnPg=="
    )

    def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        """Return the request unchanged; the switch state travels in the metadata."""
        return body
