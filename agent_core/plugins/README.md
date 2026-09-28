# 内置插件目录
#
# 每个子目录（带 __init__.py）或单个 .py 文件就是一个插件。
# 插件在设置页按 id 启用；未启用的插件不会被加载，也不贡献工具。
#
# 插件契约（详见 agent_core/plugin_loader.py 顶部 docstring）：
#   INFO  = {"id": "...", "name": "...", "description": "...", "version": "..."}
#   TOOLS = [langchain tool, ...]     # 可选：贡献给 agent 的工具
#   HOOKS = {"on_message": fn, "on_tool_end": fn}   # 可选：事件钩子
#   on_load(host) / on_unload(host)   # 可选：生命周期
#
# 自定义插件目录：设置项 plugin_dirs（os.pathsep 分隔，可叠加多个），
# 与内置目录按 id 去重，自定义目录优先。
