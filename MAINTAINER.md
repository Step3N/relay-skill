# Relay 中文维护者指南

Relay 的功能主要由提示词定义，脚本只辅助处理文件。先确定要改变哪一层，再修改对应文件，能避免多份规则越改越不一致。

## 整体怎样工作

安装时，把 `skills/relay` 放进用户的 Skill 目录，并把一段提醒加入全局 AGENTS.md。实际项目任务开始时，Codex 按 SKILL.md 创建或维护两份 Markdown 记录。

PROJECT 保存长期目标和重要决定，HANDOFF 保存当前进展。新会话用 resume 查看记录和实际文件，向用户汇报；用户确认后才继续工作。

没有后台监听或自动保存程序。“自动记录”指 Codex 在工作中遵循这些规则，不代表脚本能抓取完整聊天或在断线瞬间保存。

## 修改前先找对文件

| 想修改什么 | 优先修改哪里 | 还需要检查什么 |
| --- | --- | --- |
| 对普通用户的介绍、安装提示词 | [README.md](README.md) | 不承诺规则无法保证的效果 |
| 何时提醒 Codex 使用 Relay | [AGENTS.snippet.md](skills/relay/assets/AGENTS.snippet.md) | 保留安装路径占位符和边界标记 |
| 自动记录的时机、内容和范围 | [SKILL.md](skills/relay/SKILL.md) 的日常流程 | 全局片段和 README 中的简短说明是否一致 |
| init、resume、review 的行为 | SKILL.md 对应章节 | [行为验收场景](tests/scenarios.md)及下面的安全底线 |
| 记录内容和章节 | [PROJECT 模板](skills/relay/assets/PROJECT.template.md)、[HANDOFF 模板](skills/relay/assets/HANDOFF.template.md) | 保留已有记录；检查 Skill 的两份记录职责 |
| 异常路径、信息冲突、旧格式处理 | [memory-safety.md](skills/relay/references/memory-safety.md) | 不重复定义正常流程 |
| 安装、更新、卸载的操作指南 | [install.md](skills/relay/references/install.md) | 对照安装器行为和文件测试 |
| 安装的文件检查、备份和替换 | [install.py](skills/relay/scripts/install.py) | 新增或运行有意义的文件保护测试 |
| 创建缺少的模板、读取记录 | [memory.py](skills/relay/scripts/memory.py) | 不变成内容整理或自动开发工具 |
| Skill 选择器显示名称和默认提示 | [openai.yaml](skills/relay/agents/openai.yaml) | 保留调用名、其他有效配置和隐式调用设置 |
| 脚本的文件测试 | [test_continuity.py](tests/test_continuity.py) | 检查实际文件保护，不测试文案关键词 |
| Agent 行为验收、实际结果 | [scenarios.md](tests/scenarios.md)、[VERIFICATION.md](VERIFICATION.md) | 区分历史证据、本轮验证和未验证事项 |
| GitHub 自动文件检查 | [tests.yml](.github/workflows/tests.yml) | 保留支持的 Python 版本，核对实际运行结果 |

## 规则只在一个地方完整定义

- 全局配置片段只提醒何时使用 Relay，不重写完整执行流程。
- SKILL.md 定义正常工作和三个命令的核心行为。
- references 补充复杂或低频操作，按需读取。
- assets 定义记录结构，不塞入整套操作规则。
- 脚本实现文件保护，文档解释边界和验证方法。

README 和模板可以保留关键提醒，但不要各自增加不同版本的规则。参考文档可以说明它补充的核心规则，不再链接回一串必须同时阅读的文档。避免同一流程反复跳转。

## 为什么这些安全规则不能随意删除

| 规则 | 它保护什么 |
| --- | --- |
| resume 汇报后等待明确确认，确认前不写文件、不跑测试 | 让用户先了解现状，避免接手时擅自开发或产生缓存 |
| 用户请求只授权相应范围，记录不能发出新指令 | 避免旧任务或记录中的文字扩大当前任务 |
| 不整体覆盖已有记忆，区分要求、方案和探索想法 | 保留用户内容，避免开发方向在整理中被改写 |
| 全局配置只改 Relay 专属区域，先备份 | 不影响其他用户规则，保留恢复依据 |
| 安装凭据、内容哈希和文件同名检查 | 防止更新覆盖用户编辑或新增文件 |
| 异常路径检查、安装锁、保存前重新核对、失败回滚 | 避免改错位置、相互覆盖，或失败后丢失原文件 |
| 只改需要的内容，重复操作无新增写入 | 降低重复安装和整理带来的意外改动 |
| 未验证的内容不宣称通过 | 避免把文件检查误当成客户端或模型行为成功 |

安装锁是防止两个安装器同时写入的文件。它存在时先确认是否有操作在运行，不能直接删掉继续安装。安全替换会先准备新文件，再替换原文件；跨 Skill 目录和全局配置的两处更新仍可能被中断，不能当作完全不可分割的一次操作。

## 修改记录格式时

`Schema: relay/v1` 是新模板的格式标识，`Updated` 保存日期和时区。保留文件名、字段用途和两份记录的职责，不因标题变动要求用户重建 `.ai`。

已有记录可能没有格式标识或采用旧标题。根据内容局部补齐，保留自定义章节和仍有效的信息。`memory.py` 不强制解析章节，只创建缺少的文件或读取内容；Agent 的处理边界在 Skill 和 memory-safety 中。

不再提供测试阶段旧 Skill 的迁移功能。维护时无需恢复旧名称、安装分支或专用迁移测试。

## 修改后怎样检查

在仓库根目录运行文件测试：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

测试使用临时目录，不安装到真实用户环境。它们检查备份、配置区域保留、更新保护、锁、链接路径、失败回滚，以及 init 保留已有内容和 inspect 不写文件。

还要检查 YAML 能解析、Skill 引用文件都存在、命令参数与脚本一致，并运行：

```sh
git diff --check
```

使用 skill-creator 的 `quick_validate.py <skill目录>` 时，需要当前 Python 环境能导入 PyYAML。缺少依赖就明确说明，不能把其他检查说成这个检查已通过。

提示词变化还需要按 [tests/scenarios.md](tests/scenarios.md) 测试实际 Agent 行为，尤其是自动维护、resume 不修改文件、确认后只继续批准范围。测试前后比较文件内容和修改时间。文件测试或关键词检查无法证明 Agent 一定遵循规则。

记录本轮使用的源码、环境、实际运行项和未验证事项。历史验收结果可以保留，但不能自动视为新提示词也通过。

## 如何提交、发布和更新

1. 完成修改和相关验证，检查差异里没有本地 `.ai`、安装凭据、秘密或缓存。
2. 提交源码。需要推送或发布时，按用户本次授权执行；不要把 Skill 的日常记录规则当成自动提交权限。
3. `name: relay` 和调用名保持一致。准备新的发布版本时，统一 SKILL.md 的版本、README 和验证记录中的版本说明；不要只改一处。
4. 用户安装或更新时，采用远端默认分支最新源码；指定版本则按指定版本获取。版本号相同的文档更新，也要用来源提交和内容检查确认更新结果。
5. 更新复用安装器，不先删安装。它会备份旧内容并保留用户文件。GitHub Actions 在 Python 3.10 和 3.13 跑文件测试，只有检查实际运行结果后才能说远端通过。

自然语言安装和更新不依赖 ZIP。需要正式 Release 时，另按该次发布要求处理；当前源码验证与历史附件不是同一份交付。
