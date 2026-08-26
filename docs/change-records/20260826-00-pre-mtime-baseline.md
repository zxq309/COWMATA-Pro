# mtime 仲裁修复前基线

## 目的

在修复乐橙视频时长的 mtime 仲裁问题前，保存当前播放与索引实现，作为本地回滚基线。

## 基线状态

- 分支：`codex/imou-duration-index`
- 修改前上游提交：`3ef9d5e`
- 本地回滚标签：`checkpoint/pre-mtime-arbitration-20260826`
- GitHub：不上传
- 基线测试：`124 passed`

## 基线包含的既有工作

- 乐橙/Dahua MPEG-PS 时长和关键帧索引实现。
- 乐橙虚拟播放流和单次跳转处理。
- 通用 MPEG-PS 时间轴修正、早期跳转和拖动提交处理。
- 相关单元测试及播放兼容性审查文档。

## 本轮修改边界

下一提交只处理以下问题：完整帧扫描结果不应被明显不合理的相邻文件 mtime 差覆盖。不会同时调整 VLC 跳转、跨文件续接或占位文件识别。

## 回滚方法

查看基线：

```powershell
git show checkpoint/pre-mtime-arbitration-20260826
```

修复提交完成后，优先使用 `git revert <mtime 修复提交>` 单独撤销修复。不要用 `git reset --hard`，以免丢失其他本地工作。
