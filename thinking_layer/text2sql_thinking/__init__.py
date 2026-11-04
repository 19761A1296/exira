# text2sql_thinking/__init__.py
"""Engine-free text2sql (no analytical engines, no data coverage). See text2sql_thinking_tool.py."""

from thinking_layer.text2sql_thinking.text2sql_thinking_tool import PROMPT_A_SKELETON_BIBLE_THINKING, run

__all__ = ["run", "PROMPT_A_SKELETON_BIBLE_THINKING"]
