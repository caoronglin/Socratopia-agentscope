# AgentScope-only 真实验收矩阵

自动化 CI 必须完成：Python 3.15/AgentScope 2.0.10 官方导入、课程隔离与权限负例、最新教材逐字引文门禁、同课并发拒绝、所有 Web 浏览器用例、四平台单进程 ZIP 启动与校验。
使用真实模型凭据，分别运行一轮课程讲解及独立 D/E 两位导师发言；记录脱敏请求、模型、教材 SHA、响应、引文判定与错误。
故障、无引用、教材版本变化均不得写入权威掌握度或 45 分钟计时。模拟模型测试不等于真实推理通过。

## 依赖升级与 CI 契约（2026-11-16）

对 AgentScope、pytest、PyInstaller 的升级，测试验证**准确的版本固定语法**，而非把旧版本号写死在测试中。AgentScope 的 `pyproject.toml` 版本必须与 `platform_api/agentscope_runtime.py` 对外报告的 `sdk_version` 完全一致；pytest 和 PyInstaller 仍须在各自 requirements 文件中使用唯一的 `==` 精确固定版本。范围版本、通配版本、额外依赖行均不通过契约测试。

依赖版本更改仍需独立审查、安装与真实 API 兼容性验证；本次修复不自动批准 Dependabot PR。检查命令：`python -m pytest -q tests/test_agentscope_only.py tests/test_dependency_ci.py`；`python scripts/check.py --strict`。真实模型验收继续按上文执行，不能用静态契约测试代替。
