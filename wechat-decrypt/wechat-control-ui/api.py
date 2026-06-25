#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for Streamlit imports; shared client lives in control_client."""
from control_client import *  # noqa: F401,F403
from control_client import __all__  # noqa: F401
from control_client import set_target_dedicated_agent  # noqa: F401
