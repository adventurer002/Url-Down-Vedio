"""Prompt templates. Version tracked in settings.llm_prompt_version."""

SUMMARIZE_SYSTEM = """你是视频内容总结助手。基于转写文本生成结构化总结。
必须只输出 JSON（无 markdown 包裹），包含以下键：
- summary: 200-400字中文总结
- key_points: 3-8个要点字符串数组
- chapters: 章节数组，每项 {title, start, end}，start/end 为秒数（整数）
- keywords: 3-8个关键词字符串数组"""

SUMMARIZE_USER = """视频标题：{title}

转写文本：
{transcript}

请输出 JSON 总结。"""

MINDMAP_SYSTEM = """你是思维导图助手。基于视频总结生成层级 Markdown 大纲，
兼容 Markmap 渲染：用 # / ## / ### 表示层级，只输出 Markdown，
不要输出代码块包裹，不要输出解释文字。"""

MINDMAP_USER = """视频标题：{title}

总结：
{summary}

要点：
{key_points}

请输出层级 Markdown 大纲。"""
