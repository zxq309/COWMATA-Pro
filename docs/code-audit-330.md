# COWMATA 3.3.0 全软件代码审查报告

审查日期：2026-09-09～10。对象：第一方源码、工程数据契约和主要桌面交互调用链。基线为 `04cdf1e`（本地数据整理集成分支），同时对照已发布的 3.1、3.2.0、3.2.1 行为。本文记录修复调用链、回归与实际桌面验证。固定提交与最终安装包哈希见同版本发行附件。

## 1. 审查结论

本轮确认了工程按需索引、整理恢复、标注信息传递和更新事务中的衔接缺陷，并加入针对性回归。软件的统一流程明确为：**选择数据类别 → 审查和整理 → 加载工程并核对时间/身份 → 标注和保存 → 人工复核 → 导出**。旧工程仍可直接打开，不要求为升级先搬动原始数据。

下表按修复前的业务影响归并问题链，同一根因的多个测试合并计数。这是可靠性分级，不是安全漏洞评级，也不是未修复缺陷数量。

| 等级 | 本文归并项数 | 当前处理状态 |
| --- | ---: | --- |
| CRITICAL / 严重 | 0 | 本轮未确认此级问题；不代表不存在 |
| HIGH / 高 | 11 | 已修复并有专项回归；见第 4 节 |
| MEDIUM / 中 | 5 | 已修复并有专项回归 |
| LOW / 低 | 2 | 翻译自检和显示问题已修复 |

**剩余整体风险：MEDIUM；发行门槛：源码回归、实包验收与GitHub CI全部通过后公开。** 最终EXE生成后的新装/卸载、迁移和SHA记录发布为同版本附件 COWMATA-3.3.0-Delivery-Checks.json，不以源码专项代替实包操作。客户截图中的原始失败工程在客户电脑，本机没有该工程，不能把本地构造复现说成客户原工程已经修复验收。任何一次审查和测试都不能保证零 bug。

## 2. 版本与变更范围

| 参照 | 作用 |
| --- | --- |
| `77a5066` | 3.2.0 发布基线 |
| `a5ca2a9` | 后续加载、跳转播放和保存复核迭代 |
| `9aed571` | 3.2.1 时间戳显示与启动最新版检查 |
| `04cdf1e` | 数据整理和采集类别集成，作为本轮差异审查基线 |
| `04cdf1e..工作区` | 本文所述 3.3.0 修复与新增需求；最终提交由发布记录确定 |

最终变更以发布标签v3.3.0与9aed571的Git差异为准，包含数据整理集成与随后跨流程修复、回归测试和图文说明书。

| 变更域 | 主要模块 | 影响 |
| --- | --- | --- |
| 按需加载与播放器状态 | `workspace/demand.py`、`worker.py`、`probe.py`、`ocr.py`、`playback.py`、`window.py` | 旧目录归组、录像发现与复核、状态刷新、首次播放 |
| 整理与身份 | `organization*.py`、`device_identity.py`、`data_category.py` | 原件移动、失败续接、分类表、逐记录身份 |
| 标注与输出 | `work.py`、`team.py`、`label_file.py`、`io_task.py`、`annotation/core.py` | 保存/重开、回收冲突、时间戳、证据和训练导出 |
| 更新与旧入口 | `app/update*.py`、`model_assist/assist.py` | 下载重试、更新事务互斥/回滚、可选依赖缺失提示 |
| 展示与发行 | `modern_window.py`、`ui/translations.py`、版本及打包文件 | 共用控件、术语一致、版本资源；实包另验收 |

## 3. 功能与调用链覆盖

“深读”表示检查该模块或指定关键函数的契约、调用者和异常路径；“实测”区分真实 Qt/子进程、合成数据和替代外部边界。没有采集整库行覆盖率，故不提供覆盖百分比。

| 功能 | 核心调用链 | 本轮证据与边界 |
| --- | --- | --- |
| 启动与最新版门禁 | `app.main.main → verify_startup_update → UpdateController → update_core / update_worker` | 语义版本排序、跳过中间版、坏包与取消、离线启动门禁；启动门禁、真实旧worker迁移及独立新装/卸载分开验收 |
| 数据审查和归类 | `OrganizationWindow → QProcess organization_worker → audit / plan_import / execute` | 深读整理模块；真实 Qt 子进程执行合成文件移动，心跳持续；异常文件和命名问题逐项报告 |
| 同目录互斥与续接 | `execute → DatasetLease + ProjectLock → journal / manifest → Catalog.scan` | 父目录覆盖子工程、实际 Windows 文件锁、独立进程互斥、清单截断/扫描失败注入与重试 |
| 工程清点与索引 | `MainWindow.open_project → Catalog → IndexWorker.next_task → demand → SourceInspector` | 目录后缀/短码、按需预算、旧索引重检、开头无时间字幕和替换后重试；合成客户式目录实跑 |
| 视频、九轴时间与播放 | `SourceInspector → timeline / clocks → VideoBoard / engine；motion → capture_timing → signal_panel` | 路由线索与可用时间段分离；首帧/关闭实跑及既有播放、拖动、首帧定位、时钟测试；不代表所有显卡/录像格式吞吐一致 |
| 每份记录身份和类别 | `read_context + resolve_device_identity → bind_current_identity → SessionWork` | 规范校验、人工牛号冲突、同设备两日期两牛来回切换/保存/重开/导出；七类共用枚举 |
| 草稿、确认和修改 | `window → SessionWork → Project / Event / UndoStack` | 草稿、证据门控、编辑转待复核、删除、撤销重做、完成状态、切换自动保存等专项回归 |
| 持久化与关闭 | `save_current / save_progress → SnapshotWriter / atomic_json；closeEvent` | 保存恢复、过期回调、写入失败和关闭保护；不把突然断电当作普通异常测试 |
| 历史与图像证据 | `build_label_file / save_label_file → load_history → evidence / archive / evidence_ui` | 完整/片段标签往返、只读历史索引、源视频改路径后 SHA 核对、截图校验与离线证据包 |
| 团队回收 | `import_team_labels → run_io_task → restore_label → SessionWork / Catalog` | 同源身份核对、已有人工修改冲突、自动回收、控件刷新；真实大单份回收及后台关闭保护 |
| 人工成果与训练导出 | `export_training → training_project → core CSV / BORIS / build_meta + copy_evidence` | 所有人工成果保留；训练仅输出有效确认标签；类别/身份/真实时间和证据包逐项验收 |
| 算法与候选 | `algorithm_panel / candidate_window → event_worker → event_models → 私有运行库` | 五模型真实运行、原 CLI 对照、缓存、取消、陈旧结果拒收；静稳输入及真实九轴均实际运行，后者候选0/2/1/3/12；不证明准确率 |
| 历史界面与翻译 | `main → basic_window / model_assist_window；modern_window → ControllerWindow` | 旧模式无 Torch 仍可人工标注和保存；翻译命令实际运行，未把翻译加入持久化字段 |

### 3.1 整理到导出的统一契约

1. **先确定这批数据属于哪类。** 整理界面记录人工选择；类别进入逐素材分类映射。类别是采集背景，行为标签仍可自由选择起立、卧倒、抬尾、甩尾、排尿、排便等已定义事件。
2. **先审查再执行。** 单文件、单设备、单日、多日和嵌套目录都先生成计划。有效九轴不能因目录不规范被判成废料。待清理杂项进入计划中的隔离目录；异常和重复目标保留可核查记录，不覆盖原件。
3. **完成整理后才将该结果交给标注。** 同卷移动使用不覆盖目标的重命名；跨卷请求明确阻止，没有自动退化成大体积复制。移动、清单或末次扫描未完成时保留待恢复状态，不能提示完整成功并开放该结果。
4. **标注工程占用范围与整理占用范围不能交叠。** 同目录先保存并暂停；不相关目录可在独立子进程整理。选父目录时也检查其中旧版工程的写锁。
5. **打开记录后核对来源、牛号和同步。** 录像时间线索只用于安排检索，未经复核的旧时间段不能直接成为训练证据。九轴图轴和导出真实时间采用同一采集时间基准；采样缺口和首帧偏移保留。
6. **人工标注可以保存、重开和修改。** 切换保留未完成记录与当前位置；完成由用户明确执行。改变事件范围、牛号或同步映射时，相关确认状态按契约降为待复核，不能沿用失效证据。人工修改采集类别会同步到本份草稿和事件的背景字段，行为标签语义保持不变，并可撤销。
7. **先复核再导出训练真值。** 草稿/未审查区间不会被当成负样本；失效源文件、空牛号、身份冲突或没有有效确认事件会拦截训练导出。完整人工成果与截图用于复核追溯，截图不进入训练输入。

### 3.2 七类背景与逐记录设备身份

| 保存代码 | 显示类别 |
| --- | --- |
| `healthy` | 正常健康 |
| `estrus` | 发情 |
| `pregnancy_early` | 孕早期 |
| `pregnancy_mid` | 孕中期 |
| `pregnancy_late` | 孕晚期 |
| `calving` | 产犊 |
| `disease` | 疫病 |

原四个代码和语义不变，三个孕期均由人工明确选择，不按日期、设备号或行为推断。整理界面与标注界面共用 `data_category.CATEGORIES`。实测包含三个孕期各自的整理 QProcess 全流程、同工程多批分类表保留、草稿确认、撤销/保存重开、团队回收及各输出；不是只测试枚举存在。

新整理的九轴目录为 `完整12位十六进制设备编号-数字耳标-字母数字现场记号/采集开始日期`。设备编号规范成大写，耳标的前导 `0`、记号的大小写原样保留。目录编号必须与这一份 JSON 的设备编号一致，才允许自动带入空耳标。例：同一设备的 `546C50CA07D5-00123-w1/2026-09-01` 和 `546C50CA07D5-21100-10/2026-09-02` 是两份合法复用记录，不能按设备去重或永久绑定同一头牛。

身份进入本份 `project.extras.device_identity`，以资产、来源目录及采集时间追溯；`cow_id` 继续兼容既有字段，`field_mark` 独立保存。分类表、草稿、事件、完整/片段标签、事件 CSV、逐样本 CSV、BORIS、参考证据 CSV 和元数据都传递相应字段。BORIS 原前六列保留。

已保存人工牛号与目录冲突时，保留人工值并明确提示，需显式核对后再确认/训练导出；不会静默覆盖。人工保留的冲突处理只作用于本份资产。旧非规范目录仍允许打开和人工标注；建议不自动改名、不移动真实目录，既有身份及标签不会因此清除。

## 4. 主要缺陷、修复与复现证据

定位以编写时源码为准；行号便于查找，函数名用于后续版本追踪。下列测试均位于仓库 `tests/`；专项报告记录了先复现失败、再修复通过的过程。

| 编号 / 修复前等级 | 具体触发与后果 | 修复位置和验证 |
| --- | --- | --- |
| A01 / 高 | 旧设备目录带型号后缀或短码，首份解码后的设备名与未读记录分组不同，界面只剩首份记录 | `workspace/demand.py:19` 的 `device_aliases/device_name` 统一可证明的分组；`test_device_export_suffix_keeps_unread_records_visible`、`test_old_short_device_folder_keeps_all_records_after_first_decode` |
| A02 / 高 | 开头未读出录像时间后，空提示既不算未知又不进完整检查；旧索引升级为 pending 后原时间也不再参与路由，形成找不到视频的死路 | `demand.py:73/98`、`worker.py:121`、`probe.py`：后续采样、有限回退、旧区间仅作路由；`test_failed_video_opening_falls_back_once_and_yields_to_playback`、`test_old_project_intervals_route_recheck_without_becoming_playable` 及冷开/旧索引真实 GUI 运行 |
| A03 / 中 | 首个视频未就绪时播放推进参考位置并影响检索；索引对话框打开后状态不随后台完成刷新 | `playback.py:552` 的 `play` 与 `window.py:2070` 的 `source_manager`；`test_play_before_first_video_does_not_stop_search_or_advance_past_record`、`test_source_inspection_dialog_refreshes_completed_background_index` |
| O01 / 高 | 选择源父目录时漏掉其内部旧版工程写锁，可能移动正在标注的素材 | `organization.py:505` 的 `execute` 检查所选文件祖先工程锁；`test_parent_input_honors_nested_legacy_project_writer_lock` 使用实际 Windows 文件锁 |
| O02 / 高 | 3.1 索引没有新版 `video_hints` 表，路径迁移报错；目标残余 missing 路径占位也会触发 UNIQUE 失败 | `organization.py:433` 的 `relocate_metadata` 按表存在性迁移、保留历史后重连位置；`test_normalize_legacy_v31_index_without_hint_table`、`test_normalize_reconciles_missing_destination_cache_without_losing_history` |
| O03 / 高 | 末次扫描不完整仍移除 pending；清单追加半行后续接，可能生成不完整的“成功”记录 | `organization.py:378/505` 的清单恢复和 `execute` 完成门控；`test_incomplete_final_scan_remains_resumable_and_blocks_annotation`、`test_interrupted_manifest_append_is_recovered_on_resume` |
| N01 / 高 | 工作台保存 `source.createTimeMs`，事件 CSV 只读旧 `createTime`，真实时间列为空 | `annotation/core.py:1075` 的 `_recording_origin` 同时读取两种字段并计入首帧/坐标偏移；`test_event_csv_uses_capture_counter_and_workspace_timestamp`，另用 .NET epoch 换算独立核验毫秒 |
| N02 / 高 | 旧格式导入覆盖采集类别；团队回收后控件仍显示旧值；仅有类别的人工修改未被覆盖保护识别 | `window.py:479/2300`、`team.py:17`，保留来源类别并同步控件，区分自动带入和人工修改；`test_import_legacy_keeps_organized_category_and_updates_cow_control`、`test_category_only_local_review_is_not_overwritten_by_team_return`，并回归逐记录牛号冲突 |
| N03 / 中 | 人工成果 JSON 留有截图引用却没复制截图，离线证据不完整；参考 CSV 漏掉类别 | `window.py:2252` 的 `export_training` 先校验证据再写成果、统一附加类别/身份列；`test_training_bundle_keeps_human_evidence_portable`、`test_reference_evidence_export_has_collection_category` |
| N04 / 中 | 单份完整回收和逐样本导出在 GUI 线程超过一秒，影响响应 | `workspace/io_task.py:34` 局部后台执行，两个入口保留关闭保护；250ms 受控延迟回归与同一小时/58 标签数据前后计时，见第 5 节 |
| U01 / 高 | `.part` 已达到声明大小但 SHA 错误，重试持续校验同一坏文件，启动强制更新无法恢复 | `app/update_core.py:133` 的 `download` 仅清理已确认失败的完整下载；`test_bad_complete_download_can_recover_using_retry` |
| U02 / 高 | 新版注册失败后，清理注册再抛异常，导致旧目录回滚被跳过 | `app/update_worker.py:228` 的 `_install_locked` 记录清理警告后继续恢复；`test_registration_cleanup_error_still_restores_old_application` |
| U03 / 高 | 两个更新进程竞争同一安装目录，原路径锁可被覆盖，双方均进入替换事务 | `update_worker.py:35/222` 的安装路径系统互斥；`test_two_real_updaters_cannot_replace_the_same_installation_concurrently` 与进程死亡释放测试，使用真实独立进程 |
| U04 / 中 | 旧模型辅助入口指向不存在的模块，缺 Torch 时反复要求选择模型包 | `model_assist/assist.py:49` 修正导入并延迟可选依赖；`test_legacy_model_entry.py` 实际窗口加载、人工标注、保存重开和缺依赖提示 |
| L01 / 低 | 翻译自检命令先于后续词条注册执行，误报大量缺失词条，部分界面文案漏译 | `ui/translations.py:2034` 在全部注册完成后运行；`test_translations.py` 实际 runpy 自检与持久化数据不翻译回归 |
| A04 / 中 | 历史八路原生窗口集中show，以及播放预热时临时创建备用窗口，造成GUI停顿 | presentation.py分次显示窗口，并在暂停时仅预备一个备用原生句柄；不提前打开解码器，池上限9。播放请求最大耗时764→4.83毫秒；功能回归通过。首次单路native show仍约729毫秒、GUI间隔约793毫秒，整体750毫秒目标未达成，作为性能限制保留，未用重复测试的较低值替代 |
| N05 / 高 | Windows短暂读句柄使原子替换报WinError5；跨记录结束动作部分写入后重试可能重复草稿 | storage.py对5/32/33错误有限退避、保持旧文件和备份；window.mark固定结束帧并按group_id幂等。真实Windows读句柄、持续拒绝、磁盘满、跨记录部分写入/保存恢复/重试回归 |
| L02 / 低 | 窄波形最后两个完整日期刻度重叠；已确认标注和历史列表仍显示相对秒，难以对应视频 | signal_panel.py按实际文字矩形留间距；工作台/历史有时钟时显示日期时间，无时钟明确相对坐标。内部样本和编辑坐标保持不变，宽度/字体组合及标签展示回归 |

这些问题有具体失败输入或故障注入，并不需要假定恶意攻击。目录互斥、源文件 SHA/文件身份、写入占用、原子写入、下载来源与安装所有权等保护仍是约束条件；本次修复没有以跳过校验换取加载成功。

## 5. 测试与性能证据

### 5.1 专项结果

| 专项 | 本轮已记录结果 | 说明 |
| --- | --- | --- |
| 标注、时钟、团队、标签、证据、保存安全、续接、快照、事件模型 | 169 passed / 5 skipped，16.78s | 跳过项是源码环境下既有可选模型条件；不算通过 |
| 整理、异常恢复、设备身份 | 65 passed，9.87s | 包含真实 Qt/QProcess 与七类/跨日身份验收 |
| 更新、算法适配、旧版入口 | 97 passed / 5 skipped，8.05s | 源码目录缺私有模型运行库的五项完整性校验跳过；对应包内模型另实跑 |
| 翻译 | 5 passed | 自检命令实际执行，数据编码不变 |
| `annotation.core.self_test` | `ok=true`、验证问题 0、撤销重做通过 | 2 事件往返、旧工程迁移、IRR 合成对照通过 |
| 静态检查 | 各专项修改文件 Ruff 通过，差异空白检查通过 | 不等同类型证明或完整第三方安全扫描 |

各组可能包含重叠测试，**不能相加得到全软件通过总数**。最终全量运行、跳过原因、真实视频性能、构建及安装验证另列[功能实测记录](release-330-validation.md)。CI静态检查范围为应用源码与tests；全仓历史脚本/模型资源没有被宣称全部通过Ruff。

### 5.2 客户式 pending 场景

在隔离目录构造旧式设备目录、嵌套视角、开头时间字幕不可读的录像；分别执行冷开和带旧索引的工程重开。实际 Qt、索引线程、OCR、播放器链路完成，自动选中 `视角03` 并显示画面，记录列表保持可见，无报告异常。

| 工况 | 首份九轴 | 首个视频画面 | 退出耗时 | 证据 |
| --- | ---: | ---: | ---: | --- |
| 冷开 | 0.580s | 44.971s | 0.460s | `pending-cold-04/result.json` |
| 旧索引重检 | 0.685s | 41.403s | 0.748s | `pending-legacy-03/result.json` |

上述结果证明该复现场景能继续完成检索，没有永久 pending；也明确显示 OCR 冷启动仍需等待。不能宣称所有录像秒开，或把这些耗时当成客户设备性能。旧索引时间只安排重检，待核验区间不会直接升为证据。

### 5.3 大单份回收和训练导出

对本地真实文件只读取大小，不复制内容。按最大单份九轴约 5.27 MB 构造一小时 50 Hz、180001 样本、58 标签的数据，原始合成文件 5,280,128 字节，完整回传 7,330,122 字节。以 20ms GUI 定时器记录事件循环间隔。

| 操作 | 修改前总耗时 / 最大 GUI 间隔 | 修改后总耗时 / 最大 GUI 间隔 |
| --- | --- | --- |
| 接收一个完整回传 | 1.104s / 1.014s | 1.001s / 0.538s |
| 全套训练导出 | 1.484s / 1.485s | 1.492s / 0.384s |

修改前后均生成相同七份成果，逐样本 CSV 为 29,534,245 字节，源文件 SHA 未变。局部后台处理改善响应，但没有消除所有亚秒级停顿。线程内不访问 Qt；训练导出使用独立工作快照和只读样本副本，模态进度限制编辑/切换；完成前关闭请求不会提前释放 Catalog/数据租约。受控磁盘满测试确认解除运行标志并保留当前人工成果。

### 5.4 模型和跨卷边界实测

五个包内真实模型分别完成起立、卧倒、排尿、抬尾、甩尾推理，耗时约 5.046、2.302、3.705、2.132、2.310 秒，输出与原 CLI 一致且缓存可回读。Qt 定时器最大间隔 0.0321s；取消后私有子进程 0.248s 退出，未写候选缓存。输入是 300 秒静稳合成九轴，五者均输出零候选：这验证运行库和适配接口，**不验证阳性识别能力、召回率或模型科学有效性**。

实际 E: → C: 的合成文件归类请求返回 `blocked`；源文件身份不变、目标未创建、没有复制。此限制保证当前“快速剪切”的实现契约，跨卷搬运没有被宣称为已支持功能。

## 6. 影响范围与历史兼容

以下计数由第一方源码的直接调用点搜索得到，排除定义和测试，不是完整动态调用图；Qt 信号、间接菜单入口不计入数量。

| 关键函数 | 直接调用点数 | 间接影响 |
| --- | ---: | --- |
| `demand.next_video_task` | 2 | 当前播放点和当前九轴时间窗的录像检索 |
| `SessionWork.bind_device_identity` | 1 | 每次当前记录身份加载，继而影响保存、标签和导出 |
| `team.restore_label` | 1 | 手动/自动团队回收共用窗口入口 |
| `io_task.run_io_task` | 2 | 完整回收、训练导出与处理中关闭保护 |

3.1 的完整初始化与 3.2.x 的按需加载策略不同，`pending` 本身可能只是尚未请求读取。真正回归是未读记录消失、检索任务无法再入队、旧时间失去路由作用以及状态窗口不刷新。修复保留按需预算、文件写入保护及时间证据门控，不能简单把全部 pending 改成 ready。

旧数据库迁移允许缺少可选缓存表，保持素材 ID、相机映射和 revision 历史。导入旧标注依然进入 `legacy_unreviewed`，不会自动变成已复核真值。旧四类采集背景、旧时间字段和 BORIS 的既有列位置继续兼容。

### 6.1 冗余与历史界面的处置边界

`workspace/modern_window.py` 继承 `workspace/window.py`，共用控制器、工作状态与控件连接；它是现有展示层，不能仅因两份窗口类同时存在就判断为重复业务实现并删除。`basic_window`、`model_assist_window` 和历史回看入口由 `app.main` 明确分流，分别承担单记录兼容、旧模型辅助和只读历史任务。

本轮保留这些可达入口，并修复已证实的旧模型导入问题，没有在发行前大幅重写继承层。仍需跟踪：历史可选 `PredictionWorker` 丢弃 `_arrays` 后，旧稠密预测 CSV 扩展对缺失数组返回空；当前五事件候选及人工导出不走该分支。该旧可选导出契约尚未恢复，不应在文档中作为已验收功能承诺。

后续若收拢旧界面，必须先明确保留的文件格式和命令行入口，再迁移对应测试。源码存在历史目录或大窗口类，不足以证明其中代码可以安全删除。

## 7. 发行条件与剩余风险

| 范围 | 已知边界 / 后续动作 |
| --- | --- |
| 最终安装包 | 新装、注册/快捷方式、全清单、启动器和卸载由最终EXE验收记录确认；加载、保存重开和模型的实际组件测试见功能实测记录 |
| 3.2.1 首次升级 | 第一次仍由旧版复制出的 updater 执行；3.3 新互斥/恢复代码不能追溯改变正在运行的旧 worker。需分别验旧 worker → 新包和新 worker 事务 |
| 更新时断电 | 可捕获异常已有回滚测试；目录更名事务中强行终止/断电未实现自动回放，可能需从 backup/stage 和 job 记录人工恢复 |
| 便携/源码版更新 | 缺注册安装身份时要求运行安装器，不能宣称原位置全自动覆盖；正式安装版按启动最新版门禁执行 |
| 发布完整性 | 先上传安装包、描述文件和清单并核对 SHA，再公开最新版，避免强制启动客户端看到不完整发布 |
| 客户原始失败工程 | 不在本机；客户提供工程或回传日志后仍需针对原录像格式、路径和缓存复验 |
| 性能 | 八路首次原生窗口创建仍有约0.8秒停顿，整体750毫秒测试目标未达成；播放中备用窗口创建尖峰已修复。OCR 冷启动、大单份 I/O、网盘、机械盘和不同显卡的限制仍需分别评估 |
| 旧损坏整理清单 | 新日志支持按字节位置恢复；旧清单已损坏且没有恢复位置信息时明确停止，不能猜测删除其内容 |
| 可选算法与第三方 | 未逐行审计 vendor、解码器/SDK、第三方库与模型权重；旧 Torch 外置模型全推理、阳性准确率和科学验证不在本次证明范围 |

## 8. 审查方法与置信度

采用按风险聚焦的上下文构建与差异审查：先确定整理执行、团队回收、更新安装等核心函数的输入、状态和副作用，再沿调用者与持久化出口检查全流程；对证实问题先加入失败回归，再局部修复并复测。审查工作流参考并实际使用已安装的 `audit-context-building`、`differential-review`（[Trail of Bits Skills](https://github.com/trailofbits/skills)）及 `systematic-debugging`、`test-driven-development`、`verification-before-completion`（[Superpowers](https://github.com/obra/superpowers)）。技能用于约束方法，不能作为测试通过或安全认证的替代证据。

整理、标签/团队/证据、更新事务相关核心模块完成深读；主窗口、时钟、索引、算法与核心序列化围绕表中调用链审查。结合 Git 历史、实际 SQLite 模式、Windows 文件锁/系统互斥、SHA 与文件 ID、异常注入、Qt 心跳、真实子进程和输出文件逐字段检查。源数据测试操作使用隔离合成文件，未清理、移动或覆盖真实牧场数据。

对已有失败复现和同输入回归的修复置信度较高；对未取得的客户原工程、未覆盖的硬件组合、断电恢复和模型准确率不作通过判断。“全软件审查”在本文指跨功能流程与第一方模块的审查范围，不表示所有代码逐行证明、所有用户操作穷举或第三方二进制无缺陷。

## 9. 证据索引与最终验收补录

可随源码复跑的主要新增回归：

- `tests/test_workspace_demand.py`：pending、旧目录分组、路由/证据、后台状态和播放前置条件。
- `tests/test_organization_regressions.py`、`tests/test_device_identity.py`：整理恢复、旧索引、设备身份、七类和真实 QProcess。
- `tests/test_annotation_pipeline_v330.py`：旧格式/团队衔接、类别/身份全输出、跨日期换牛、证据包、后台 I/O 生命周期。
- `tests/test_update_recovery.py`、`tests/test_legacy_model_entry.py`、`tests/test_translations.py`：更新恢复/竞争、旧入口及真实翻译自检。

本地审查证据目录 `audit-330/`（不是客户数据，也不要求写入发布包）保留 `annotation-context.md`、`organization-context.md`、`update-context.md` 三份初始函数契约；专项结论为 `annotation-review.md`、`organization-review.md`、`organization-device-identity-review.md`、`update-algorithm-review.md`。运行原始结果包括 `pending-cold-04/`、`pending-legacy-03/`、`annotation-large-225722/`、`annotation-large-231222/`、`organization-volumes/`、`algorithm-runtime/` 和 `core-selftest/`。

**最终验收索引：** [功能实测记录](release-330-validation.md)汇总全量结果、真实本机工程、播放、保存、导出、历史、模型和安装边界；[图文教程验证](manual-330-validation.md)对照32页实际操作。最终EXE的新装/卸载、旧worker迁移、全清单SHA和固定提交记录在发行附件COWMATA-3.3.0-Delivery-Checks.json；发布后另核对GitHub latest、标签提交、全部资源摘要及公共更新描述文件。数据正确性、安装和更新完整性或功能回归失败应阻止公开；上述首次原生窗口性能限制单独披露，原失败结果仍保留，不计为性能测试通过。
