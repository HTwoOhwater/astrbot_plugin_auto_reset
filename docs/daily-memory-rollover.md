# 每日归档与换新：设计与适配

状态：实现已完成，需隔离实例联调。2026-10-26。安装、启用及真实换新不包含在本次代码提交中。

## 目标与来源

每天北京时间 04:00，仅为明确选择的 QQ 共享群会话建立新的原生对话边界。原文先本地保存并导入现有记忆系统；旧原生对话、人格、陪伴关系和长期记忆保留。摘要允许稍后完成，失败任务持久化。

Fork 来源为 Song-yunru/astrbot_plugin_auto_reset，基线提交 `a367e2ad5e3a684aef6fff1039578c79837bbe34`。原实现定时清空所有活跃原生历史；本版重写为名单限定的归档协调器。上游未声明许可证，本 fork 未新增许可证。

适配仅接受 AstrBot 4.29.1、PrivateCompanion 6.7.0、MemoryCompanion 1.10.5。依赖私有属性，因此精确版本和运行时结构检查是必要约束。

## 模块与数据流

| 文件 | 职责 |
| --- | --- |
| `main.py` | 生命周期、60 秒巡检、管理员命令、配置读取 |
| `rollover/engine.py` | 北京日界、白名单、阶段推进、幂等性、摘要退避 |
| `rollover/state.py` | 原子账本、内容寻址原文归档、完整性校验 |
| `rollover/adapters.py` | 三个目标版本的会话、缓冲、导入和摘要适配 |
| `tests/test_rollover.py` | 标准库离线替身与故障注入测试 |

```text
waiting
  -> archived     原始快照已经持久化
  -> imported     时间线导入已确认；记录待处理 ID
  -> prepared     空对话已创建，尚未完成切换
  -> done         陪伴近期窗口已处理，原生选择器已切换

done + summary_state=pending / paused / complete
外部已切换原生对话 -> superseded
```

每个群/逻辑日的任务键由 SHA-256 生成。`baseline` 只安排下一日界；首次启动不立即换新。停机跨数日时补一次，旧任务恢复与当天手动排队并存也只执行一次；实际完成的逻辑日单独记为 `completed_day`。

## 已核对的上游接口

以下路径均按目标源码核对，接口存在不等于真实部署联调完成。

- [AstrBot v4.29.1 conversation_mgr.py](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/conversation_mgr.py)：当前对话、完整历史、列表、原生数据库创建、选择器切换。
- [AstrBot v4.29.1 internal.py](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py)：LLM 以 UMO 获取 `session_lock_manager` 锁。
- [AstrBot v4.29.1 active_event_registry.py](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/utils/active_event_registry.py)：活动事件、原生后台任务集合。
- [AstrBot v4.29.1 group_chat_context.py](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/builtin_stars/astrbot/group_chat_context.py)：原生群聊临时缓存及其独立锁。
- MemoryCompanion 1.10.5 安装源码 `core/store.py`：`add_historical_timeline_events` 事务写入与消息键去重、`unsummarized_timeline_window`、`get_timeline_by_ids`、`get_summary_failure`；`core/service.py`：scope 捕获检查、会话桥接、单批强制摘要。
- PrivateCompanion 6.7.0 源码 `core_store.py`：群 ID 归一化、`data['groups']`、`_data_lock`、同步持久保存；`group_observation.py`：近期窗口与群事件/黑话后台整理标记。

记忆和陪伴的安装源码仅在本地研究目录核对，未把整个外部源码或任何服务器聊天数据提交进本仓库。

## 并发与恢复

协调器串行推进巡检。归档和切换期间获取原生会话锁，前后检查活动事件/原生后台任务；原生锁最多等待 2 秒，繁忙则下轮重试。陪伴群事件/黑话整理运行标记按其 10 分钟保护窗口延后处理。

快照保存原生历史、人格、两个陪伴近期窗口和原生群缓存。导入后重新比较快照；发生变化先停止并在下轮重新归档，不覆盖未归档的变化。最终修改陪伴窗口和原生缓存时分别获取其锁，保留陪伴截止点之后的记录，并在修改前再次检查活动事件。

原生新对话先以确定性 `[auto_reset:<任务键>]` 标题创建但不选中，再将 ID 写入账本、完成切换。若创建后账本未更新就中断，按标题找回空对话并确认人格未变。切换方法即使部分成功，也会重放以修复持久化选择器。

陪伴窗口同步保存异常时恢复内存中的原列表。三个存储系统没有共同事务：保存后、选择器切换前中断可能留下暂时不同步的边界；原文归档已经保存，任务恢复不重新清空旧原生历史。用户自行切换对话时，旧任务转为 `superseded`，不覆盖用户选择。

对话正在进行时原则上延后到空闲点。未遵循原生活动注册表/锁的外部插件或陪伴自有主动发送任务仍可能跨过边界，本版未声称可以暂停全部插件任务。

## 记忆导入与摘要

近期记录使用原始发言人和消息 ID；缺少消息 ID 时使用稳定指纹。原生历史的用户身份/原始时间不完整时，导入为 `conversation_archive / archive_unknown`，文字注明不确定性；不从一个未知发言人推断具体个人事实。工具、系统和媒体二进制不作为摘要输入，但原生历史结构仍在本地快照。

导入目标对文字有 4000 字符截断，本适配器按 3500 字符分片，分片键可重复导入。同一内容的原生摘录与近期记录仍可能重复，跨来源语义去重未实现。

本版单次导入与待处理窗口上限均为 50000 条，超出停止换新。使用记忆插件历史导入接口，不直接写其 SQLite 文件。导入映射不完整时停止切换。

摘要每次只调用一个有界批次，最多等待 240 秒。通过时间线实际 `summarized_at` 标记确认进度，不以返回的摘要 ID 判断完成。进展后 60 秒再检查；无进展或异常后按 10、20 分钟退避，第三次标记暂停，需要管理员 `retry`。每项任务独立记录状态；手动重试不能与进行中的巡检同时覆盖账本。

已有证据隔离/死信时保留待处理任务，不强行跳过。摘要读取该群最早未处理记录，可能包含其他日期，因此“每天换新”不代表“每份摘要严格属于一天”。

## 本地验证与运行时缺口

测试检查白名单与禁用范围、北京日界、首次启用、补跑幂等性、跨日旧任务与手动任务冲突、人格和旧历史保留、归档损坏、写入失败、长文分片、幂等导入、多批摘要、暂停/重试、证据隔离、普通/后台繁忙、切换中断、孤立新对话找回、新消息保留及手动切换冲突。

离线测试使用替身模拟已核对的接口，不调用真实模型、不连接 QQ、不读取服务器聊天数据。尚需在隔离 AstrBot 实例验证：插件实际加载、命令权限与 UI、SQLite 真实导入、摘要生成/召回、观察器并发以及热重载恢复。

数据备份应同时包括 AstrBot 原生历史、两个插件各自数据及本插件账本/归档。此版不自动清理原文归档，也不会更改关系、画像、话题/事件记忆或召回设置。
