"""Build the illustrated quick start from audited, real application captures.

Authoring dependency: ReportLab (not required in the user's offline app).
No private raw recording or IMU is copied into the PDF; captures must be reviewed.
"""

import argparse
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PAGES = [
    (
        "01",
        "打开一份九轴，就开始",
        "01-open-project.png",
        [
            "安装：下载 Release 的 Setup.exe，选择父目录和软件文件夹名，保留桌面快捷方式后安装。无需配置 Python。",
            "打开：文件 → 打开工程（Ctrl+O）；也可打开九轴（Ctrl+J），任选原始 JSON，不必按顺序标。",
            "核对设备、牛号及视角。仅当前九轴需要的录像优先检索；“尚未检索”不等于“没有录像”。",
        ],
        "本教程为 3.2.0 实际运行截图。DEMO 使用真实素材及测试同步/标签，只演示操作，不是已核实的科研真值。",
    ),
    (
        "02",
        "先核对同步，再判定真值",
        "02-synchronization.png",
        [
            "设备 create_time 是开始采集时间；update_time 是服务器收包时间。新老 JSON 均可读，旧协议时间质量会提示。",
            "工具 → 时间同步 → 九轴校准：用确定的同一事件填写“九轴内部秒数 ↔ 录像参考时间”。不要照抄示例数值。",
            "相机时间有偏差时使用“相机校准”。有漂移可增加对应点；不确定区间保持未确认，不能硬拼成连续真值。",
        ],
        "软件自动粗定位不能代替目标牛与同步关系的人工核对。修改同步关系后，旧标签可能需要重新复核。",
    ),
    (
        "03",
        "看录像，标动作起止",
        "03-label-interval.png",
        [
            "点击波形定位；鼠标移入视频可见播放、前后跳转、倍速和放大按钮。默认只播放主路，点击另一视角的播放可切换。",
            "在底部选择标签：动作开始点按“动作起止”，结束点再按一次；点事件只记录一个时刻。标签轨道与波形分开。",
            "在“标注列表”选中草稿并回看两端；核对目标牛、原片帧和同步后，编辑 → 标注与证据 → 确认所选草稿为九轴真值。",
        ],
        "辅路暂停图有自己的时间，不代表正在实时播放。看不清或同步未确认时，保留草稿，不确认真值。",
    ),
    (
        "04",
        "单独运行一种行为算法",
        "04-behavior-algorithm.png",
        [
            "行为识别 → 选择行为；右侧选择一个摄像头和模型版本，然后点“运行算法”。切换摄像头保持同一参考时刻。",
            "已接入：起立、卧倒、抬尾、甩尾、排尿。双击结果定位；“选择对应人工标签”仅选择标签，不自动画区间或确认。",
            "这个独立检查面板只显示单摄像头。原“工具 → 事件候选预测”仍是另外的批量候选流程，两类结果分别保存。",
        ],
        "现有五类模型使用完整 V2 九轴记录。分数不等于概率，未检出不等于无事件。点击“返回标注布局”恢复原布局。",
    ),
    (
        "05",
        "未完成的算法，明确标为待接入",
        "05-health-placeholder.png",
        [
            "健康与繁殖菜单包含发情、产犊、怀孕、疫病；本版尚无这些预测算法，因此运行按钮禁用，不生成健康结论。",
            "行为识别中的排便、爬跨、努责也为待接入。菜单与对应行为标签使用稳定代码，后续模型可以版本化注册。",
            "这些入口同样只显示一个可切换摄像头，不会打开多视角网格，也不会把占位结果混进候选或人工标签。",
        ],
        "算法迭代不应覆盖历史人工成果。新旧模型的版本与结果独立留档，最终标签仍由人复核。",
    ),
    (
        "06",
        "每个视角留一张证据图",
        "06-evidence.png",
        [
            "在标注列表选择一条已确认标签；编辑 → 标注与证据 → 留存多视角证据图。先选好需要的摄像头。",
            "用“波形建议点”或“当前视频时刻”选择代表时刻，再点“提取 / 更新图片”。每视角从原录像提一张原分辨率图。",
            "点击图片放大，核对目标牛、时刻和遮挡；勾选人工核对后保存。缺失视角会明示，不用其他时刻图片冒充。",
        ],
        "截图仅供以后辅助回看，不参与九轴算法，也不能代替整段动作录像。移走本机录像前，先完成外部归档副本核验。",
    ),
    (
        "07",
        "做完下一份，没做完留住位置",
        "07-finish-or-resume.png",
        [
            "点击底部“完成本份…”（Ctrl+Enter）。确实检查完整份后，选“已完成，下一份”或“已完成，保存退出”。",
            "还没检查完就选“没做完，暂存退出”。下次打开同工程恢复未完成记录和位置，不先加载全部录像。",
            "素材列表可以搜索，显示未开始、进行中、已完成；悬停“素材”临时展开，Ctrl+L 固定或收起。",
        ],
        "有几个标签不代表整份已检查。完成状态由人确认；待确认候选不会因为“完成本份”而自动升级为真值。",
    ),
    (
        "08",
        "导出与多人回传",
        "08-team-return.png",
        [
            "文件 → 导出 → 完整成果：导出包含原始九轴、标签和来源/同步信息的 annotations.json；证据放在旁边“证据”目录。",
            "回传时把成果 JSON 与整个“证据”目录一起复制。接收端：工具 → 多人协作 → 回传设置，选一个专用回传文件夹。",
            "先勾选自动发现，核对后接收。只有来源、样本范围、证据与完成状态校验通过且无冲突时，才可按设置自动放置。",
        ],
        "接收位置是当前数据工程的“标注工程”目录；不移动原始九轴或录像。冲突保留双方版本，缺失或不符的成果不直接接收。",
    ),
    (
        "09",
        "录像移走后，仍可回看标签",
        "09-history-evidence.png",
        [
            "文件 → 历史回看（Ctrl+Shift+O），打开导出的 annotations.json。九轴记录随成果携带，不靠标签文件名猜来源。",
            "顶部选择“留存证据图”，选择一条标签，可同时看九轴片段与当时保存的各视角图片；点击图片可以放大。",
            "要复核完整动作，重新选择数据工程或连接已归档录像。历史窗口只读，不改原标注；图片不能用于确认新的录像真值。",
        ],
        "本页实际验证了 4 张图片的完整性。原片不在本机时，仍应保留完整录像的已核验外部副本。",
    ),
    (
        "10",
        "查看版本与更新",
        "10-about.png",
        [
            "帮助 → 关于：查看版本号、构建标识、公司信息与许可。点“版本与更新…”可查看设置、检查版本和下载状态。",
            "有更新器的安装版会在启动及运行期间定时检查 GitHub；可后台下载，保存并正常退出后执行校验与原位置更新。",
            "旧版若没有更新器，需要手动安装一次。离线仍可标注；不要覆盖用户数据，不要在标注进行中强行关机更新。",
        ],
        "查找教程：帮助 → 新手图文教程。遇到故障时附版本、操作步骤与“性能诊断”信息，不要修改原始素材来绕过问题。",
    ),
]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--screenshots", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--font", default="C:/Windows/Fonts/msyh.ttc")
    p.add_argument("--bold-font", default="C:/Windows/Fonts/msyhbd.ttc")
    a = p.parse_args()
    pdfmetrics.registerFont(TTFont("CJK", a.font))
    pdfmetrics.registerFont(TTFont("CJKBold", a.bold_font))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(a.out), pagesize=(1000, 790), pageCompression=1)
    c.setTitle("COWMATA Annotator 3.2.0 - 新手图文教程")
    c.setAuthor("COWMATA")
    for number, title, filename, steps, note in PAGES:
        c.setFillColor(HexColor("#f7faf5"))
        c.rect(0, 0, 1000, 790, fill=1, stroke=0)
        c.setFillColor(HexColor("#92C142"))
        c.roundRect(28, 728, 46, 38, 7, fill=1, stroke=0)
        c.setFillColor(HexColor("#20332A"))
        c.setFont("CJKBold", 18)
        c.drawCentredString(51, 739, number)
        c.setFont("CJKBold", 23)
        c.drawString(87, 738, title)
        c.setFont("CJK", 11)
        c.drawRightString(970, 747, "COWMATA Annotator 3.2.0")
        shot = a.screenshots / filename
        reader = ImageReader(str(shot))
        iw, ih = reader.getSize()
        scale = min(940 / iw, 525 / ih, 1.12)
        width, height = iw * scale, ih * scale
        x, y = (1000 - width) / 2, 192 + (525 - height) / 2
        c.setFillColor(HexColor("#ffffff"))
        c.roundRect(28, 187, 944, 530, 8, fill=1, stroke=0)
        c.drawImage(reader, x, y, width, height, mask="auto")
        c.setFillColor(HexColor("#20332A"))
        c.setFont("CJK", 12)
        for i, line in enumerate(steps):
            assert pdfmetrics.stringWidth(line, "CJK", 12) < 910, title
            c.drawString(34, 159 - 28 * i, f"{i + 1}.  " + line)
        c.setStrokeColor(HexColor("#d4e2cc"))
        c.line(30, 79, 970, 79)
        c.setFillColor(HexColor("#446354"))
        c.setFont("CJK", 10.5)
        assert pdfmetrics.stringWidth(note, "CJK", 10.5) < 940, title
        c.drawString(30, 56, note)
        c.setFont("CJK", 9)
        c.drawString(30, 22, "实际程序截图  /  2026-09-09  /  原始数据保持不变")
        c.drawRightString(970, 22, f"{number} / {len(PAGES)}")
        c.showPage()
    c.save()
    print(a.out)


if __name__ == "__main__":
    main()
