# GA4 六模块埋点说明

依据：UX Analysis.docx 的事件清单、任务成功定义和数据质量要求。

## 当前发送链路

已从公开发布的 GTM-EXAMPLE 确认 GA4 衡量 ID 为 G-EXAMPLE000。容器已有基础 Google tag，但没有 rental_event 自定义事件转发标签。因此本版保留 head 中的 GTM 容器，并使用 gtag event 命令和 send_to=G-EXAMPLE000 将白名单业务事件发送到现有 GA4；不依赖额外的 GTM 自定义事件标签。不会再同时推送同名结构化 GTM 事件，避免重复转发。

上传本目录 index.html 替换原网页即可。本地 file:// 演示不上报。配置在 analytics-config.json，event_transport=gtag；不要同时新增重复发送相同业务事件的 GTM 标签。

## 事件清单

| 范围 | 开始、过程、成功事件 |
| --- | --- |
| 模块 | page_view：六模块实际切换一次，重复渲染和轮询不增加浏览；module_visit_ended 记录前台时长、有效操作和目标状态 |
| 画像 | profile_edit_start、target_search、target_selected、profile_save_attempt、profile_saved |
| 房源录入 | investigation_started、investigation_stage_changed、investigation_completed、investigation_results_viewed |
| 候选 | candidate_opened、candidate_edit_start、candidate_saved、candidate_archived、report_opened、evidence_opened |
| 现场看房 | viewing_started、viewing_item_changed、viewing_conclusion_selected、viewing_saved |
| 沟通 | reply_generate_requested、reply_generated、reply_generate_failed、reply_copy、reply_confirmed、conversation_ended |
| 合同 | contract_upload_started、contract_parsed、contract_check_requested、contract_check_completed、contract_check_failed、contract_result_viewed、contract_document_viewed |
| 共用操作 | operation_started、operation_succeeded、operation_failed、operation_retried |
| 共用任务 | task_started、task_completed、task_failed、task_cancelled、task_interrupted、validation_blocked |

失败请求不发成功事件。重试沿用任务尝试 ID，operation_retried 带 attempt_number 和 retry_count；任务完成带 first_attempt_success、retry_count、validation_count。有失败、重试或校验阻塞后完成，不计为首次尝试成功。

## 用户目标

- 画像：首次编辑开始，保存并锁定成功后完成。
- 录入：开始自动或单帖调查后，自动变绿只记 investigation_completed；用户主动查看且成功载入结果后才记任务完成。
- 候选：编辑后成功保存。
- 看房：保存成功且已选择现场结论；只保存草稿不算目标完成。
- 沟通：请求生成后，成功复制建议回复或确认记录。不能解释为已向外部发送。
- 合同：请求检查后，用户主动打开检查历史结果，或主动滚动阅读可见的新结果。自动渲染和程序滚动不算目标完成。

## 模拟与真实模式

environment=demo；data_mode=synthetic 表示演示资料场景。execution_mode 分别为 simulated（自动房源流程）、prefilled（无 Key 的沟通样例）、local_rule（合同检查）、real_model（有 Key 的沟通模型请求）。trigger_source=user 或 automatic。自动进度和预填结果不得直接标为关键事件，也不能证明真实准确率或租房效果。

## 参数与单位

共用字段：module、previous_module、app_version、environment、data_mode、execution_mode、trigger_source、api_configured、task_attempt_id、module_visit_id、operation、result、error_code。

耗时单位均为毫秒：duration_ms 是墙钟时间；foreground_ms 为前台可见时间；active_ms 为最近30秒有交互且未等待的前台时间近似；wait_ms 为前台等待时间。模块切换不丢弃进行中任务，后台不计主动操作。演示播放耗时不当作模型性能。

按事件附带出租帖数、候选数、筛除数、轮次、阶段、偏好数量、修改字段类别、回复风格、是否编辑、风险数和文档分档。只传允许的枚举、计数、布尔和临时随机关联 ID，不传原始业务 ID。

## GA4 后台一次性设置

1. 数据流增强型衡量中，关闭自动页面历史、表单互动、站外链接和文件下载等可能重复或带入应用内容的采集。网页已在初始化前设置 send_page_view=false，并手动发送虚拟 page_view；若 GTM 标签显式覆盖此值为 true，应改为 false。
2. 在自定义定义中注册需要分析的低基数事件维度，优先 module、previous_module、execution_mode、trigger_source、operation、result、error_code、analysis_mode。不要把 task_attempt_id 或 module_visit_id 注册成常规维度。
3. 需要报表汇总的 duration_ms、foreground_ms、active_ms、wait_ms 等注册为对应毫秒自定义指标；精确中位数、P95、重试漏斗和完整7日窗口建议使用明细导出计算。
4. 仅选择符合研究目标的用户完成事件作为关键事件；不批量把自动进度、page_view 和预填查看标为关键事件。
5. 上传新版网页后在 GA4 实时/DebugView 检查事件名、module 和 execution_mode。仅看到 page_view 或 session_start 不代表业务事件已覆盖。

## 隐私与验证边界

不上传 API Key、原始错误文本、房源地址、备注、文件名、聊天或合同正文。URL 去除任意查询与片段，仅保留固定模块标识。自由回答、访谈和主观评分不自动采集。

页面隐藏不当作离站；pagehide 未完成任务保留 pending，不能立即当失败。离站事件可能丢失，最终结合会话路径判断。拦截器、同意设置和跨设备身份会影响统计。本次未修改 GA4/GTM 后台，也未向正式 GA4 发送测试事件。

验证：analytics.test.cjs 检查去重、各模块口径和隐私；telemetry.test.cjs 用 Google/模型替身验证真实模式失败、重试后成功、目标 ID 路由及耗时；single.test.cjs 回归原有演示流程。

参考：https://developers.google.com/tag-platform/gtagjs/reference 和 https://developers.google.com/analytics/devguides/collection/ga4/event-parameters
## 推广来源 UTM

入口网址保留 utm_source、utm_medium、utm_campaign、utm_content、utm_term 五个推广参数，仅允许1至64位小写英文字母、数字、下划线和连字符。其他任意参数和片段继续清理。首次 Google 配置与首次虚拟 page_view 使用含 UTM 的入口网址；后续模块切换不重复携带 UTM。自然外部来源保留来源域名，不保留来源网址的查询内容。

小红书示例：https://k1aonix.github.io/631604rental-demo/?utm_source=xiaohongshu&utm_medium=social&utm_campaign=demo_launch&utm_content=post01
微信群示例：https://k1aonix.github.io/631604rental-demo/?utm_source=wechat&utm_medium=social&utm_campaign=demo_launch&utm_content=group01
朋友分享示例：https://k1aonix.github.io/631604rental-demo/?utm_source=friend&utm_medium=referral&utm_campaign=demo_launch&utm_content=share01

在 GA4 流量获取报告中按会话来源/媒介、会话广告系列查看，不需要另注册 UTM 自定义维度。链接被转发后可能继续携带原标记，所以表示推广链接来源，不保证等同实际最后转发平台或具体人员。所有标记使用固定渠道代号，不放姓名、手机号、Key 或个人标识。可用同一推广链接生成二维码。

验证：telemetry.test.cjs 已检查入口保留规范 UTM、配置与首次 page_view 一致、后续模块不重复带入，以及敏感查询参数仍被过滤；未向真实 GA4 注入测试事件。