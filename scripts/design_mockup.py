"""Create editable SVG design studies for the two CANFlow desktop pages."""

from __future__ import annotations

import math
from html import escape
from pathlib import Path


OUT = Path(__file__).resolve().parents[1] / "docs" / "design"
INK = "#e8effa"
MUTED = "#93a4bd"
LINE = "#2a3951"
PANEL = "#172337"
CANVAS = "#101a2b"
CYAN = "#49c7f5"
GREEN = "#66e1a7"
AMBER = "#f6bd60"


def rect(x, y, w, h, fill, radius=0, stroke=None):
    outline = f' stroke="{stroke}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}"{outline}/>'


def label(x, y, value, size=14, color=INK, weight=400, anchor="start"):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>')


def rule(x1, y1, x2, y2, color=LINE, width=1, dash=None):
    pattern = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"{pattern}/>'


def button(x, y, w, value, fill=PANEL, color=INK, stroke=LINE):
    return rect(x, y, w, 34, fill, 8, stroke) + label(x + w / 2, y + 22, value, 13, color, 600, "middle")


def shell(active):
    parts = [rect(0, 0, 1600, 900, "#0c1422"),
             rect(0, 0, 1600, 68, "#111d2f"),
             label(28, 42, "CANFlow", 23, INK, 700),
             label(156, 41, "BLF 波形回放", 14, MUTED),
             rect(1220, 18, 208, 32, "#1d2c42", 7),
             label(1234, 40, "demo_capture.blf", 13, INK),
             rect(1440, 18, 132, 32, "#153c36", 7),
             label(1506, 40, "●  回放完成", 13, GREEN, 600, "middle"),
             rect(0, 68, 1600, 60, "#111d2f"),
             rule(0, 127, 1600, 127)]
    for x, title, index in ((28, "波形分析", 0), (176, "文件与信号配置", 1)):
        color = INK if active == index else MUTED
        parts.append(label(x, 106, title, 15, color, 650 if active == index else 400))
        if active == index:
            parts.append(rect(x, 122, 108 if index == 0 else 132, 3, CYAN, 1))
    return parts


def svg(parts):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" '
            'viewBox="0 0 1600 900"><style>text{font-family:"Microsoft YaHei UI",'
            '"Noto Sans CJK SC",sans-serif}</style>' + "".join(parts) + "</svg>")


def waveform_page():
    p = shell(0)
    p += [rect(24, 150, 388, 686, PANEL, 14, LINE),
          label(44, 184, "游标读数", 18, INK, 700),
          label(392, 184, "固定游标  ◇", 12, MUTED, anchor="end"),
          rect(44, 199, 348, 74, "#223854", 10),
          label(60, 225, "相对首帧时间", 12, MUTED),
          label(60, 257, "15.000 s", 28, INK, 700),
          label(375, 257, "09:00:15.000", 13, MUTED, anchor="end"),
          label(44, 302, "信号值", 15, INK, 650),
          label(392, 302, "32 / 40 个信号", 12, MUTED, anchor="end"),
          rect(44, 315, 348, 36, "#111c2d", 7, LINE),
          label(57, 338, "⌕  搜索信号 / 报文 ID", 12, MUTED),
          rect(44, 360, 348, 26, "#22344d", 5),
          label(58, 378, "信号", 11, MUTED, 600),
          label(377, 378, "当前值", 11, MUTED, 600, "end")]
    signals = [(GREEN, "BatterySOC", "62 %"),
               (CYAN, "BrakePressure", "16 bar"),
               ("#59d2c5", "InverterTemp", "26 °C"),
               (AMBER, "MotorSpeed", "3 428 rpm"),
               ("#b491f5", "PackVoltage", "384.2 V"),
               ("#f2859e", "PackCurrent", "−18.6 A"),
               ("#87b9f8", "CellTempMax", "31.4 °C"),
               ("#d4bc85", "DCBusVoltage", "386.1 V"),
               ("#83d3be", "MotorTorque", "42.8 Nm"),
               ("#9db8f4", "VehicleSpeed", "58.2 km/h")]
    for i, (color, name, value) in enumerate(signals):
        y = 388 + i * 35
        p += [rect(44, y, 348, 34, "#1d3049" if i < 3 else "#19283d", 4),
              rect(53, y + 11, 5, 12, color, 2),
              label(67, y + 22, name, 12, INK if i < 3 else "#bdcbe0", 550),
              label(379, y + 22, value, 13, INK, 650, "end")]
    p += [rect(388, 388, 3, 348, "#30415a", 2),
          rect(388, 389, 3, 110, CYAN, 2),
          label(44, 760, "显示 1–10 / 32  ·  滚动查看更多", 11, MUTED),
          label(44, 810, "显示与回放  ▸", 12, MUTED, 600),
          label(392, 810, "32 已选 · 3 聚焦", 11, MUTED, anchor="end"),
          rect(430, 150, 1146, 686, CANVAS, 14, LINE),
          label(454, 184, "信号波形", 18, INK, 700),
          label(454, 208, "32 条已选  ·  3 条聚焦  ·  30.0 秒可见范围", 12, MUTED),
          button(1274, 164, 88, "跟随播放"),
          button(1372, 164, 84, "暂停"),
          button(1466, 164, 86, "重新播放", "#235273", INK, "#367fa8")]
    chart_x, chart_y, chart_w, chart_h = 454, 232, 1098, 506
    p += [rect(chart_x, chart_y, chart_w, chart_h, "#111c2d", 8, LINE)]
    for i in range(31):
        x = chart_x + 90 + i * (chart_w - 112) / 30
        p.append(rule(x, chart_y, x, chart_y + chart_h, "#26354b" if i % 5 == 0 else "#1c2a3d"))
        if i % 5 == 0:
            p.append(label(x, 763, f"{i} s", 11, MUTED, anchor="middle"))
    for y in (394, 556):
        p.append(rule(chart_x, y, chart_x + chart_w, y, "#35475f"))
    for y, color, name, unit in ((261, GREEN, "BatterySOC", "%"),
                                 (423, CYAN, "BrakePressure", "bar"),
                                 (585, "#59d2c5", "InverterTemp", "degC")):
        p += [rect(466, y - 17, 118, 25, "#203149", 6),
              label(476, y, f"{name}  {unit}", 11, color, 600)]
    for index, color in enumerate((GREEN, CYAN, "#59d2c5")):
        points = []
        lane_top = 276 + index * 162
        for t in range(0, 301):
            sec = t / 10
            x = chart_x + 90 + sec * (chart_w - 112) / 30
            if index == 0:
                value = 1 - sec / 32
            elif index == 1:
                value = 1 - ((sec % 7) / 7)
            else:
                value = 0.5 - 0.38 * math.sin(sec / 3)
            y = lane_top + value * 104
            points.append(f"{x:.1f},{y:.1f}")
        coordinates = " ".join(points)
        p.append(f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="2"/>')
    cursor_x = chart_x + 90 + 15 * (chart_w - 112) / 30
    p += [rule(cursor_x, chart_y, cursor_x, chart_y + chart_h, "#f5f8ff", 1.4, "5 4"),
          rect(cursor_x - 32, 740, 64, 24, "#dbe8fb", 6),
          label(cursor_x, 757, "15.000 s", 11, "#183452", 700, "middle"),
          label(454, 806, "00:00", 12, MUTED), label(1550, 806, "00:30", 12, MUTED, anchor="end"),
          rect(504, 795, 994, 5, "#2b3c55", 3),
          rect(504, 795, 497, 5, CYAN, 3),
          '<circle cx="1001" cy="797.5" r="8" fill="#dceefa" stroke="#5dbfed" stroke-width="3"/>']
    return svg(p)


def config_page():
    p = shell(1)
    p += [label(28, 174, "文件与信号配置", 24, INK, 700),
          label(28, 200, "导入 BLF、映射 DBC，然后选择需要查看的信号。", 13, MUTED),
          button(1354, 157, 110, "保存项目", "#235273", INK, "#367fa8"),
          button(1474, 157, 98, "返回波形"),
          rect(24, 228, 760, 622, PANEL, 14, LINE),
          rect(802, 228, 774, 622, PANEL, 14, LINE),
          label(48, 264, "01  记录文件", 17, INK, 700),
          label(48, 286, "按首帧时间排序，逐个文件连续回放", 12, MUTED),
          button(612, 246, 148, "+ 添加 BLF 文件", "#235273", INK, "#367fa8"),
          rect(48, 310, 712, 88, "#1c2b42", 9, LINE),
          label(66, 340, "demo_capture.blf", 15, INK, 600),
          label(66, 367, "2026-09-20 09:00:00  ·  CH1", 12, MUTED),
          label(742, 348, "301 帧", 14, GREEN, 650, "end"),
          rule(48, 430, 760, 430),
          label(48, 466, "02  DBC 通道映射", 17, INK, 700),
          label(48, 490, "DBC 独立于 BLF，仅用于解码信号", 12, MUTED),
          button(644, 448, 116, "+ 新增通道"),
          rect(48, 516, 712, 44, "#22344d", 8),
          label(66, 544, "通道", 12, MUTED), label(190, 544, "DBC 文件", 12, MUTED),
          label(714, 544, "状态", 12, MUTED, anchor="end"),
          rect(48, 560, 712, 64, "#1c2b42", 8, LINE),
          label(66, 599, "CH 1", 14, INK, 600),
          label(190, 599, "demo_signals.dbc", 14, INK),
          label(714, 599, "已映射", 13, GREEN, 600, "end"),
          button(48, 648, 116, "指定 DBC"), button(176, 648, 116, "最近 DBC"),
          button(304, 648, 116, "删除映射"),
          label(826, 264, "03  信号与信号组", 17, INK, 700),
          label(826, 286, "勾选信号后应用，即可在波形页查看游标值", 12, MUTED),
          rect(826, 310, 726, 42, "#1c2b42", 8, LINE),
          label(842, 337, "搜索信号名称或报文 ID…", 13, MUTED),
          label(826, 388, "可用信号", 13, MUTED),
          label(1530, 388, "3 个已选", 12, GREEN, anchor="end")]
    for y, color, name, unit in ((404, GREEN, "BatterySOC", "%"),
                                 (466, CYAN, "BrakePressure", "bar"),
                                 (528, "#59d2c5", "InverterTemp", "degC")):
        p += [rect(826, y, 726, 52, "#1c2b42", 8, LINE),
              rect(842, y + 15, 20, 20, "#367fa8", 4),
              label(846, y + 31, "✓", 15, INK, 700),
              rect(876, y + 18, 5, 16, color, 2),
              label(894, y + 32, name, 14, INK, 600),
              label(1530, y + 32, f"CH1 · 0x100 · {unit}", 12, MUTED, anchor="end")]
    p += [label(826, 631, "信号组", 13, MUTED),
          rect(826, 646, 726, 42, "#1c2b42", 8, LINE),
          label(842, 673, "选择已保存的信号组", 13, MUTED),
          button(826, 707, 110, "保存组"), button(948, 707, 110, "导入"),
          button(1070, 707, 110, "导出"),
          button(1204, 707, 348, "应用所选信号  →", "#235273", INK, "#367fa8"),
          label(826, 793, "配置更改可在播放完成后补画历史波形", 12, MUTED)]
    return svg(p)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "canflow-waveform.svg").write_text(waveform_page(), encoding="utf-8")
    (OUT / "canflow-configuration.svg").write_text(config_page(), encoding="utf-8")


if __name__ == "__main__":
    main()
