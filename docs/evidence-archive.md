# 证据图片与外部录像归档 / Evidence and archived video

版本 3.1.0 · 2026-09-07

## 日常标注

1. 打开数据工程，确认牛号、九轴与相机同步，选择需要的 1–8 个视角。
2. 观看完整动作，修订起止范围并确认真值后，出现证据留存窗口。也可从“标注列表 → 留存多视角证据图”重新打开。
3. 默认推荐标签内加速度偏离明显的样本；这不是自动识别的正确事件。可改为当前视频时刻或手动调整相对秒，再点“提取 / 更新图片”。只取同一对应时刻，每个选中视角一张，不生成小视频。
4. 核对目标牛、画面和时刻，勾选人工核对后保存。不可用视角会说明原因；可见但被遮挡的画面由人判断，程序不宣称自动识别遮挡。未配置单独相机校准时，会提示沿用工程参考时钟。
5. “导出当前成果”或“导出所选九轴片段”保存一个 JSON；有证据图时，同级只增加一个“证据”文件夹。不同标签可共用这个文件夹，内容相同的图按 SHA-256 去重。移动成果时将 JSON 与“证据”文件夹一起带走，不要只复制 JSON。

截图为原始分辨率、高质量 JPEG（Q95、4:4:4），不加水印、不修改原片。保存实际帧位置、目标与实际参考时刻及偏差、录像内容身份、牛号、原九轴位置、标签区间和同步版本。既有索引确认的时间映射不是对全部时钟误差的保证；模糊、缺帧或跨缺口的抽帧会拒绝或明确标记不可用。

静态图只能辅助复核姿态，不能证明整个动态行为或所有起止边界。图片是人工留证，不送入九轴事件模型，不进入训练特征/元数据。确认真值仍需要原录像及已校准的对应范围。边界/牛号/同步修改后，旧图片留作追溯，不被静默冒充成新版本证据。

## 腾出本机空间

先把完整原录像复制到外部归档目录，不覆盖重名批次。然后在“更多 → 素材 → 录像归档副本核验”选择副本目录：后台核对本机原片与副本的完整 SHA-256，而不是仅比较名字或文件大小。只处理录像，忽略监控配套杂文件；发现未入索引录像、缺失副本、内容不同或写入变化会报告失败。

核对通过后仍要人工抽查归档播放、确认整批标签与证据导出完整；勾选相应检查项，保存核验记录，再重新导出要携带归档线索的标签。核验记录是当时的内容一致性记录，不是长期磁盘可靠性保证。重要原录像建议另留独立备份。软件从不自动移动、删除本机录像，也不把“本机没有录像”当作正常负样本。

之后由用户在文件管理器中清理本机录像副本。保留原始九轴、整个“标注工程”工作目录，以及导出的 JSON 和同级“证据”文件夹。请不要删除标注工程里的工作成果或证据目录来“清缓存”。

## 离线回看

“打开历史标注回看”可独立打开原九轴/片段、标签和证据图片；没有原片时默认显示图片。点击各图片可按原分辨率放大。缺图或校验失败会提醒，但不会销毁标签。使用“连接归档录像”选归档根目录，按已保存的内容身份核对并恢复定位；不要求归档电脑安装 Python，也不写入归档索引。重新命名过的原片依赖核验时保存的归档相对路径，若此后再次任意改名，需重新核验或用工程索引按内容身份定位。

此前已确认且归档核验过的标签，可以在原片离线时按保留的审核状态导出；这不允许确认新的真值，也不允许用图片推断未观看区间。

## English summary

Confirm the event against video, then retain one original-resolution still per selected camera at one aligned time. The acceleration peak is only an adjustable suggestion. Check the target animal before saving. Export one annotation JSON with a flat sibling `证据` directory; keep both together. Images are review-only, never IMU model inputs.

Copy complete recordings externally, verify full hashes using the archive checklist, spot-check playback and check exported annotations/images before manually removing local copies. The application never deletes videos. History supports offline image/IMU review and identity-checked relinking to recorded archive paths; stills do not replace dynamic evidence or justify new ground truth.
